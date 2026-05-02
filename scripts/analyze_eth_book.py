"""Analyze a recorded ETH book log (kalshi_book + spot) for lag/edge.

Usage:
    python scripts/analyze_eth_book.py logs/eth_book_20260501T1302.jsonl

Outputs (to stdout):
  1. Log shape: spot/book record counts, time span, events covered
  2. Spot dynamics: range, realized hourly vol over the window
  3. Kalshi mid-implied price vs spot lag distribution
  4. Model edge scan: fair-value vs Kalshi mid (per event, per tick)

Self-contained — only relies on stdlib + numpy + project's GBM posterior.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# Project imports
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from strategies.eth.model.posterior import GBMPosterior, bracket_probability  # noqa: E402


def load(path: Path):
    spot = []
    books = []
    bad = 0
    with path.open() as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                bad += 1
                continue
            if rec.get("kind") == "spot":
                spot.append(rec)
            elif rec.get("kind") == "kalshi_book":
                books.append(rec)
    spot.sort(key=lambda r: r["t"])
    books.sort(key=lambda r: r["t"])
    return spot, books, bad


def fmt_ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def event_settle_ts(event_ticker: str) -> float:
    """KXETHD-26MAY0114 -> Unix ts for that hour, ET (UTC-4 in May)."""
    # Format: KXETHD-YYMMMDDHH
    body = event_ticker.split("-", 1)[1]  # 26MAY0114
    yy = int(body[:2]) + 2000
    mon_str = body[2:5]
    dd = int(body[5:7])
    hh = int(body[7:9])
    months = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
              "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
    mon = months[mon_str]
    # ET is UTC-4 in May (EDT). Settlement is at the top of that ET hour.
    et_dt = datetime(yy, mon, dd, hh)
    # convert ET (UTC-4) -> UTC
    return et_dt.timestamp() + 4 * 3600  # naive ET -> UTC


def kalshi_mid_quantile_in_spot(book_markets: list[dict]) -> float | None:
    """The Kalshi book is a discrete CDF of P(P_T > strike). Convert it to
    an implied 'fair price' by finding the strike where YES mid ≈ 0.5.
    Linear interp between bracketing strikes. Returns implied ETH price
    (the book's median estimate of the settlement price)."""
    rows = []
    for m in book_markets:
        if m.get("strike_type") != "greater":
            continue
        strike = m.get("strike")
        bid = m.get("yes_bid", 0.0) or 0.0
        ask = m.get("yes_ask", 0.0) or 0.0
        if ask <= 0 or strike is None:
            continue
        mid = 0.5 * (bid + ask)
        rows.append((strike, mid))
    if not rows:
        return None
    rows.sort()
    # Find strike where mid crosses 0.5 (YES mid decreasing as strike increases)
    for i in range(len(rows) - 1):
        s_lo, p_lo = rows[i]
        s_hi, p_hi = rows[i + 1]
        # We want a strike where P(P_T>strike)=0.5
        if p_lo >= 0.5 >= p_hi:
            if p_lo == p_hi:
                return 0.5 * (s_lo + s_hi)
            # linear interp in (strike, prob) with prob descending in strike
            frac = (p_lo - 0.5) / (p_lo - p_hi)
            return s_lo + frac * (s_hi - s_lo)
    return None


def realized_hourly_sigma(spot: list[dict], window_seconds: float = 3600) -> float:
    """Compute hourly log-return realized vol from the spot stream."""
    pts = [(r["t"], r["price"]) for r in spot]
    if len(pts) < 30:
        return 0.0
    # sample once per minute
    step = 60.0
    t0 = pts[0][0]
    t1 = pts[-1][0]
    times = [t0 + i * step for i in range(int((t1 - t0) // step) + 1)]
    ts_arr = [p[0] for p in pts]
    px_arr = [p[1] for p in pts]
    sampled = []
    for t in times:
        i = bisect.bisect_left(ts_arr, t)
        i = min(max(i, 0), len(px_arr) - 1)
        sampled.append(px_arr[i])
    if len(sampled) < 5:
        return 0.0
    rets = np.diff(np.log(sampled))
    sigma_per_min = float(np.std(rets, ddof=1))
    sigma_hourly = sigma_per_min * math.sqrt(60.0)
    return sigma_hourly


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--min-edge", type=float, default=0.04)
    ap.add_argument("--max-print", type=int, default=15)
    args = ap.parse_args()

    spot, books, bad = load(args.path)
    print(f"=== Log shape ===")
    print(f"  file: {args.path}")
    print(f"  spot records:  {len(spot):,}")
    print(f"  book records:  {len(books):,}")
    print(f"  malformed:     {bad}")
    if not spot or not books:
        print("Nothing to analyze.")
        return
    t_lo = min(spot[0]["t"], books[0]["t"])
    t_hi = max(spot[-1]["t"], books[-1]["t"])
    print(f"  span:          {fmt_ts(t_lo)}  ->  {fmt_ts(t_hi)}")
    dur = (t_hi - t_lo) / 60
    print(f"  duration:      {dur:.1f} min ({dur/60:.2f} h)")

    # Cadence
    spot_dts = np.diff([s["t"] for s in spot])
    book_dts = np.diff([b["t"] for b in books])
    print(f"  spot poll:     median {np.median(spot_dts):.2f}s  p95 {np.percentile(spot_dts,95):.2f}s")
    print(f"  book poll:     median {np.median(book_dts):.2f}s  p95 {np.percentile(book_dts,95):.2f}s")

    events = sorted({b["event_ticker"] for b in books})
    print(f"  events seen:   {len(events)}  ->  {events}")

    # Spot
    px = np.array([s["price"] for s in spot])
    print()
    print(f"=== Spot dynamics ===")
    print(f"  range:         ${px.min():.2f}  ->  ${px.max():.2f}  (span ${px.max()-px.min():.2f})")
    print(f"  first / last:  ${px[0]:.2f} -> ${px[-1]:.2f}  (delta {px[-1]-px[0]:+.2f}, {((px[-1]/px[0])-1)*100:+.2f}%)")
    sigma_hourly = realized_hourly_sigma(spot)
    print(f"  realized hourly sigma (over window): {sigma_hourly:.4%}")
    sigma_annual = sigma_hourly * math.sqrt(24 * 365)
    print(f"  annualized:    {sigma_annual:.1%}")

    # ---- Lag: book-implied median vs spot at the time of the book record
    spot_t = [s["t"] for s in spot]
    spot_p = [s["price"] for s in spot]

    def spot_at(t: float) -> float | None:
        if not spot_t:
            return None
        i = bisect.bisect_right(spot_t, t) - 1
        if i < 0:
            return None
        return spot_p[i]

    diffs_dollars = []
    diffs_pct = []
    per_event_diffs = defaultdict(list)
    book_implied_series = []

    for b in books:
        implied = kalshi_mid_quantile_in_spot(b["markets"])
        if implied is None:
            continue
        sp = spot_at(b["t"])
        if sp is None:
            continue
        d = implied - sp
        diffs_dollars.append(d)
        diffs_pct.append(d / sp)
        per_event_diffs[b["event_ticker"]].append(d)
        book_implied_series.append((b["t"], implied, sp))

    print()
    print(f"=== Kalshi-implied median vs Coinbase spot ===")
    if diffs_dollars:
        d_arr = np.array(diffs_dollars)
        print(f"  pairs:         {len(d_arr):,}")
        print(f"  mean delta:    ${d_arr.mean():+.2f}   median ${np.median(d_arr):+.2f}")
        print(f"  std:           ${d_arr.std():.2f}   |delta|.p95: ${np.percentile(np.abs(d_arr),95):.2f}")
        print(f"  pct mean:      {np.mean(diffs_pct)*100:+.3f}%   p95|pct|: {np.percentile(np.abs(diffs_pct),95)*100:.3f}%")
        # Per event
        print(f"  by event (mean delta vs spot):")
        for ev, vals in sorted(per_event_diffs.items()):
            v = np.array(vals)
            print(f"    {ev}: n={len(v):4d}  mean ${v.mean():+7.2f}  median ${np.median(v):+7.2f}")

    # ---- Edge scan: model fair value vs Kalshi mid
    # Use trailing realized vol as estimate. Approximation: vol fixed across
    # the file; in production we'd use a rolling window. The window we have
    # is ~5 hours so this is acceptable for a smoke-test.
    print()
    print(f"=== Model edge scan (sigma_hourly = {sigma_hourly:.4%}) ===")
    if sigma_hourly <= 0:
        print("  vol too small to compute model edge.")
        return

    edges = []
    edges_by_event = defaultdict(list)
    for b in books:
        sp = spot_at(b["t"])
        if sp is None:
            continue
        evt = b["event_ticker"]
        try:
            settle_ts = event_settle_ts(evt)
        except Exception:
            continue
        hours_to_settle = max((settle_ts - b["t"]) / 3600.0, 1e-4)
        post = GBMPosterior(
            spot=sp,
            sigma_hourly=sigma_hourly,
            hours_to_settle=hours_to_settle,
            mu_hourly=0.0,
        )
        for m in b["markets"]:
            if m.get("strike_type") != "greater":
                continue
            bid = m.get("yes_bid") or 0.0
            ask = m.get("yes_ask") or 0.0
            if ask <= 0 or bid <= 0:
                continue
            mid = 0.5 * (bid + ask)
            try:
                fv = bracket_probability(
                    post,
                    floor_strike=m["strike"],
                    cap_strike=None,
                    strike_type="greater",
                )
            except ValueError:
                continue
            edge = fv - mid
            edges.append({
                "t": b["t"], "event": evt, "ticker": m["ticker"],
                "strike": m["strike"], "spot": sp, "fv": fv, "mid": mid,
                "edge": edge, "h_settle": hours_to_settle,
                "bid": bid, "ask": ask,
            })
            edges_by_event[evt].append(edge)

    print(f"  total quote-instant edges: {len(edges):,}")
    if edges:
        e_arr = np.array([e["edge"] for e in edges])
        print(f"  edge mean: {e_arr.mean():+.4f}   stdev {e_arr.std():.4f}")
        big = [e for e in edges if abs(e["edge"]) >= args.min_edge]
        print(f"  |edge|>={args.min_edge}: {len(big):,}  ({len(big)/len(edges)*100:.1f}% of quotes)")
        # Tradable: also need mid in (0.05, 0.95) to avoid edge that's
        # really just rounding the cent-tick
        tradable = [e for e in big if 0.05 < e["mid"] < 0.95]
        print(f"  tradable (mid in (0.05,0.95)): {len(tradable):,}")

        # By event
        print(f"  by event:")
        for ev, vals in sorted(edges_by_event.items()):
            v = np.array(vals)
            print(f"    {ev}: n={len(v):5d}  mean {v.mean():+.4f}  pct big={(np.abs(v)>=args.min_edge).mean()*100:5.1f}%")

        # Show some of the biggest edges
        big_sorted = sorted(big, key=lambda e: -abs(e["edge"]))
        print(f"  top |edge| (printing {min(args.max_print, len(big_sorted))}):")
        for e in big_sorted[: args.max_print]:
            ts = fmt_ts(e["t"])
            print(
                f"    {ts}  {e['ticker']}  spot=${e['spot']:.2f}  "
                f"strike=${e['strike']:.2f}  h={e['h_settle']:.2f}h  "
                f"fv={e['fv']:.3f}  mid={e['mid']:.3f}  "
                f"edge={e['edge']:+.3f}"
            )


def settled_outcomes(spot, books):
    """For events whose settle ts falls inside the recording window,
    compute the proxy settlement price (60s mean of spot before settle ts)
    and score model fair-values + Kalshi mids vs realized outcomes."""
    spot_t = np.array([s["t"] for s in spot])
    spot_p = np.array([s["price"] for s in spot])
    by_event = defaultdict(list)
    for b in books:
        by_event[b["event_ticker"]].append(b)

    results = []
    for ev, recs in by_event.items():
        try:
            settle_ts = event_settle_ts(ev)
        except Exception:
            continue
        if settle_ts < spot_t.min() or settle_ts > spot_t.max():
            continue  # didn't settle inside window
        # 60s TWAP proxy
        mask = (spot_t >= settle_ts - 60) & (spot_t <= settle_ts)
        if mask.sum() < 3:
            continue
        twap = float(spot_p[mask].mean())
        # Find the snapshot closest to t = settle - 30min for "early" view
        target = settle_ts - 30 * 60
        idx = min(range(len(recs)), key=lambda i: abs(recs[i]["t"] - target))
        early = recs[idx]
        # Last snapshot before settle
        pre = max(recs, key=lambda r: r["t"] if r["t"] < settle_ts else -1)
        results.append({
            "event": ev,
            "settle_ts": settle_ts,
            "twap": twap,
            "early": early,
            "pre": pre,
        })
    return results


def score_snapshot(rec, twap, sigma_hourly, spot_t, spot_p, settle_ts):
    """For one book snapshot, compute predicted_prob (model) and
    market_mid for each greater-strike, plus realized {0,1}."""
    # Use spot at the snapshot time
    i = bisect.bisect_right(spot_t.tolist(), rec["t"]) - 1
    if i < 0:
        return []
    sp = float(spot_p[i])
    h = max((settle_ts - rec["t"]) / 3600.0, 1e-4)
    post = GBMPosterior(spot=sp, sigma_hourly=sigma_hourly,
                        hours_to_settle=h, mu_hourly=0.0)
    rows = []
    for m in rec["markets"]:
        if m.get("strike_type") != "greater":
            continue
        bid = m.get("yes_bid") or 0.0
        ask = m.get("yes_ask") or 0.0
        if ask <= 0 or bid <= 0:
            continue
        try:
            fv = bracket_probability(post, floor_strike=m["strike"],
                                     cap_strike=None, strike_type="greater")
        except ValueError:
            continue
        mid = 0.5 * (bid + ask)
        outcome = 1.0 if twap > m["strike"] else 0.0
        rows.append({
            "ticker": m["ticker"], "strike": m["strike"],
            "spot_at_snap": sp, "fv": fv, "mid": mid,
            "outcome": outcome, "h": h,
        })
    return rows


def settle_report(spot, books, sigma_hourly):
    spot_t = np.array([s["t"] for s in spot])
    spot_p = np.array([s["price"] for s in spot])
    settled = settled_outcomes(spot, books)
    print()
    print(f"=== Settled-event reconciliation ({len(settled)} events with full window) ===")
    if not settled:
        print("  (no event fully settled in window)")
        return
    for s in settled:
        print(f"\n  {s['event']}  settle TWAP=${s['twap']:.2f}  "
              f"({fmt_ts(s['settle_ts'])})")
        for label, rec in (("T-30m", s["early"]), ("T-0",   s["pre"])):
            rows = score_snapshot(rec, s["twap"], sigma_hourly,
                                  spot_t, spot_p, s["settle_ts"])
            if not rows:
                print(f"    {label}: no rows")
                continue
            fv = np.array([r["fv"] for r in rows])
            md = np.array([r["mid"] for r in rows])
            ot = np.array([r["outcome"] for r in rows])
            brier_model = float(np.mean((fv - ot) ** 2))
            brier_market = float(np.mean((md - ot) ** 2))
            ll = lambda p, y: -np.mean(y*np.log(np.clip(p,1e-6,1-1e-6)) + (1-y)*np.log(np.clip(1-p,1e-6,1-1e-6)))
            print(f"    {label} ({len(rows)} mkts, h={rows[0]['h']:.2f}h, spot=${rows[0]['spot_at_snap']:.2f}): "
                  f"Brier model={brier_model:.4f}  Brier market={brier_market:.4f}  "
                  f"LL model={ll(fv,ot):.4f}  LL market={ll(md,ot):.4f}")
            # show the strike near twap to see whether market or model called it
            ikill = np.argmin(np.abs(np.array([r["strike"] for r in rows]) - s["twap"]))
            r = rows[ikill]
            print(f"      ATM-ish strike ${r['strike']:.2f} (twap=${s['twap']:.2f}, "
                  f"realized={int(r['outcome'])}): model fv={r['fv']:.3f} mid={r['mid']:.3f}")


if __name__ == "__main__":
    # Patch main() to also call settle_report — re-run loaders in a single
    # pass for clarity.
    args_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if args_path:
        spot, books, _ = load(args_path)
        sigma_hourly = realized_hourly_sigma(spot)
    main()
    if args_path:
        settle_report(spot, books, sigma_hourly)
