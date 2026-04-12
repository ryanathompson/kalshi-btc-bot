#!/usr/bin/env python3
"""
analyze_edge.py — Mine the 136 ground-truth fills for exploitable patterns.

Pulls fills + settlements + BTC price history and runs a battery of cuts
to find any signal that reliably separates winners from losers in the
KXBTC15M universe. No assumptions about what the edge is — let the data
show us.

Signals tested:
  1. Entry price band (cheap OTM vs near-ATM vs expensive)
  2. BTC momentum at fill time (multiple lookback windows)
  3. Time-to-close at entry
  4. Hour-of-day / session (Asia / Europe / US)
  5. Strike distance from BTC spot at entry
  6. BTC realized volatility regime (calm vs volatile)
  7. Previous-market result (the "consensus" signal — does it actually predict?)
  8. Interactions: cheap + volatile, momentum + time-to-close, etc.

For each cut, reports: n, WR, avg_pnl, total_pnl, edge_vs_fair.
Edge_vs_fair = WR - breakeven_WR(avg_entry_price). A positive edge_vs_fair
means the signal is beating the implied odds of the entry price.

Usage:
    python analyze_edge.py              # human report
    python analyze_edge.py --json       # machine-readable
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

try:
    from bot import (
        KalshiClient, load_private_key, BTC_TICKER,
        ET, parse_trade_ts,
    )
except Exception as e:
    print(f"FATAL: could not import bot.py: {e}", file=sys.stderr)
    sys.exit(1)


# ── Kalshi API helpers ──────────────────────────────────────────────

def _paginate(client, path, key, extra=None):
    out, cursor = [], None
    while True:
        p = {"limit": 200}
        if extra: p.update(extra)
        if cursor: p["cursor"] = cursor
        resp = client.get(path, params=p)
        page = resp.get(key, []) or []
        out.extend(page)
        cursor = resp.get("cursor")
        if not cursor or not page: break
    return out


def fetch_fills(client):
    fills = _paginate(client, "/portfolio/fills", "fills")
    return [f for f in fills if BTC_TICKER in (f.get("ticker") or "")]


def fetch_settlements(client):
    setts = _paginate(client, "/portfolio/settlements", "settlements")
    by_ticker = {}
    for s in setts:
        t = s.get("ticker") or s.get("market_ticker") or ""
        if BTC_TICKER in t:
            by_ticker[t] = s
    return by_ticker


def fetch_orders(client):
    orders = _paginate(client, "/portfolio/orders", "orders")
    return {o["order_id"]: o for o in orders if o.get("order_id")}


def fetch_markets_settled(client, limit=500):
    """Fetch recently settled KXBTC15M markets for previous-result analysis."""
    markets = _paginate(client, "/markets", "markets",
                        extra={"series_ticker": BTC_TICKER,
                               "status": "settled", "limit": 200})
    return sorted(markets, key=lambda m: m.get("close_time", ""))


# ── BTC price history from Coinbase ─────────────────────────────────

def fetch_btc_klines(start_iso: str, end_iso: str, granularity: int = 300):
    """Fetch BTC-USD candles from Coinbase. granularity in seconds (300=5min)."""
    url = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    all_candles = []
    # Coinbase limits to 300 candles per request
    chunk = timedelta(seconds=granularity * 300)
    t = start_dt
    while t < end_dt:
        t_end = min(t + chunk, end_dt)
        params = {
            "start": t.isoformat(),
            "end": t_end.isoformat(),
            "granularity": granularity,
        }
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            candles = r.json()
            all_candles.extend(candles)
        except Exception as e:
            print(f"[btc] chunk {t} failed: {e}", file=sys.stderr)
        t = t_end
    # Coinbase format: [timestamp, low, high, open, close, volume]
    # Sort by timestamp ascending
    all_candles.sort(key=lambda c: c[0])
    return all_candles


def build_btc_index(candles):
    """Build {epoch_s: close_price} for quick lookup."""
    idx = {}
    for c in candles:
        idx[int(c[0])] = float(c[4])  # close
    return idx


def btc_at(idx, epoch_s, max_age=600):
    """Find closest BTC price at or before epoch_s."""
    best_ts, best_px = None, None
    for ts, px in idx.items():
        if ts <= epoch_s and (best_ts is None or ts > best_ts):
            best_ts = ts
            best_px = px
    if best_ts and (epoch_s - best_ts) <= max_age:
        return best_px
    return None


def btc_momentum(idx, epoch_s, lookback_s):
    """Compute BTC % change over lookback_s ending at epoch_s."""
    px_now = btc_at(idx, epoch_s)
    px_before = btc_at(idx, epoch_s - lookback_s)
    if px_now and px_before and px_before > 0:
        return (px_now - px_before) / px_before
    return None


def btc_realized_vol(idx, epoch_s, window_s=3600):
    """Realized volatility (stddev of 5-min returns) over window_s."""
    returns = []
    candle_times = sorted(ts for ts in idx if epoch_s - window_s <= ts <= epoch_s)
    for i in range(1, len(candle_times)):
        p0 = idx[candle_times[i-1]]
        p1 = idx[candle_times[i]]
        if p0 > 0:
            returns.append((p1 - p0) / p0)
    if len(returns) < 3:
        return None
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / len(returns)
    return var ** 0.5


# ── Position builder ────────────────────────────────────────────────

PREFIX_TO_STRATEGY = {"LAG": "LAG", "CON": "CONSENSUS", "TAI": "TAIL"}


def build_positions(fills, settlements, orders):
    by_order = defaultdict(list)
    for f in fills:
        oid = f.get("order_id") or f.get("fill_id") or "unk"
        by_order[oid].append(f)

    positions = []
    for oid, group in by_order.items():
        group.sort(key=lambda f: f.get("created_time", ""))
        f0 = group[0]
        ticker = f0.get("ticker", "")
        side = f0.get("side", "")

        total_count = sum(float(f.get("count_fp", f.get("count", 0))) for f in group)
        total_cost = 0.0
        for f in group:
            c = float(f.get("count_fp", f.get("count", 0)))
            px = float(f.get("yes_price_dollars" if side == "yes" else "no_price_dollars", 0))
            total_cost += c * px
        entry_price = total_cost / total_count if total_count else 0

        sett = settlements.get(ticker)
        outcome = sett.get("market_result") if sett else None
        result = None
        pnl = None
        payout = 0.0
        if outcome:
            won = (side == outcome)
            result = "WIN" if won else "LOSS"
            payout = round(total_count, 2) if won else 0.0
            pnl = round(payout - total_cost, 2)

        order = orders.get(oid, {})
        coid = order.get("client_order_id", "")
        prefix = coid.split("-", 1)[0].upper() if "-" in coid else ""
        strategy = PREFIX_TO_STRATEGY.get(prefix, "UNKNOWN")

        # Parse fill timestamp
        fill_ts_str = f0.get("created_time", "")
        fill_dt = parse_trade_ts(fill_ts_str) if fill_ts_str else None
        fill_epoch = fill_dt.timestamp() if fill_dt else None

        # Parse market close time from ticker or settlement
        close_ts_str = None
        if sett:
            close_ts_str = sett.get("settled_time") or sett.get("close_time")

        positions.append({
            "order_id": oid,
            "ticker": ticker,
            "side": side,
            "strategy": strategy,
            "count": round(total_count, 4),
            "cost": round(total_cost, 4),
            "entry_price": round(entry_price, 4),
            "payout": payout,
            "pnl": pnl,
            "result": result,
            "outcome": outcome,
            "fill_ts": fill_ts_str,
            "fill_epoch": fill_epoch,
            "fill_dt": fill_dt,
        })

    return positions


# ── Analysis cuts ───────────────────────────────────────────────────

def breakeven_wr(entry_price):
    """Win rate needed to break even at a given entry price (binary contract)."""
    if entry_price <= 0 or entry_price >= 1:
        return 0.5
    return entry_price  # at entry p, payout is 1-p on win, lose p on loss; BE = p


def analyze_cut(label, positions):
    """Compute stats for a subset of positions."""
    settled = [p for p in positions if p["result"]]
    if not settled:
        return None
    n = len(settled)
    wins = sum(1 for p in settled if p["result"] == "WIN")
    losses = n - wins
    wr = wins / n if n else 0
    total_pnl = sum(p["pnl"] for p in settled if p["pnl"] is not None)
    avg_pnl = total_pnl / n if n else 0
    total_cost = sum(p["cost"] for p in settled)
    roi = total_pnl / total_cost if total_cost else 0
    avg_entry = sum(p["entry_price"] for p in settled) / n if n else 0
    be_wr = breakeven_wr(avg_entry)
    edge = wr - be_wr

    return {
        "label": label,
        "n": n,
        "wins": wins,
        "losses": losses,
        "wr": round(wr * 100, 1),
        "avg_entry": round(avg_entry, 3),
        "be_wr": round(be_wr * 100, 1),
        "edge_vs_fair": round(edge * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(avg_pnl, 2),
        "roi": round(roi * 100, 1),
        "total_cost": round(total_cost, 2),
    }


def print_cut(c):
    if not c:
        return
    edge_marker = "✓" if c["edge_vs_fair"] > 0 else "✗"
    print(f"  {c['label']:<40} n={c['n']:<4} WR={c['wr']:>5.1f}%  "
          f"BE={c['be_wr']:>5.1f}%  edge={c['edge_vs_fair']:>+5.1f}pp {edge_marker}  "
          f"pnl=${c['total_pnl']:>+8.2f}  roi={c['roi']:>+6.1f}%")


# ── Main ────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    if not api_key_id:
        print("FATAL: KALSHI_API_KEY_ID not set", file=sys.stderr)
        return 1
    pkey = load_private_key()
    client = KalshiClient(api_key_id, pkey, dry_run=True)

    print("[edge] Fetching fills...", file=sys.stderr)
    fills = fetch_fills(client)
    print(f"[edge] {len(fills)} BTC fills", file=sys.stderr)

    print("[edge] Fetching settlements...", file=sys.stderr)
    settlements = fetch_settlements(client)

    print("[edge] Fetching orders...", file=sys.stderr)
    orders = fetch_orders(client)

    positions = build_positions(fills, settlements, orders)
    settled = [p for p in positions if p["result"]]
    print(f"[edge] {len(settled)} settled positions", file=sys.stderr)

    if not settled:
        print("No settled positions to analyze.")
        return 0

    # Find date range for BTC history
    fill_epochs = [p["fill_epoch"] for p in settled if p["fill_epoch"]]
    if not fill_epochs:
        print("No parseable fill timestamps.")
        return 1
    min_epoch = min(fill_epochs) - 7200  # 2 hours before first fill
    max_epoch = max(fill_epochs) + 3600  # 1 hour after last fill
    start_iso = datetime.fromtimestamp(min_epoch, tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(max_epoch, tz=timezone.utc).isoformat()

    print(f"[edge] Fetching BTC candles {start_iso[:16]} → {end_iso[:16]}...",
          file=sys.stderr)
    candles = fetch_btc_klines(start_iso, end_iso, granularity=300)
    print(f"[edge] {len(candles)} candles", file=sys.stderr)
    btc_idx = build_btc_index(candles)

    # Enrich positions with BTC context at fill time
    for p in settled:
        ep = p["fill_epoch"]
        if not ep:
            continue
        p["btc_at_fill"] = btc_at(btc_idx, ep)
        p["mom_60s"] = btc_momentum(btc_idx, ep, 60)
        p["mom_300s"] = btc_momentum(btc_idx, ep, 300)
        p["mom_900s"] = btc_momentum(btc_idx, ep, 900)
        p["rvol_1h"] = btc_realized_vol(btc_idx, ep, 3600)

        # Strike distance: parse strike from ticker
        import re
        m = re.search(r"-([TB])(\d+)(?:-(\d+))?$", p["ticker"])
        if m and p["btc_at_fill"]:
            strike = float(m.group(2))
            p["strike_dist_pct"] = (strike - p["btc_at_fill"]) / p["btc_at_fill"] * 100
            p["strike_dist_abs"] = abs(p["strike_dist_pct"])
        else:
            p["strike_dist_pct"] = None
            p["strike_dist_abs"] = None

        # Hour of day (ET)
        if p["fill_dt"]:
            et_dt = p["fill_dt"].astimezone(ET)
            p["hour_et"] = et_dt.hour
        else:
            p["hour_et"] = None

    # ── Run all cuts ──────────────────────────────────────────────
    all_cuts = []

    print()
    print("=" * 100)
    print("EDGE ANALYSIS — 136 ground-truth fills, KXBTC15M")
    print("=" * 100)

    # 1. Overall
    print("\n── OVERALL ──")
    c = analyze_cut("ALL FILLS", settled)
    print_cut(c)
    all_cuts.append(c)

    # 2. Entry price bands
    print("\n── ENTRY PRICE BANDS ──")
    bands = [
        ("1-10c (deep OTM)",  0.01, 0.10),
        ("11-25c (OTM)",      0.11, 0.25),
        ("26-39c (mild OTM)", 0.26, 0.39),
        ("40-49c (near ATM)", 0.40, 0.49),
        ("50-55c (ATM+)",     0.50, 0.55),
    ]
    for label, lo, hi in bands:
        subset = [p for p in settled if lo <= p["entry_price"] <= hi]
        c = analyze_cut(label, subset)
        if c:
            print_cut(c)
            all_cuts.append(c)

    # 3. BTC momentum at entry (60s)
    print("\n── BTC 60s MOMENTUM AT ENTRY ──")
    mom_cuts = [
        ("Strong down (<-0.1%)",  -999, -0.001),
        ("Mild down (-0.1% to -0.03%)", -0.001, -0.0003),
        ("Dead zone (-0.03% to +0.03%)", -0.0003, 0.0003),
        ("Mild up (+0.03% to +0.1%)",    0.0003, 0.001),
        ("Strong up (>+0.1%)",           0.001, 999),
    ]
    for label, lo, hi in mom_cuts:
        subset = [p for p in settled if p.get("mom_60s") is not None
                  and lo <= p["mom_60s"] <= hi]
        c = analyze_cut(label, subset)
        if c:
            print_cut(c)
            all_cuts.append(c)

    # 4. BTC 5-min momentum at entry
    print("\n── BTC 5min MOMENTUM AT ENTRY ──")
    mom5_cuts = [
        ("5m strong down (<-0.2%)",  -999, -0.002),
        ("5m mild down (-0.2% to 0)", -0.002, 0),
        ("5m mild up (0 to +0.2%)",   0, 0.002),
        ("5m strong up (>+0.2%)",     0.002, 999),
    ]
    for label, lo, hi in mom5_cuts:
        subset = [p for p in settled if p.get("mom_300s") is not None
                  and lo <= p["mom_300s"] <= hi]
        c = analyze_cut(label, subset)
        if c:
            print_cut(c)
            all_cuts.append(c)

    # 5. BTC 15-min momentum at entry
    print("\n── BTC 15min MOMENTUM AT ENTRY ──")
    mom15_cuts = [
        ("15m strong down (<-0.3%)",  -999, -0.003),
        ("15m mild down (-0.3% to 0)", -0.003, 0),
        ("15m mild up (0 to +0.3%)",   0, 0.003),
        ("15m strong up (>+0.3%)",     0.003, 999),
    ]
    for label, lo, hi in mom15_cuts:
        subset = [p for p in settled if p.get("mom_900s") is not None
                  and lo <= p["mom_900s"] <= hi]
        c = analyze_cut(label, subset)
        if c:
            print_cut(c)
            all_cuts.append(c)

    # 6. Realized volatility regime
    print("\n── REALIZED VOLATILITY REGIME (1h) ──")
    vol_positions = [p for p in settled if p.get("rvol_1h") is not None]
    if vol_positions:
        vols = [p["rvol_1h"] for p in vol_positions]
        med_vol = sorted(vols)[len(vols) // 2]
        for label, pred in [
            ("Low vol (below median)", lambda p: p["rvol_1h"] < med_vol),
            ("High vol (above median)", lambda p: p["rvol_1h"] >= med_vol),
        ]:
            subset = [p for p in vol_positions if pred(p)]
            c = analyze_cut(label, subset)
            if c:
                print_cut(c)
                all_cuts.append(c)

    # 7. Strike distance from spot
    print("\n── STRIKE DISTANCE FROM SPOT ──")
    dist_cuts = [
        ("ATM (< 0.1% from spot)",   0, 0.1),
        ("Near OTM (0.1-0.3%)",      0.1, 0.3),
        ("OTM (0.3-0.5%)",           0.3, 0.5),
        ("Deep OTM (>0.5%)",         0.5, 999),
    ]
    for label, lo, hi in dist_cuts:
        subset = [p for p in settled if p.get("strike_dist_abs") is not None
                  and lo <= p["strike_dist_abs"] < hi]
        c = analyze_cut(label, subset)
        if c:
            print_cut(c)
            all_cuts.append(c)

    # 8. Hour of day (ET)
    print("\n── HOUR OF DAY (ET) ──")
    hour_groups = [
        ("US session (9-16 ET)",   range(9, 17)),
        ("US evening (17-23 ET)",  range(17, 24)),
        ("Asia/night (0-8 ET)",    range(0, 9)),
    ]
    for label, hours in hour_groups:
        subset = [p for p in settled if p.get("hour_et") in hours]
        c = analyze_cut(label, subset)
        if c:
            print_cut(c)
            all_cuts.append(c)

    # 9. Side (YES vs NO)
    print("\n── SIDE ──")
    for side_val in ["yes", "no"]:
        subset = [p for p in settled if p["side"] == side_val]
        c = analyze_cut(f"Side = {side_val.upper()}", subset)
        if c:
            print_cut(c)
            all_cuts.append(c)

    # 10. Interaction: cheap + high vol
    print("\n── INTERACTIONS ──")
    if vol_positions:
        cheap_highvol = [p for p in vol_positions
                         if p["entry_price"] <= 0.25 and p["rvol_1h"] >= med_vol]
        c = analyze_cut("Cheap (≤25c) + High vol", cheap_highvol)
        if c:
            print_cut(c)
            all_cuts.append(c)

        cheap_lowvol = [p for p in vol_positions
                        if p["entry_price"] <= 0.25 and p["rvol_1h"] < med_vol]
        c = analyze_cut("Cheap (≤25c) + Low vol", cheap_lowvol)
        if c:
            print_cut(c)
            all_cuts.append(c)

    # Momentum alignment with side
    print("\n── MOMENTUM ALIGNMENT ──")
    for mom_key, mom_label in [("mom_60s", "60s"), ("mom_300s", "5min"), ("mom_900s", "15min")]:
        aligned = [p for p in settled
                   if p.get(mom_key) is not None
                   and ((p["side"] == "yes" and p[mom_key] > 0)
                        or (p["side"] == "no" and p[mom_key] < 0))]
        counter = [p for p in settled
                   if p.get(mom_key) is not None
                   and ((p["side"] == "yes" and p[mom_key] < 0)
                        or (p["side"] == "no" and p[mom_key] > 0))]
        c1 = analyze_cut(f"Aligned w/ {mom_label} momentum", aligned)
        c2 = analyze_cut(f"Counter to {mom_label} momentum", counter)
        if c1: print_cut(c1); all_cuts.append(c1)
        if c2: print_cut(c2); all_cuts.append(c2)

    # 11. Summary: rank all cuts by edge_vs_fair
    print("\n" + "=" * 100)
    print("RANKED BY EDGE vs FAIR (positive = beating the odds)")
    print("=" * 100)
    ranked = sorted([c for c in all_cuts if c and c["n"] >= 5],
                    key=lambda c: c["edge_vs_fair"], reverse=True)
    for c in ranked:
        print_cut(c)

    print()
    if args.json:
        json.dump({"cuts": all_cuts}, sys.stdout, indent=2, default=str)
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
