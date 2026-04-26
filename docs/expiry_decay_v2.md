# EXPIRY_DECAY v2 — Contrarian-Lottery Floor

**Status:** Active beta as of 2026-04-25. Replaces v1.
**Change:** Added `EXPIRY_DECAY_MIN_PRICE_CENTS=5` floor on the chosen-side ask.
**Where:** `bot.py` `ExpiryDecayStrategy.evaluate()` — gate runs after side
selection, before the cooldown stamp.

## Why

v1 ran ~3 days of dry-run (2026-04-23 → 2026-04-25, n=76 simulated fills).
The dist_sigmas filter (≥2.0σ) was meant to keep entries to "BTC clearly
on one side of the strike." It does — but the surviving population
contains two structurally opposite trades the sigma gate doesn't
distinguish:

| Sub-population | Example | What it means |
|---|---|---|
| **Consensus side, mid-to-high price** | "BTC 2σ above strike → buy NO at 80c" | Pay for near-certain pinning, collect a few cents of decay. |
| **Contrarian-lottery side, sub-5c price** | "BTC 2σ above strike → buy NO at 1c" | Bet the 99c market is wrong by a few percentage points. |

The first is the strategy's actual thesis. The second is a side effect:
when the model says `p_yes = 0.96` and the market says `0.99`, the
opposite-side ask at 1c clears the `MIN_EDGE_CENTS=3` gate even though
this is a small absolute edge against a market that's nearly always
right at this extreme.

In the v1 sample, **the contrarian sub-population was 24 fills, 24
losses**.  GBM with short-window realized vol systematically
underestimates pinning in the last few minutes of a strike-based
market — the model sees vol that the market knows isn't there
anymore.

## The numbers

ROI by price floor on v1's n=76 sample:

| Floor | Trades kept | Sim PnL | Sim ROI | WR | Notes |
|---|---|---|---|---|---|
| none | 76 | −$19 | −1.3% | 54% | All trades. |
| ≥2¢ | 59 | +$321 | +27.7% | 70% | Drops 17 losers @ 0–1¢. |
| **≥5¢** ← chosen | **52** | **+$461** | **+45.2%** | **79%** | Drops 24 losers @ 0–4¢. |
| ≥6¢…≥10¢ | 52 | +$461 | +45.2% | 79% | Identical (no v1 trades in this band). |
| ≥11¢ | 51 | +$300 | +30.0% | 78% | Loses the +$161 win at 11c. |
| ≥20¢ | 48 | +$150 | +15.9% | 79% | Loses three big winners. |
| ≥25¢ | 45 | +$33 | +3.7% | 80% | Collapses. |

`5c` is the cleanest cut: it removes 100% of losers in the contrarian
tail (24/24) and 0% of winners. There were no v1 trades priced 5–10c
to disagree with, so floors anywhere in `[5, 10]` are observationally
identical.

## Caveats — read before promoting

1. **Sim is optimistic.** Per `docs/beta_promotion.md`, beta fills assume
   you take the full ask in size with no slippage. The largest v1 wins
   (+$161 at 11c, +$85 at 19c, +$70 at 22c) came from large
   counts (180, 105, 90) on the *less-liquid* side. Live fills at those
   sizes are not realistic. Expect the +45% sim ROI to be inflated by an
   amount we won't know until v2 has live exposure — assume real ROI is
   no better than half of sim, per the promotion doc's "2× rule."

2. **Fire counter resets.** v2 changes a parameter mid-flight, which
   per the promotion doc's "re-tuning ≡ overfitting" rule means the v1
   fire count doesn't carry over. We need ≥100 v2 simulated fills before
   any promotion conversation.

3. **`beta_model_id` retag.** New trades are tagged
   `EXPIRY_DECAY_V2`. The 76 v1 records keep their `EXPIRY_DECAY_V1`
   tag, so the dashboard's per-model view stays a clean before/after.

4. **The "fix" is empirical, not theoretical.** A more principled fix
   would be to recalibrate the GBM vol estimate near expiry (e.g.,
   damp `sigma_T` as `secs_left → 0`, or apply a tail correction). The
   floor is a cheap, observable proxy that catches the same failure
   mode without committing to a particular vol model. If v2 still
   loses on near-floor entries (5–10c), that's a signal the underlying
   miscalibration extends further up the price curve and we should do
   the harder fix.

## Env knobs

| Var | Default | Notes |
|---|---|---|
| `EXPIRY_DECAY_V2_BETA_ENABLED` | `false` | Canonical on/off. Falls back to `EXPIRY_DECAY_V1_BETA_ENABLED` if unset, so existing Render config keeps working through deploy. |
| `EXPIRY_DECAY_MIN_PRICE_CENTS` | `5` | The v2 floor. Set to `0` to disable the floor without rolling back to v1 (useful for A/B). |

## What to watch in v2

* WR on the 5–15c band specifically. If sub-15c v2 entries lose at the
  same rate v1 sub-5c did, the floor needs to move up.
* Fill realism on the consensus side. Track `filled_count` vs
  `initial_count_fp` on the high-price entries — if we routinely don't
  fill at the simulated price, that confirms the optimistic-sim
  inflation and we discount sim ROI accordingly before promoting.
* Time to n=100. v1 produced ~25 fires/day, so v2 should hit promotion
  sample size in ~5 days assuming similar BTC vol regime.
