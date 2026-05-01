"""Weather strategy — Kalshi KXHIGH daily-high temperature markets.

Self-contained module. Depends only on stdlib, third-party libs, and
``master/``. Does NOT import from sibling strategies.

Layout:
    config.py         — city configs (series, station, timezone)
    strategy.py       — implements master.interfaces.Strategy
    data/iem.py       — Iowa Environmental Mesonet ASOS + CLI fetchers
    data/kalshi_public.py — public Kalshi API reader (no auth)
    model/posterior.py — daily-high posterior estimator
    model/climo.py    — climatology residual statistics
    backtest/runner.py — backtest harness CLI
    backtest/metrics.py — Brier score, log-loss, calibration
    tests/            — unit tests with synthetic fixtures
"""

from strategies.weather.strategy import WeatherStrategy

__all__ = ["WeatherStrategy"]
