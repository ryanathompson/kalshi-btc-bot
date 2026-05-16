"""Polymarket read-only spike: capture BTC 15-min mids alongside
Kalshi KXBTC15M mids and Coinbase BTC-USD spot.

Goal: see how often Polymarket and Kalshi disagree by enough
to be tradeable after fees. No execution, no auth needed.

Run on a host with internet egress to:
  - gamma-api.polymarket.com  (market metadata)
  - clob.polymarket.com       (live mids, via py-clob-client)
  - api.elections.kalshi.com  (Kalshi public)
  - api.exchange.coinbase.com (spot)

Usage:
  pip install py-clob-client httpx[socks]
  python3 tools/polymarket_spike.py --duration 300 \
      --out logs/poly_spike_$(date +%Y%m%dT%H%M).jsonl

Output rows are one JSON per line, alternating per source.
Run tools/polymarket_spike.py --analyze logs/poly_spike_*.jsonl
to print spread stats.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from collections import defaultdict
from typing import Any


GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
CB_TICKER = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"


# ---------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------

def http_json(url: str, timeout: float = 5.0) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "kalshi-poly-spike/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def find_open_polymarket_btc_15m() -> dict | None:
    """Return the soonest-resolving open BTC 15-min market.

    Polymarket Gamma slug pattern: btc-updown-15m-<unix_ts_start>.
    We search for active markets with the slug prefix and pick the
    one whose end time is closest to now (still open).
    """
    # Gamma supports ?active=true&closed=false&slug_prefix=...
    # but the canonical path is /markets with filters. We try a
    # paginated active sweep filtered by tag.
    params = "active=true&closed=false&limit=50&tag=crypto"
    try:
        markets = http_json(f"{GAMMA}/markets?{params}")
    except Exception as exc:  # noqa: BLE001
        print(f"  gamma fetch failed: {exc}", file=sys.stderr)
        return None
    candidates = []
    now = time.time()
    for m in (markets if isinstance(markets, list) else markets.get("markets", [])):
        slug = m.get("slug") or ""
        end = m.get("endDate") or m.get("end_date_iso") or ""
        if not slug.startswith("btc-updown-15m-"):
            continue
        # Remaining time to settlement
        try:
            # end may be ISO8601
            from datetime import datetime, timezone
            tend = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
        except Exception:  # noqa: BLE001
            tend = now + 900
        if tend <= now:
            continue
        candidates.append((tend - now, m))
    if not candidates:
        return None
    candidates.sort()
    return candidates[0][1]


def find_open_kalshi_btc_15m() -> dict | None:
    try:
        body = http_json(f"{KALSHI}/events?series_ticker=KXBTC15M&status=open&limit=20")
    except Exception as exc:  # noqa: BLE001
        print(f"  kalshi events failed: {exc}", file=sys.stderr)
        return None
    events = body.get("events") or []
    if not events:
        return None
    events.sort(key=lambda e: e.get("close_time") or "")
    # Get markets for that event
    ev = events[0]
    body = http_json(f"{KALSHI}/markets?event_ticker={ev['event_ticker']}&limit=200")
    markets = body.get("markets") or []
    # KXBTC15M is binary up/down — pick the "up" market
    if markets:
        return {"event": ev, "markets": markets}
    return None


# ---------------------------------------------------------------
# Snapshot loop
# ---------------------------------------------------------------

def snapshot_polymarket(client, token_id: str) -> dict:
    out: dict = {"t": time.time(), "kind": "poly", "token_id": token_id}
    try:
        out["mid"] = float(client.get_midpoint(token_id)["mid"])
    except Exception as exc:  # noqa: BLE001
        out["err_mid"] = str(exc)
    try:
        out["spread"] = float(client.get_spread(token_id)["spread"])
    except Exception as exc:  # noqa: BLE001
        out["err_spread"] = str(exc)
    return out


def snapshot_kalshi(market: dict) -> dict:
    yb = market.get("yes_bid")
    ya = market.get("yes_ask")
    return {
        "t": time.time(), "kind": "kalshi",
        "ticker": market.get("ticker"),
        "yes_bid_c": yb,
        "yes_ask_c": ya,
        "mid": ((yb + ya) / 200.0) if (yb and ya) else None,
    }


def snapshot_spot() -> dict:
    out = {"t": time.time(), "kind": "spot"}
    try:
        body = http_json(CB_TICKER)
        out["price"] = float(body["price"])
    except Exception as exc:  # noqa: BLE001
        out["err"] = str(exc)
    return out


def capture(duration: int, out_path: str) -> str:
    from py_clob_client.client import ClobClient

    poly_market = find_open_polymarket_btc_15m()
    kalshi_state = find_open_kalshi_btc_15m()
    if not poly_market or not kalshi_state:
        raise SystemExit("could not find both open markets")
    print(f"  poly slug: {poly_market.get('slug')}", file=sys.stderr)
    # Polymarket markets have two outcomes (yes/no); pick the YES token.
    tokens = poly_market.get("clobTokenIds") or poly_market.get("tokens")
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    if not tokens:
        raise SystemExit("no clobTokenIds on poly market")
    yes_token = tokens[0] if isinstance(tokens, list) else tokens.get("yes")
    print(f"  poly YES token: {yes_token}", file=sys.stderr)

    # Kalshi BTC15M event has many strike markets; the up/down is
    # actually one ticker in this series — pick the one whose
    # ticker ends with "-T" (no strike). Fall back to highest volume.
    candidates = sorted(
        kalshi_state["markets"],
        key=lambda m: m.get("volume_24h") or 0,
        reverse=True,
    )
    kalshi_market = candidates[0]
    print(f"  kalshi ticker: {kalshi_market.get('ticker')}", file=sys.stderr)

    client = ClobClient(CLOB)

    deadline = time.time() + duration
    fout = open(out_path, "w", encoding="utf-8")
    try:
        while time.time() < deadline:
            t0 = time.time()
            fout.write(json.dumps(snapshot_spot()) + "\n")
            fout.write(json.dumps(snapshot_polymarket(client, yes_token)) + "\n")
            # Refresh kalshi market state every tick
            try:
                refreshed = http_json(
                    f"{KALSHI}/markets/{kalshi_market['ticker']}", timeout=3.0
                )
                m = refreshed.get("market") or kalshi_market
            except Exception:  # noqa: BLE001
                m = kalshi_market
            fout.write(json.dumps(snapshot_kalshi(m)) + "\n")
            fout.flush()
            time.sleep(max(0.0, 1.0 - (time.time() - t0)))
    finally:
        fout.close()
    return out_path


# ---------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------

def analyze(path: str) -> None:
    poly: list[tuple[float, float]] = []
    kal:  list[tuple[float, float]] = []
    spot: list[tuple[float, float]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("t")
            if d.get("kind") == "poly" and "mid" in d:
                poly.append((t, d["mid"]))
            elif d.get("kind") == "kalshi" and d.get("mid") is not None:
                kal.append((t, d["mid"]))
            elif d.get("kind") == "spot" and "price" in d:
                spot.append((t, d["price"]))
    if not poly or not kal:
        print("not enough overlap")
        return
    # Resample to 1Hz grid
    t0 = math.ceil(max(poly[0][0], kal[0][0]))
    t1 = math.floor(min(poly[-1][0], kal[-1][0]))
    if t1 - t0 < 30:
        print(f"overlap {t1-t0:.0f}s — too short")
        return

    def grid(seq):
        out = [math.nan] * (int(t1 - t0) + 1)
        j = 0
        last = math.nan
        for i in range(len(out)):
            ti = t0 + i
            while j < len(seq) and seq[j][0] <= ti:
                last = seq[j][1]
                j += 1
            out[i] = last
        return out

    pg = grid(poly)
    kg = grid(kal)
    spreads = [pg[i] - kg[i] for i in range(len(pg))
               if not math.isnan(pg[i]) and not math.isnan(kg[i])]
    if not spreads:
        print("no aligned points")
        return
    spreads.sort()
    n = len(spreads)
    p50 = spreads[n // 2]
    p90 = spreads[int(n * 0.9)]
    p99 = spreads[int(n * 0.99)] if n > 100 else spreads[-1]
    mean_abs = sum(abs(s) for s in spreads) / n
    print(f"file:      {path}")
    print(f"window:    {int(t1-t0)}s  ({(t1-t0)/60:.1f} min)")
    print(f"n:         {n} aligned 1s pairs")
    print(f"mean |Δ|: {mean_abs:.4f}  ({mean_abs*100:.2f}¢)")
    print(f"median Δ: {p50:+.4f}")
    print(f"p90 Δ:    {p90:+.4f}")
    print(f"p99 Δ:    {p99:+.4f}")
    # Time fraction with |Δ| > 3¢ (rough fee threshold)
    above = sum(1 for s in spreads if abs(s) > 0.03)
    print(f"|Δ|>3¢:    {above}/{n}  ({above*100/n:.1f}%)")


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--duration", type=int, default=180)
    p.add_argument("--out", default=f"logs/poly_spike_{int(time.time())}.jsonl")
    p.add_argument("--analyze")
    args = p.parse_args(argv)
    if args.analyze:
        analyze(args.analyze)
        return
    path = capture(args.duration, args.out)
    print(f"\n=== analysis ===")
    analyze(path)


if __name__ == "__main__":
    main(sys.argv[1:])
