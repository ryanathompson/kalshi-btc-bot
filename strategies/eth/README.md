# strategies/eth — Kalshi KXETHD hourly ETH price

Self-contained strategy module for Kalshi's hourly ETH-price markets
(series ticker `KXETHD`).

## Market structure (verified live 2026-04-30)

KXETHD is **simpler than KXHIGH**:

- **50 markets per event**, all `strike_type="greater"` (no brackets)
- **Strikes $40 apart**, currently spanning $1299.99 to $3259.99
- **One event per hour** (e.g. `KXETHD-26MAY0117` = 5pm ET on May 1)
- **Settlement**: 60-second TWAP of CF Benchmarks ERTI (Ethereum
  Real-Time Index) immediately before the strike time
- **Liquidity**: ~$17k 24h volume per event, ~$50k aggregate across
  the 3 events typically open at once

Each market resolves YES if `ETH_price_at_settlement > strike`. So for
the model, fair value is simply `1 − CDF(strike)` under whatever
posterior we put over `P_T`.

## Edge thesis

The Kalshi book is updated by humans and market-makers; the spot ETH
reference price (Coinbase, Kraken, Bitstamp — same as ERTI's inputs)
moves continuously. The strategy estimates `P(P_T > strike)` from
current spot + an implied volatility for the remaining time, and
trades markets where the model probability differs from the market
mid by more than `min_edge`.

Settlement is at 1-hour horizons typically, so volatility scaling and
drift assumptions are clean and short-horizon (drift ≈ 0).

## Architectural compliance

Follows the master + strategies pattern (see `../../master/README.md`):

- Implements `master.interfaces.Strategy` — see `strategy.py`
- Depends only on stdlib, requests, numpy, and `master/`
- Does NOT import from any sibling strategy
- Has its own data feeds, model, backtester, and tests

## Layout

```
strategies/eth/
├── __init__.py
├── config.py                    # KXETHD config + reference exchange
├── strategy.py                  # EthStrategy(master.Strategy)
├── data/
│   └── coinbase.py              # Hourly candles + spot ticker, public API, cache
├── model/
│   ├── posterior.py             # GBM (log-normal) posterior over P_T
│   └── volatility.py            # Trailing-window realized hourly vol
├── backtest/
│   └── runner.py                # CLI: python -m strategies.eth.backtest.runner
└── tests/
    ├── test_posterior.py        # GBM math + bracket dispatch
    ├── test_volatility.py       # vol estimator
    └── test_strategy.py         # interface conformance + intent emission
```

## Reference data sources

| Source | What | Auth | Use |
|---|---|---|---|
| **Coinbase Exchange API** | ETH-USD hourly candles + ticker | None | Spot reference + historical for backtest |
| **Kalshi public API** | Market metadata + book | None | Live strike enumeration (master/kalshi_public.py) |
| **CF Benchmarks ERTI** | Settlement source | $$ | Settlement reality, but Coinbase is a strong proxy |

ERTI itself is a paid feed; we approximate it with Coinbase ETH-USD
last-trade. ERTI averages Coinbase + Kraken + Bitstamp, so Coinbase
correlates ~1.0 over short horizons.

## Running the backtester

```bash
# from repo root, with .venv activated
python -m strategies.eth.backtest.runner \
    --start 2026-03-01 --end 2026-03-31 \
    --vol-window-hours 72 \
    --strikes-from-spot -200,200,40 \
    --out backtest_eth_mar.csv
```

Output: CSV of (settlement_hour, strike, predicted_prob, realized_outcome)
plus aggregate Brier / log-loss / calibration printed to stdout.

The backtester picks synthetic strikes around the trailing-window mid
because we don't have Kalshi's historical strike-by-strike book.

## Status

- ✅ Module scaffolded
- ✅ Coinbase data adapter (hourly candles + ticker, cached)
- ✅ GBM posterior + realized-vol estimator
- ✅ Backtester (model-quality scoring; trading-P&L backtest is v2,
       blocked on Kalshi orderbook history same as weather)
- ✅ Synthetic-data unit tests
- ⏳ EWMA / GARCH vol model (v2)
- ⏳ Drift estimation from funding rate or other inputs
- ⏳ Live integration via `master/runner.py`
