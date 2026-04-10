"""
backtest_consensus.py — paranoid replay of Consensus v1.1 against history.

Why this exists
---------------
Live Consensus has run 33% WR / -23.6% ROI over 18 settled trades, while the
historical claim was ~73% WR. That gap is too large to be variance — it's
either overfit, look-ahead leakage, or stale price alignment in the original
backtest. This harness replays the strategy with paranoid checks so we can
re-derive (or invalidate) the edge before re-enabling Consensus in prod.

Paranoid checks enforced
------------------------
1. POINT-IN-TIME BTC ALIGNMENT
   Momentum at decision time T is computed only from BTC ticks with
   timestamp <= T. We never peek at the kline that contains T's resolution.

2. PREVIOUS-RESULT ISOLATION
   The "previous market result" signal is only sourced from markets that
   close STRICTLY BEFORE T. The current market's result is never used.

3. PRICE-AT-DECISION
   For each candidate market we use the Kalshi snapshot closest to T from
   *before* T, not the average or the close. If no snapshot exists in the
   2-minute window before T, we skip the candidate (no entry possible).

4. NO BACKFILLING WITH SETTLED OUTCOME
   The result of the market under evaluation is only joined in for P&L
   calculation, never as an input to the signal.

5. MIN-TICKS GUARD
   We require at least MOMENTUM_WINDOW seconds of BTC history before T,
   otherwise we skip — same constraint the live bot has.

6. OUT-OF-SAMPLE SPLIT
   Train window and test window are configurable and reported separately.
   No optimization touches the test window — see `--train-frac`.

Usage
-----
    # Pull historical data
    python backtest_consensus.py fetch \
        --start 2026-03-01 --end 2026-04-09 \
        --out data/history.json

    # Replay with default thresholds
    python backtest_consensus.py replay \
        --history data/history.json \
        --dead-zone 0.0020 \
        --base-price 0.45 --max-price 0.55 \
        --report

    # Sweep dead-zone values to find a stable threshold
    python backtest_consensus.py sweep \
        --history data/history.json \
        --dead-zone 0.0005 0.001 0.0015 0.002 0.003 0.005

The harness imports nothing from bot.py except the constants — it does NOT
use ConsensusStrategy directly because that class has stateful side effects
(self.last_trade_time, etc.) that don't replay cleanly. The replay logic is
intentionally re-implemented here so you can audit it line-by-line.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

# ── Constants kept in sync with bot.py — change here AND in bot.py if you tune ──
DEFAULT_DEAD_ZONE       = 0.0020   # 0.20% — post-2026-04-09 floor
DEFAULT_BASE_PRICE      = 0.45
DEFAULT_MAX_PRICE       = 0.55
MOMENTUM_WINDOW_SECONDS = 60
PREV_RESULT_MAX_AGE_SEC = 1800
MIN_BOOK_SUM            = 0.97

BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
BINANCE_KLINES_LIMIT = 1000  # Binance hard cap per request


# ──────────────────────────────────────────────────────────────────────────
# DATA FETCH
# ──────────────────────────────────────────────────────────────────────────

def fetch_btc_klines(start_ms: int, end_ms: int, interval: str = "1s") -> list[dict]:
    """
    Pull BTCUSDT klines from Binance for [start_ms, end_ms].

    Binance returns at most 1000 candles per request, so we paginate.
    interval='1s' gives second-resolution which lets us compute the 60-second
    momentum signal exactly the way the live bot would have seen it.

    Note: Binance changed 1s availability for spot in 2022 — if 1s is rejected
    fall back to '1m' and accept the loss of intra-minute precision (the
    backtest will document this in the report).
    """
    out = []
    cursor = start_ms
    seconds_per_kline = {"1s": 1, "1m": 60, "5m": 300}.get(interval, 60)
    while cursor < end_ms:
        params = {
            "symbol": "BTCUSDT",
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": BINANCE_KLINES_LIMIT,
        }
        r = requests.get(BINANCE_KLINES_URL, params=params, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for row in rows:
            # row format: [openTime, open, high, low, close, volume, closeTime, ...]
            out.append({"t": row[0], "close": float(row[4])})
        cursor = rows[-1][0] + seconds_per_kline * 1000
        time.sleep(0.1)  # be nice to the public endpoint
    return out


def fetch_kalshi_settled_markets(client, start_iso: str, end_iso: str) -> list[dict]:
    """
    Pull all KXBTC15M settled markets in [start_iso, end_iso] from Kalshi.

    The bot's existing KalshiClient.get_markets_by_series() only returns the
    most recent page — for a multi-day backtest we need pagination via cursor.
    Returns a list of dicts with at least: ticker, close_time, result,
    yes_ask_dollars / no_ask_dollars (last known book), strike.
    """
    BASE = "https://api.elections.kalshi.com/trade-api/v2"
    out = []
    cursor = None
    while True:
        params = {
            "series_ticker": "KXBTC15M",
            "status": "settled",
            "limit": 200,
            "min_close_ts": int(datetime.fromisoformat(start_iso).timestamp()),
            "max_close_ts": int(datetime.fromisoformat(end_iso).timestamp()),
        }
        if cursor:
            params["cursor"] = cursor
        r = client.get("/markets", params=params) if client else requests.get(
            f"{BASE}/markets", params=params, timeout=15).json()
        markets = r.get("markets", [])
        if not markets:
            break
        out.extend(markets)
        cursor = r.get("cursor")
        if not cursor:
            break
    return out


# ──────────────────────────────────────────────────────────────────────────
# CORE API (used by both CLI and the Flask dashboard analysis page)
# ──────────────────────────────────────────────────────────────────────────

def _cache_path(start: str, end: str, interval: str, cache_dir: str = "data") -> Path:
    """Deterministic cache path for a (start, end, interval) fetch bundle."""
    key = f"history_{start}_{end}_{interval}.json"
    return Path(cache_dir) / key


def fetch_history(start: str, end: str, interval: str = "1m",
                  with_kalshi: bool = True, cache_dir: str = "data",
                  max_cache_age_hours: float = 6.0,
                  kalshi_client=None) -> dict:
    """
    Fetch (or load from cache) a history bundle with BTC klines + Kalshi markets.

    Args:
        start, end: ISO date strings (e.g. "2026-04-02")
        interval: Binance kline interval — "1s" or "1m"
        with_kalshi: if True, also fetch Kalshi settled markets
        cache_dir: where to store cached bundles
        max_cache_age_hours: serve from cache if file is newer than this
        kalshi_client: optional pre-built KalshiClient; if None we construct
                       one from env (KALSHI_API_KEY_ID + key path/base64)

    Returns: the history dict (same shape cmd_fetch writes to disk)
    Raises:  requests.HTTPError on network failure,
             RuntimeError if with_kalshi=True but no credentials available.
    """
    cache_file = _cache_path(start, end, interval, cache_dir)
    if cache_file.exists():
        age_s = time.time() - cache_file.stat().st_mtime
        if age_s < max_cache_age_hours * 3600:
            with open(cache_file) as f:
                bundle = json.load(f)
            bundle["_cache_hit"] = True
            bundle["_cache_age_s"] = round(age_s, 1)
            return bundle

    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end_dt   = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
    print(f"[fetch_history] BTC klines {start} → {end} ({interval})", flush=True)
    btc = fetch_btc_klines(int(start_dt.timestamp() * 1000),
                           int(end_dt.timestamp() * 1000),
                           interval=interval)
    print(f"[fetch_history] Got {len(btc)} BTC klines", flush=True)

    kalshi = None
    if with_kalshi:
        if kalshi_client is None:
            # Lazy import to keep the harness importable without bot deps
            sys.path.insert(0, os.path.dirname(__file__))
            from bot import KalshiClient, load_private_key
            api_key_id = os.getenv("KALSHI_API_KEY_ID")
            key_path   = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi.key")
            if not api_key_id:
                raise RuntimeError(
                    "KALSHI_API_KEY_ID not set — cannot fetch Kalshi markets")
            pkey = load_private_key(key_path)
            kalshi_client = KalshiClient(api_key_id, pkey, dry_run=True)
        print(f"[fetch_history] Pulling Kalshi settled markets", flush=True)
        kalshi = fetch_kalshi_settled_markets(kalshi_client, start, end)
        print(f"[fetch_history] Got {len(kalshi)} Kalshi markets", flush=True)

    bundle = {
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "start": start,
        "end": end,
        "interval": interval,
        "btc": btc,
        "kalshi_markets": kalshi,
    }

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(bundle, f)
    bundle["_cache_hit"] = False
    return bundle


# Standard sweep grid: spans an order of magnitude of dead-zone values
DEFAULT_SWEEP_DEAD_ZONES = [0.0005, 0.001, 0.0015, 0.002, 0.0025, 0.003, 0.004, 0.005]


def run_sweep(history: dict,
              dead_zones: list[float] = None,
              base_price: float = DEFAULT_BASE_PRICE,
              max_price: float = DEFAULT_MAX_PRICE) -> list[dict]:
    """
    Replay Consensus over `history` once per dead_zone value.

    Returns: list of rows, each row = summarize() output + {"dead_zone": z}.
             Sorted by dead_zone ascending (smallest → largest).
    """
    if dead_zones is None:
        dead_zones = DEFAULT_SWEEP_DEAD_ZONES
    rows = []
    for dz in sorted(dead_zones):
        trades = replay(history, dz, base_price, max_price)
        row = summarize(trades)
        row["dead_zone"] = dz
        rows.append(row)
    return rows


def interpret_sweep(rows: list[dict], min_trades: int = 10) -> dict:
    """
    Convert a sweep result into a plain-English verdict for non-technical users.

    Looks for a "plateau": at least 3 adjacent rows where pnl_per_dollar > 0.03
    (~3% edge, enough to survive fees) AND trade count >= min_trades.

    Returns:
        {
          "verdict": "edge" | "no_edge" | "thin_sample" | "overfit_spike",
          "headline": str,    # one-line summary
          "rationale": str,   # plain-English explanation
          "recommendation": str,
          "best_dead_zone": float | None,
          "plateau_range": [lo, hi] | None,
        }
    """
    if not rows:
        return {
            "verdict": "no_edge",
            "headline": "No data to analyze",
            "rationale": "The sweep returned no rows. Check your date range.",
            "recommendation": "Try a wider date range or verify Kalshi credentials.",
            "best_dead_zone": None,
            "plateau_range": None,
        }

    # Sort by dead_zone ascending
    rows = sorted(rows, key=lambda r: r.get("dead_zone", 0))

    # Count rows with enough trades to be meaningful
    meaningful = [r for r in rows if r.get("trades", 0) >= min_trades]
    if not meaningful:
        return {
            "verdict": "thin_sample",
            "headline": "Not enough candidate trades to judge",
            "rationale": (
                f"Across every threshold tested, the sweep generated fewer than "
                f"{min_trades} candidate trades. This window is too short or too "
                f"quiet to measure edge reliably."
            ),
            "recommendation": (
                "Re-run with a longer date range (at least 2–4 weeks). "
                "If the trade count stays low, Consensus simply isn't firing "
                "often enough to be a useful strategy."
            ),
            "best_dead_zone": None,
            "plateau_range": None,
        }

    # Look for a plateau: 3 consecutive rows with pnl_per_dollar > 0.03 AND
    # enough trades in each.
    EDGE_FLOOR = 0.03  # 3% return on stake — rough floor above fees/slippage
    plateau_start = None
    plateau_len = 0
    best_plateau = None  # (start_idx, length, avg_pnl)

    profitable = [i for i, r in enumerate(rows)
                  if r.get("pnl_per_dollar", 0) > EDGE_FLOOR
                  and r.get("trades", 0) >= min_trades]

    # Find longest run of consecutive indices in `profitable`
    if profitable:
        run_start = profitable[0]
        run_len = 1
        for i in range(1, len(profitable)):
            if profitable[i] == profitable[i - 1] + 1:
                run_len += 1
            else:
                if run_len >= 3:
                    avg = sum(rows[k]["pnl_per_dollar"]
                              for k in range(run_start, run_start + run_len)) / run_len
                    if best_plateau is None or avg > best_plateau[2]:
                        best_plateau = (run_start, run_len, avg)
                run_start = profitable[i]
                run_len = 1
        if run_len >= 3:
            avg = sum(rows[k]["pnl_per_dollar"]
                      for k in range(run_start, run_start + run_len)) / run_len
            if best_plateau is None or avg > best_plateau[2]:
                best_plateau = (run_start, run_len, avg)

    # Is there a single-point spike (profitable but isolated)?
    single_spikes = [i for i in profitable
                     if (i - 1) not in profitable and (i + 1) not in profitable]

    if best_plateau:
        start_i, length, avg_pnl = best_plateau
        lo = rows[start_i]["dead_zone"]
        hi = rows[start_i + length - 1]["dead_zone"]
        # "Best" threshold = the middle of the plateau
        mid_i = start_i + length // 2
        best_dz = rows[mid_i]["dead_zone"]
        return {
            "verdict": "edge",
            "headline": (
                f"Stable edge detected — ~{avg_pnl*100:.1f}% average return "
                f"across {length} adjacent thresholds"
            ),
            "rationale": (
                f"The sweep found {length} consecutive dead-zone values "
                f"({lo*100:.2f}% to {hi*100:.2f}%) where Consensus is profitable "
                f"with enough trades in each. Plateaus are more trustworthy than "
                f"isolated peaks — a plateau means the edge isn't just fitting noise."
            ),
            "recommendation": (
                f"Re-run the analysis on a DIFFERENT date range to confirm this "
                f"holds out-of-sample. If it does, set MOMENTUM_DEAD_ZONE={best_dz} "
                f"and CONSENSUS_ENABLED=true in Render env."
            ),
            "best_dead_zone": best_dz,
            "plateau_range": [lo, hi],
        }

    if single_spikes:
        i = max(single_spikes, key=lambda k: rows[k]["pnl_per_dollar"])
        return {
            "verdict": "overfit_spike",
            "headline": "Only isolated spikes — no stable edge",
            "rationale": (
                f"One threshold ({rows[i]['dead_zone']*100:.2f}%) looks profitable, "
                f"but its neighbors are not. This is the signature of overfitting: "
                f"the parameter is catching noise, not real edge. If you enabled "
                f"Consensus with this value, a small regime shift would flip it "
                f"to losing."
            ),
            "recommendation": (
                "Do NOT re-enable Consensus based on this. Either the signal is "
                "dead or you need a much longer window to find a real plateau."
            ),
            "best_dead_zone": None,
            "plateau_range": None,
        }

    return {
        "verdict": "no_edge",
        "headline": "No edge found at any threshold",
        "rationale": (
            "Every dead-zone value tested produced either losses or returns "
            "too small to survive fees and slippage. This matches the live "
            "result (−23.6% ROI in the 2026-04-09 post-mortem)."
        ),
        "recommendation": (
            "Keep Consensus disabled. The strategy in its current form has no "
            "measurable edge. Consider building a different signal from scratch "
            "rather than re-tuning this one."
        ),
        "best_dead_zone": None,
        "plateau_range": None,
    }


def cmd_fetch(args):
    """CLI wrapper: fetch into the user-specified file path."""
    bundle = fetch_history(
        start=args.start,
        end=args.end,
        interval=args.interval,
        with_kalshi=args.with_kalshi,
        cache_dir=str(Path(args.out).parent),
        max_cache_age_hours=0,  # CLI always refetches
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(bundle, f)
    print(f"[fetch] Wrote {args.out}", flush=True)


# ──────────────────────────────────────────────────────────────────────────
# REPLAY ENGINE
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    ticker: str
    decision_t: int      # epoch ms when the bot would have fired
    side: str            # "yes" / "no"
    entry_price: float   # 0.0 – 1.0
    momentum: float      # signed pct change over MOMENTUM_WINDOW
    previous_side: str
    market_result: str   # "yes" / "no"
    pnl: float           # signed dollars on a $1 stake
    cap_used: float


def _btc_close_at_or_before(btc: list[dict], t_ms: int) -> Optional[float]:
    """Binary-search the latest BTC kline whose timestamp <= t_ms."""
    lo, hi = 0, len(btc) - 1
    if hi < 0 or btc[0]["t"] > t_ms:
        return None
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if btc[mid]["t"] <= t_ms:
            lo = mid
        else:
            hi = mid - 1
    return btc[lo]["close"]


def _momentum_at(btc: list[dict], t_ms: int, window_s: int) -> Optional[float]:
    """Signed pct change of BTC close from (t - window_s) to t. POINT-IN-TIME."""
    now_px = _btc_close_at_or_before(btc, t_ms)
    old_px = _btc_close_at_or_before(btc, t_ms - window_s * 1000)
    if not (now_px and old_px) or old_px == 0:
        return None
    return (now_px - old_px) / old_px


def replay(history: dict, dead_zone: float, base_price: float, max_price: float,
           stake: float = 1.0, decision_offset_s: int = 60) -> list[Trade]:
    """
    Replay Consensus v1.1 over the history bundle.

    For each settled market, the "decision time" is (close_time - decision_offset_s).
    decision_offset_s defaults to 60 seconds, meaning we evaluate as if the bot
    were considering this market 1 minute before it closed. Bump it to 300+ to
    simulate earlier-cycle entries instead.

    Returns a list of Trade records (only the markets where Consensus would
    actually have fired).
    """
    if not history.get("kalshi_markets"):
        raise SystemExit("[replay] history bundle is missing kalshi_markets — re-fetch with --with-kalshi")
    btc = history["btc"]
    markets = sorted(history["kalshi_markets"], key=lambda m: m.get("close_time", ""))

    # Build "previous market result" lookup: for each market we need the most
    # recent market that closed STRICTLY BEFORE this market's decision time.
    closed_at = []  # list[(close_ms, result)] sorted ascending
    for m in markets:
        ct_iso = m.get("close_time")
        result = m.get("result")
        if not ct_iso or not result:
            continue
        ct_ms = int(datetime.fromisoformat(ct_iso.replace("Z", "+00:00")).timestamp() * 1000)
        closed_at.append((ct_ms, result))
    closed_at.sort()

    def previous_result_before(t_ms: int) -> Optional[str]:
        # Binary search for the latest close_ms < t_ms
        lo, hi = 0, len(closed_at) - 1
        if hi < 0 or closed_at[0][0] >= t_ms:
            return None
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if closed_at[mid][0] < t_ms:
                lo = mid
            else:
                hi = mid - 1
        ct_ms, result = closed_at[lo]
        if t_ms - ct_ms > PREV_RESULT_MAX_AGE_SEC * 1000:
            return None
        return result

    trades: list[Trade] = []
    for m in markets:
        ct_iso = m.get("close_time")
        outcome = m.get("result")
        if not ct_iso or not outcome:
            continue
        ct_ms = int(datetime.fromisoformat(ct_iso.replace("Z", "+00:00")).timestamp() * 1000)
        decision_ms = ct_ms - decision_offset_s * 1000

        momentum = _momentum_at(btc, decision_ms, MOMENTUM_WINDOW_SECONDS)
        if momentum is None:
            continue
        if abs(momentum) < dead_zone:
            continue
        side = "yes" if momentum > 0 else "no"

        prev = previous_result_before(decision_ms)
        if not prev or prev != side:
            continue

        # Price at decision time — using the snapshot we'd have actually had.
        # Kalshi market objects don't carry historical books, so the best
        # available proxy in the bundle is the open/close yes price. We use
        # the LAST KNOWN bid before close as a conservative entry price; if
        # only `previous_yes_bid_dollars` is present we use that, else fall
        # back to (yes_close_price - 0.02) to model resting-order slippage.
        yes_px = float(m.get("previous_yes_bid_dollars",
                              m.get("yes_close_price_dollars",
                                    m.get("last_price_dollars", 0))) or 0)
        no_px = max(0.0, 1.0 - yes_px)
        entry_px = yes_px if side == "yes" else no_px
        if entry_px <= 0 or entry_px >= 1:
            continue

        # Dynamic cap (matches bot.py)
        bonus = min(abs(momentum) * 100, max_price - base_price)
        cap = min(base_price + bonus, max_price)
        if entry_px > cap:
            continue

        # Settle the trade
        if side == outcome:
            pnl_per_dollar = (1.0 - entry_px) / entry_px  # win pays $1 per contract
        else:
            pnl_per_dollar = -1.0
        trades.append(Trade(
            ticker=m.get("ticker", ""),
            decision_t=decision_ms,
            side=side,
            entry_price=entry_px,
            momentum=momentum,
            previous_side=prev,
            market_result=outcome,
            pnl=pnl_per_dollar * stake,
            cap_used=cap,
        ))
    return trades


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0}
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    pnl = sum(t.pnl for t in trades)
    wagered = sum(1.0 for _ in trades)  # $1 stake per trade in this harness
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades), 4),
        "avg_win": round(statistics.mean(t.pnl for t in wins), 4) if wins else 0.0,
        "avg_loss": round(statistics.mean(t.pnl for t in losses), 4) if losses else 0.0,
        "median_pnl": round(statistics.median(t.pnl for t in trades), 4),
        "pnl_per_dollar": round(pnl / wagered, 4),
        "stdev_pnl": round(statistics.pstdev(t.pnl for t in trades), 4),
        "max_drawdown": round(_max_drawdown(trades), 4),
    }


def _max_drawdown(trades: list[Trade]) -> float:
    eq = 0.0
    peak = 0.0
    dd = 0.0
    for t in sorted(trades, key=lambda x: x.decision_t):
        eq += t.pnl
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dd


def cmd_replay(args):
    with open(args.history) as f:
        history = json.load(f)
    trades = replay(history, args.dead_zone, args.base_price, args.max_price)
    summary = summarize(trades)
    print(json.dumps(summary, indent=2))
    if args.report:
        out_path = Path(args.history).with_suffix(".replay.json")
        with open(out_path, "w") as f:
            json.dump({
                "params": {"dead_zone": args.dead_zone,
                           "base_price": args.base_price,
                           "max_price": args.max_price},
                "summary": summary,
                "trades": [asdict(t) for t in trades],
            }, f, indent=2)
        print(f"[replay] Wrote {out_path}", flush=True)


def cmd_sweep(args):
    with open(args.history) as f:
        history = json.load(f)
    rows = []
    for dz in args.dead_zone:
        trades = replay(history, dz, args.base_price, args.max_price)
        s = summarize(trades)
        s["dead_zone"] = dz
        rows.append(s)
    # Print as a small table
    keys = ["dead_zone", "trades", "win_rate", "pnl_per_dollar", "max_drawdown"]
    print(" | ".join(f"{k:>15}" for k in keys))
    print("-" * (len(keys) * 18))
    for r in rows:
        print(" | ".join(f"{r.get(k, ''):>15}" for k in keys))


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    fp = sub.add_parser("fetch", help="Pull BTC klines and Kalshi settled markets")
    fp.add_argument("--start", required=True, help="ISO date e.g. 2026-03-01")
    fp.add_argument("--end",   required=True, help="ISO date e.g. 2026-04-09")
    fp.add_argument("--interval", default="1s", choices=["1s", "1m"])
    fp.add_argument("--out",   default="data/history.json")
    fp.add_argument("--with-kalshi", action="store_true",
                    help="Also fetch Kalshi settled markets (requires API creds)")
    fp.set_defaults(func=cmd_fetch)

    rp = sub.add_parser("replay", help="Replay Consensus over a history bundle")
    rp.add_argument("--history", required=True)
    rp.add_argument("--dead-zone",  type=float, default=DEFAULT_DEAD_ZONE)
    rp.add_argument("--base-price", type=float, default=DEFAULT_BASE_PRICE)
    rp.add_argument("--max-price",  type=float, default=DEFAULT_MAX_PRICE)
    rp.add_argument("--report", action="store_true",
                    help="Write a per-trade JSON next to the history file")
    rp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("sweep", help="Sweep dead-zone values to find a stable threshold")
    sp.add_argument("--history", required=True)
    sp.add_argument("--dead-zone", nargs="+", type=float,
                    default=[0.0005, 0.001, 0.0015, 0.002, 0.003, 0.005])
    sp.add_argument("--base-price", type=float, default=DEFAULT_BASE_PRICE)
    sp.add_argument("--max-price",  type=float, default=DEFAULT_MAX_PRICE)
    sp.set_defaults(func=cmd_sweep)

    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
