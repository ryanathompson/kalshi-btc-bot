#!/usr/bin/env python3
"""
reconcile_pnl.py — Compute TRUE P&L from Kalshi API, bypassing bot_trades.json.

This script is the authoritative P&L calculator. It talks directly to
Kalshi's /portfolio/fills and /portfolio/settlements endpoints and
computes P&L from first principles, ignoring bot_trades.json entirely.

It then loads bot_trades.json and shows exactly where and why the
ledger diverges from reality.

Usage:
    python reconcile_pnl.py              # human report
    python reconcile_pnl.py --json       # machine-readable
    python reconcile_pnl.py --fix        # rewrite bot_trades.json from API truth

READ-ONLY by default. --fix is the only flag that writes.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Any

try:
    from bot import (
        KalshiClient, load_private_key, load_trades,
        LOG_FILE, BTC_TICKER, parse_trade_ts,
    )
except Exception as e:
    print(f"FATAL: could not import bot.py: {e}", file=sys.stderr)
    sys.exit(1)


# ---- Kalshi API fetchers ----

def _paginate(client: KalshiClient, path: str, key: str,
              extra_params: dict | None = None) -> list[dict]:
    out: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": 200}
        if extra_params:
            params.update(extra_params)
        if cursor:
            params["cursor"] = cursor
        resp = client.get(path, params=params)
        page = resp.get(key, []) or []
        out.extend(page)
        cursor = resp.get("cursor")
        if not cursor or not page:
            break
    return out


def fetch_all_fills(client: KalshiClient) -> list[dict]:
    fills = _paginate(client, "/portfolio/fills", "fills")
    btc = [f for f in fills if BTC_TICKER in (f.get("ticker") or "")]
    print(f"[reconcile] {len(btc)} BTC fills out of {len(fills)} total",
          file=sys.stderr)
    return btc


def fetch_all_settlements(client: KalshiClient) -> dict[str, dict]:
    setts = _paginate(client, "/portfolio/settlements", "settlements")
    by_ticker: dict[str, dict] = {}
    for s in setts:
        t = s.get("ticker") or s.get("market_ticker") or ""
        if BTC_TICKER in t:
            by_ticker[t] = s
    print(f"[reconcile] {len(by_ticker)} BTC settlements out of "
          f"{len(setts)} total", file=sys.stderr)
    return by_ticker


def fetch_all_orders(client: KalshiClient) -> dict[str, dict]:
    orders = _paginate(client, "/portfolio/orders", "orders")
    by_oid: dict[str, dict] = {}
    for o in orders:
        oid = o.get("order_id")
        if oid:
            by_oid[oid] = o
    print(f"[reconcile] {len(by_oid)} orders fetched", file=sys.stderr)
    return by_oid


# ---- Ground-truth P&L from fills + settlements ----

PREFIX_TO_STRATEGY = {"LAG": "LAG", "CON": "CONSENSUS", "TAI": "TAIL"}


def compute_true_pnl(fills: list[dict], settlements: dict[str, dict],
                      orders: dict[str, dict]) -> dict:
    """
    Compute P&L purely from Kalshi API data.

    Groups fills by order_id, aggregates partial fills, joins against
    settlements for outcome, and computes real P&L from actual fill
    prices (not limit prices).

    Returns {
        "positions": [ {ticker, side, count, cost, payout, pnl,
                        result, strategy, order_id, fill_ts, ...}, ... ],
        "summary": { total_cost, total_payout, total_pnl,
                      wins, losses, unsettled, trades },
    }
    """
    # Group fills by order_id
    by_order: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        oid = f.get("order_id") or f.get("fill_id") or "unknown"
        by_order[oid].append(f)

    positions: list[dict] = []

    for order_id, group in by_order.items():
        group.sort(key=lambda f: f.get("created_time", ""))
        f0 = group[0]
        ticker = f0.get("ticker", "")
        side = f0.get("side", "")

        # Aggregate across partial fills using ACTUAL fill prices
        total_count = 0.0
        total_cost = 0.0
        for f in group:
            c = float(f.get("count_fp", f.get("count", 0)))
            total_count += c
            # Use the actual fill price (what we really paid), not the
            # limit price. This is the key difference vs bot_trades.json
            # which uses the intended limit price.
            if side == "yes":
                px = float(f.get("yes_price_dollars", 0))
            else:
                px = float(f.get("no_price_dollars", 0))
            total_cost += c * px

        total_cost = round(total_cost, 4)

        # Settlement
        sett = settlements.get(ticker)
        outcome = None
        result = None
        payout = 0.0
        pnl = None

        if sett:
            outcome = sett.get("market_result")
            # Revenue from settlement endpoint is ground truth for payout
            revenue = float(sett.get("revenue", 0) or 0)
            # But revenue is per-contract in some responses; check if
            # settlement gives us a yes/no settlement price instead.
            # Most reliable: if side matches outcome, payout = count * $1.
            # If side doesn't match, payout = $0.
            if outcome:
                won = (side == outcome)
                result = "WIN" if won else "LOSS"
                payout = round(total_count, 2) if won else 0.0
                pnl = round(payout - total_cost, 2)

        # Strategy recovery from client_order_id
        order = orders.get(order_id, {})
        coid = order.get("client_order_id", "")
        prefix = coid.split("-", 1)[0].upper() if "-" in coid else ""
        strategy = PREFIX_TO_STRATEGY.get(prefix, "UNKNOWN")

        positions.append({
            "order_id":   order_id,
            "ticker":     ticker,
            "side":       side,
            "strategy":   strategy,
            "count":      round(total_count, 4),
            "cost":       total_cost,
            "payout":     payout,
            "pnl":        pnl,
            "result":     result,
            "outcome":    outcome,
            "fill_ts":    f0.get("created_time"),
            "order_status": order.get("status"),
        })

    # Summary
    settled = [p for p in positions if p["result"]]
    total_cost = sum(p["cost"] for p in settled)
    total_payout = sum(p["payout"] for p in settled)
    total_pnl = sum(p["pnl"] for p in settled if p["pnl"] is not None)
    wins = sum(1 for p in settled if p["result"] == "WIN")
    losses = sum(1 for p in settled if p["result"] == "LOSS")
    unsettled = len([p for p in positions if not p["result"]])

    return {
        "positions": positions,
        "summary": {
            "total_cost":   round(total_cost, 2),
            "total_payout": round(total_payout, 2),
            "total_pnl":    round(total_pnl, 2),
            "wins":         wins,
            "losses":       losses,
            "unsettled":    unsettled,
            "trades":       len(positions),
        },
    }


# ---- Duplicate detection in bot_trades.json ----

def find_duplicates(trades: list[dict]) -> list[tuple[int, int, dict, dict]]:
    """Find pairs of trades in bot_trades.json that cover the same fill.

    A duplicate is two records with the same ticker and overlapping
    placement/fill time (within 15 minutes — the contract lifetime).
    Returns list of (idx_a, idx_b, trade_a, trade_b).
    """
    # Group by ticker
    by_ticker: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for i, t in enumerate(trades):
        by_ticker[t.get("ticker", "")].append((i, t))

    dupes: list[tuple[int, int, dict, dict]] = []
    for ticker, group in by_ticker.items():
        if len(group) < 2:
            continue
        # For each pair, check if they're the same fill
        for a_idx in range(len(group)):
            for b_idx in range(a_idx + 1, len(group)):
                i_a, t_a = group[a_idx]
                i_b, t_b = group[b_idx]
                # Same ticker + same side = almost certainly a duplicate
                # (bot only trades one direction per market per cycle)
                if t_a.get("side") == t_b.get("side"):
                    dupes.append((i_a, i_b, t_a, t_b))
    return dupes


# ---- Report ----

def print_report(truth: dict, trades: list[dict],
                 dupes: list[tuple[int, int, dict, dict]]) -> None:
    s = truth["summary"]

    print()
    print("=" * 72)
    print("P&L RECONCILIATION — Kalshi API vs bot_trades.json")
    print("=" * 72)

    # Ground truth from API
    print()
    print("GROUND TRUTH (from /portfolio/fills + /portfolio/settlements)")
    print(f"  Settled trades:    {s['wins'] + s['losses']}")
    print(f"  Wins / Losses:     {s['wins']} / {s['losses']}")
    print(f"  Unsettled:         {s['unsettled']}")
    print(f"  Total cost:        ${s['total_cost']:,.2f}")
    print(f"  Total payout:      ${s['total_payout']:,.2f}")
    print(f"  TRUE P&L:          ${s['total_pnl']:+,.2f}")

    # Ledger from bot_trades.json
    settled_log = [t for t in trades if t.get("result")]
    ledger_pnl = sum(float(t.get("pnl") or 0) for t in settled_log)
    print()
    print(f"BOT LEDGER (bot_trades.json)")
    print(f"  Total records:     {len(trades)}")
    print(f"  Settled records:   {len(settled_log)}")
    print(f"  Ledger P&L:        ${ledger_pnl:+,.2f}")

    # Delta
    delta = ledger_pnl - s["total_pnl"]
    print()
    print(f"DISCREPANCY")
    print(f"  Ledger - Truth:    ${delta:+,.2f}")
    if abs(delta) > 1:
        print(f"  ⚠  The bot is {'over' if delta < 0 else 'under'}-reporting "
              f"losses by ${abs(delta):,.2f}")

    # Duplicates
    print()
    print(f"DUPLICATE DETECTION in bot_trades.json")
    print(f"  Duplicate pairs found: {len(dupes)}")
    if dupes:
        dupe_pnl = 0.0
        for i_a, i_b, t_a, t_b in dupes[:20]:
            # The duplicate's P&L is spurious
            pnl_a = float(t_a.get("pnl") or 0)
            pnl_b = float(t_b.get("pnl") or 0)
            strat_a = (t_a.get("strategy") or "?")[:4]
            strat_b = (t_b.get("strategy") or "?")[:4]
            ticker = t_a.get("ticker", "?")
            ts_a = (t_a.get("timestamp") or "")[:19]
            ts_b = (t_b.get("timestamp") or "")[:19]
            print(f"  [{i_a}] {strat_a} {ts_a} pnl={pnl_a:+.2f}  "
                  f"⇄  [{i_b}] {strat_b} {ts_b} pnl={pnl_b:+.2f}  "
                  f"{ticker}")
            dupe_pnl += pnl_b  # second record is the duplicate
        if len(dupes) > 20:
            print(f"  ... and {len(dupes) - 20} more")
        print(f"  Estimated P&L from duplicates: ${dupe_pnl:+,.2f}")

    # Per-strategy ground truth
    print()
    print("PER-STRATEGY GROUND TRUTH")
    by_strat: dict[str, list[dict]] = defaultdict(list)
    for p in truth["positions"]:
        by_strat[p["strategy"]].append(p)
    for strat, group in sorted(by_strat.items()):
        settled = [p for p in group if p["result"]]
        w = sum(1 for p in settled if p["result"] == "WIN")
        l = sum(1 for p in settled if p["result"] == "LOSS")
        pnl = sum(p["pnl"] for p in settled if p["pnl"] is not None)
        cost = sum(p["cost"] for p in settled)
        wr = w / (w + l) * 100 if (w + l) else 0
        roi = pnl / cost * 100 if cost else 0
        print(f"  {strat:<12} W={w} L={l} WR={wr:.0f}%  "
              f"cost=${cost:,.2f}  pnl=${pnl:+,.2f}  ROI={roi:+.1f}%")
    print()


def rebuild_from_api(truth: dict, client: KalshiClient) -> list[dict]:
    """Build a clean bot_trades.json from API truth. This is the --fix path."""
    orders = fetch_all_orders(client)
    new_trades: list[dict] = []
    for p in truth["positions"]:
        oid = p["order_id"]
        order = orders.get(oid, {})
        coid = order.get("client_order_id", "")

        trade = {
            "strategy":       p["strategy"],
            "ticker":         p["ticker"],
            "side":           p["side"],
            "price":          int(round(p["cost"] / p["count"] * 100)) if p["count"] else 0,
            "count":          int(p["count"]) if p["count"] == int(p["count"]) else p["count"],
            "dollars":        round(p["cost"], 2),
            "reason":         f"Reconciled from Kalshi API (order {oid[:8]}...)",
            "timestamp":      p["fill_ts"] or "",
            "dry_run":        False,
            "order":          {"order_id": oid, "reconciled": True},
            "order_id":       oid,
            "fill_timestamp": p["fill_ts"],
            "outcome":        p["outcome"],
            "result":         p["result"],
            "pnl":            p["pnl"],
        }
        new_trades.append(trade)
    return new_trades


# ---- Main ----

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Reconcile bot P&L against Kalshi API ground truth.",
    )
    ap.add_argument("--json", action="store_true",
                    help="JSON output instead of human report.")
    ap.add_argument("--fix", action="store_true",
                    help="Rewrite bot_trades.json from API truth. DESTRUCTIVE.")
    args = ap.parse_args()

    # Build client
    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    if not api_key_id:
        print("FATAL: KALSHI_API_KEY_ID not set", file=sys.stderr)
        return 1
    pkey = load_private_key()
    client = KalshiClient(api_key_id, pkey, dry_run=True)

    # Fetch ground truth
    fills = fetch_all_fills(client)
    settlements = fetch_all_settlements(client)
    orders = fetch_all_orders(client)

    truth = compute_true_pnl(fills, settlements, orders)

    # Load bot ledger
    trades = load_trades()
    live_trades = [t for t in trades
                   if not t.get("dry_run") and BTC_TICKER in (t.get("ticker") or "")]

    # Find duplicates
    dupes = find_duplicates(live_trades)

    if args.json:
        out = {
            "ground_truth": truth["summary"],
            "ledger_pnl": round(
                sum(float(t.get("pnl") or 0)
                    for t in live_trades if t.get("result")), 2),
            "duplicate_count": len(dupes),
            "positions": truth["positions"],
        }
        json.dump(out, sys.stdout, indent=2, default=str)
        print()
    else:
        print_report(truth, live_trades, dupes)

    if args.fix:
        print("=" * 72, file=sys.stderr)
        print("REBUILDING bot_trades.json from API truth...", file=sys.stderr)
        new_trades = rebuild_from_api(truth, client)
        # Preserve dry-run trades (they're not in the API)
        dry_runs = [t for t in trades if t.get("dry_run")]
        combined = dry_runs + new_trades
        backup = LOG_FILE + ".bak"
        if os.path.exists(LOG_FILE):
            import shutil
            shutil.copy2(LOG_FILE, backup)
            print(f"  Backed up {LOG_FILE} → {backup}", file=sys.stderr)
        with open(LOG_FILE, "w") as f:
            json.dump(combined, f, indent=2)
        print(f"  Wrote {len(combined)} trades ({len(dry_runs)} dry + "
              f"{len(new_trades)} live) to {LOG_FILE}", file=sys.stderr)
        print("  Done. Dashboard will reflect true P&L on next refresh.",
              file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
