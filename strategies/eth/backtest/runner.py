"""ETH backtest runner.

For each historical hour H in [start, end):
  1. Use candles up to H-1 to estimate trailing realized volatility.
  2. Set spot = close at H-1.
  3. For each candidate strike (synthetic — generated around spot to
     mirror Kalshi's $40 spacing), compute GBM ``P(P_T > strike)`` for
     the next-hour settlement.
  4. Compare to realized close at H. Score Brier / log-loss / calibration.

Synthetic strikes are needed because we don't have Kalshi's historical
strike-by-strike book; the goal of v1 is model-quality scoring, not
trading-P&L scoring. We use the same $40 spacing Kalshi uses live.

Usage::

    python -m strategies.eth.backtest.runner \
        --start 2026-03-01 --end 2026-03-31 \
        --vol-window-hours 72 \
        --strikes-from-spot -200,200,40 \
        --out backtest_eth_mar.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from master.metrics import ScoredPrediction, format_metrics
from strategies.eth.config import ETH, get_market
from strategies.eth.data.coinbase import (
    CoinbaseClient,
    HourlyCandle,
    candles_through,
    hourly_log_returns,
)
from strategies.eth.model.posterior import (
    EmpiricalReturnPosterior,
    GBMPosterior,
    PosteriorAny,
    make_eth_posterior,
)
from strategies.eth.model.volatility import trailing_realized_vol


COINBASE_CACHE_DIR = Path(__file__).resolve().parent.parent / ".coinbase_cache"


@dataclass
class BacktestRow:
    market_code: str
    settlement_utc: str
    spot_at_t_minus_1: float
    realized_close: float
    sigma_hourly: float
    n_returns_in_window: int
    posterior_kind: str  # "gbm" or "empirical"
    strike: float
    predicted_prob: float
    realized_outcome: int


def _parse_strikes_spec(spec: str) -> tuple[float, float, float]:
    """Parse ``"-200,200,40"`` → ``(-200.0, 200.0, 40.0)``."""
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    if len(parts) != 3:
        raise ValueError(f"--strikes-from-spot expects 'lo,hi,step', got: {spec!r}")
    lo, hi, step = (float(p) for p in parts)
    if step <= 0:
        raise ValueError("step must be positive")
    return lo, hi, step


def _strikes_around(spot: float, spec: tuple[float, float, float]) -> list[float]:
    """Generate strikes at ``spot + lo`` to ``spot + hi`` in ``step`` increments."""
    lo, hi, step = spec
    out = []
    offset = lo
    while offset <= hi + 1e-9:
        out.append(round(spot + offset, 2))
        offset += step
    return out


def run_backtest(
    market_code: str,
    start_utc: datetime,
    end_utc: datetime,
    vol_window_hours: int,
    strikes_spec: tuple[float, float, float],
    coinbase: Optional[CoinbaseClient] = None,
    use_empirical: bool = True,
) -> tuple[list[BacktestRow], list[ScoredPrediction]]:
    """Run the model backtest. Returns ``(rows, predictions)``.

    When ``use_empirical=True`` (default), the posterior is built from
    the empirical CDF of the trailing log returns. Falls back to GBM
    automatically when there aren't enough samples.
    """
    market = get_market(market_code)
    cb = coinbase or CoinbaseClient(cache_dir=COINBASE_CACHE_DIR)

    # Pull enough history to populate the trailing vol window before start
    fetch_start = start_utc - timedelta(hours=vol_window_hours + 24)
    print(f"fetching {market.coinbase_product} hourly candles "
          f"{fetch_start.isoformat()}..{end_utc.isoformat()} ...")
    candles = cb.fetch_hourly_candles(market.coinbase_product, fetch_start, end_utc)
    if not candles:
        print("  no candles returned", file=sys.stderr)
        return [], []
    print(f"  fetched {len(candles)} candles")

    # Index candles by exact UTC timestamp for O(1) lookup
    by_time: dict[datetime, HourlyCandle] = {c.time_utc: c for c in candles}
    settlement_times = sorted(t for t in by_time if start_utc <= t <= end_utc)

    rows: list[BacktestRow] = []
    preds: list[ScoredPrediction] = []
    for settle_t in settlement_times:
        prior_t = settle_t - timedelta(hours=1)
        prior_candle = by_time.get(prior_t)
        settle_candle = by_time.get(settle_t)
        if prior_candle is None or settle_candle is None:
            continue
        spot = prior_candle.close
        if spot <= 0:
            continue
        # Trailing vol from candles strictly before prior_t (so we don't peek)
        history = candles_through(candles, prior_t)
        # Drop the last candle so vol uses returns from the window leading
        # up to (but not including) prior_t's close
        if len(history) < 2:
            continue
        log_rets = hourly_log_returns(history)
        vol = trailing_realized_vol(log_rets, vol_window_hours)
        if vol.sigma_hourly <= 1e-12 or vol.n_returns < 8:
            continue
        # Slice to trailing window so empirical CDF reflects current regime
        windowed_returns = log_rets[-vol_window_hours:] if vol_window_hours > 0 else log_rets

        if use_empirical:
            post: PosteriorAny = make_eth_posterior(
                spot=spot,
                sigma_hourly=vol.sigma_hourly,  # used as fallback inside helper
                hourly_log_returns=windowed_returns,
                hours_to_settle=1.0,
            )
        else:
            post = GBMPosterior(
                spot=spot,
                sigma_hourly=vol.sigma_hourly,
                hours_to_settle=1.0,
            )

        kind = "empirical" if isinstance(post, EmpiricalReturnPosterior) else "gbm"
        realized_close = settle_candle.close
        for strike in _strikes_around(spot, strikes_spec):
            pred = post.prob_greater_than(strike)
            outcome = 1 if realized_close > strike else 0
            rows.append(BacktestRow(
                market_code=market.code,
                settlement_utc=settle_t.isoformat(),
                spot_at_t_minus_1=round(spot, 4),
                realized_close=round(realized_close, 4),
                sigma_hourly=round(vol.sigma_hourly, 6),
                n_returns_in_window=vol.n_returns,
                posterior_kind=kind,
                strike=round(strike, 4),
                predicted_prob=round(pred, 6),
                realized_outcome=outcome,
            ))
            preds.append(ScoredPrediction(
                prob=pred,
                outcome=outcome,
                label=f"{market.code} {settle_t.isoformat()} K={strike:.2f}",
            ))

    return rows, preds


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ETH strategy model backtester")
    parser.add_argument("--market", default="ETH", help="Crypto market code (default: ETH)")
    parser.add_argument("--start", required=True, help="ISO datetime UTC, e.g. 2026-03-01 or 2026-03-01T00:00")
    parser.add_argument("--end", required=True, help="ISO datetime UTC")
    parser.add_argument(
        "--vol-window-hours",
        type=int,
        default=72,
        help="Trailing window for realized-vol estimate (default: 72 = 3 days)",
    )
    parser.add_argument(
        "--strikes-from-spot",
        default="-200,200,40",
        help="Synthetic strike grid spec: 'lo,hi,step' offsets from spot in $ (default: -200,200,40)",
    )
    parser.add_argument("--out", default=None, help="Optional CSV output path")
    parser.add_argument(
        "--no-empirical",
        action="store_true",
        help="Force GBM (lognormal). Default is empirical-CDF over trailing log returns.",
    )
    args = parser.parse_args(argv)

    start_utc = _parse_iso_or_date(args.start)
    end_utc = _parse_iso_or_date(args.end, end_of_day=True)
    if end_utc <= start_utc:
        print("error: --end must be after --start", file=sys.stderr)
        return 2

    strikes_spec = _parse_strikes_spec(args.strikes_from_spot)

    rows, preds = run_backtest(
        market_code=args.market,
        start_utc=start_utc,
        end_utc=end_utc,
        vol_window_hours=args.vol_window_hours,
        strikes_spec=strikes_spec,
        use_empirical=not args.no_empirical,
    )

    if rows:
        kinds = {r.posterior_kind for r in rows}
        print(f"posterior used: {sorted(kinds)}")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(BacktestRow.__dataclass_fields__))
            w.writeheader()
            for r in rows:
                w.writerow(asdict(r))
        print(f"wrote {len(rows)} rows to {out_path}")

    print()
    print(f"=== {args.market} {args.start}..{args.end} | "
          f"window={args.vol_window_hours}h | strikes={args.strikes_from_spot} | "
          f"{len(preds)} predictions ===")
    print(format_metrics(preds))
    return 0


def _parse_iso_or_date(s: str, end_of_day: bool = False) -> datetime:
    """Accept ``YYYY-MM-DD`` or full ISO datetime; return aware UTC."""
    try:
        d = date.fromisoformat(s)
    except ValueError:
        # Assume datetime
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    if end_of_day:
        return datetime(d.year, d.month, d.day, 23, 59, tzinfo=timezone.utc)
    return datetime(d.year, d.month, d.day, 0, 0, tzinfo=timezone.utc)


if __name__ == "__main__":
    sys.exit(main())
