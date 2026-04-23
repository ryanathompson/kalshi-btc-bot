# Beta Model Promotion Playbook

**Status:** Active bar for any beta → live promotion, as of 2026-04-23.
**Scope:** Applies to every model registered in `KalshiBot.beta_strategies`.

## Why this doc exists

Beta models run in optimistic simulation — fills happen at the signal price,
no slippage, no queue, no partial fills. Real live trading has all of those.
That's why the promotion bar is not "did it make simulated money." It has
to be materially higher.

The doc is also a pre-commitment device. It's the yardstick you set *before*
the data comes in, so future-you can't rationalize a promotion when a
strategy is 60 fires in with a 70% WR and you want to ship it. Either it
clears this bar or it doesn't.

## The minimum bar

A beta model may be promoted when **all** of the following are true:

| Criterion | Threshold | Why |
|---|---|---|
| Fire count | ≥ 100 resolved trades | Smaller samples are just noise at the kind of edges we're hunting. |
| Simulated ROI | ≥ 2× the live ROI you'd demand | Optimistic-sim inflation. Assume real fills give back half the edge, minimum. |
| WR vs breakeven gap | Statistically meaningful at n ≥ 100 | See "breakeven WR" below. A 52% WR on a 50¢ contract is noise; a 62% WR is signal. |
| Parameter stability | No re-tuning during the eval window | Re-tuning ≡ overfitting. Reset the fire counter whenever you change a parameter. |
| Thesis/behavior match | Qualitative: signals look like the thesis says they should | Spot-check 10 fires; does each `reason` field make sense? |
| No catastrophic tail | No single trade lost > 3× average stake | Optimism inflates WR but can mask fat-tail losses. |

"Simulated ROI ≥ 2× live bar" in practice: if you'd accept a live strategy at
+5% ROI, the beta version must show ≥ +10% in simulation.

## Breakeven WR cheat sheet

For a yes/no contract bought at price *p* (in cents), breakeven WR is *p%*.
To have edge, WR must be **materially above** *p%*. How much is "material"?

A rough rule using binomial standard error at n = 100:

| Contract price | Breakeven WR | Noise band (±1σ) | "Meaningful" gap |
|---|---|---|---|
| 25¢ | 25% | ±4.3% | WR ≥ 34% |
| 50¢ | 50% | ±5.0% | WR ≥ 60% |
| 75¢ | 75% | ±4.3% | WR ≥ 84% |
| 90¢ | 90% | ±3.0% | WR ≥ 96% |

At higher prices, the breakeven bar gets steeper and the noise band gets
tighter, so "meaningful" needs to be ≥2σ above breakeven. At lower prices,
losses are cheap but wins are fat — noise bands widen, and you need the WR
cushion to cover fee drag.

## Qualitative checks (do these before promotion)

Before flipping the flag, eyeball at least 10 fires from the model's
`/beta/<model_id>` history:

1. **Reason field sanity.** Each fire should have a `reason` explaining *why*
   the signal triggered. Read them. Does the logic described match the
   thesis of the strategy? Any "should not have fired" cases?
2. **Fire cadence.** Does the cadence match what the thesis implies? Expiry
   decay should cluster in the last 1-3 minutes of 15-min markets. If it's
   firing at 8 minutes left, something is wrong.
3. **Price distribution.** Pull the distribution of `price` across fires.
   Does it look like the band the thesis targets?
4. **WR by price band.** The `/beta/<model_id>` table lists resolved trades
   with their prices. Are high-price winners driving the ROI (cheap wins,
   expensive losses = structural danger)? Or is edge distributed across
   the price range?
5. **Compare to live strategy behavior.** If the beta is a shadow of a live
   model (e.g., CONSENSUS_V1 vs live CONSENSUS), are the fire patterns
   similar? Different? Why?

If any of these raise red flags, the model is not ready — fix first, then
reset the counter and re-run the eval window.

## Anti-patterns — don't do these

- **Promoting on small n.** 40 fires at 65% WR is not signal, it's a coin.
  Wait for 100.
- **Re-tuning parameters, then using post-tune stats.** If you changed
  `MIN_DIST_SIGMAS` last Thursday, your counter resets Thursday. Don't
  count the pre-change fires.
- **Promoting because "the thesis is sound."** The data clears the bar, or
  the thesis is wrong. Theses that look obvious in hindsight have blown up
  plenty of traders.
- **Confusing "no losses yet" with "positive edge."** A strategy that's
  fired 12 times with 12 wins on 85¢ contracts is exactly what you'd
  expect if the contract prices are correct and not a sign of edge.
- **Promoting right before an expected regime change.** FOMC, CPI, halving
  events — don't flip the switch days before a known vol event. The
  historical stats won't apply.
- **Stealth promotion.** If you flip is_beta=False, log the decision in the
  commit message with the stats at the time of promotion. Future-you needs
  the audit trail.

## The promotion process

When a model clears the bar:

1. **Snapshot the stats.** Screenshot `/beta/<model_id>` and save to
   `docs/promotions/<model_id>_<date>.png`. (The archive will help you
   calibrate the bar itself over time.)
2. **Write a promotion commit.** Change `is_beta=True` to `is_beta=False`
   in `KalshiBot.__init__`, rename `<MODEL_ID>_BETA_ENABLED` to
   `<MODEL_ID>_ENABLED` (or whatever's appropriate), and write the commit
   message with the stats snapshot inline so it's searchable.
3. **Deploy with a reduced stake.** The first week live, run the promoted
   model at half its intended `MAX_STAKE_PER_TRADE` to validate fills
   behave as simulation suggested.
4. **Monitor the first 20 live fires.** If live WR diverges from simulated
   WR by more than 1σ of the binomial, pause and investigate — that's the
   optimistic-sim gap you budgeted for showing up. If it's within 1σ, scale
   to full stake.
5. **Keep the beta entry for the same model_id alive, or retire it.** If
   you want a continuous shadow book (beta keeps running alongside live
   for ongoing comparison), leave the beta strategy registered too. If
   not, remove it from `beta_strategies` and let the model_id show as
   "Retired" on the grid — its history stays available.

## When to retire a beta model

Retire (remove from `beta_strategies`) when any of these are true:

- The model has fired ≥ 200 times and failed to clear the promotion bar
- A better model has been built around the same thesis (promote that one)
- The market structure has changed in a way that invalidates the thesis
  (rare but possible — e.g., Kalshi changes fee structure)

A retired model's trade history stays on `/beta/<model_id>` as "Retired"
status, so the data isn't lost — you just stop adding to it.

## Open questions / future refinements

- **Simulated fees.** Current simulation doesn't deduct Kalshi's maker/taker
  fees. If a strategy's edge is thin (e.g., <3¢ per trade), fees can erase
  it entirely. TODO: add a fee model to `_log_signal` so the beta PnL
  number reflects post-fee reality.
- **Slippage model.** Phase 4 expiry decay is especially sensitive — late-
  window fills often walk the book. A 2¢ expected slippage turns a 3¢-edge
  fire into a losing trade. TODO: add optional slippage parameters to the
  beta fill simulation (not the main framework — beta-specific).
- **Bootstrap confidence intervals.** The noise band in the breakeven table
  above is a 1σ binomial approximation. For a proper promotion decision,
  bootstrap resampling on the actual PnL series is more robust. TODO: add
  a "bootstrap CI" column to `/beta/<model_id>`.

These are "nice to have" but not blockers. Current bar is usable today.
