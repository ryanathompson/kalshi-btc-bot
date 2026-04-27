# EXPIRY_DECAY_V1 — How it works (plain English)

**Status: RETIRED on 2026-04-25.** Replaced by `EXPIRY_DECAY_V2`.
History stays here for reference and so the v1 trade tape stays attributable.

## The thesis (same as v2)

In the last few minutes of a 15-minute Kalshi BTC market, when BTC has
clearly cleared (or clearly missed) the strike, buy the side that's already
winning. We're paying a small premium for near-certainty and capturing the
gap as the market repricings to ~100¢ at settlement.

## How v1 decided to fire

Same logic as v2 *minus the price floor*:

1. Window must be open: 20s ≤ secs_left ≤ 180s.
2. Per-market cooldown not in effect.
3. BTC at least 2σ from the strike (using realized vol over the prior
   15 minutes).
4. Pick the winning side, check it has at least 3¢ of expected edge.
5. Fire.

## Why it was retired

v1 ran ~3 days of dry-run. Across 76 simulated fires, two structurally
opposite trades were both clearing the same gates:

- **Consensus side, mid-to-high price.** "BTC above strike, buy YES at 80¢."
  This is the strategy's actual thesis — and it worked.
- **Contrarian side, sub-5¢ price.** "BTC above strike, but model says NO
  has 4% odds, market says 1%, so 3¢ of edge — buy NO at 1¢." On paper this
  is the same edge math. In reality, the model is wrong near expiry: GBM
  volatility from a 15-minute window assumes BTC can keep moving at recent
  rates for the next 60 seconds, but near close liquidity dries up and BTC
  pins. The model sees vol that the market knows isn't there anymore.

In the v1 sample, **the contrarian sub-population was 24 fires, 24 losses.**
Every one. The 5¢ price floor in v2 removes them entirely without sacrificing
any winners.

## Why this still appears on the dashboard

Two reasons:

1. **Honesty.** The v1 trade tape is real, and rolling it into v2's stats
   would silently launder the historical losses. Keeping `EXPIRY_DECAY_V1`
   visible-but-retired makes the before/after honest.
2. **Audit trail.** If we ever revisit the strike-pinning thesis from a
   different angle, we want the v1 fire log right there to compare against,
   not buried in an aggregate.

For details on the fix and the numbers, see
`docs/models/expiry_decay_v2.md` and `docs/expiry_decay_v2.md`.
