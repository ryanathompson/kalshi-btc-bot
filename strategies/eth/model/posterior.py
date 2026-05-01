"""GBM (geometric Brownian motion) posterior over future ETH price.

Model:

    log(P_T / P_t) ~ Normal(mu * dt, sigma^2 * dt)

where ``dt`` is in hours, ``sigma`` is per-hour volatility (from
:mod:`strategies.eth.model.volatility`), and ``mu`` is per-hour drift.
For short horizons (≤ 24h) drift is effectively zero — we accept it as
a parameter but default to 0.

The posterior is log-normal in price space. CDF:

    P(P_T <= x) = Phi( (log(x/P_t) - mu*dt) / (sigma * sqrt(dt)) )

For Kalshi KXETHD's ``greater_than`` markets, fair value is ``1 − CDF(strike)``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass(frozen=True)
class GBMPosterior:
    """Log-normal posterior over the price at settlement.

    Constructed from spot, per-hour volatility, hours to settlement, and
    optional drift.
    """

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
        """``P(P_T <= x)`` under log-normal."""
        if x <= 0 or self.spot <= 0:
            return 0.0
        if self.total_sigma <= 1e-12:
            # Vanishing vol → degenerate point mass at expected price
            expected = self.spot * math.exp(self.total_drift)
            return 1.0 if x >= expected else 0.0
        z = (math.log(x / self.spot) - self.total_drift) / self.total_sigma
        return _normal_cdf(z)

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


def bracket_probability(
    posterior: GBMPosterior,
    floor_strike: Optional[float],
    cap_strike: Optional[float],
    strike_type: str,
) -> float:
    """Translate a Kalshi market into a probability under the posterior.

    Mirrors the public-API ``floor_strike``/``cap_strike``/``strike_type``
    schema. KXETHD only uses ``greater``, but we support all three so
    other crypto-price series (KXBTCD etc.) can plug in unchanged.
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
