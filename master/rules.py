"""Global rules that apply to every strategy.

These are the constraints that exist for the whole bot regardless of
which strategy generated an order: total bankroll cap, fee math,
kill switch, blacklists. Strategy-specific rules live in
``strategies/<name>/`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from master.interfaces import OrderIntent


# --- Kalshi fee math (same formula for everyone) ----------------------------


def kalshi_taker_fee(price: float, contracts: int) -> float:
    """Per-contract Kalshi taker fee.

    Formula: 7¢ × C × (1 − C) where C is the contract price in [0.01, 0.99].
    Returns total fee in dollars for ``contracts`` contracts at ``price``.

    Verified from Kalshi public fee schedule, 2026-04.
    """
    if contracts <= 0:
        return 0.0
    p = max(0.01, min(0.99, price))
    per_contract = 0.07 * p * (1.0 - p)
    # Kalshi rounds up to nearest cent per contract
    per_contract_cents = int(per_contract * 100 + 0.9999)
    return per_contract_cents * contracts / 100.0


def kalshi_maker_fee(price: float, contracts: int) -> float:
    """Per-contract Kalshi maker fee — 25% of taker."""
    return kalshi_taker_fee(price, contracts) * 0.25


# --- Global rules -----------------------------------------------------------


@dataclass
class GlobalRules:
    """Configuration for cross-strategy guardrails.

    These bound what any strategy can do regardless of its own logic.
    Strategies receive a per-strategy bankroll already adjusted by these
    rules in the :class:`StrategyContext`; they should not need to
    re-implement these checks.
    """

    total_bankroll: float = 5000.0
    """Hard cap on aggregate USD at risk across all strategies."""

    per_strategy_bankroll: Mapping[str, float] = field(default_factory=dict)
    """Allocation per strategy. Sum should be <= total_bankroll."""

    max_contracts_per_order: int = 500
    """Cap on a single order's size, regardless of strategy."""

    max_position_per_market: int = 2000
    """Cap on net position in any one Kalshi market."""

    blacklisted_series: frozenset = field(default_factory=frozenset)
    """Series tickers no strategy may trade. Empty by default."""

    kill_switch: bool = False
    """If True, master rejects ALL orders. Toggled externally during incidents."""

    min_edge_cents: float = 1.0
    """Minimum edge (fair_value vs market) required before placing an order, in cents."""

    def validate(self, intent: OrderIntent, current_position: int = 0) -> tuple[bool, str]:
        """Apply global rules to a candidate order. Returns (ok, reason_if_blocked)."""
        if self.kill_switch:
            return False, "global kill switch engaged"

        series = intent.ticker.split("-", 1)[0] if "-" in intent.ticker else intent.ticker
        if series in self.blacklisted_series:
            return False, f"series {series} is blacklisted"

        if abs(intent.desired_position) > self.max_contracts_per_order:
            return False, f"order size {intent.desired_position} exceeds max_contracts_per_order={self.max_contracts_per_order}"

        projected = current_position + intent.desired_position
        if abs(projected) > self.max_position_per_market:
            return False, f"projected position {projected} exceeds max_position_per_market={self.max_position_per_market}"

        if intent.limit_price is not None and not (0.01 <= intent.limit_price <= 0.99):
            return False, f"limit_price {intent.limit_price} outside [0.01, 0.99]"

        return True, ""

    def filter_intents(
        self,
        intents: Iterable[OrderIntent],
        positions: Mapping[str, int],
    ) -> tuple[list[OrderIntent], list[tuple[OrderIntent, str]]]:
        """Partition intents into (accepted, rejected_with_reason)."""
        accepted = []
        rejected = []
        for intent in intents:
            ok, reason = self.validate(intent, positions.get(intent.ticker, 0))
            if ok:
                accepted.append(intent)
            else:
                rejected.append((intent, reason))
        return accepted, rejected
