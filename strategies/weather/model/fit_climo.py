"""Fit empirical climatology residuals from IEM ASOS history.

Run once per city, against 2-3 years of historical hourly data:

    python -m strategies.weather.model.fit_climo \
        --city NYC --start 2023-01-01 --end 2026-04-30

Output: ``strategies/weather/climo_data/<CITY>.json`` with one
``ResidualParams`` per ``(month, hour)`` cell. Cells with fewer than
``MIN_SAMPLES_PER_CELL`` samples are omitted; the loader falls back to
parametric defaults for those cells.

Algorithm: for each historical day,
  1. Compute ``daily_high`` = max temperature over the local-day window
  2. For each hour ``H`` in 0..23:
       a. ``obs_max_through_H`` = max temperature over observations
          with ``valid_local.hour <= H``
       b. ``residual = daily_high - obs_max_through_H`` (always >= 0)
       c. Append residual to bucket ``(month, H)``
  3. Fit Gaussian ``(mu_lift, sigma_lift)`` per bucket
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from strategies.weather.config import CITIES, get_city
from strategies.weather.data.iem import (
    HourlyObservation,
    IEMClient,
)
from strategies.weather.model.climo import (
    CLIMO_DATA_DIR,
    MIN_SAMPLES_PER_CELL,
    ResidualParams,
)


# IEM cache lives next to the climo_data dir so re-fits are free
IEM_CACHE_DIR = CLIMO_DATA_DIR.parent / ".iem_cache"


def collect_residuals(
    observations: list[HourlyObservation],
) -> dict[tuple[int, int], list[float]]:
    """Group residuals by (month, hour) over historical observations.

    Returns ``{(month, hour): [residual_F, ...]}`` where month is 1..12
    and hour is 0..23. Residuals are non-negative by construction.
    """
    # Bucket observations by local date
    by_date: dict[date, list[HourlyObservation]] = defaultdict(list)
    for o in observations:
        if o.tmpf is not None:
            by_date[o.valid_local.date()].append(o)

    residuals: dict[tuple[int, int], list[float]] = defaultdict(list)
    for d, day_obs in by_date.items():
        day_obs.sort(key=lambda o: o.valid_local)
        temps = [o.tmpf for o in day_obs if o.tmpf is not None]
        if not temps:
            continue
        daily_high = max(temps)
        # Sweep hours 0..23, accumulating max-so-far
        running_max: Optional[float] = None
        # Build a (hour -> max-so-far-through-hour) table for the day
        max_through_hour: dict[int, float] = {}
        idx = 0
        for h in range(24):
            # Advance idx through observations whose hour <= h
            while idx < len(day_obs) and day_obs[idx].valid_local.hour <= h:
                t = day_obs[idx].tmpf
                if t is not None:
                    running_max = t if running_max is None else max(running_max, t)
                idx += 1
            if running_max is not None:
                max_through_hour[h] = running_max
        for h, m_far in max_through_hour.items():
            residuals[(d.month, h)].append(daily_high - m_far)
    return residuals


def fit_gaussian(samples: list[float]) -> ResidualParams:
    """Sample mean and std-dev (n-1 denominator). Asserts at least 1 sample."""
    n = len(samples)
    if n == 0:
        raise ValueError("cannot fit empty sample")
    mu = sum(samples) / n
    if n == 1:
        return ResidualParams(mu_lift_f=mu, sigma_lift_f=0.0)
    var = sum((x - mu) ** 2 for x in samples) / (n - 1)
    return ResidualParams(mu_lift_f=mu, sigma_lift_f=math.sqrt(max(0.0, var)))


def fit_city(
    city_code: str,
    start_date: date,
    end_date: date,
    iem: Optional[IEMClient] = None,
) -> dict:
    """Fetch + fit + return a JSON-serializable dict ready for write."""
    city = get_city(city_code)
    iem_client = iem or IEMClient(cache_dir=IEM_CACHE_DIR)
    print(f"fetching {city.asos_station} ({city.display_name}) "
          f"{start_date.isoformat()}..{end_date.isoformat()} ...")
    obs = iem_client.fetch_hourly(
        station=city.asos_station,
        start=datetime(start_date.year, start_date.month, start_date.day),
        end=datetime(end_date.year, end_date.month, end_date.day, 23, 59),
        tz_name=city.timezone,
    )
    print(f"  fetched {len(obs)} hourly observations")

    residuals = collect_residuals(obs)
    n_days = len({o.valid_local.date() for o in obs if o.tmpf is not None})
    print(f"  {n_days} days with valid observations; {sum(len(v) for v in residuals.values())} residual samples")

    by_month_hour: dict[str, dict[str, dict]] = defaultdict(dict)
    fitted_cells = 0
    skipped_cells = 0
    for (month, hour), samples in residuals.items():
        n = len(samples)
        if n < MIN_SAMPLES_PER_CELL:
            skipped_cells += 1
            continue
        params = fit_gaussian(samples)
        # Store sorted residuals (rounded to 0.1°F) for the v3 empirical-CDF path.
        # 0.1° precision is far below the 1° market bracket resolution and keeps
        # JSON files small.
        sorted_residuals = sorted(round(r, 1) for r in samples)
        by_month_hour[str(month)][str(hour)] = {
            "mu_lift_f": round(params.mu_lift_f, 3),
            "sigma_lift_f": round(params.sigma_lift_f, 3),
            "n": n,
            "residuals": sorted_residuals,
        }
        fitted_cells += 1

    out = {
        "city_code": city.code,
        "asos_station": city.asos_station,
        "fit_window": f"{start_date.isoformat()}..{end_date.isoformat()}",
        "n_days": n_days,
        "fitted_cells": fitted_cells,
        "skipped_cells": skipped_cells,
        "min_samples_per_cell": MIN_SAMPLES_PER_CELL,
        "model_version": 3,
        "by_month_hour": dict(by_month_hour),
    }
    print(f"  fitted {fitted_cells}/{12*24} cells; "
          f"{skipped_cells} cells skipped (n < {MIN_SAMPLES_PER_CELL})")
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fit empirical climatology residuals from IEM ASOS history",
    )
    parser.add_argument(
        "--city",
        required=True,
        help="3-letter city code (NYC, CHI, MIA, LAX, DEN, AUS), comma-separated for multiple, or 'all'",
    )
    parser.add_argument("--start", required=True, help="ISO date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="ISO date YYYY-MM-DD")
    parser.add_argument(
        "--out-dir",
        default=None,
        help=f"Output directory (default: {CLIMO_DATA_DIR}). Files written as <CITY>.json.",
    )
    parser.add_argument(
        "--pause-between-cities",
        type=float,
        default=15.0,
        help="Seconds to sleep between cities to avoid IEM rate limits (default: 15)",
    )
    args = parser.parse_args(argv)

    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end)
    if end_d < start_d:
        print("error: --end must be >= --start", file=sys.stderr)
        return 2

    if args.city.lower() == "all":
        city_list = sorted(CITIES.keys())
    else:
        city_list = [c.strip().upper() for c in args.city.split(",") if c.strip()]

    out_dir = Path(args.out_dir) if args.out_dir else CLIMO_DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # Single shared client → cache hits across cities (and across reruns)
    iem = IEMClient(cache_dir=IEM_CACHE_DIR)

    for i, city_code in enumerate(city_list):
        if i > 0 and args.pause_between_cities > 0:
            print(f"sleeping {args.pause_between_cities:.0f}s before next city...")
            time.sleep(args.pause_between_cities)
        print(f"\n=== {city_code} ===")
        try:
            out = fit_city(city_code, start_d, end_d, iem=iem)
        except Exception as e:
            print(f"  ERROR fitting {city_code}: {e}", file=sys.stderr)
            print(f"  (cached responses preserved at {IEM_CACHE_DIR}; rerun to retry)", file=sys.stderr)
            return 1
        out_path = out_dir / f"{city_code}.json"
        with out_path.open("w") as f:
            json.dump(out, f, indent=2, sort_keys=True)
        print(f"  wrote {out_path}")

    print(f"\nfit complete. cache: {IEM_CACHE_DIR}, output: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
