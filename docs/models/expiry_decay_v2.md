# EXPIRY_DECAY_V2 — How it works (plain English)

**One-liner:** In the last few minutes of a 15-minute market, when BTC has
clearly cleared (or clearly missed) the strike, buy the side that's already
winning — but skip the cheap "lottery" tail.

## The thesis

Kalshi's 15-minute BTC markets resolve based on the price at the close. As
the close approaches, two things happen:

1. **Time runs out.** The amount BTC could plausibly move in the remaining
   seconds shrinks. By the last minute, it's basically pinned.
2. **The market keeps repricing.** But it sometimes lags the obvious.
   When BTC is plainly above the strike with 90 seconds left, the YES side
   should be ~99¢. Frequently it's 92¢ or 95¢ — there's a few cents of
   "decay alpha" left to collect by buying the certain-winner side.

We're not predicting BTC. We're paying a tiny premium for near-certainty
and capturing the gap as the market catches up.

## How the bot decides

In every cycle of the last ~3 minutes of a market:

1. **Time check.** Only fire when `MIN_SECS_LEFT` ≤ secs_left ≤ `MAX_SECS_LEFT`
   (default 20s ≤ x ≤ 180s). Before that, anything can still happen. After
   that, the spread is too wide and we're chasing pennies.

2. **Cooldown.** Don't re-fire within `EXPIRY_DECAY_COOLDOWN_S` seconds on
   the same market — one shot per signal.

3. **"How far is BTC from the strike, in standard deviations?"**
   Estimate BTC's short-window realized volatility (last 15 min of ticks).
   Convert that into "how big a move could plausibly happen in the remaining
   seconds." Compare BTC's current distance from the strike to that.
   If BTC is **≥ 2σ** away, a flip is statistically unlikely.

4. **Pick the side that's winning** (BTC above strike → YES; below → NO).

5. **Edge check.** The chosen side's ask must be far enough below 100¢ to
   leave at least `MIN_EDGE_CENTS` (default 3¢) of expected profit, given
   the model's implied probability.

6. **[v2-only] Price floor.** Skip the trade if the chosen-side ask is
   under `EXPIRY_DECAY_MIN_PRICE_CENTS` (default **5¢**). This is the
   v2 fix — see below.

## Why the 5¢ floor exists (the v1 → v2 story)

v1 had everything above except the price floor. Across 76 simulated fires,
two structurally different things were both clearing the gates:

- **Consensus side, 30¢–95¢ ask:** "BTC is above strike, buy YES at 80¢."
  This is the actual thesis. WR was strong, ROI positive.
- **Contrarian side, 0¢–4¢ ask:** "BTC is above strike, but the model
  thinks NO has 4% chance, market prices NO at 1¢, so 3¢ of edge — buy NO
  at 1¢." On paper this clears the edge gate. In reality the model's
  volatility estimate is wrong near expiry.

Why is it wrong? GBM volatility from a 15-minute window assumes BTC can
keep moving at recent rates for the remaining 60 seconds. But near close,
liquidity dries up and BTC pins — the model sees vol that the market knows
isn't there anymore. So the model says "5% chance of a flip" and the
market correctly says "1%."

In v1's data, the **sub-5¢ contrarian sub-population was 24 fires, 24 losses.**
Every single one. The 5¢ floor drops them, and on the same v1 sample, sim
ROI goes from −1.3% (with all fires) to +45.2% (with the floor).

The floor anywhere in 5–10¢ would have given the same result on that
sample — there were no v1 fires in that band — so 5¢ is the cleanest cut.

## Why it might lose

- **Sim ROI is optimistic.** Beta fills assume we get the full ask in size
  with no slippage. The biggest v1 wins came from large counts on the
  *less-liquid* side; live fills won't be that clean. Per the promotion
  doc's "2× rule," assume real ROI is no better than half of sim.
- **Late-window slippage.** Buying 100 contracts at 95¢ might mean filling
  20 at 95¢ and the rest walking up to 97¢. That eats most of the edge.
- **The vol model can still be wrong above 5¢.** If sub-15¢ v2 entries
  start losing at the same rate sub-5¢ v1 entries did, the floor moves up.
- **Pinning isn't guaranteed.** A late-breaking news event (Fed minutes
  releasing 90 seconds before close, an exchange outage) can move BTC
  enough to flip the strike. Rare, but the loss is the full position.

## Why it might win

- The 5¢ floor surgically removed 100% of v1 losers in the contrarian
  tail and 0% of winners. That's a clean cut, not curve-fitting noise.
- The thesis matches a known structural feature of Kalshi's market: late
  decay is uneven, the obvious side trades at a small discount to certainty,
  and that discount is the alpha.
- σ-distance gating means we don't fire on coin flips. We need BTC clearly
  on one side — most of the time it isn't, and we wait.

## Settings you can tune

All env vars, defaults shown:

| Var | Default | What it controls |
|---|---|---|
| `EXPIRY_DECAY_V2_BETA_ENABLED` | `false` | Whole strategy on/off |
| `EXPIRY_DECAY_MIN_PRICE_CENTS` | `5` | The v2 floor. `0` to disable. |
| `EXPIRY_DECAY_MIN_DIST_SIGMAS` | `2.0` | How "clear" BTC has to be |
| `EXPIRY_DECAY_MIN_EDGE_CENTS` | `3` | Required model-vs-market gap |
| `EXPIRY_DECAY_MAX_SECS_LEFT` | `180` | Window opens at 3 min left |
| `EXPIRY_DECAY_MIN_SECS_LEFT` | `20` | Window closes at 20s left |
| `EXPIRY_DECAY_VOL_WINDOW_S` | `900` | Realized-vol lookback (15 min) |
| `EXPIRY_DECAY_COOLDOWN_S` | `60` | Per-market re-fire cooldown |

The deeper engineering write-up is in `docs/expiry_decay_v2.md`.
