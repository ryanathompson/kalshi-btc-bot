"""Strategy interface contract.

Every module under ``strategies/<name>/`` exposes a class implementing
:class:`Strategy`. The orchestrator in ``master/runner.py`` (TBD) loads
all enabled strategies, gives each a :class:`MarketSnapshot` for the
markets it cares about, and collects :class:`OrderIntent`s back. Global
rules (bankroll cap, fee math, kill switch) are applied by master before
any order reaches Kalshi.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Iterable, Mapping, Optional, Protocol, runtime_checkable


class Side(str, Enum):
    YES = "yes"
    NO = "no"


@dataclass(frozen=True)
class MarketSnapshot:
    """Point-in-time view of a single Kalshi market.

    The fields mirror what the public Kalshi API returns from
    ``GET /trade-api/v2/markets``. Strategies receive these via the
    orchestrator; they should not call Kalshi directly for live data.
    """

    ticker: str               # e.g. "KXHIGHNY-26MAY01-T62"
    event_ticker: str         # e.g. "KXHIGHNY-26MAY01"
    series_ticker: str        # e.g. "KXHIGHNY"
    yes_bid: float            # 0..1, dollars
    yes_ask: float            # 0..1, dollars
    yes_bid_size: float
    yes_ask_size: float
    last_price: float
    volume_24h: float
    open_interest: float
    close_time: datetime      # UTC
    # Strike metadata — populated for bracket markets
    floor_strike: Optional[float] = None
    cap_strike: Optional[float] = None
    strike_type: Optional[str] = None  # "greater" | "less" | "between"
    title: str = ""
    raw: Mapping[str, object] = field(default_factory=dict)  # full API payload for escape hatches


@dataclass(frozen=True)
class OrderIntent:
    """A strategy's request to take or post a position.

    The orchestrator validates against global rules and may reject,
    resize, or rate-limit before submitting to Kalshi.
    """

    strategy_name: str
    ticker: str
    side: Side
    desired_position: int     # signed; positive = long YES (or NO if side=NO), negative = flatten
    limit_price: Optional[float] = None  # None = market order, else 0..1 dollars
    post_only: bool = True    # default to maker for fee minimization
    reason: str = ""          # free-form, for logging/auditing
    confidence: float = 0.0   # 0..1, used by risk for sizing decisions


@dataclass(frozen=True)
class StrategyContext:
    """Runtime context handed to a strategy on each tick.

    Includes wall-clock time (so strategies can be deterministic in
    backtests) and per-strategy bankroll already adjusted by master rules.
    """

    now: datetime              # UTC; replayable in backtests
    strategy_bankroll: float   # USD allocated to this strategy after global caps
    open_positions: Mapping[str, int]  # ticker -> signed contracts
    is_backtest: bool = False


@runtime_checkable
class Strategy(Protocol):
    """The contract every strategy implements."""

    name: str  # short id, e.g. "weather", "btc15m"

    def list_target_series(self) -> Iterable[str]:
        """Return the Kalshi series tickers this strategy cares about.

        Master uses this to decide which markets to subscribe to and
        snapshot for the strategy.
        """
        ...

    def fair_value(self, snapshot: MarketSnapshot, ctx: StrategyContext) -> Optional[float]:
        """Return the strategy's estimated fair-value probability for YES.

        Returning ``None`` means "no opinion right now" — master will not
        generate orders for this market on this tick.
        """
        ...

    def desired_intents(
        self,
        snapshots: Iterable[MarketSnapshot],
        ctx: StrategyContext,
    ) -> Iterable[OrderIntent]:
        """Yield :class:`OrderIntent` objects for the current tick.

        Master applies global rules afterward — strategies should size
        based on per-strategy bankroll in ``ctx`` but do not need to
        re-implement global caps.
        """
        ...
