# BRIDGE_V1 — How it works (plain English)

**One-liner:** In the 3-5 minute window before a market closes — too late
for pure momentum, too early for pure math — BRIDGE only fires when both
signals agree at the same time.

## The thesis

Kalshi's 15-minute BTC markets have a coverage gap. SNIPER works best with
5+ minutes left (enough time for momentum to be meaningful). EXPIRY_DECAY
works best under 3 minutes (BTC is pinning near its final price). Between
3 and 5 minutes, neither strategy is confident:

- Momentum is getting stale — the 5-minute move happened, but will it hold?
- Vol-distance isn't decisive yet — BTC could still swing back.

BRIDGE requires **both** signals simultaneously. It won't fire unless
momentum is strong, 60-second action confirms the direction, **and** the
GBM volatility model says BTC is far enough from the strike that a flip is
unlikely. Requiring both is the edge — most of the time one or the other
says "pass," so BRIDGE fires rarely but with higher conviction.

## How the bot decides

Every cycle during the 3-5 minute window:

1. **Time check.** Only active when `BRIDGE_MIN_SECS_LEFT` (180s) ≤
   secs_left ≤ `BRIDGE_MAX_SECS_LEFT` (300s). Outside that window, SNIPER
   or EXPIRY_DECAY own the trade.

2. **Cooldown.** Won't re-fire within `BRIDGE_COOLDOWN_S` (120s) on the
   same market.

3. **5-minute momentum gate** (from SNIPER). BTC must have moved ≥ 0.06%
   in the last 5 minutes. Below that, there's no directional signal.

4. **60-second confirmation** (from SNIPER v3). The most recent 60 seconds
   must confirm the 5-minute direction. If BTC moved up over 5 min but the
   last minute is flat or reversed, the momentum thesis is failing — pass.

5. **Vol-normalized distance gate** (from EXPIRY_DECAY). Using 15-minute
   realized volatility, measure how many standard deviations BTC is from
   the strike price given the time remaining. Must be ≥ 1.5σ (lighter than
   EXPIRY_DECAY's 2.0σ threshold because more time remains and we have the
   momentum confirmation as a second leg).

6. **GBM edge check.** Compute the model's fair value using a geometric
   Brownian motion model. The market ask must be at least `BRIDGE_MIN_EDGE_CENTS`
   (2¢) below fair value. If the market is already priced correctly, there's
   nothing to capture.

7. **Agreement gate.** The momentum direction and the GBM model direction
   must point the same way. If 5m momentum says "up" but the model says
   "buy NO," the signals are contradicting each other — pass. This is the
   key filter that makes BRIDGE different from either parent strategy.

If all seven gates pass, BRIDGE fires on the agreed side.

## Why it's a hybrid

Think of BRIDGE as the intersection of two Venn diagrams:

- **SNIPER-style momentum** tells you *which way BTC is going*.
- **EXPIRY_DECAY-style vol math** tells you *whether the market should be
  more certain than it's priced*.

Either signal alone has noise. Together, they form a tighter filter: BTC is
moving in a direction, the recent action confirms it, **and** the math says
the contract is underpriced for how far BTC is from the strike. Both engines
have to agree before a dollar gets risked.

## Why it might lose

- **The gap window is small.** Only 2 minutes of eligibility (3-5 min left)
  means low fire frequency. Small sample sizes take a long time to become
  statistically meaningful.
- **Double-gating might be too restrictive.** If both signals rarely align,
  the model could fire so infrequently that even a high WR doesn't produce
  enough PnL to matter.
- **Momentum is decaying in this window.** By 3-4 minutes left, the 5-min
  signal is partly stale. The 60s confirmation helps, but can't fully
  compensate.
- **1.5σ is lighter than EXPIRY_DECAY's 2.0σ.** This was intentional
  (momentum provides the extra confidence), but if the momentum leg is
  noisy, the lower vol bar could let in worse trades.

## Why it might win

- The thesis is mechanically sound: two independent signals that must agree
  should produce a more filtered — and therefore higher WR — fire set.
- The 3-5 minute window is genuinely uncovered. If there's edge there, no
  other strategy is capturing it.
- The 60s confirmation gate (borrowed from SNIPER v3) was the single
  biggest WR improvement in SNIPER's history — it filters out fading
  momentum before the trade.

## Settings you can tune

All env vars, defaults shown:

| Var | Default | What it controls |
|---|---|---|
| `BRIDGE_V1_BETA_ENABLED` | `false` | Whole strategy on/off |
| `BRIDGE_MIN_SECS_LEFT` | `180` | Window opens at 5 min left |
| `BRIDGE_MAX_SECS_LEFT` | `300` | Window closes at 3 min left |
| `BRIDGE_5M_MIN_MOMENTUM` | `0.0006` | Min 5-min BTC move (0.06%) |
| `BRIDGE_60S_CONFIRM_MIN` | `0.0001` | Min 60s confirmation (0.01%) |
| `BRIDGE_MIN_DIST_SIGMAS` | `1.5` | Vol-distance threshold |
| `BRIDGE_MIN_EDGE_CENTS` | `2` | Min GBM edge to trade |
| `BRIDGE_VOL_WINDOW_S` | `900` | Realized-vol lookback (15 min) |
| `BRIDGE_COOLDOWN_S` | `120` | Per-market re-fire cooldown |
