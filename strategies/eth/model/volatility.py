"""Realized hourly volatility estimator.

v1: trailing-window standard deviation of hourly log returns. Simple,
robust, no parameters beyond window length.

Future variants (v2):
    - EWMA / RiskMetrics with α≈0.94 daily ≈ 0.06 hourly
    - GARCH(1,1) for vol clustering
    - Implied vol scraped from options markets (when available)

The output is per-hour σ. Scaling to other horizons is the caller's
responsibility (multiply by sqrt(Δt_hours) for a Δt-hour forecast).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class VolEstimate:
    """One realized-vol observation."""

    sigma_hourly: float   # std-dev of 1-hour log returns
    n_returns: int        # how many returns went into the estimate

    def sigma_for_horizon(self, hours: float) -> float:
        """Volatility scaled for an ``hours``-hour horizon."""
        return self.sigma_hourly * math.sqrt(max(0.0, hours))


def realized_vol(log_returns: Sequence[float]) -> VolEstimate:
    """Sample standard deviation (n−1 denominator) of the log-return series.

    Returns ``sigma_hourly=0`` if fewer than 2 returns are supplied.
    """
    n = len(log_returns)
    if n < 2:
        return VolEstimate(sigma_hourly=0.0, n_returns=n)
    mean = sum(log_returns) / n
    var = sum((r - mean) ** 2 for r in log_returns) / (n - 1)
    return VolEstimate(sigma_hourly=math.sqrt(max(0.0, var)), n_returns=n)


def trailing_realized_vol(
    log_returns: Sequence[float],
    window: int,
) -> VolEstimate:
    """Realized vol over the last ``window`` returns. ``window=0`` ⇒ all."""
    if window <= 0 or window >= len(log_returns):
        return realized_vol(log_returns)
    return realized_vol(log_returns[-window:])
