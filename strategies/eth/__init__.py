"""ETH strategy — Kalshi KXETHD hourly ETH price markets.

Self-contained module. Depends only on stdlib, third-party libs, and
``master/``. Does NOT import from sibling strategies.

Layout mirrors ``strategies/weather/`` but with crypto-specific feeds
and a GBM (geometric Brownian motion) posterior over future price.
"""

from strategies.eth.strategy import EthStrategy

__all__ = ["EthStrategy"]
