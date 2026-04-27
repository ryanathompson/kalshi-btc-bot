# CONSENSUS_V1_WITH_EXIT — How it works (plain English)

**One-liner:** Same entries as `CONSENSUS_V1`, but try to exit early when the
reason we entered no longer holds.

## What's different from `CONSENSUS_V1`

The entry rules are identical — Momentum + Previous must agree, price below
the cap, not in cooldown, etc. See `consensus_v1.md` for those.

The new behavior is on the way out: instead of holding every trade to
settlement, this model checks whether the **signal that put us in the trade
is still alive** and bails when it isn't.

## The exit ladder

After we enter, every cycle we check the current Consensus signal state and
classify it relative to entry:

- **HARD — the signal inverted.** Momentum has flipped to the *other* side
  (and is past the dead-zone), or the previous-result regime flipped. The
  reason we bought is now actively pointing the other way.
  → Close immediately, capped at `HARD_MAX_LOSS` per trade.

- **SOFT — the signal decayed.** Momentum is still on the right side but has
  fallen into the dead-zone (it's just noise now), or the previous-result
  signal has aged past `PREV_RESULT_MAX_AGE`. The reason we bought is
  fading, not flipping.
  → Close only if the current paper loss is small (≤ `SOFT_MAX_LOSS`).
  Don't crystallize a big loss just because the signal got quiet.

- **HOLD — everything else.** Includes the case where momentum weakened from
  STRONG to non-STRONG but is still on the right side. We log it but don't
  exit on this alone in v1.

## Why this exists as a separate beta

We want to measure exit alpha cleanly. `CONSENSUS_V1` rides every entry to
resolution, `CONSENSUS_V1_WITH_EXIT` mirrors the same entries but applies
the ladder above. Compare their PnL on the same fires and you can see, in
isolation, what exit logic is worth.

## Why it might help

- Some Consensus losses come from trades where the signal flipped halfway
  through the 15-minute window. Holding to settlement on those is just
  pride — the thesis is dead. Cutting early caps the bleed.
- BTC's intraday paths aren't memoryless. A momentum reversal is information
  about the next 5–10 minutes too, not just the last 60 seconds.

## Why it might hurt

- Bid/ask spread eats every early exit. A small win turns into a small loss
  if you're constantly crossing the spread for false alarms.
- Kalshi's late-market liquidity is thin. The "current bid" you'd exit at
  in the last few minutes can be unreasonably wide.
- Some "decayed" signals re-fire seconds later. Exiting on a momentum
  dead-zone print and re-entering one cycle later is just paying spread for
  the same view.

## How exits are simulated

Beta-only: when a HARD or SOFT exit triggers, the model logs a simulated
sale at the **current top-of-book bid**, no slippage modeled. Real live
exits would walk the book, especially in the last 60 seconds — so any
positive PnL here is optimistic by some unknown amount. See
`docs/beta_promotion.md` "2× rule" before promoting.

## Rehydration

If the bot restarts mid-window, the tracker reads any open
`CONSENSUS_V1_WITH_EXIT` trade records out of the trade log and rebuilds
its in-memory positions. So a Render redeploy doesn't orphan open beta
positions or skip exits.
