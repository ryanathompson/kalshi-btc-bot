# CONSENSUS_V2 — How it works (plain English)

**One-liner:** Trade the 36–45c OTM band when SNIPER-grade momentum and the
last market's result both point the same direction.

## Why V2 exists

Consensus V1 ran as a beta for 100 trades (Apr 23–28, 2026). The overall
numbers were messy — 46% WR, lots of bleed. But hidden inside was a sharp
split by price band:

| Band | Trades | WR | PnL | ROI |
|---|---|---|---|---|
| 25–35c | 32 | 31.2% | -$15.32 | -15.0% |
| **36–45c** | **33** | **63.6%** | **+$50.79** | **+71.3%** |
| 46–55c | 33 | 42.4% | -$4.22 | -17.4% |

The 36–45c band had genuine edge. The other bands were diluting it. V2 strips
the strategy down to *only* that band and upgrades the signal from V1's noisy
60-second momentum to SNIPER's proven 5-minute pipeline.

## The thesis

SNIPER owns the 50–55c "conviction" zone. But contracts priced at 36–45c —
slightly out of the money — have a different dynamic: the crowd is skeptical
that BTC will reach the strike, so the contract is cheap relative to how often
momentum actually carries it there. When two independent signals agree —
strong recent momentum AND the previous market settling in the same direction
— the market is underpricing continuation.

The previous-result signal is a simple form of regime detection. If the last
15-minute window resolved the same way current momentum is pointing, BTC is
probably in a trending micro-regime, not a mean-reverting chop. Momentum
signals are more reliable during trends.

## How the bot decides

Every cycle, for each open 15-minute market:

1. **Time check.** At least 5 minutes must remain (`CONSENSUS_V2_MIN_MINS_LEFT`).
   Less than that and there isn't enough runway for momentum to carry the
   contract across the strike.

2. **Cooldown.** Won't re-fire within 300 seconds of the last V2 trade.

3. **5-minute momentum gate.** BTC must have moved ≥ 0.07% in the last
   5 minutes (`CONSENSUS_V2_5M_MIN_MOMENTUM`). Same threshold as SNIPER
   v3.2 — proven to filter noise while keeping real signals.

4. **60-second contradiction check.** If the last 60 seconds moved against
   the 5-minute direction by more than 0.03%, the trade is killed.

5. **60-second confirmation.** The last 60 seconds must actively move in
   the same direction as the 5-minute trend by at least 0.01%. This is
   SNIPER v3's key innovation — it filters stale/decaying momentum where
   the 5m number looks good but the move has already exhausted itself.

6. **Previous-result agreement.** The last settled 15-minute BTC market
   must have resolved in the same direction as current momentum. This is
   the regime signal — it's the one piece of information V2 has that SNIPER
   does not. Without it, this would just be SNIPER with a wider price zone.

7. **Price zone filter.** The contract must be priced between 36c and 45c
   (inclusive). Below 36c the V1 data showed disastrous WR. Above 45c
   overlaps with SNIPER's domain.

If all gates pass, the bot fires in the direction of momentum.

## How it sizes trades

Kelly sizing with corrected priors:
- Win rate prior: 60% (conservative vs the raw 63.6% from V1 data)
- Average price: 41c (center of the 36–45c band)
- Hard cap: $3 per trade (`CONSENSUS_V2_MAX_STAKE`)

The hard cap is a safety rail while the beta accumulates sample. Kelly's
priors are based on only 33 trades — enough to suggest edge but not enough
to bet the farm on. Once the beta has 50+ fills with sustained WR above 55%,
the cap can be raised.

## Why it wins

- **The 36–45c zone is genuinely underserved.** SNIPER's kill-zone filter
  refuses to trade it. No other active strategy touches it. If there's edge
  here, V2 is the only thing capturing it.
- **5m momentum + regime agreement is a tighter filter than either alone.**
  Many momentum signals fire during chop (BTC bouncing without trend). The
  previous-result gate filters those out — if the last market went the other
  way, the "trend" may not be real.
- **The payout math favors OTM.** A 40c contract that wins pays $0.60 on a
  $0.40 risk (1.5:1). You only need ~40% WR to break even. At 60%+ WR the
  ROI is substantial.

## Why it loses

- **Regime signal is fragile.** The previous-result is a single binary data
  point from 15+ minutes ago. BTC can change character quickly — the last
  market settling YES doesn't guarantee the next 15 minutes continue up.
- **36–45c contracts can be volatile.** These are OTM — a small BTC move
  against you swings the contract value fast. There's no early exit monitor
  on this strategy (unlike SNIPER Conviction), so every trade rides to
  settlement.
- **Small sample.** The 63.6% WR is from 33 trades. Statistical confidence
  is limited. The true WR could easily be 50–55%, which would make the
  strategy marginal at best.
- **Overlapping fire windows with SNIPER.** Both strategies use 5m momentum
  and 60s confirmation. When SNIPER fires at 50c, V2 might also fire at 42c
  on the same market. This creates correlated exposure.

## What's different from V1

| Aspect | V1 | V2 |
|---|---|---|
| Momentum window | 60 seconds | 5 minutes |
| 60s confirmation | No | Yes (required) |
| Previous-result | Required | Required |
| Price zone | 25–55c (dynamic cap) | 36–45c (hard) |
| STRONG-only gate | Yes (gated below 0.065%) | No (floor is already 0.07%) |
| Kelly prior WR | 55% (generic) | 60% (from actual band data) |
| Stake cap | None (Kelly could go wild) | $3 hard cap |

## Settings you can tune

All env vars, defaults shown:

| Var | Default | What it controls |
|---|---|---|
| `CONSENSUS_V2_BETA_ENABLED` | `true` | Whole strategy on/off |
| `CONSENSUS_V2_5M_MIN_MOMENTUM` | `0.0007` | Min 5-min BTC move (0.07%) |
| `CONSENSUS_V2_60S_CONFIRM_MIN` | `0.0001` | Min 60s confirmation (0.01%) |
| `CONSENSUS_V2_MIN_PRICE_CENTS` | `36` | Price floor (cents) |
| `CONSENSUS_V2_MAX_PRICE_CENTS` | `45` | Price cap (cents) |
| `CONSENSUS_V2_COOLDOWN_S` | `300` | Seconds between fires |
| `CONSENSUS_V2_MIN_MINS_LEFT` | `5` | Min minutes remaining |
| `CONSENSUS_V2_MAX_STAKE` | `3.0` | Hard $ cap per trade |

## Promotion criteria

Before promoting V2 from beta to live:
1. At least 50 filled trades
2. Win rate ≥ 55% sustained (above the ~42% breakeven for avg 41c entry)
3. Positive ROI after simulated spread/slippage
4. No clustering of losses in a single time-of-day or day-of-week window
5. Correlated-exposure analysis: confirm V2 + SNIPER firing together doesn't
   create unacceptable drawdown concentration
