#!/usr/bin/env python3
"""
analyze_book_data.py — Offline analyzer for the book_collector.py output.

Answers three questions about KXBTC15M that decide whether a stale-quote
scalping strategy is viable:

  Q1. What does the bid/ask spread look like across the 15-minute lifecycle,
      sliced by minute-in-cycle, for ATM / near-ATM strikes? (Can we scalp
      at all, or does the spread eat any edge?)

  Q2. What is the distribution of "stale-quote residence time" — the number
      of seconds between a BTC truth-feed tick crossing the mid-implied
      breakeven of a strike and that strike's book actually repricing?
      (This is the takeable edge.)

  Q3. Under a configurable round-trip latency L_rt (Render → Kalshi), what
      fraction of 15-min cycles would have had ≥1 taking opportunity worth
      ≥MIN_EDGE cents after fees? (The GO / NO-GO answer.)

Run locally after you've downloaded book_data.db from the Render worker:

    python analyze_book_data.py --db book_data.db \\
        --latency-ms 400 --min-edge-cents 3 --fee-cents 1

Produces:
  * A printed summary on stdout with the verdict.
  * scalp_report.csv — per-cycle feature table.
  * spread_by_minute.csv — mean/median/p90 spread by minute-in-cycle.
  * stale_residence.csv — histogram of stale-quote residence time in seconds.

This script is read-only. Safe to run against a live collector's db (uses
WAL reader).
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass

# Matches a ticker's strike suffix: '-T85000' or '-B85000-85250'
_STRIKE_SUFFIX_RE = re.compile(r"-([TB])\d+(?:-\d+)?$")


def event_ticker_of(ticker: str) -> str:
    """Return the event ticker (everything before the strike suffix)."""
    return _STRIKE_SUFFIX_RE.sub("", ticker)


# ---- config ----
DEFAULTS = {
    "latency_ms": 400,      # assumed Render → Kalshi round trip
    "min_edge_cents": 3,    # takeable edge must clear this after fees
    "fee_cents": 1,         # round-trip fee assumption per contract
    "btc_move_threshold": 25.0,  # $ of BTC move required to count as a
                                  # "truth tick" that should move the book
    "stale_window_s_min": 1.0,    # stale windows below this don't count
    "strike_distance_from_atm": 2,  # analyze strikes within ±N of ATM only
    "min_fills_per_bucket": 30,
}


@dataclass
class Row:
    ts_ms: int
    ticker: str
    strike: float
    minutes_to_close: float
    yes_bid: int | None
    yes_ask: int | None
    yes_bid_size: int | None
    yes_ask_size: int | None
    no_bid: int | None
    no_ask: int | None
    btc: float

    @property
    def yes_mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def yes_spread(self) -> int | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid


# ---- data access ----
def load_rows(db_path: str) -> list[Row]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cur = con.execute(
        """SELECT ts_ms, ticker, strike, minutes_to_close,
                  yes_bid, yes_ask, yes_bid_size, yes_ask_size,
                  no_bid, no_ask, btc_price
             FROM book_snapshots
             WHERE btc_price IS NOT NULL
             ORDER BY ts_ms ASC"""
    )
    rows = [Row(*r) for r in cur.fetchall() if r[3] is not None]
    con.close()
    return rows


def group_by_ticker(rows: list[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        out[r.ticker].append(r)
    for k in out:
        out[k].sort(key=lambda x: x.ts_ms)
    return out


# ---- Q1: spread by minute-in-cycle ----
def spread_by_minute(rows: list[Row], atm_band: int) -> dict[int, dict]:
    """Bucket by floor(minutes_to_close). Contracts run 15→0 minutes to
    close, so bucket 0 = final minute, bucket 14 = first minute."""
    # Filter to near-ATM only: within ±atm_band strikes of the closest-to-BTC
    # strike per-snapshot. Because we don't know the full strike ladder here,
    # we use |strike - btc| / median_strike_step as a proxy.
    by_ts: dict[int, list[Row]] = defaultdict(list)
    for r in rows:
        by_ts[r.ts_ms].append(r)
    step = _median_strike_step(rows)
    buckets: dict[int, list[int]] = defaultdict(list)
    for ts, group in by_ts.items():
        if not group:
            continue
        btc = group[0].btc
        group.sort(key=lambda x: abs(x.strike - btc))
        for r in group[: atm_band * 2 + 1]:
            if r.yes_spread is not None:
                m = int(math.floor(r.minutes_to_close))
                m = max(0, min(14, m))
                buckets[m].append(r.yes_spread)
    out: dict[int, dict] = {}
    for m in range(15):
        vals = buckets.get(m, [])
        if not vals:
            out[m] = {"n": 0}
            continue
        vals_sorted = sorted(vals)
        out[m] = {
            "n": len(vals),
            "mean": statistics.mean(vals),
            "median": statistics.median(vals),
            "p90": vals_sorted[int(len(vals_sorted) * 0.9)],
            "p99": vals_sorted[int(len(vals_sorted) * 0.99)] if len(vals_sorted) >= 100 else None,
        }
    return out


def _median_strike_step(rows: list[Row]) -> float:
    strikes = sorted({r.strike for r in rows})
    if len(strikes) < 2:
        return 250.0  # KXBTC15M default
    diffs = [strikes[i + 1] - strikes[i] for i in range(len(strikes) - 1)]
    return statistics.median(diffs) if diffs else 250.0


# ---- Q2: stale-quote residence time ----
def detect_stale_windows(
    ticker_rows: list[Row],
    btc_move_threshold: float,
    stale_window_s_min: float,
) -> list[dict]:
    """For a single ticker's time-ordered rows, detect windows where BTC moved
    far enough that this strike's yes_mid 'should' have moved, but didn't for
    some number of seconds.

    Heuristic: whenever BTC moves ≥ btc_move_threshold between two adjacent
    snapshots, flag the moment as a 'truth tick'. Then measure how many seconds
    elapse before yes_mid changes by any amount. That elapsed time is the
    stale-residence window. The window's 'takeable edge' is the yes_mid
    *change* once it eventually happens — that's how much you could have
    captured by taking before the move.

    This is a lower bound on the true edge (real scalpers react to much
    smaller BTC moves than $25), but it's robust to our 3-second sampling.
    """
    windows: list[dict] = []
    n = len(ticker_rows)
    if n < 2:
        return windows
    for i in range(1, n):
        prev, cur = ticker_rows[i - 1], ticker_rows[i]
        if prev.btc is None or cur.btc is None:
            continue
        btc_delta = cur.btc - prev.btc
        if abs(btc_delta) < btc_move_threshold:
            continue
        if cur.yes_mid is None:
            continue
        pre_mid = cur.yes_mid
        t0 = cur.ts_ms
        # Walk forward until mid changes
        stale_until: int | None = None
        post_mid: float | None = None
        for j in range(i + 1, n):
            m = ticker_rows[j].yes_mid
            if m is None:
                continue
            if abs(m - pre_mid) >= 0.5:  # half a cent = smallest real move
                stale_until = ticker_rows[j].ts_ms
                post_mid = m
                break
        if stale_until is None:
            continue
        duration_s = (stale_until - t0) / 1000.0
        if duration_s < stale_window_s_min:
            continue
        edge_cents = abs((post_mid or pre_mid) - pre_mid)
        windows.append({
            "ticker": cur.ticker,
            "t0_ms": t0,
            "duration_s": duration_s,
            "btc_delta": btc_delta,
            "edge_cents": edge_cents,
            "minutes_to_close": cur.minutes_to_close,
        })
    return windows


# ---- Q3: GO/NO-GO verdict ----
def compute_verdict(
    all_windows: list[dict],
    rows: list[Row],
    latency_ms: int,
    min_edge_cents: float,
    fee_cents: float,
    min_fills_per_bucket: int,
) -> dict:
    latency_s = latency_ms / 1000.0

    # Takeable = stale window is longer than our round-trip latency AND
    # edge after fees clears the threshold.
    takeable = [
        w for w in all_windows
        if w["duration_s"] > latency_s
        and (w["edge_cents"] - fee_cents) >= min_edge_cents
    ]

    # Cycle coverage: how many distinct 15-min cycles are in the dataset?
    observed_cycles = {event_ticker_of(r.ticker) for r in rows}
    cycles_with_takeable = {event_ticker_of(w["ticker"]) for w in takeable}

    frac_cycles_takeable = (
        len(cycles_with_takeable) / len(observed_cycles)
        if observed_cycles else 0.0
    )

    median_edge = (
        statistics.median(w["edge_cents"] - fee_cents for w in takeable)
        if takeable else 0.0
    )
    median_duration = (
        statistics.median(w["duration_s"] for w in takeable)
        if takeable else 0.0
    )

    # Decision rule:
    if len(takeable) < min_fills_per_bucket:
        verdict = "RERUN_WITH_MORE_DATA"
        reason = (f"only {len(takeable)} takeable windows; need ≥{min_fills_per_bucket} "
                  f"to decide confidently")
    elif frac_cycles_takeable >= 0.30 and median_edge >= min_edge_cents:
        verdict = "GO"
        reason = (f"{frac_cycles_takeable:.0%} of cycles had a takeable window, "
                  f"median net edge {median_edge:.1f}¢")
    elif frac_cycles_takeable >= 0.15 and median_edge >= min_edge_cents:
        verdict = "MARGINAL"
        reason = (f"only {frac_cycles_takeable:.0%} of cycles had a takeable window; "
                  f"edge exists but turnover may be too low for capital efficiency")
    else:
        verdict = "NO_GO"
        reason = (f"only {frac_cycles_takeable:.0%} of cycles had a takeable window "
                  f"with ≥{min_edge_cents}¢ net edge; scalp economics don't clear")

    return {
        "verdict": verdict,
        "reason": reason,
        "total_windows": len(all_windows),
        "takeable_windows": len(takeable),
        "cycles_observed": len(observed_cycles),
        "cycles_with_takeable": len(cycles_with_takeable),
        "frac_cycles_takeable": frac_cycles_takeable,
        "median_net_edge_cents": median_edge,
        "median_stale_duration_s": median_duration,
    }


# ---- output ----
def write_spread_csv(path: str, spread: dict[int, dict]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["minute_to_close", "n", "mean", "median", "p90", "p99"])
        for m in range(14, -1, -1):
            b = spread.get(m, {})
            w.writerow([m, b.get("n", 0), b.get("mean"), b.get("median"),
                        b.get("p90"), b.get("p99")])


def write_residence_csv(path: str, windows: list[dict]) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "t0_ms", "duration_s", "btc_delta",
                    "edge_cents", "minutes_to_close"])
        for r in windows:
            w.writerow([r["ticker"], r["t0_ms"], r["duration_s"],
                        r["btc_delta"], r["edge_cents"], r["minutes_to_close"]])


def write_report_csv(path: str, report: dict) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "value"])
        for k, v in report.items():
            w.writerow([k, v])


# ---- main ----
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="book_data.db")
    p.add_argument("--latency-ms", type=int, default=DEFAULTS["latency_ms"])
    p.add_argument("--min-edge-cents", type=float, default=DEFAULTS["min_edge_cents"])
    p.add_argument("--fee-cents", type=float, default=DEFAULTS["fee_cents"])
    p.add_argument("--btc-move", type=float, default=DEFAULTS["btc_move_threshold"])
    p.add_argument("--stale-min-s", type=float, default=DEFAULTS["stale_window_s_min"])
    p.add_argument("--atm-band", type=int, default=DEFAULTS["strike_distance_from_atm"])
    p.add_argument("--min-fills", type=int, default=DEFAULTS["min_fills_per_bucket"])
    args = p.parse_args()

    rows = load_rows(args.db)
    if not rows:
        print("No rows in database.", file=sys.stderr)
        return 1

    print(f"Loaded {len(rows):,} snapshots from {args.db}")
    print(f"Time range: {rows[0].ts_ms} → {rows[-1].ts_ms} "
          f"({(rows[-1].ts_ms - rows[0].ts_ms)/3600000:.1f}h)")
    print()

    # Q1
    spread = spread_by_minute(rows, args.atm_band)
    print("=== Spread (cents) by minute-to-close, ATM band ===")
    print(f"{'min':>4} {'n':>8} {'mean':>8} {'median':>8} {'p90':>6}")
    for m in range(14, -1, -1):
        b = spread[m]
        if b["n"] == 0:
            continue
        print(f"{m:>4} {b['n']:>8} {b['mean']:>8.2f} {b['median']:>8.1f} {b['p90']:>6}")
    write_spread_csv("spread_by_minute.csv", spread)
    print()

    # Q2
    by_ticker = group_by_ticker(rows)
    all_windows: list[dict] = []
    for t, tr in by_ticker.items():
        all_windows.extend(
            detect_stale_windows(tr, args.btc_move, args.stale_min_s)
        )
    write_residence_csv("stale_residence.csv", all_windows)
    print(f"=== Stale-quote residence windows ({len(all_windows)} detected) ===")
    if all_windows:
        durs = sorted(w["duration_s"] for w in all_windows)
        edges = sorted(w["edge_cents"] for w in all_windows)
        print(f"duration  median={statistics.median(durs):.1f}s  "
              f"p90={durs[int(len(durs)*0.9)]:.1f}s  "
              f"max={max(durs):.1f}s")
        print(f"edge      median={statistics.median(edges):.1f}¢  "
              f"p90={edges[int(len(edges)*0.9)]:.1f}¢  "
              f"max={max(edges):.1f}¢")
    print()

    # Q3
    report = compute_verdict(
        all_windows, rows, args.latency_ms, args.min_edge_cents,
        args.fee_cents, args.min_fills,
    )
    print("=== Verdict ===")
    for k, v in report.items():
        print(f"  {k}: {v}")
    write_report_csv("scalp_report.csv", report)
    print()
    print(f"[{report['verdict']}] {report['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
