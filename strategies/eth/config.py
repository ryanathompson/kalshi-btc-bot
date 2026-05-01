"""Configuration for the ETH strategy.

Single asset for now (KXETHD on ETH). Structured so additional
crypto-price series (e.g. KXBTCD if we later promote BTC into this
shape) can be added without touching the strategy logic.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CryptoMarketConfig:
    """Configuration for a single crypto-price series."""

    code: str                   # short id, e.g. "ETH"
    kalshi_series: str          # e.g. "KXETHD"
    coinbase_product: str       # e.g. "ETH-USD"
    settlement_minutes_offset: int = 0
    """Settlement is at (close_time + offset). For KXETHD it's the strike
    time itself; for other series may differ."""


# KXETHD: settles at the top of the strike hour (e.g. 5pm EDT for the
# `-26MAY0117` event). Reference is ERTI 60s TWAP, which we approximate
# with Coinbase ETH-USD last-trade.
ETH = CryptoMarketConfig(
    code="ETH",
    kalshi_series="KXETHD",
    coinbase_product="ETH-USD",
)


SUPPORTED_MARKETS: dict[str, CryptoMarketConfig] = {
    ETH.code: ETH,
}


def get_market(code: str) -> CryptoMarketConfig:
    key = code.upper()
    if key not in SUPPORTED_MARKETS:
        known = ", ".join(sorted(SUPPORTED_MARKETS))
        raise KeyError(f"Unknown market code {code!r}; known: {known}")
    return SUPPORTED_MARKETS[key]
