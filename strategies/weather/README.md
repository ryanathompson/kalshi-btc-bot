# strategies/weather — Kalshi KXHIGH daily-high temperature

Self-contained strategy module for Kalshi's daily-high temperature
bracket markets across US cities (KXHIGHNY, KXHIGHCHI, KXHIGHMIA,
KXHIGHLAX, KXHIGHDEN, KXHIGHAUS).

## Edge thesis

A daily-high market becomes increasingly deterministic as the day
progresses — once the high is observed, the outcome distribution
collapses to a narrow band — but Kalshi order books often lag the true
posterior. The strategy estimates `P(daily_high ∈ bracket)` from
intraday observations + remaining-hours forecast and trades brackets
where the model edge exceeds market price by at least the configured
threshold.

## Architectural compliance

This module follows the master + strategies architecture (see
`../../master/README.md`). It:

- Implements `master.interfaces.Strategy` — see `strategy.py`
- Depends only on stdlib, requests/numpy, and `master/`
- Does NOT import from any sibling strategy
- Has its own data feeds, model, backtester, and tests

## Layout

```
strategies/weather/
├── __init__.py
├── config.py                    # city → ASOS station mapping, timezones
├── strategy.py                  # WeatherStrategy(master.Strategy)
├── data/
│   ├── iem.py                   # Iowa Environmental Mesonet ASOS + CLI
│   └── kalshi_public.py         # public-API Kalshi market enumerator
├── model/
│   ├── posterior.py             # daily-high posterior given partial-day obs
│   └── climo.py                 # climatology residual statistics
├── backtest/
│   ├── runner.py                # CLI: python -m strategies.weather.backtest.runner
│   └── metrics.py               # Brier, log-loss, calibration plot data
└── tests/
    ├── test_posterior.py        # synthetic-data unit tests
    └── test_strategy.py         # interface-conformance tests
```

## Data feeds (recommended)

| Source | What | Auth | Use |
|---|---|---|---|
| **Iowa Environmental Mesonet (IEM)** | ASOS hourly observations | None | Intraday signal — temp every hour for each city's station |
| **IEM CLI archive** | NWS daily climate reports | None | Ground truth daily highs for backtest scoring |
| **NOAA HRRR** via AWS Open Data | 3km hourly forecast | None | Remaining-hours forecast for live posterior |
| **NOAA GEFS** via Open-Meteo Ensemble API | 21-member ensemble | None | Probability calibration when far from settlement |
| **Kalshi public API** | Market metadata, prices | None | Bracket enumeration and live book |

We ship `data/iem.py` and `data/kalshi_public.py` in this module. HRRR
and GEFS adapters are stubbed for v1 — the backtester runs on observed
data + climatology residuals first, and the forecast adapters get added
once the model shape stabilizes.

## Running the backtester

```bash
# from repo root, with .venv activated
python -m strategies.weather.backtest.runner \
    --city NYC \
    --start 2025-04-01 \
    --end 2025-04-30 \
    --hours-out 1,3,6,12
```

Output: per-(date, hour-of-day) table of model posterior vs realized
outcome, plus aggregate Brier score and calibration breakdown.

## Status

- ✅ Module scaffolded
- ✅ IEM data adapter (ASOS hourly + daily-high aggregation)
- ✅ Kalshi public-API reader
- ✅ Posterior model v1 (Gaussian climatology residual; replace with
       empirical residuals in v2)
- ✅ Backtester (model-quality scoring; trading-P&L backtest is v2,
       blocked on Kalshi orderbook history)
- ✅ Synthetic-data unit tests
- ⏳ HRRR forecast adapter
- ⏳ GEFS ensemble adapter
- ⏳ Live integration via `master/runner.py` (master is itself a stub)

## Cities supported (verified live 2026-04-30)

| Code | Series | NWS source | ASOS station |
|---|---|---|---|
| NYC | KXHIGHNY | Central Park CLI | NYC |
| CHI | KXHIGHCHI | Chicago O'Hare CLI | ORD |
| MIA | KXHIGHMIA | Miami Intl CLI | MIA |
| LAX | KXHIGHLAX | LA Intl CLI | LAX |
| DEN | KXHIGHDEN | Denver Intl CLI | DEN |
| AUS | KXHIGHAUS | Austin-Bergstrom CLI | AUS |

Station mapping for non-NYC cities should be re-verified against
each market's `rules_primary` text before live trading.
