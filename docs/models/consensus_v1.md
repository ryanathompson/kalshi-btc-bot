# CONSENSUS_V1 — How it works (plain English)

**One-liner:** Trade only when two unrelated, simple signals point the same way.

## The thesis

A Kalshi BTC 15-minute market resolves YES or NO based on whether BTC is above
or below a strike at the close. The market price is roughly the crowd's
probability. We're looking for spots where that probability is a little bit
wrong — but instead of trying to "predict BTC," we just look for moments when
two cheap, weak signals happen to agree. Each signal alone is noise. Together,
the noise cancels and what's left is a small directional nudge.

## The two signals

1. **Momentum.** Has BTC moved up or down in the last 60 seconds? Up → "YES."
   Down → "NO." Tiny moves under the dead-zone (≈ 0.02%) are treated as
   "no signal" — that's just market microstructure, not direction.

2. **Previous result.** How did the last 15-minute Kalshi BTC market settle?
   YES or NO. We carry that forward as a weak prior on the *next* market.

## When the bot fires

All of these must be true:

- Momentum and Previous **agree** (both say YES, or both say NO).
- Momentum is **strong enough** — past the dead-zone, and (in the v2.4 gate)
  past the STRONG threshold (≈ 0.065%). Below STRONG, live data was 23% WR;
  at STRONG and above it was 50% WR. Non-STRONG was the bleed.
- The chosen side's **price is below a cap.** Base cap is 45¢; stronger
  momentum lifts the cap up to 55¢. A high price means the crowd already
  agrees with us — no edge left to capture.
- We're **not in cooldown.** After a fire, wait `CONSENSUS_COOLDOWN` seconds
  before firing again on the same direction. Stops the bot from stacking the
  same trade over and over while the signal is still warm.
- The book is **liquid enough** (yes_ask + no_ask ≥ MIN_BOOK_SUM). If the
  book is thin, the "price" we'd transact at isn't really the price.

## Why the bot might lose

- **Both signals can be right and the trade still loses.** A 60% probability
  is still a 40% chance to lose. Over 100 fires, that's expected.
- **Momentum is a 60-second window.** A spike at second 59 looks the same as
  a slow climb over the full minute, but they don't predict the next minute
  the same way.
- **Previous result decays.** If the last market settled 14 minutes ago, the
  signal is fresh. If it settled an hour ago because we restarted, it's stale.
  The bot expires it after `PREV_RESULT_MAX_AGE`.
- **Regime changes.** During FOMC, CPI, or sudden-news spikes, the
  "two-weak-signals" frame stops being weak — the whole market is pricing one
  story and our small edge gets swamped.

## Why the bot might win

- The two signals are **uncorrelated** sources of bias — recent micro-trend
  and recent regime — so when they agree, the joint probability is higher
  than either alone.
- Price cap means we only fire when the crowd hasn't already priced in our
  view. We're paying for an edge, not chasing consensus.
- Cooldown prevents over-firing — historically the worst losses came from
  re-entering on the same direction after a near-miss.

## What "v1" means

This is the entry-only version. Once we buy, we hold to settlement and let
the market resolve us. There's a sister beta — `CONSENSUS_V1_WITH_EXIT` —
that uses the same entries but tries to exit early when the signal decays.
Both run in parallel so we can measure how much exit logic actually adds.
