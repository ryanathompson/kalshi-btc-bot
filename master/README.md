# master/ — cross-strategy rules and contract

This is the only module that all strategies depend on. It defines:

- **`interfaces.py`** — The `Strategy` Protocol every module under `strategies/<name>/` implements, plus shared data classes (`MarketSnapshot`, `OrderIntent`, `Side`, `StrategyContext`).
- **`rules.py`** — Global rules that apply to *every* strategy regardless of asset: bankroll caps, fee math, kill switch, blacklists. Also Kalshi maker/taker fee helpers since the formula is the same for everyone.
- **`risk.py`** — *(stub)* Cross-strategy risk aggregation. When the orchestrator runs multiple strategies, this is where aggregate position limits, correlated-asset caps, and global kill switches live.
- **`runner.py`** — *(stub)* Orchestrator that loads strategies, fans out market snapshots, collects `OrderIntent`s, applies global rules, and routes orders to Kalshi.

## Architectural rule (immutable)

A strategy MUST NOT import from another strategy. Cross-strategy concerns
go through `master/`. Period.

```
strategies/weather/strategy.py    can import:  master.*  +  strategies/weather/**
strategies/weather/strategy.py    CANNOT import: strategies/btc/*  or anything in bot.py
```

## Why this shape

Ryan wants modular architecture with master rules + module-level functionality,
and explicitly does NOT want logic to cross-contaminate between strategies.
The codebase will host BTC, Weather, ETH, SOL, and many more over time. Each
strategy needs to be:

1. **Auditable** — its full logic lives in one directory.
2. **Disable-able independently** — toggling a strategy off doesn't risk
   breaking another.
3. **Replaceable** — a v2 weather model can replace v1 without touching BTC.
4. **Testable in isolation** — unit tests per module, no shared state.

## What's in vs out of master

| Concern | Where it lives |
|---|---|
| Bankroll cap (e.g., $5k total at risk) | `master/rules.py` |
| Per-strategy bankroll cap | `master/rules.py` |
| Aggregate-crypto position cap (BTC + ETH + SOL combined) | `master/risk.py` |
| Kalshi fee formula | `master/rules.py` |
| Kalshi REST/WS client | `master/` (TBD — currently in `bot.py`, will migrate) |
| Order rate limiter | `master/runner.py` |
| Global kill switch | `master/rules.py` |
| Logging / metrics format | `master/` (TBD) |
| BTC microstructure signal | `strategies/btc/` |
| Weather posterior model | `strategies/weather/` |
| ETH bracket fair-value | `strategies/eth/` |
| GFS ensemble fetcher | `strategies/weather/data/` (only weather uses it) |

## Status

**Today:** master defines the interface and global rules. The existing
`bot.py` is the legacy KXBTC15M bot — it does NOT yet use this interface
and runs untouched on Render. It will be refactored into `strategies/btc/`
in a later pass; until then, treat it as a closed legacy system.

**Today:** `strategies/weather/` is the first module built against this
interface. It includes data feeds, posterior model, and a backtester.
It is NOT live trading — it's a research module.

## Migration path

1. ✅ Define interface (`master/interfaces.py`)
2. ✅ Build `strategies/weather/` against the interface, with its own backtester
3. ⏳ Build `master/runner.py` that can load and run a strategy in paper-trade mode
4. ⏳ Promote weather to live trading (small size)
5. ⏳ Refactor `bot.py` into `strategies/btc/` — last, because it's already running and we don't want to introduce a regression
