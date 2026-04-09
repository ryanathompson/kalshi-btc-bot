#!/usr/bin/env python3
"""
analyze_stale_fills.py
======================
Read-only analysis of Kalshi limit-order fill latency for KXBTC15M.

Joins /portfolio/orders against /portfolio/fills by order_id to compute
how long each filled order rested before matching, then buckets by
latency and reports the win rate / P&L per bucket.

The goal is to answer: "Do slow-filled limit orders have a meaningfully
worse win rate than fast-filled ones?" If yes, resting-order cancel
logic (signal-reversal cancel, etc.) has measurable edge. If the win
rate is flat across latency buckets, the stale-fill problem is
hypothetical and building cancel logic is premature optimization.

SAFETY
------
This script is strictly read-only. It only issues GET requests to:
  - /portfolio/orders
  - /portfolio/fills
  - /portfolio/settlements

It never places, modifies, or cancels orders. Safe to run anytime,
including while the live bot is trading.

USAGE
-----
    python analyze_stale_fills.py
    python analyze_stale_fills.py --csv fills_analysis.csv
    python analyze_stale_fills.py --days 14      # limit to last 14 days

Requires the same env vars as bot.py (KALSHI_API_KEY_ID + private key).
"""

import argparse
import csv
import datetime
import os
import sys
from collections import defaultdict

# Reuse auth + constants from bot.py so we don't diverge
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bot import (
    BASE_URL,
    BTC_TICKER,
    load_private_key,
    auth_headers,
)
import requests
from dotenv import load_dotenv

load_dotenv()


# ───────────────────────────────────────────────────────────
# BUCKETS
# ───────────────────────────────────────────────────────────

# (label, lower_bound_seconds_inclusive, upper_bound_seconds_exclusive)
LATENCY_BUCKETS = [
    ("< 2s (instant)",    0,     2),
    ("2-10s",             2,     10),
    ("10-60s",            10,    60),
    ("60-300s",           60,    300),
    ("> 300s (stale)",    300,   10**9),
]


def bucket_for(latency_s):
    for label, lo, hi in LATENCY_BUCKETS:
        if lo <= latency_s < hi:
            return label
    return "?"


# ───────────────────────────────────────────────────────────
# KALSHI API (read-only)
# ───────────────────────────────────────────────────────────

def _get(path, key_id, pkey, params=None):
    headers = auth_headers(pkey, key_id, "GET", path)
    r = requests.get(BASE_URL + path, headers=headers, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def paginate(path, key_id, pkey, collection_key, extra_params=None):
    """Generic cursor pagination for Kalshi list endpoints."""
    items = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 200}
        if extra_params:
            params.update(extra_params)
        if cursor:
            params["cursor"] = cursor
        data = _get(path, key_id, pkey, params=params)
        page = data.get(collection_key, [])
        items.extend(page)
        pages += 1
        cursor = data.get("cursor")
        if not cursor or not page:
            break
        if pages > 100:   # hard safety
            print(f"  [warn] Stopped pagination at {pages} pages for {path}")
            break
    return items


# ───────────────────────────────────────────────────────────
# TIMESTAMP HELPERS
# ───────────────────────────────────────────────────────────

def parse_ts(s):
    """Parse Kalshi ISO timestamps into tz-aware UTC datetimes."""
    if not s:
        return None
    try:
        # Kalshi emits trailing Z; Python 3.11+ tolerates it but 3.9 doesn't
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def latency_seconds(order_ts, fill_ts):
    o = parse_ts(order_ts)
    f = parse_ts(fill_ts)
    if not o or not f:
        return None
    return (f - o).total_seconds()


# ───────────────────────────────────────────────────────────
# ANALYSIS
# ───────────────────────────────────────────────────────────

