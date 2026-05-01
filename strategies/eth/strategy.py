"""EthStrategy — implements ``master.interfaces.Strategy``.

Live-trading wiring is minimal in v1; the heavy lifting is the
posterior model and the backtester. This file exists to satisfy the
master interface contract.
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
from strategies.eth.config import ETH, CryptoMarketConfig
from strategies.eth.model.posterior import (
    GBMPosterior,
    bracket_probability,
)


class EthStrategy:
    """KXETHD ETH-price greater-than-strike strategy.

    Each tick the orchestrator gives us :class:`MarketSnapshot`s for the
    KXETHD series plus the most-recent (spot, sigma_hourly,
    hours_to_settle) injected via :meth:`set_market_state`. We compute
    fair values from the GBM posterior and emit orders where edge
    exceeds ``min_edge``.
    """

    name: str = "eth"

    def __init__(
        self,
        markets: Optional[list[CryptoMarketConfig]] = None,
        min_edge: float = 0.04,
    ) -> None:
        self.markets: list[CryptoMarketConfig] = markets or [ETH]
        self.min_edge = min_edge
        # Per-series state injected externally:
        # series_ticker -> (spot_price, sigma_hourly, hours_to_settle, mu_hourly)
        self._state: dict[str, tuple[float, float, float, float]] = {}

    # --- Strategy protocol ---------------------------------------------------

    def list_target_series(self) -> Iterable[str]:
        return [m.kalshi_series for m in self.markets]

    def fair_value(
        self,
        snapshot: MarketSnapshot,
        ctx: StrategyContext,
    ) -> Optional[float]:
        state = self._state.get(snapshot.series_ticker)
        if state is None:
            return None
        spot, sigma_hourly, hours_to_settle, mu_hourly = state
        if sigma_hourly <= 0 or hours_to_settle <= 0 or spot <= 0:
            return None
        post = GBMPosterior(
            spot=spot,
            sigma_hourly=sigma_hourly,
            hours_to_settle=hours_to_settle,
            mu_hourly=mu_hourly,
        )
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
            side = Side.YES if edge > 0 else Side.NO
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

    def set_market_state(
        self,
        series_ticker: str,
        *,
        spot: float,
        sigma_hourly: float,
        hours_to_settle: float,
        mu_hourly: float = 0.0,
    ) -> None:
        """Inject the latest reference state for a series.

        Called by the data adapter on each tick, before the orchestrator
        invokes :meth:`desired_intents`.
        """
        self._state[series_ticker] = (spot, sigma_hourly, hours_to_settle, mu_hourly)


# Conformance check (raises at import time if the protocol drifts)
_check: Strategy = EthStrategy()
