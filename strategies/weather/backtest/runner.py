"""Backtest harness.

Replays historical days for a city, evaluates the posterior model at a
set of intraday cutoff hours, and scores predictions against the
realized daily high.

Run from repo root::

    python -m strategies.weather.backtest.runner \
        --city NYC --start 2025-04-01 --end 2025-04-30 \
        --hours-out 1,3,6,9,12,15,18,21

The scored output is a CSV plus an aggregate metrics summary printed
to stdout. The default bracket structure mirrors what Kalshi runs for
KXHIGH (5 brackets centered on a daily climatology mean) — the
backtester's job is to score model calibration, not P&L.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from strategies.weather.config import get_city
from strategies.weather.data.iem import (
    HourlyObservation,
    IEMClient,
    aggregate_daily_highs,
    observations_through_hour,
)
from master.metrics import ScoredPrediction, format_metrics
from strategies.weather.model.climo import CLIMO_DATA_DIR, EmpiricalClimatology
from strategies.weather.model.posterior import daily_high_posterior


# Reuse the same on-disk cache as fit_climo so a one-time fetch serves
# both fitting and backtesting.
IEM_CACHE_DIR = CLIMO_DATA_DIR.parent / ".iem_cache"


@dataclass
class BacktestRow:
    """One scored prediction for the output CSV."""

    city: str
    local_date: str
    hour_cutoff: int
    obs_max_so_far: float
    realized_daily_high: float
    bracket_lo: int
    bracket_hi: int
    predicted_prob: float
    realized_outcome: int


def _bracket_centers(realized: float, brackets: int = 5, width: int = 2) -> list[tuple[int, int]]:
    """Build a 5-bracket structure around the realized daily high.

    Mirrors Kalshi's KXHIGH layout: a center bracket and neighbors
    spaced by ``width`` °F. Used so the backtester can score predictions
    even on dates when we don't have actual Kalshi historical brackets.
    """
    center = int(round(realized))
    half = brackets // 2
    out: list[tuple[int, int]] = []
    # Bottom open-ended (everything below)
    bottom_cap = center - half * width
    out.append((-1000, bottom_cap - 1))
    # Middle equal-width brackets
    for i in range(brackets):
        lo = bottom_cap + i * width
        hi = lo + width - 1
        out.append((lo, hi))
    # Top open-ended (everything above)
    top_floor = bottom_cap + brackets * width
    out.append((top_floor, 1000))
    return out


def run_backtest(
    city_code: str,
    start_date: date,
    end_date: date,
    hours_out: Iterable[int],
    iem: Optional[IEMClient] = None,
    climatology: Optional[EmpiricalClimatology] = None,
) -> tuple[list[BacktestRow], list[ScoredPrediction]]:
    """Run the model backtest. Returns ``(rows, predictions)``.

    If ``climatology`` is provided, the posterior uses empirical
    ``(mu_lift, sigma_lift)`` per (month, hour). Otherwise the parametric
    defaults from ``model.climo.DEFAULT_RESIDUAL_TABLE`` are used.
    """
    city = get_city(city_code)
    iem_client = iem or IEMClient(cache_dir=IEM_CACHE_DIR)

    # Pull hourly observations across the entire window in one shot
    range_start = datetime(start_date.year, start_date.month, start_date.day)
    range_end = datetime(end_date.year, end_date.month, end_date.day, 23, 59)
    all_obs = iem_client.fetch_hourly(
        station=city.asos_station,
        start=range_start,
        end=range_end,
        tz_name=city.timezone,
    )

    # Index observations by local date
    by_date: dict[date, list[HourlyObservation]] = {}
    for o in all_obs:
        by_date.setdefault(o.valid_local.date(), []).append(o)

    # Compute realized daily highs once
    daily_highs = {dh.local_date: dh.high_tmpf for dh in aggregate_daily_highs(all_obs)}

    rows: list[BacktestRow] = []
    preds: list[ScoredPrediction] = []

    cur = start_date
    while cur <= end_date:
        day_obs = sorted(by_date.get(cur, []), key=lambda o: o.valid_local)
        realized = daily_highs.get(cur)
        if realized is None or not day_obs:
            cur += timedelta(days=1)
            continue
        brackets = _bracket_centers(realized)
        for h in hours_out:
            cutoff = datetime(cur.year, cur.month, cur.day, h, 59, tzinfo=day_obs[0].valid_local.tzinfo)
            obs_so_far = observations_through_hour(day_obs, cutoff)
            valid_obs = [o.tmpf for o in obs_so_far if o.tmpf is not None]
            if not valid_obs:
                continue
            obs_max = max(valid_obs)
            residual = None
            samples = None
            if climatology is not None:
                samples = climatology.samples_at(cur.month, h)
                if samples is None:
                    residual = climatology.residual_at(cur.month, h)
            posterior = daily_high_posterior(
                obs_max, h, residual=residual, samples=samples,
            )
            for lo, hi in brackets:
                if lo <= -999:
                    pred = posterior.cdf(hi + 0.5)
                elif hi >= 999:
                    pred = posterior.sf(lo - 0.5)
                else:
                    pred = posterior.prob_between(lo, hi)
                outcome = 1 if (lo <= realized <= hi) else 0
                rows.append(BacktestRow(
                    city=city.code,
                    local_date=cur.isoformat(),
                    hour_cutoff=h,
                    obs_max_so_far=obs_max,
                    realized_daily_high=realized,
                    bracket_lo=lo,
                    bracket_hi=hi,
                    predicted_prob=pred,
                    realized_outcome=outcome,
                ))
                preds.append(ScoredPrediction(
                    prob=pred,
                    outcome=outcome,
                    label=f"{city.code} {cur.isoformat()} h={h} [{lo},{hi}]",
                ))
        cur += timedelta(days=1)

    return rows, preds


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Weather strategy model backtester")
    parser.add_argument("--city", required=True, help="3-letter city code (NYC, CHI, MIA, LAX, DEN, AUS)")
    parser.add_argument("--start", required=True, help="ISO date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="ISO date YYYY-MM-DD")
    parser.add_argument(
        "--hours-out",
        default="1,6,12,18",
        help="Comma-separated local hours to cut off at and predict from",
    )
    parser.add_argument("--out", default=None, help="Optional CSV output path")
    parser.add_argument(
        "--no-empirical",
        action="store_true",
        help="Force parametric defaults even if climo_data/<CITY>.json exists",
    )
    args = parser.parse_args(argv)

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)
    hours = [int(h) for h in args.hours_out.split(",") if h.strip()]

    climatology = None if args.no_empirical else EmpiricalClimatology.for_city(args.city)
    if climatology:
        gaussian_filled, total = climatology.coverage()
        empirical_filled, _ = climatology.samples_coverage()
        mode = "v3 empirical-CDF" if empirical_filled > 0 else "v2 Gaussian"
        print(f"using {mode} climatology for {args.city}: "
              f"gaussian={gaussian_filled}/{total} cells, "
              f"empirical={empirical_filled}/{total} cells, "
              f"fit_window={climatology.fit_window}, "
              f"n_days={climatology.n_days}, "
              f"model_version={climatology.model_version}")
    else:
        if args.no_empirical:
            print(f"using parametric defaults (--no-empirical)")
        else:
            print(f"no empirical fit found for {args.city}; using parametric defaults")

    rows, preds = run_backtest(args.city, start_date, end_date, hours, climatology=climatology)

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
    print(f"=== {args.city} {args.start}..{args.end} | {len(preds)} predictions ===")
    print(format_metrics(preds))
    return 0


if __name__ == "__main__":
    sys.exit(main())