def analyze(orders, fills, settlements, since_epoch=None):
    # Filter to KXBTC15M series only
    btc_orders = [o for o in orders if BTC_TICKER in (o.get("ticker") or "")]
    btc_fills  = [f for f in fills  if BTC_TICKER in (f.get("ticker") or "")]

    # Optional date cutoff
    if since_epoch is not None:
        def _after(rec, field):
            dt = parse_ts(rec.get(field))
            return dt is not None and dt.timestamp() >= since_epoch
        btc_orders = [o for o in btc_orders if _after(o, "created_time")]
        btc_fills  = [f for f in btc_fills  if _after(f, "created_time")]

    # Restrict to limit orders (bot places all orders as limits, but guard
    # against any manual market orders in the account)
    btc_orders = [o for o in btc_orders if (o.get("type") or "").lower() == "limit"]

    # Group fills by order_id so we can join back to each order
    fills_by_order = defaultdict(list)
    for f in btc_fills:
        oid = f.get("order_id")
        if oid:
            fills_by_order[oid].append(f)

    # Sort fill lists by created_time so first-fill is first in list
    for oid in fills_by_order:
        fills_by_order[oid].sort(key=lambda f: parse_ts(f.get("created_time")) or datetime.datetime.min)

    # Build settlement lookup by ticker
    settlement_by_ticker = {}
    for s in settlements:
        t = s.get("ticker") or s.get("market_ticker") or ""
        if BTC_TICKER in t:
            settlement_by_ticker[t] = s

    rows = []
    for o in btc_orders:
        oid    = o.get("order_id")
        ticker = o.get("ticker", "")
        side   = o.get("side", "")
        status = (o.get("status") or "").lower()

        placed_ts = o.get("created_time")
        o_fills   = fills_by_order.get(oid, [])

        filled = bool(o_fills)
        fill_ts = o_fills[0].get("created_time") if filled else None
        lat_s   = latency_seconds(placed_ts, fill_ts) if filled else None

        # Aggregate fill size
        if filled:
            total_count = sum(
                float(f.get("count", f.get("count_fp", 0)) or 0) for f in o_fills
            )
            price_dollars = float(
                o_fills[0].get("yes_price_dollars" if side == "yes" else "no_price_dollars", 0)
                or 0
            )
        else:
            total_count   = 0.0
            price_dollars = (o.get("yes_price" if side == "yes" else "no_price", 0) or 0) / 100.0

        dollars = round(total_count * price_dollars, 2) if filled else 0.0

        # Outcome + P&L via settlement
        result = None
        pnl    = None
        sett   = settlement_by_ticker.get(ticker)
        if filled and sett:
            outcome = sett.get("market_result") or sett.get("result")
            if outcome:
                won = (side == outcome)
                result = "WIN" if won else "LOSS"
                if won and price_dollars > 0:
                    pnl = round(total_count * (1 - price_dollars), 2)
                else:
                    pnl = -dollars

        rows.append({
            "order_id":    oid,
            "ticker":      ticker,
            "side":        side,
            "status":      status,
            "placed_ts":   placed_ts,
            "fill_ts":     fill_ts,
            "latency_s":   lat_s,
            "filled":      filled,
            "price_$":     price_dollars,
            "count":       total_count,
            "$_size":      dollars,
            "result":      result,
            "pnl":         pnl,
            "bucket":      bucket_for(lat_s) if lat_s is not None else None,
        })
    return rows


# ───────────────────────────────────────────────────────────
# REPORTING
# ───────────────────────────────────────────────────────────

def pct(n, d):
    return f"{(100.0 * n / d):5.1f}%" if d else "    —"


