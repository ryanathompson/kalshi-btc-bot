"""WeatherStrategy — implements ``master.interfaces.Strategy``.

Live-trading wiring is intentionally minimal in v1; the heavy lifting
is the model and the backtester. This file exists to satisfy the
master interface contract and to make integration tests possible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Optional

from master.interfaces import (
    MarketSnapshot,
    OrderIntent,
    Side,
    Strategy,
    StrategyContext,
)
from strategies.weather.config import CITIES, CityConfig
from strategies.weather.model.posterior import (
    Posterior,
    bracket_probability,
    daily_high_posterior,
)


class WeatherStrategy:
    """KXHIGH daily-high temperature strategy.

    Each tick, the orchestrator hands us :class:`MarketSnapshot`s for
    KXHIGH series we care about plus the current observed-max-so-far
    per city (injected externally — see ``set_observation``). We compute
    bracket fair values from the posterior model and emit
    :class:`OrderIntent`s where ``|fair - mid| > min_edge``.
    """

    name: str = "weather"

    def __init__(
        self,
        cities: Optional[list[str]] = None,
        min_edge: float = 0.04,
    ) -> None:
        self.cities: list[CityConfig] = [
            CITIES[c] for c in (cities or list(CITIES.keys()))
        ]
        self.min_edge = min_edge
        # Per-city most-recent (obs_max_so_far, local_hour) — populated
        # externally by the orchestrator (via the data adapter) and read
        # here when computing fair value.
        self._city_state: dict[str, tuple[float, int]] = {}

    # --- Strategy protocol ---------------------------------------------------

    def list_target_series(self) -> Iterable[str]:
        return [c.kalshi_series for c in self.cities]

    def fair_value(
        self,
        snapshot: MarketSnapshot,
        ctx: StrategyContext,
    ) -> Optional[float]:
        city = self._city_for_series(snapshot.series_ticker)
        if city is None:
            return None
        state = self._city_state.get(city.code)
        if state is None:
            return None
        obs_max, hour_local = state
        post = daily_high_posterior(obs_max, hour_local)
        try:
            return bracket_probability(
                post,
                floor_strike=snapshot.floor_strike,
                cap_strike=snapshot.cap_strike,
                strike_type=snapshot.strike_type or "",
            )
        except ValueError:
            return None

    def desired_intents(
        self,
        snapshots: Iterable[MarketSnapshot],
        ctx: StrategyContext,
    ) -> Iterable[OrderIntent]:
        for snap in snapshots:
            fv = self.fair_value(snap, ctx)
            if fv is None:
                continue
            mid = 0.5 * (snap.yes_bid + snap.yes_ask)
            if mid <= 0.0:
                continue
            edge = fv - mid
            if abs(edge) < self.min_edge:
                continue
            # Direction: long YES if fair > mid, long NO if fair < mid
            side = Side.YES if edge > 0 else Side.NO
            # Sizing: small fixed-cap for v1, refined later
            size = 5
            yield OrderIntent(
                strategy_name=self.name,
                ticker=snap.ticker,
                side=side,
                desired_position=size if side == Side.YES else -size,
                limit_price=snap.yes_bid if side == Side.YES else 1.0 - snap.yes_ask,
                post_only=True,
                reason=f"edge={edge:+.3f} fv={fv:.3f} mid={mid:.3f}",
                confidence=min(1.0, abs(edge) / 0.20),
            )

    # --- helpers used by the orchestrator/data layer ------------------------

    def set_observation(self, city_code: str, obs_max_f: float, hour_local: int) -> None:
        """Inject the latest (obs_max_so_far, local_hour) for a city.

        The data adapter calls this on each tick before the orchestrator
        invokes ``desired_intents``. Kept as a separate method so the
        strategy itself stays oblivious to data-fetching mechanics.
        """
        self._city_state[city_code] = (obs_max_f, hour_local)

    def _city_for_series(self, series_ticker: str) -> Optional[CityConfig]:
        for c in self.cities:
            if c.kalshi_series == series_ticker:
                return c
        return None


# Conformance check (raises at import time if the protocol drifts)
_check: Strategy = WeatherStrategy()
