"""Daily-high posterior estimator.

Two implementations of the same interface:

- :class:`GaussianPosterior` (v1/v2): models future_lift as Normal(mu, sigma).
  Used when only ``ResidualParams`` are available.
- :class:`EmpiricalPosterior` (v3): uses the empirical CDF of historical
  residuals directly. No distributional assumption — captures right-skew,
  heavy tails, and the point mass at zero automatically.

Both expose the same methods (``cdf``, ``sf``, ``prob_between``,
``prob_greater_than``, ``prob_less_than``) so callers don't need to care
which one they got.

Common model:

    daily_high = max(obs_max, obs_max + future_lift)
              = obs_max + max(0, future_lift)
    future_lift ~ <Gaussian or empirical>

So the CDF of the daily high is:

    P(daily_high <= T) =
        P(future_lift <= T - obs_max)  if T >= obs_max
        0                               if T <  obs_max
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Optional, Sequence, Union

from strategies.weather.model.climo import ResidualParams, residual_at_hour


def _normal_cdf(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


class _PosteriorMixin:
    """Shared bracket-probability helpers built on top of ``cdf()``.

    Subclasses must define ``obs_max: float`` and ``cdf(t: float) -> float``.
    """

    obs_max: float

    def cdf(self, t: float) -> float:  # pragma: no cover - overridden
        raise NotImplementedError

    def sf(self, t: float) -> float:
        return 1.0 - self.cdf(t)

    def prob_between(self, lo: float, hi: float) -> float:
        if hi < lo:
            return 0.0
        return max(0.0, self.cdf(hi + 0.5) - self.cdf(lo - 0.5))

    def prob_greater_than(self, threshold: float) -> float:
        return self.sf(threshold + 0.5)

    def prob_less_than(self, threshold: float) -> float:
        return self.cdf(threshold - 0.5)


@dataclass(frozen=True)
class GaussianPosterior(_PosteriorMixin):
    """v1/v2 Gaussian-residual posterior.

    ``daily_high = obs_max + max(0, X)`` where ``X ~ Normal(mu_lift, sigma_lift)``.
    """

    obs_max: float
    mu_lift: float
    sigma_lift: float

    def cdf(self, t: float) -> float:
        if t < self.obs_max:
            return 0.0
        if self.sigma_lift <= 1e-9:
            return 1.0 if t >= self.obs_max + max(0.0, self.mu_lift) else 0.0
        z = (t - self.obs_max - self.mu_lift) / self.sigma_lift
        return _normal_cdf(z)


# Backwards-compat alias for older imports: ``Posterior`` was the
# Gaussian-only class in v1/v2.
Posterior = GaussianPosterior


@dataclass(frozen=True)
class EmpiricalPosterior(_PosteriorMixin):
    """v3 empirical-CDF posterior.

    Directly uses the historical residuals `(daily_high - obs_max_through_h)`
    for the matching `(city, month, hour)` cell, queried via empirical CDF.
    No distributional assumption.

    The samples MUST be sorted ascending — the constructor doesn't sort,
    so callers (or the climo loader) sort once at load time.
    """

    obs_max: float
    sorted_residuals: tuple[float, ...]

    def cdf(self, t: float) -> float:
        if t < self.obs_max:
            return 0.0
        n = len(self.sorted_residuals)
        if n == 0:
            return 1.0
        x = t - self.obs_max
        if x < 0:
            return 0.0
        # P(future_lift <= x) = (count of residuals <= x) / n
        idx = bisect_right(self.sorted_residuals, x)
        return idx / n


PosteriorAny = Union[GaussianPosterior, EmpiricalPosterior]


def daily_high_posterior(
    obs_max_so_far: Optional[float],
    hour_local: int,
    residual: Optional[ResidualParams] = None,
    samples: Optional[Sequence[float]] = None,
) -> PosteriorAny:
    """Build the posterior given observations through hour H of local day.

    Resolution rules:
      - If ``samples`` is provided and non-empty → :class:`EmpiricalPosterior` (v3).
      - Else if ``residual`` is provided → :class:`GaussianPosterior` (v2).
      - Else → :class:`GaussianPosterior` with parametric defaults (v1).

    Args:
        obs_max_so_far: max °F observed in the day so far. ``None`` means
            no observations yet — we fall back to a wide prior centered on
            a season-typical value (defensive; backtester only invokes
            this at hour >= 1 in practice).
        hour_local: hour 0..23 in the station's local timezone.
        residual: Gaussian residual params; ignored when ``samples`` is given.
        samples: empirical residual samples for the matching (month, hour)
            cell. The function sorts them — callers don't need to.
    """
    if samples is not None and len(samples) > 0:
        if obs_max_so_far is None:
            obs_max_so_far = 60.0  # defensive; same as Gaussian fallback
        return EmpiricalPosterior(
            obs_max=obs_max_so_far,
            sorted_residuals=tuple(sorted(samples)),
        )
    # Gaussian path
    if residual is None:
        residual = residual_at_hour(hour_local)
    if obs_max_so_far is None:
        return GaussianPosterior(
            obs_max=60.0,
            mu_lift=residual.mu_lift_f,
            sigma_lift=residual.sigma_lift_f * 1.5,
        )
    return GaussianPosterior(
        obs_max=obs_max_so_far,
        mu_lift=residual.mu_lift_f,
        sigma_lift=residual.sigma_lift_f,
    )


def bracket_probability(
    posterior: PosteriorAny,
    floor_strike: Optional[float],
    cap_strike: Optional[float],
    strike_type: str,
) -> float:
    """Translate a Kalshi bracket market into a probability under the posterior.

    Mirrors the ``floor_strike`` / ``cap_strike`` / ``strike_type`` schema
    returned by the Kalshi public-markets endpoint.
    """
    if strike_type == "greater":
        if floor_strike is None:
            raise ValueError("strike_type=greater requires floor_strike")
        return posterior.prob_greater_than(floor_strike)
    if strike_type == "less":
        if cap_strike is None:
            raise ValueError("strike_type=less requires cap_strike")
        return posterior.prob_less_than(cap_strike)
    if strike_type == "between":
        if floor_strike is None or cap_strike is None:
            raise ValueError("strike_type=between requires both strikes")
        return posterior.prob_between(floor_strike, cap_strike)
    raise ValueError(f"unknown strike_type: {strike_type!r}")