def print_report(rows):
    total_orders = len(rows)
    filled_rows  = [r for r in rows if r["filled"]]
    unfilled     = [r for r in rows if not r["filled"]]
    settled      = [r for r in filled_rows if r["result"] in ("WIN", "LOSS")]

    print()
    print("═══════════════════════════════════════════════════════════")
    print("  KXBTC15M LIMIT-ORDER FILL ANALYSIS")
    print("═══════════════════════════════════════════════════════════")
    print(f"  Total orders found       : {total_orders}")
    print(f"  Filled                   : {len(filled_rows):4d}  ({pct(len(filled_rows), total_orders)})")
    print(f"  Unfilled / cancelled     : {len(unfilled):4d}  ({pct(len(unfilled), total_orders)})")
    print(f"  Filled + settled         : {len(settled):4d}")

    if settled:
        wins = sum(1 for r in settled if r["result"] == "WIN")
        total_pnl = sum(r.get("pnl") or 0 for r in settled)
        print(f"  Overall win rate         : {pct(wins, len(settled))}")
        print(f"  Overall P&L              : ${total_pnl:+.2f}")
    print()

    if not settled:
        print("  No settled filled orders yet — can't compute bucketed win rates.")
        return

    # ── Bucketed table ─────────────────────────────────────
    print("─────────────────────────────────────────────────────────────")
    print("  WIN RATE BY RESTING LATENCY (filled + settled only)")
    print("─────────────────────────────────────────────────────────────")
    print(f"  {'Bucket':<20}  {'N':>5}  {'Win%':>7}  {'Avg$':>9}  {'Total$':>9}")
    print(f"  {'-'*20}  {'-'*5}  {'-'*7}  {'-'*9}  {'-'*9}")

    by_bucket = defaultdict(list)
    for r in settled:
        if r["bucket"]:
            by_bucket[r["bucket"]].append(r)

    for label, _, _ in LATENCY_BUCKETS:
        group = by_bucket.get(label, [])
        n     = len(group)
        wins  = sum(1 for r in group if r["result"] == "WIN")
        tpnl  = sum(r.get("pnl") or 0 for r in group)
        apnl  = (tpnl / n) if n else 0.0
        print(f"  {label:<20}  {n:5d}  {pct(wins, n):>7}  {apnl:+9.2f}  {tpnl:+9.2f}")

    print()

    # ── Thesis-reversal proxy ──────────────────────────────
    # An order that rests >10s and then fills is a candidate "stale fill"
    # because the market had time to move against the placement snapshot.
    slow_settled = [r for r in settled if (r["latency_s"] or 0) >= 10]
    fast_settled = [r for r in settled if (r["latency_s"] or 0) <  10]

    def _winrate(group):
        n = len(group)
        w = sum(1 for r in group if r["result"] == "WIN")
        return (w / n) if n else None

    fw = _winrate(fast_settled)
    sw = _winrate(slow_settled)

    print("─────────────────────────────────────────────────────────────")
    print("  STALE-FILL PROXY   (fast = <10s rest, slow = ≥10s rest)")
    print("─────────────────────────────────────────────────────────────")
    print(f"  Fast-filled orders : n={len(fast_settled):<4}  "
          f"win%={pct(sum(1 for r in fast_settled if r['result']=='WIN'), len(fast_settled))}  "
          f"pnl=${sum(r.get('pnl') or 0 for r in fast_settled):+.2f}")
    print(f"  Slow-filled orders : n={len(slow_settled):<4}  "
          f"win%={pct(sum(1 for r in slow_settled if r['result']=='WIN'), len(slow_settled))}  "
          f"pnl=${sum(r.get('pnl') or 0 for r in slow_settled):+.2f}")

    if fw is not None and sw is not None:
        delta = (sw - fw) * 100
        verdict = (
            "Slow fills look meaningfully worse → cancel logic likely has edge."
            if delta <= -5 else
            "Slow fills look meaningfully better → adverse selection not visible here."
            if delta >= +5 else
            "Roughly flat → stale-fill problem may be hypothetical at current volume."
        )
        print()
        print(f"  Δ win rate (slow − fast) : {delta:+.1f} pp")
        print(f"  Read: {verdict}")
    else:
        print()
        print("  Not enough data in one of the groups to compare.")

    print()


def write_csv(rows, path):
    if not rows:
        print(f"  [csv] Nothing to write.")
        return
    fields = [
        "order_id", "ticker", "side", "status",
        "placed_ts", "fill_ts", "latency_s", "bucket",
        "filled", "price_$", "count", "$_size",
        "result", "pnl",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})
    print(f"  [csv] Wrote {len(rows)} rows to {path}")


# ───────────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Analyze KXBTC15M limit-order fill latency.")
    ap.add_argument("--csv", metavar="PATH", help="Write raw per-order rows to CSV.")
    ap.add_argument("--days", type=int, default=None,
                    help="Only consider orders/fills from the last N days.")
    args = ap.parse_args()

    key_id = os.getenv("KALSHI_API_KEY_ID")
    if not key_id:
        print("ERROR: KALSHI_API_KEY_ID not set (check .env).", file=sys.stderr)
        sys.exit(1)

    try:
        pkey = load_private_key()
    except Exception as e:
        print(f"ERROR loading private key: {e}", file=sys.stderr)
        sys.exit(1)

    since_epoch = None
    if args.days is not None:
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)
        since_epoch = cutoff.timestamp()
        print(f"  [filter] Only including records after {cutoff.isoformat()}")

    print("  [fetch] Pulling orders...")
    orders = paginate("/portfolio/orders", key_id, pkey, "orders")
    print(f"          got {len(orders)} orders")

    print("  [fetch] Pulling fills...")
    fills = paginate("/portfolio/fills", key_id, pkey, "fills")
    print(f"          got {len(fills)} fills")

    print("  [fetch] Pulling settlements...")
    settlements = paginate("/portfolio/settlements", key_id, pkey, "settlements")
    print(f"          got {len(settlements)} settlements")

    rows = analyze(orders, fills, settlements, since_epoch=since_epoch)
    print_report(rows)

    if args.csv:
        write_csv(rows, args.csv)


if __name__ == "__main__":
    main()
