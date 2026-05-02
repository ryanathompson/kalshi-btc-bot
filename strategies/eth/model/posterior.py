"""Posterior over future ETH price.

Two implementations of the same interface:

- :class:`GBMPosterior` (v1): lognormal — assumes Gaussian log returns.
- :class:`EmpiricalReturnPosterior` (v2): uses the empirical CDF of
  historical log returns directly. No distributional assumption;
  captures fat tails automatically.

Both expose ``cdf``, ``sf``, ``prob_greater_than``, ``prob_less_than``,
``prob_between`` so callers don't care which one they got.

Common model:

    P_T = P_t * exp(R)
    R ~ <Gaussian or empirical> over the horizon

Kalshi's KXETHD ``greater``-strike fair value is ``1 − CDF(strike)``.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass
from typing import Optional, Sequence, Union


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


class _EthPosteriorMixin:
    """Shared helpers built on top of ``cdf()``."""

    spot: float

    def cdf(self, x: float) -> float:  # pragma: no cover - overridden
        raise NotImplementedError

    def sf(self, x: float) -> float:
        return 1.0 - self.cdf(x)

    def prob_greater_than(self, strike: float) -> float:
        """For Kalshi ``greater``-strike markets: P(P_T > strike)."""
        return self.sf(strike)

    def prob_less_than(self, strike: float) -> float:
        return self.cdf(strike)

    def prob_between(self, lo: float, hi: float) -> float:
        if hi < lo:
            return 0.0
        return max(0.0, self.cdf(hi) - self.cdf(lo))


@dataclass(frozen=True)
class GBMPosterior(_EthPosteriorMixin):
    """v1 lognormal posterior. ``log(P_T/P_t) ~ Normal(mu*dt, sigma^2*dt)``."""

    spot: float           # current price P_t
    sigma_hourly: float   # σ_h, per-hour log-return std-dev
    hours_to_settle: float
    mu_hourly: float = 0.0

    @property
    def total_sigma(self) -> float:
        return self.sigma_hourly * math.sqrt(max(0.0, self.hours_to_settle))

    @property
    def total_drift(self) -> float:
        return self.mu_hourly * self.hours_to_settle

    def cdf(self, x: float) -> float:
        if x <= 0 or self.spot <= 0:
            return 0.0
        if self.total_sigma <= 1e-12:
            expected = self.spot * math.exp(self.total_drift)
            return 1.0 if x >= expected else 0.0
        z = (math.log(x / self.spot) - self.total_drift) / self.total_sigma
        return _normal_cdf(z)


@dataclass(frozen=True)
class EmpiricalReturnPosterior(_EthPosteriorMixin):
    """v2 empirical-CDF posterior over horizon-h log returns.

    The samples MUST be sorted ascending — the constructor doesn't
    sort. Callers (or :func:`make_eth_posterior`) sort once at build time.
    """

    spot: float
    sorted_log_returns: tuple[float, ...]

    def cdf(self, x: float) -> float:
        if x <= 0 or self.spot <= 0:
            return 0.0
        n = len(self.sorted_log_returns)
        if n == 0:
            return 0.5  # uninformative
        target = math.log(x / self.spot)
        idx = bisect_right(self.sorted_log_returns, target)
        return idx / n


PosteriorAny = Union[GBMPosterior, EmpiricalReturnPosterior]


def horizon_log_returns(hourly_log_returns: Sequence[float], hours: int) -> list[float]:
    """Build cumulative h-hour log returns from a hourly-return series.

    For ``hours=1`` returns the input as a list. For ``hours>1`` returns
    overlapping windowed sums (length = n - hours + 1), which is the
    standard way to convert hourly returns to multi-hour returns when
    you want maximum samples per fit window.

    Overlapping samples are autocorrelated, but the empirical CDF stays
    valid as a frequency estimator.
    """
    if hours <= 1:
        return list(hourly_log_returns)
    n = len(hourly_log_returns)
    if n < hours:
        return []
    cum = [0.0]
    for r in hourly_log_returns:
        cum.append(cum[-1] + r)
    return [cum[i + hours] - cum[i] for i in range(n - hours + 1)]


def make_eth_posterior(
    spot: float,
    *,
    sigma_hourly: Optional[float] = None,
    hourly_log_returns: Optional[Sequence[float]] = None,
    hours_to_settle: float = 1.0,
    mu_hourly: float = 0.0,
    min_empirical_samples: int = 24,
) -> PosteriorAny:
    """Build the posterior. Empirical when samples are sufficient, GBM otherwise.

    Resolution rules:
      - If ``hourly_log_returns`` has at least ``min_empirical_samples`` after
        horizon scaling, return :class:`EmpiricalReturnPosterior` (v2).
      - Else if ``sigma_hourly`` is provided, return :class:`GBMPosterior` (v1).
      - Else raise ``ValueError`` — caller must give one or the other.

    ``hours_to_settle`` is used to scale empirical returns to the right
    horizon and to scale GBM σ.
    """
    if hourly_log_returns is not None:
        scaled = horizon_log_returns(list(hourly_log_returns), max(1, int(round(hours_to_settle))))
        if len(scaled) >= min_empirical_samples:
            return EmpiricalReturnPosterior(
                spot=spot,
                sorted_log_returns=tuple(sorted(scaled)),
            )
    if sigma_hourly is None:
        raise ValueError(
            "make_eth_posterior needs either hourly_log_returns (>= "
            f"{min_empirical_samples} samples after horizon scaling) "
            "or sigma_hourly"
        )
    return GBMPosterior(
        spot=spot,
        sigma_hourly=sigma_hourly,
        hours_to_settle=hours_to_settle,
        mu_hourly=mu_hourly,
    )


def bracket_probability(
    posterior: PosteriorAny,
    floor_strike: Optional[float],
    cap_strike: Optional[float],
    strike_type: str,
) -> float:
    """Translate a Kalshi market into a probability under the posterior."""
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
