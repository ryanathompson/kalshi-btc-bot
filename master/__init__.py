"""Master layer: cross-strategy rules and the Strategy interface contract.

Strategies live under ``strategies/<name>/``. They depend on this module but
NEVER on each other. See master/README.md for the full architectural rationale.
"""

from master.interfaces import (
    Strategy,
    MarketSnapshot,
    OrderIntent,
    Side,
    StrategyContext,
)
from master.rules import GlobalRules, kalshi_taker_fee, kalshi_maker_fee

__all__ = [
    "Strategy",
    "MarketSnapshot",
    "OrderIntent",
    "Side",
    "StrategyContext",
    "GlobalRules",
    "kalshi_taker_fee",
    "kalshi_maker_fee",
]
