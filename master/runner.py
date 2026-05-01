"""Strategy orchestrator (stub).

Future home for the live runner that loads strategies, fans out
:class:`MarketSnapshot` data, applies global rules, and routes orders
to Kalshi. For now this exists only as an interface placeholder so
strategies can be developed against a known shape.

The existing live BTC bot (``bot.py``) does NOT use this orchestrator —
it remains a closed legacy system on Render until explicitly migrated
into ``strategies/btc/``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Mapping

from master.interfaces import (
    MarketSnapshot,
    OrderIntent,
    Strategy,
    StrategyContext,
)
from master.rules import GlobalRules


class Orchestrator:
    """Loads strategies and runs one tick at a time.

    Not yet wired to live Kalshi — for now used only by backtest
    harnesses and unit tests.
    """

    def __init__(self, rules: GlobalRules, strategies: Iterable[Strategy]) -> None:
        self.rules = rules
        self.strategies = list(strategies)
        # Sanity: enforce no-import-across-strategies at runtime by checking
        # each strategy's name is unique.
        names = [s.name for s in self.strategies]
        if len(names) != len(set(names)):
            raise ValueError(f"Duplicate strategy names: {names}")

    def tick(
        self,
        snapshots_by_strategy: Mapping[str, Iterable[MarketSnapshot]],
        positions: Mapping[str, Mapping[str, int]],
        now: datetime | None = None,
    ) -> tuple[list[OrderIntent], list[tuple[OrderIntent, str]]]:
        """Run one orchestration tick.

        Args:
            snapshots_by_strategy: market snapshots keyed by strategy name
            positions: current positions, ``{strategy_name: {ticker: qty}}``
            now: timestamp for this tick (UTC); defaults to wall-clock

        Returns:
            (accepted_intents, rejected_intents_with_reason)
        """
        if now is None:
            now = datetime.now(timezone.utc)

        all_accepted: list[OrderIntent] = []
        all_rejected: list[tuple[OrderIntent, str]] = []

        for strategy in self.strategies:
            ctx = StrategyContext(
                now=now,
                strategy_bankroll=self.rules.per_strategy_bankroll.get(
                    strategy.name, self.rules.total_bankroll / max(1, len(self.strategies))
                ),
                open_positions=positions.get(strategy.name, {}),
                is_backtest=False,
            )
            snaps = list(snapshots_by_strategy.get(strategy.name, []))
            intents = list(strategy.desired_intents(snaps, ctx))
            accepted, rejected = self.rules.filter_intents(
                intents, positions.get(strategy.name, {})
            )
            all_accepted.extend(accepted)
            all_rejected.extend(rejected)

        return all_accepted, all_rejected
