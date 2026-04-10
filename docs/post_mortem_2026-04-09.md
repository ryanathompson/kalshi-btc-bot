# Post-mortem — 2026-04-09

## TL;DR

Two trades out of 63 are carrying the entire P&L. Strip them and the bot is at **−44% ROI** on **−$715 net** over 28 hours. The Consensus strategy is **losing 23.6%** in its 18 properly-attributed trades. The headline +19% ROI is survival bias from tail bets, not edge.

## The numbers (as of dashboard snapshot 19:11 ET)

| Metric | As shown | Stripping the two mega-wins |
|---|---|---|
| Settled trades | 63 | 61 |
| W / L | 17 / 46 | 15 / 46 |
| Win rate | 27% | 25% |
| Cost basis | $1,659.25 | $1,624.68 |
| Realized P&L | **+$314.75** | **−$714.68** |
| ROI | +18.97% | **−44.0%** |

The two mega-wins:

| Date | Ticker | Side | Entry | Stake | P&L |
|---|---|---|---|---|---|
| 2026-04-09 08:24 | KXBTC15M-26APR090830-30 | YES | 3¢ | $21.87 (810 contracts) | **+$788.13** |
| 2026-04-08 14:38 | KXBTC15M-26APR081445-45 | NO | 5¢ | $12.70 (254 contracts) | **+$241.30** |

These are 36× and 19× payouts on tickets bought far out of the money. Nothing else in the trade history looks remotely like them.

## Consensus is the bigger problem

Consensus has 18 properly-tagged settled trades. The dashboard displays "P&L $126.64 / ROI −23.60%" — the P&L sign is dropped in the UI but the ROI is correctly negative. Manual reconciliation:

- Wins: $53.12 + $31.11 + $32.24 + $27.36 + $32.24 + $55.25 = **$231.32**
- Losses: 12 trades × ~$29.82 = **−$357.96**
- **Net: −$126.64 on $536.64 wagered = −23.6% ROI**

The losing trades fired on momentum values of ±0.03–0.07%, which is right at or below the dead-zone floor. That's noise, not signal, for BTC over a 13-minute settlement horizon.

The original Consensus claim was ~73% win rate in backtest. Live is 33%. That gap is too large to be variance. The most likely explanations are:

1. **Look-ahead leakage** in the backtest (using current-cycle BTC data to predict the same cycle).
2. **Overfit dead-zone / cap parameters** on the same window the WR was reported on.
3. **Survivorship bias** in the backtest market set.
4. **Stale price alignment** — mismatching Kalshi tick timestamps against BTC ticks.

Until the backtest is rebuilt with the paranoid checks in `backtest_consensus.py`, treat the 73% number as fiction.

## Why the daily loss limit didn't catch this

The limit is **$100 net**. After today's mega-win the bot was sitting on +$700 from a single trade, which means the net P&L can absorb 25+ losing $30 trades before the limit fires. The check has no concept of gross losses, no concept of drawdown from peak, and no concept of open-position worst-case exposure.

**Fixed in this commit:** added `DAILY_GROSS_LOSS_LIMIT` (default 1.5× net) and an open-exposure check that pessimistically assumes every open position will settle losing.

## Why strategy attribution was lost

The repo runs on Render's free tier, which has an **ephemeral filesystem** — `bot_trades.json` is wiped on every redeploy. The bot rebuilds history from `/portfolio/fills` on startup, but Kalshi's API doesn't store the bot's strategy tag, so every recovered trade gets bucketed as `RECOVERED`. This is why **45 of 63 settled trades have no strategy attribution** and we can't tell whether the two mega-wins were LAG, an old strategy, or manual entries.

**Fixed in this commit:** the bot now writes strategy prefixes into `client_order_id` (e.g. `LAG-{uuid}`, `CON-{uuid}`), and `rebuild_trades_from_api` walks `/portfolio/orders` to recover the strategy field after a redeploy. Going forward, history survives deploys with full attribution. To go further, set `TRADES_LOG_PATH=/var/data/bot_trades.json` once you have a Render persistent disk.

## Don't build a "tail-bet strategy" off two data points

The temptation after this post-mortem is to think "the cheap-tail trades are working — let's build a strategy around them." Don't. Two trades is not a sample, it's two data points. The mathematically honest move is:

1. Use the new `backtest_consensus.py` harness to identify *every* historical case where a sub-10¢ contract resolved in the money.
2. Compute the unconditional base rate. If sub-10¢ contracts resolve true 5% of the time, paying 5¢ for them is exactly fair — there's no edge.
3. If the base rate is materially above the entry price (e.g. 8% true vs 5¢ entry), then there's a real cheap-tail edge worth coding up. Build it as a separate strategy with its own kill switch.

Until that analysis exists, the cheap-tail wins should be modeled as **survival bias**, not signal.

## Action items

| # | Action | Status |
|---|---|---|
| 1 | Disable Consensus by default; raise MOMENTUM_DEAD_ZONE from 0.03% → 0.20% | ✅ Done in this commit |
| 2 | Strategy-tagged client_order_id + rebuild attribution + env-driven LOG_FILE | ✅ Done in this commit |
| 3 | Build paranoid-checks backtest harness (`backtest_consensus.py`) | ✅ Done in this commit |
| 4 | Add gross-loss + open-exposure daily limits to RiskManager | ✅ Done in this commit |
| 5 | Re-fetch history with `python backtest_consensus.py fetch --with-kalshi` and run a sweep before re-enabling Consensus | 🔲 Manual — operator |
| 6 | Cheap-tail base-rate analysis | 🔲 Manual — operator |
| 7 | Move trade log to a Render persistent disk (set `TRADES_LOG_PATH`) | 🔲 Manual — requires plan upgrade |

## To re-enable Consensus

Do not flip `CONSENSUS_ENABLED=true` until the backtest harness reproduces a positive ROI on out-of-sample data with `MOMENTUM_DEAD_ZONE >= 0.0020`. Specifically:

1. `python backtest_consensus.py fetch --start 2026-03-01 --end 2026-04-09 --interval 1s --with-kalshi --out data/history.json`
2. `python backtest_consensus.py sweep --history data/history.json --dead-zone 0.001 0.0015 0.002 0.003 0.005`
3. Pick the dead-zone where ROI is positive AND stable across nearby values (a "plateau," not a spike).
4. Re-replay only on data the sweep didn't see. If ROI stays positive, you have edge. If it craters, the sweep was overfit.
5. Only then set `CONSENSUS_ENABLED=true` in Render env.
