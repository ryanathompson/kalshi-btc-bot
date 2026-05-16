"""Measure Kalshi-vs-spot lag.

Two modes:
  - --analyze <jsonl>: analyze an existing book_collector log
      (lines: {"t":..., "kind":"spot"|"kalshi_book"|"kalshi_atm", ...}).
  - --live --series KXBTC15M --duration 180: live capture +
      analyze. Uses repo's master/kalshi_public.py (requests-based,
      Cloudflare-friendly UA) and Coinbase advanced-trade REST for
      spot.

Lag estimator: cross-correlation of differenced spot and
mid-of-ATM-strike on a common 1s grid. Reports the lag (in
seconds) that maximizes Pearson correlation; positive lag means
Kalshi follows spot.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass

# Make repo modules importable regardless of cwd
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import requests  # noqa: E402  (after sys.path)

from master.kalshi_public import KalshiPublicReader  # noqa: E402


# Coinbase product per Kalshi series prefix.
SERIES_TO_COINBASE = {
    "KXBTC15M": "BTC-USD",
    "KXETHD":   "ETH-USD",
    "KXSOL15M": "SOL-USD",
}

CB_TICKER = "https://api.exchange.coinbase.com/products/{p}/ticker"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    return s


# ---------------------------------------------------------------
# Live capture
# ---------------------------------------------------------------

def fetch_spot(session: requests.Session, product: str) -> float | None:
    try:
        r = session.get(CB_TICKER.format(p=product), timeout=4)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception as exc:  # noqa: BLE001
        print(f"  spot fetch failed: {exc}", file=sys.stderr)
        return None


def pick_atm_market(markets, spot: float):
    """Pick the market whose mid is closest to 0.5 (ATM by price).

    Falls back to the strike closest to spot.
    """
    candidates = []
    for m in markets:
        if m.yes_bid > 0 and m.yes_ask > 0:
            mid = (m.yes_bid + m.yes_ask) / 2.0
            candidates.append((abs(mid - 0.5), mid, m))
    if candidates:
        candidates.sort()
        return candidates[0][2]
    # Fallback: numeric strike closest to spot
    best = None
    best_d = math.inf
    for m in markets:
        # Try ticker suffix like ...-T64500 / ...-B64500
        t = m.ticker
        if "-" not in t:
            continue
        last = t.rsplit("-", 1)[-1]
        try:
            strike = float(last.lstrip("TBLG"))
        except ValueError:
            continue
        d = abs(strike - spot)
        if d < best_d:
            best_d = d
            best = m
    return best


def live_capture(series: str, duration: int, out_path: str | None) -> str:
    product = SERIES_TO_COINBASE.get(series)
    if not product:
        raise SystemExit(f"unsupported series {series}")

    session = make_session()
    reader = KalshiPublicReader(session=session)

    events = reader.list_events(series, status="open", limit=20)
    if not events:
        raise SystemExit(f"no open event for {series}")
    events.sort(key=lambda e: e.strike_date)
    event = events[0]
    print(f"  using event {event.event_ticker} (close {event.strike_date.isoformat()})",
          file=sys.stderr)

    out_path = out_path or f"logs/lag_{series}_{int(time.time())}.jsonl"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    print(f"  capturing -> {out_path}", file=sys.stderr)

    deadline = time.time() + duration
    fout = open(out_path, "w", encoding="utf-8")
    try:
        chosen_ticker: str | None = None
        while time.time() < deadline:
            t0 = time.time()
            spot = fetch_spot(session, product)
            if spot is not None:
                fout.write(json.dumps({"t": time.time(), "kind": "spot",
                                       "product": product, "price": spot}) + "\n")
            try:
                markets = reader.list_markets(event_ticker=event.event_ticker, limit=200)
                m = pick_atm_market(markets, spot or 0)
                if m:
                    chosen_ticker = m.ticker
                    fout.write(json.dumps({
                        "t": time.time(), "kind": "kalshi_atm",
                        "series": series, "event_ticker": event.event_ticker,
                        "ticker": m.ticker,
                        "yes_bid": m.yes_bid,
                        "yes_ask": m.yes_ask,
                    }) + "\n")
            except Exception as exc:  # noqa: BLE001
                print(f"  market fetch err: {exc}", file=sys.stderr)
            fout.flush()
            sleep = max(0.0, 1.0 - (time.time() - t0))
            time.sleep(sleep)
    finally:
        fout.close()
    print(f"  done. ATM ticker observed: {chosen_ticker}", file=sys.stderr)
    return out_path


# ---------------------------------------------------------------
# Lag analysis (works on either format)
# ---------------------------------------------------------------

@dataclass
class Series:
    ts: list[float]
    val: list[float]


def load_jsonl(path: str) -> tuple[Series, Series]:
    spots: list[tuple[float, float]] = []
    mids: dict[str, list[tuple[float, float]]] = defaultdict(list)
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("t")
            if d.get("kind") == "spot":
                spots.append((t, float(d["price"])))
            elif d.get("kind") == "kalshi_atm":
                yb = d.get("yes_bid") or 0
                ya = d.get("yes_ask") or 0
                # New format = dollars (0..1). Old format = cents (0..100).
                # Detect by magnitude.
                if yb > 1 or ya > 1:
                    yb /= 100.0
                    ya /= 100.0
                if yb > 0 and ya > 0:
                    mids[d["ticker"]].append((t, (yb + ya) / 2.0))
            elif d.get("kind") == "kalshi_book":
                # eth_book format: pick strike whose mid is closest to 0.5
                if not spots:
                    continue
                best = None
                best_d = math.inf
                for m in d.get("markets", []):
                    yb = m.get("yes_bid") or 0
                    ya = m.get("yes_ask") or 0
                    if yb <= 0 or ya <= 0:
                        continue
                    mid = (yb + ya) / 2.0
                    score = abs(mid - 0.5)
                    if score < best_d:
                        best_d = score
                        best = (m["ticker"], mid)
                if best:
                    mids[best[0]].append((t, best[1]))
    if not mids:
        return Series([], []), Series([], [])
    chosen = max(mids.items(), key=lambda kv: len(kv[1]))
    print(f"  spot pts={len(spots)}  ATM ticker={chosen[0]}  pts={len(chosen[1])}",
          file=sys.stderr)
    spot_s = Series([t for t, _ in spots], [v for _, v in spots])
    mid_s = Series([t for t, _ in chosen[1]], [v for _, v in chosen[1]])
    return spot_s, mid_s


def resample_1s(s: Series, t0: float, t1: float) -> list[float]:
    n = int(t1 - t0) + 1
    out = [math.nan] * n
    if not s.ts:
        return out
    j = 0
    last = math.nan
    for i in range(n):
        ti = t0 + i
        while j < len(s.ts) and s.ts[j] <= ti:
            last = s.val[j]
            j += 1
        out[i] = last
    return out


def diff(seq: list[float]) -> list[float]:
    out = []
    for i in range(1, len(seq)):
        a, b = seq[i - 1], seq[i]
        if math.isnan(a) or math.isnan(b):
            out.append(0.0)
        else:
            out.append(b - a)
    return out


def pearson(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n < 5:
        return 0.0
    ma = sum(a[:n]) / n
    mb = sum(b[:n]) / n
    num = 0.0
    da = 0.0
    db = 0.0
    for i in range(n):
        x = a[i] - ma
        y = b[i] - mb
        num += x * y
        da += x * x
        db += y * y
    if da <= 0 or db <= 0:
        return 0.0
    return num / math.sqrt(da * db)


def best_lag(spot_d: list[float], mid_d: list[float], max_lag: int = 30):
    best = (0, -2.0)
    for lag in range(0, max_lag + 1):
        if lag >= len(spot_d):
            break
        a = spot_d[: len(spot_d) - lag]
        b = mid_d[lag:]
        c = pearson(a, b)
        if c > best[1]:
            best = (lag, c)
    return best


def analyze(path: str) -> None:
    spot_s, mid_s = load_jsonl(path)
    if not spot_s.ts or not mid_s.ts:
        print("not enough data", file=sys.stderr)
        return
    t0 = math.ceil(max(spot_s.ts[0], mid_s.ts[0]))
    t1 = math.floor(min(spot_s.ts[-1], mid_s.ts[-1]))
    if t1 - t0 < 60:
        print(f"overlap too short ({t1-t0:.0f}s)", file=sys.stderr)
        return
    spot_g = resample_1s(spot_s, t0, t1)
    mid_g = resample_1s(mid_s, t0, t1)
    spot_d = diff(spot_g)
    mid_d = diff(mid_g)
    lag, corr = best_lag(spot_d, mid_d, max_lag=30)
    print(f"file:        {path}")
    print(f"window:      {int(t1-t0)}s  ({(t1-t0)/60:.1f} min)")
    print(f"best lag:    {lag}s     (corr={corr:+.3f})")
    print("lag(s)  corr")
    for L in range(0, 16):
        if L >= len(spot_d):
            break
        a = spot_d[: len(spot_d) - L] if L else spot_d[:]
        b = mid_d[L:]
        print(f"  {L:>3}   {pearson(a, b):+.3f}")


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--analyze", help="analyze a captured jsonl")
    p.add_argument("--live", action="store_true")
    p.add_argument("--series", help="KXBTC15M / KXETHD / KXSOL15M")
    p.add_argument("--duration", type=int, default=180)
    p.add_argument("--out", help="output jsonl path")
    args = p.parse_args(argv)
    if args.live:
        if not args.series:
            raise SystemExit("--series required with --live")
        path = live_capture(args.series, args.duration, args.out)
        print()
        analyze(path)
        return
    if args.analyze:
        analyze(args.analyze)
        return
    p.print_help()


if __name__ == "__main__":
    main(sys.argv[1:])
