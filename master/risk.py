"""Cross-strategy risk aggregation (stub).

Future home for things that span multiple strategies — e.g., aggregate
crypto position cap (so BTC + ETH + SOL combined exposure is bounded
even if each strategy stays within its individual cap).

Kept as a separate module from :mod:`master.rules` because these are
*relational* concerns between strategies, not just per-order rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass
class CorrelationGroup:
    """A set of strategies whose exposures should aggregate.

    Example: ``CorrelationGroup(name="crypto", members={"btc15m", "eth", "sol"}, cap_usd=3000.0)``
    means combined notional across those three strategies must stay
    under $3k regardless of per-strategy caps.
    """

    name: str
    members: frozenset[str]
    cap_usd: float


def aggregate_exposure(
    positions: Mapping[str, Mapping[str, int]],
    last_prices: Mapping[str, float],
) -> Mapping[str, float]:
    """Compute USD exposure per strategy.

    Args:
        positions: ``{strategy_name: {ticker: signed_contracts}}``
        last_prices: ``{ticker: last_price_in_dollars}``

    Returns:
        ``{strategy_name: usd_at_risk}``
    """
    out: dict[str, float] = {}
    for strat, by_ticker in positions.items():
        usd = 0.0
        for ticker, qty in by_ticker.items():
            price = last_prices.get(ticker, 0.5)
            # max loss on a YES contract = price * qty; on a NO = (1-price)*qty
            usd += abs(qty) * max(price, 1.0 - price)
        out[strat] = usd
    return out
