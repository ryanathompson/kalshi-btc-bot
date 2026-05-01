"""Climatology residual statistics for the daily-high posterior.

Two modes are supported:

1. **Parametric defaults** (``residual_at_hour``) — coarse, season-agnostic,
   built into the module. Used when no empirical fit is available. Good
   enough to run the backtester out of the box; not tradable.

2. **Empirical** (``EmpiricalClimatology.from_json_file``) — loaded from
   ``strategies/weather/climo_data/<CITY>.json``, produced by
   ``strategies.weather.model.fit_climo``. Fits ``(mu_lift, sigma_lift)``
   per ``(city, month, hour)`` from IEM history, with parametric fallback
   for cells with insufficient samples.

Model shape (both modes):

    daily_high = max(obs_so_far, obs_so_far + future_lift)
    future_lift ~ Normal(mu_lift, sigma_lift)
    future_lift is clipped at 0 implicitly (max() above)

The "lift" is the additional warming beyond what's been observed through
hour H. Late in the day mu_lift and sigma_lift go to ~0 and the posterior
collapses to a point mass at obs_max.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ResidualParams:
    """Parameters for the future-max distribution at hour-of-day H.

    The "lift" is how much the daily max can still climb beyond what's
    been observed through hour H.
    """

    mu_lift_f: float    # expected additional °F of warming
    sigma_lift_f: float # standard deviation in °F


# Hour-of-day (local time, 0-23) → ResidualParams.
#
# Rough qualitative shape:
# - Before sunrise (0–6): wide; the day hasn't started warming
# - Morning (7–11): expected warming high, uncertainty moderate
# - Around peak (12–15): smaller incremental warming, narrow uncertainty
# - Afternoon (16–18): peak likely passed; small lift, narrow band
# - Evening (19–23): peak certainly passed; lift ~0, very narrow band
#
# These are coarse defaults — calibrate empirically before live trading.
DEFAULT_RESIDUAL_TABLE: dict[int, ResidualParams] = {
    0:  ResidualParams(mu_lift_f=15.0, sigma_lift_f=8.0),
    1:  ResidualParams(mu_lift_f=15.0, sigma_lift_f=8.0),
    2:  ResidualParams(mu_lift_f=15.0, sigma_lift_f=8.0),
    3:  ResidualParams(mu_lift_f=15.5, sigma_lift_f=8.0),
    4:  ResidualParams(mu_lift_f=16.0, sigma_lift_f=8.0),
    5:  ResidualParams(mu_lift_f=17.0, sigma_lift_f=7.5),
    6:  ResidualParams(mu_lift_f=17.0, sigma_lift_f=7.0),
    7:  ResidualParams(mu_lift_f=15.0, sigma_lift_f=6.5),
    8:  ResidualParams(mu_lift_f=12.0, sigma_lift_f=6.0),
    9:  ResidualParams(mu_lift_f=9.0,  sigma_lift_f=5.0),
    10: ResidualParams(mu_lift_f=6.0,  sigma_lift_f=4.0),
    11: ResidualParams(mu_lift_f=4.0,  sigma_lift_f=3.5),
    12: ResidualParams(mu_lift_f=3.0,  sigma_lift_f=3.0),
    13: ResidualParams(mu_lift_f=2.0,  sigma_lift_f=2.5),
    14: ResidualParams(mu_lift_f=1.5,  sigma_lift_f=2.0),
    15: ResidualParams(mu_lift_f=1.0,  sigma_lift_f=1.8),
    16: ResidualParams(mu_lift_f=0.5,  sigma_lift_f=1.5),
    17: ResidualParams(mu_lift_f=0.3,  sigma_lift_f=1.2),
    18: ResidualParams(mu_lift_f=0.2,  sigma_lift_f=1.0),
    19: ResidualParams(mu_lift_f=0.1,  sigma_lift_f=0.8),
    20: ResidualParams(mu_lift_f=0.1,  sigma_lift_f=0.6),
    21: ResidualParams(mu_lift_f=0.1,  sigma_lift_f=0.5),
    22: ResidualParams(mu_lift_f=0.1,  sigma_lift_f=0.4),
    23: ResidualParams(mu_lift_f=0.0,  sigma_lift_f=0.3),
}


def residual_at_hour(hour_local: int) -> ResidualParams:
    """Return parametric (default) residual parameters for hour-of-day."""
    h = max(0, min(23, int(hour_local)))
    return DEFAULT_RESIDUAL_TABLE[h]


# --- Empirical climatology ---------------------------------------------------


CLIMO_DATA_DIR = Path(__file__).resolve().parent.parent / "climo_data"
"""Default location for fitted climatology JSON files."""

MIN_SAMPLES_PER_CELL = 10
"""Minimum residual samples per (month, hour) cell before trusting the fit.
Cells with fewer samples fall back to the parametric defaults."""


@dataclass(frozen=True)
class EmpiricalClimatology:
    """Empirically-fit residual data for one city.

    Stored as a 12 (months) × 24 (hours) grid. Each cell contains both:
      - Gaussian-fit ``ResidualParams`` (mu, sigma) for the v1/v2 path
      - Sorted residual samples for the v3 empirical-CDF path

    Cells with insufficient samples (n < MIN_SAMPLES_PER_CELL) hold ``None``
    and the lookup falls back to the parametric defaults.
    """

    city_code: str
    fit_window: str
    n_days: int
    model_version: int  # 2 = Gaussian-only, 3 = also includes residual samples
    table: tuple[tuple[Optional[ResidualParams], ...], ...]  # [month-1][hour]
    samples_table: tuple[tuple[Optional[tuple[float, ...]], ...], ...]  # sorted

    def residual_at(self, month: int, hour_local: int) -> ResidualParams:
        """Look up Gaussian residual for (month, hour) with parametric fallback."""
        m = max(1, min(12, int(month)))
        h = max(0, min(23, int(hour_local)))
        cell = self.table[m - 1][h]
        if cell is None:
            return residual_at_hour(h)
        return cell

    def samples_at(self, month: int, hour_local: int) -> Optional[tuple[float, ...]]:
        """Look up sorted residual samples for (month, hour).

        Returns ``None`` if the climo file pre-dates v3 or the cell has
        too few samples. The runner falls back to Gaussian in that case.
        """
        m = max(1, min(12, int(month)))
        h = max(0, min(23, int(hour_local)))
        return self.samples_table[m - 1][h]

    @classmethod
    def from_json_file(cls, path: Path) -> "EmpiricalClimatology":
        with path.open("r") as f:
            data = json.load(f)
        table_raw = data["by_month_hour"]
        model_version = int(data.get("model_version", 2))
        table: list[tuple[Optional[ResidualParams], ...]] = []
        samples_table: list[tuple[Optional[tuple[float, ...]], ...]] = []
        for m in range(1, 13):
            params_row: list[Optional[ResidualParams]] = []
            samples_row: list[Optional[tuple[float, ...]]] = []
            month_data = table_raw.get(str(m), {})
            for h in range(24):
                cell = month_data.get(str(h))
                if cell is None or cell.get("n", 0) < MIN_SAMPLES_PER_CELL:
                    params_row.append(None)
                    samples_row.append(None)
                    continue
                params_row.append(ResidualParams(
                    mu_lift_f=float(cell["mu_lift_f"]),
                    sigma_lift_f=float(cell["sigma_lift_f"]),
                ))
                raw_samples = cell.get("residuals")
                if isinstance(raw_samples, list) and len(raw_samples) >= MIN_SAMPLES_PER_CELL:
                    samples_row.append(tuple(sorted(float(x) for x in raw_samples)))
                else:
                    samples_row.append(None)
            table.append(tuple(params_row))
            samples_table.append(tuple(samples_row))
        return cls(
            city_code=data.get("city_code", ""),
            fit_window=data.get("fit_window", ""),
            n_days=int(data.get("n_days", 0)),
            model_version=model_version,
            table=tuple(table),
            samples_table=tuple(samples_table),
        )

    @classmethod
    def for_city(cls, city_code: str, base_dir: Path = CLIMO_DATA_DIR) -> Optional["EmpiricalClimatology"]:
        """Load ``climo_data/<CITY>.json`` if it exists; return None otherwise."""
        path = base_dir / f"{city_code.upper()}.json"
        if not path.exists():
            return None
        return cls.from_json_file(path)

    def coverage(self) -> tuple[int, int]:
        """Return ``(gaussian_filled, total)`` — for sanity reporting."""
        filled = sum(1 for row in self.table for cell in row if cell is not None)
        return filled, 12 * 24

    def samples_coverage(self) -> tuple[int, int]:
        """Return ``(empirical_filled, total)`` — how many cells have sample arrays."""
        filled = sum(1 for row in self.samples_table for cell in row if cell is not None)
        return filled, 12 * 24
