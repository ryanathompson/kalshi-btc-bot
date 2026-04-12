#!/usr/bin/env python3
"""
audit_fills.py — READ-ONLY ghost-trade diagnostic.

Context
-------
The live trade-logging path in bot.py records a trade as soon as Kalshi
returns HTTP 201 on POST /portfolio/orders. 201 means the order was
*accepted into the book*, not that it was filled. Our orders are plain
GTC limit orders (no time_in_force, no expiration_ts), so they can sit
resting until the contract closes. `resolve_trades()` then marks those
unfilled orders WIN/LOSS based purely on market settlement, charging
synthetic P&L against exposure we never actually had.

This script does not fix the bug. It only measures it.

What it does
------------
1. Loads bot_trades.json (or $TRADES_LOG_PATH if set) — same path the
   bot writes to — and filters to live (non-dry-run) trades.
2. Fetches all of /portfolio/fills (paginated) and groups by order_id.
3. Optionally also fetches /portfolio/orders so it can show the order's
   terminal status (resting / executed / canceled) — pass --with-orders.
4. For every live trade, compares recorded count/dollars against what
   actually filled, and classifies each trade:
       OK           — fully filled, sizes match
       PARTIAL      — filled count < recorded count
       GHOST        — zero fills (never executed, never had exposure)
       UNKNOWN      — live trade with no recorded order_id (legacy rows
                      from before order_id was captured; cannot be
                      joined against /portfolio/fills, so we can't
                      classify them)
5. Prints a summary and, for each non-OK category, a one-line row per
   trade. Also recomputes the P&L delta: what the ledger currently
   claims vs. what we would get if ghosts were zeroed and partials were
   scaled down to their real fill fraction.

The script never writes to bot_trades.json, Kalshi, or anywhere else.
It is safe to run on the live Render instance or locally.

Run
---
    # Local (with kalshi.key file present)
    python audit_fills.py

    # With order status resolution (+1 API page per 200 orders)
    python audit_fills.py --with-orders

    # JSON output (for downstream piping)
    python audit_fills.py --json > audit_report.json

    # Point at a different trades file
    TRADES_LOG_PATH=/path/to/bot_trades.json python audit_fills.py

Exit codes
----------
    0 — ran successfully (regardless of findings)
    1 — could not fetch fills or load trades

Output caveats
--------------
- Fill totals are aggregated in fractional contracts (count_fp) where
  present to handle partial-contract fills, then compared against the
  recorded integer count.
- "P&L delta" is an *approximation* of what would happen if ghosts were
  zeroed and partials scaled linearly. The real fix may want to use
  the average fill price, not the limit price, which would shift
  numbers further. Treat this output as a LOWER bound on the
  misreporting, not the exact correction.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from collections import defaultdict
from typing import Any

# Import the existing bot internals directly — same auth, same base URL,
# same key-loading path. No fallback: this script is meant to run inside
# the same repo as bot.py.
try:
    from bot import (
        KalshiClient, load_private_key, load_trades,
        LOG_FILE, BTC_TICKER,
    )
except Exception as e:
    print(f"FATAL: could not import bot.py internals: {e}", file=sys.stderr)
    print(
        "This script must live next to bot.py so it can reuse KalshiClient "
        "and load_trades(). Run it from the repo root.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---- Kalshi fetchers ----
def fetch_all_fills(client: KalshiClient) -> list[dict]:
    """Paginate all of /portfolio/fills. Returns raw fill records."""
    out: list[dict] = []
    cursor: str | None = None
    page = 0
    while True:
        params: dict[str, Any] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/portfolio/fills", params=params)
        fills = resp.get("fills", []) or []
        out.extend(fills)
        page += 1
        cursor = resp.get("cursor")
        if not cursor or not fills:
            break
    print(f"[audit] fetched {len(out)} fills across {page} page(s)",
          file=sys.stderr)
    return out


def fetch_all_orders(client: KalshiClient) -> dict[str, dict]:
    """Paginate /portfolio/orders. Returns {order_id: order_dict}."""
    out: dict[str, dict] = {}
    cursor: str | None = None
    page = 0
    while True:
        params: dict[str, Any] = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/portfolio/orders", params=params)
        orders = resp.get("orders", []) or []
        for o in orders:
            oid = o.get("order_id")
            if oid:
                out[oid] = o
        page += 1
        cursor = resp.get("cursor")
        if not cursor or not orders:
            break
    print(f"[audit] fetched {len(out)} orders across {page} page(s)",
          file=sys.stderr)
    return out


# ---- Aggregation ----
def _fill_count(fill: dict) -> float:
    """Extract fill size as a float. Prefer count_fp (fractional) when
    available, else count, else 0."""
    for k in ("count_fp", "count"):
        if k in fill and fill[k] is not None:
            try:
                return float(fill[k])
            except Exception:
                pass
    return 0.0


def _fill_price_dollars(fill: dict, side: str) -> float | None:
    """Extract the side-appropriate fill price in dollars, if recorded."""
    if side == "yes":
        p = fill.get("yes_price_dollars")
    else:
        p = fill.get("no_price_dollars")
    if p is None:
        return None
    try:
        return float(p)
    except Exception:
        return None


def aggregate_fills_by_order(fills: list[dict]) -> dict[str, dict]:
    """Group fills by order_id and sum fill counts + dollar notional.

    Returns {order_id: {
        "count": total fractional contracts filled,
        "dollars": total $ paid (= Σ count_i * price_dollars_i),
        "fills": raw fill records (sorted by created_time),
        "side": "yes" or "no" (from first fill),
        "ticker": ticker (from first fill),
    }}
    """
    by_oid: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        oid = f.get("order_id")
        if oid:
            by_oid[oid].append(f)

    out: dict[str, dict] = {}
    for oid, group in by_oid.items():
        group.sort(key=lambda f: f.get("created_time", ""))
        side = group[0].get("side", "")
        ticker = group[0].get("ticker", "")
        total_count = 0.0
        total_dollars = 0.0
        for f in group:
            c = _fill_count(f)
            px = _fill_price_dollars(f, side)
            total_count += c
            if px is not None:
                total_dollars += c * px
        out[oid] = {
            "count":   total_count,
            "dollars": round(total_dollars, 4),
            "fills":   group,
            "side":    side,
            "ticker":  ticker,
        }
    return out


# ---- Classification ----
def classify_trade(trade: dict, fills_by_oid: dict[str, dict]) -> dict:
    """Classify a single trade record against the fill index.

    Returns an audit row dict with the original trade plus a verdict.
    """
    row = {
        "ticker":           trade.get("ticker"),
        "strategy":         trade.get("strategy"),
        "side":             trade.get("side"),
        "timestamp":        trade.get("timestamp"),
        "order_id":         trade.get("order_id"),
        "recorded_count":   trade.get("count"),
        "recorded_dollars": trade.get("dollars"),
        "recorded_price":   trade.get("price"),  # cents
        "recorded_result":  trade.get("result"),
        "recorded_pnl":     trade.get("pnl"),
        "filled_count":     None,
        "filled_dollars":   None,
        "verdict":          None,
        "correction":       0.0,   # Δ to apply to ledger P&L
    }

    oid = trade.get("order_id")
    if not oid:
        row["verdict"] = "UNKNOWN"
        return row

    agg = fills_by_oid.get(oid)
    if agg is None:
        # Order placed, accepted, never matched. Pure ghost.
        row["filled_count"] = 0.0
        row["filled_dollars"] = 0.0
        row["verdict"] = "GHOST"
        # Ledger currently claims this P&L; correction reverses it.
        if trade.get("pnl") is not None:
            row["correction"] = -float(trade["pnl"])
        return row

    row["filled_count"] = agg["count"]
    row["filled_dollars"] = agg["dollars"]

    rec_count = trade.get("count") or 0
    if agg["count"] <= 0:
        row["verdict"] = "GHOST"
        if trade.get("pnl") is not None:
            row["correction"] = -float(trade["pnl"])
        return row
    if agg["count"] + 1e-9 < rec_count:
        row["verdict"] = "PARTIAL"
        # Scale ledger P&L down linearly by fill ratio. This is an
        # approximation — real fix should recompute pnl from the actual
        # filled dollars at the real fill price — but good enough for
        # spotting which trades are misreported and by roughly how much.
        if rec_count > 0 and trade.get("pnl") is not None:
            ratio = agg["count"] / rec_count
            real_pnl = float(trade["pnl"]) * ratio
            row["correction"] = real_pnl - float(trade["pnl"])
        return row
    row["verdict"] = "OK"
    return row


# ---- Reporting ----
def print_summary(rows: list[dict], with_orders: dict[str, dict] | None) -> None:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        buckets[r["verdict"]].append(r)

    total_live   = len(rows)
    num_ghost    = len(buckets.get("GHOST", []))
    num_partial  = len(buckets.get("PARTIAL", []))
    num_ok       = len(buckets.get("OK", []))
    num_unknown  = len(buckets.get("UNKNOWN", []))

    # Ledger-side totals (what bot_trades.json currently claims)
    ledger_pnl = sum(
        float(r["recorded_pnl"] or 0) for r in rows
    )
    correction = sum(r["correction"] or 0 for r in rows)
    corrected_pnl = ledger_pnl + correction

    print()
    print("=" * 72)
    print("KALSHI BOT — FILL AUDIT")
    print("=" * 72)
    print(f"Live trades examined:            {total_live}")
    print(f"  OK (fully filled):             {num_ok}")
    print(f"  PARTIAL (filled < recorded):   {num_partial}")
    print(f"  GHOST  (never filled):         {num_ghost}")
    print(f"  UNKNOWN (no order_id):         {num_unknown}")
    print()
    print("P&L impact estimate")
    print(f"  Ledger claims:                 ${ledger_pnl:+,.2f}")
    print(f"  Correction (ghosts + partials):${correction:+,.2f}")
    print(f"  Corrected P&L (est):           ${corrected_pnl:+,.2f}")
    print()
    print("NOTE: 'correction' is an approximation. It assumes linear")
    print("scaling of ledger P&L by fill ratio for partials and full")
    print("reversal for ghosts. It does NOT re-price from actual fill")
    print("prices — the real correction could be larger.")
    print()

    def _fmt_row(r: dict) -> str:
        ticker = r["ticker"] or "?"
        strat  = (r["strategy"] or "?")[:4]
        side   = (r["side"] or "?")[:3]
        ts     = (r["timestamp"] or "")[:19]
        rc     = r["recorded_count"]
        fc     = r["filled_count"]
        rp     = r["recorded_pnl"]
        corr   = r["correction"]
        return (
            f"  {ts}  {strat:<4}  {ticker:<36}  {side:<3}  "
            f"rec={rc}  filled={fc}  pnl={rp}  Δ={corr:+.2f}"
        )

    for label in ("GHOST", "PARTIAL", "UNKNOWN"):
        bucket = buckets.get(label, [])
        if not bucket:
            continue
        print(f"--- {label} ({len(bucket)}) ---")
        for r in bucket:
            print(_fmt_row(r))
            if with_orders:
                o = with_orders.get(r.get("order_id") or "")
                if o:
                    print(f"      order.status={o.get('status')} "
                          f"remaining_count={o.get('remaining_count')}")
        print()

    # Per-strategy ghost rate — the most actionable single number.
    by_strat: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_strat[r["strategy"] or "?"].append(r)
    print("Per-strategy fill rates")
    for s, group in sorted(by_strat.items()):
        n = len(group)
        g = sum(1 for r in group if r["verdict"] == "GHOST")
        p = sum(1 for r in group if r["verdict"] == "PARTIAL")
        ok = sum(1 for r in group if r["verdict"] == "OK")
        ghost_pct   = (g / n * 100) if n else 0.0
        partial_pct = (p / n * 100) if n else 0.0
        print(f"  {s:<10} trades={n}  OK={ok}  "
              f"PARTIAL={p} ({partial_pct:.1f}%)  "
              f"GHOST={g} ({ghost_pct:.1f}%)")
    print()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read-only ghost-trade audit against Kalshi fills API.",
    )
    ap.add_argument(
        "--with-orders", action="store_true",
        help="Also fetch /portfolio/orders to show terminal order status.",
    )
    ap.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of a human report (safer for piping).",
    )
    ap.add_argument(
        "--trades-file", default=None,
        help=f"Path to trades JSON (default: {LOG_FILE} or $TRADES_LOG_PATH).",
    )
    args = ap.parse_args()

    # Load trades
    trades_path = args.trades_file or LOG_FILE
    if not os.path.exists(trades_path):
        print(f"FATAL: trades file not found: {trades_path}", file=sys.stderr)
        return 1
    with open(trades_path) as f:
        all_trades = json.load(f)
    live_trades = [
        t for t in all_trades
        if not t.get("dry_run") and BTC_TICKER in (t.get("ticker", "") or "")
    ]
    print(f"[audit] loaded {len(all_trades)} trades total, "
          f"{len(live_trades)} live KXBTC15M trades",
          file=sys.stderr)

    if not live_trades:
        print("No live trades to audit.", file=sys.stderr)
        return 0

    # Build Kalshi client (read-only — we only GET)
    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    if not api_key_id:
        print("FATAL: KALSHI_API_KEY_ID not set", file=sys.stderr)
        return 1
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi.key")
    pkey = load_private_key(key_path)
    client = KalshiClient(api_key_id, pkey, dry_run=True)

    # Fetch + aggregate
    try:
        fills = fetch_all_fills(client)
    except Exception as e:
        print(f"FATAL: fetch_all_fills failed: {e}", file=sys.stderr)
        return 1
    fills_by_oid = aggregate_fills_by_order(fills)

    orders_by_oid: dict[str, dict] | None = None
    if args.with_orders:
        try:
            orders_by_oid = fetch_all_orders(client)
        except Exception as e:
            print(f"WARN: fetch_all_orders failed, continuing without "
                  f"status resolution: {e}", file=sys.stderr)

    # Classify
    rows = [classify_trade(t, fills_by_oid) for t in live_trades]

    if args.json:
        out = {
            "generated_at": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
            "trades_file": trades_path,
            "total_live_trades": len(live_trades),
            "counts": {
                "OK":      sum(1 for r in rows if r["verdict"] == "OK"),
                "PARTIAL": sum(1 for r in rows if r["verdict"] == "PARTIAL"),
                "GHOST":   sum(1 for r in rows if r["verdict"] == "GHOST"),
                "UNKNOWN": sum(1 for r in rows if r["verdict"] == "UNKNOWN"),
            },
            "ledger_pnl": round(
                sum(float(r["recorded_pnl"] or 0) for r in rows), 2),
            "correction": round(
                sum(r["correction"] or 0 for r in rows), 2),
            "rows": rows,
        }
        json.dump(out, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        print_summary(rows, orders_by_oid)

    return 0


if __name__ == "__main__":
    sys.exit(main())
