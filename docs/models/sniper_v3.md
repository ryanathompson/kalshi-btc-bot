# SNIPER v3 — How it works (plain English)

**One-liner:** Bet on BTC's short-term direction in 15-minute Kalshi markets,
but only when momentum is strong, recent price action confirms it, and the
contract is priced in a zone where that signal historically has edge.

## The thesis

BTC doesn't move randomly over 15 minutes — it trends. When BTC has been
moving meaningfully in one direction over the last 5 minutes, and the most
recent 60 seconds confirms that move is still alive, the corresponding Kalshi
contract is likely underpriced. But this only works at specific price points.
In the mushy middle (11–49c), the market is too uncertain for momentum to
provide a reliable edge. SNIPER avoids that zone entirely.

## How the bot decides

Every cycle (~30 seconds), for each open 15-minute market:

1. **Time check.** The market must have at least `SNIPER_MIN_MINS_LEFT`
   (5 min) remaining. With less time, momentum can't play out and the
   signal becomes noise.

2. **Cooldown.** Won't re-fire within `SNIPER_COOLDOWN` (180s) of the last
   SNIPER trade. Prevents overtrading the same signal.

3. **5-minute momentum gate.** BTC must have moved ≥ 0.07% in the last
   5 minutes (`SNIPER_5M_MIN_MOMENTUM`). Below that, the move is
   indistinguishable from noise. This threshold was raised twice — from
   0.03% (v1) to 0.06% (v3.0) to 0.07% (v3.2) — each time after data
   showed the weakest signals were coin flips that bled money.

4. **60-second contradiction check.** If the last 60 seconds moved against
   the 5-minute direction by more than 0.03%, the trade is killed. In live
   data, counter-60s trades were 0% WR across 12 fills.

5. **60-second confirmation (v3.0).** Beyond just not contradicting, the
   last 60 seconds must actively move *in* the same direction by at least
   0.01% (`SNIPER_60S_CONFIRM_MIN`). This filters out stale momentum —
   where the 5-minute number looks good but the move already happened and
   BTC is now drifting sideways.

6. **Price floor.** The contract price must be ≥ 25c (`MIN_ENTRY_PRICE_CENTS`).
   Sub-25c entries (the old "LOTTERY" zone) were 0/12 in live trading.

7. **Entry zone filter.** Only two zones are allowed:
   - **CONVICTION (50–55c):** Near the money. ~52% WR needed to break even.
     This is where virtually all live SNIPER trades fire.
   - **LOTTERY (1–10c):** Deep out of the money. Currently unreachable due
     to the 25c price floor, but the code path is preserved.
   - **Everything else (11–49c, 56c+):** Kill zone. No signal, no trade.

If all gates pass, SNIPER fires in the direction of the 5-minute momentum
(BTC up → buy YES, BTC down → buy NO).

## How it sizes trades

SNIPER uses **quarter-Kelly sizing** to scale bets relative to bankroll and
measured edge:

- Kelly calculates the optimal bet size using the strategy's win rate and
  the contract price. Quarter-Kelly (25% of the full Kelly bet) is a
  conservative choice that limits drawdowns.
- If Kelly returns zero (no measurable edge at this price), the bot places
  a $1 exploratory stake instead of the full flat bet. This was a critical
  v3.1 fix — the old code fell back to $25 stakes when Kelly said "no edge,"
  causing massive losses on a 60% WR day.
- Bets are capped at 10% of bankroll (`KELLY_MAX_BET_PCT`), regardless of
  what Kelly says.
- An auto-scoring system tracks the last 20 trades' performance. If the
  strategy is running cold (score < 50), the stake is throttled to half.
  Below 25, the strategy is auto-blocked entirely.

## Early exit (v2.0, tuned v3.2)

Conviction trades (50–55c) are monitored after placement. If BTC reverses
more than 0.15% against the trade direction after a 2-minute minimum hold,
the bot tries to sell the position back at a small loss rather than riding
it to $0 at settlement.

The logic: a 50c contract that loses settles at $0 (100% loss). If BTC is
reversing hard, selling at 40c (20% loss) is better than the coin-flip of
hoping it comes back. The reversal threshold was lowered from 0.3% (v2.0)
to 0.15% (v3.2) because the old threshold was too high to ever trigger in
15-minute markets — it had zero activations across 43+ trades.

The exit only fires if the current bid still makes the loss manageable
(≤ 40% of entry price). It won't panic-sell into a wide spread.

## Why it wins

- **Momentum is real in BTC.** 5-minute directional moves in BTC are
  autocorrelated over the next 10–15 minutes more often than chance. The
  Kalshi 15-minute market is the right timescale to capture this.
- **Two-layer momentum filter.** Requiring both 5-minute direction *and*
  60-second confirmation eliminates a large class of stale/decaying signals
  that pass the 5-minute gate but have already exhausted their move.
- **Zone discipline.** By refusing to trade 11–49c and 56c+, SNIPER avoids
  the price bands where live data showed consistent negative edge.

## Why it loses

- **Choppy markets.** When BTC is mean-reverting (bouncing in a range
  without trending), momentum signals whipsaw. The 5-minute move looks
  directional but reverses before settlement. SNIPER has no regime
  detection — it fires the same way in trends and chop.
- **50c is maximum variance.** Most Conviction trades are near 50c, where
  losses are $1.00 (the contract settles at $0). Two or three bad 50c
  trades in a row can wipe a day's gains. The win rate only needs to slip
  a few points below breakeven for this zone to bleed.
- **The 60-second window is short.** A single large trade in the last
  minute can create a false confirmation signal, or a brief pause can
  suppress a real one.

## Settings you can tune

All env vars, defaults shown:

| Var | Default | What it controls |
|---|---|---|
| `SNIPER_5M_MIN_MOMENTUM` | `0.0007` | Min 5-min BTC move to fire (0.07%) |
| `SNIPER_REQUIRE_60S_CONFIRM` | `true` | Require 60s same-direction confirmation |
| `SNIPER_60S_CONFIRM_MIN` | `0.0001` | Min 60s move for confirmation (0.01%) |
| `SNIPER_COOLDOWN` | `180` | Seconds between SNIPER trades |
| `SNIPER_MIN_MINS_LEFT` | `5` | Min minutes remaining in market |
| `SNIPER_CONVICTION_STAKE` | (max stake) | Flat stake for Conviction trades |
| `SNIPER_LOTTERY_STAKE` | (max stake) | Flat stake for Lottery trades |
| `MIN_ENTRY_PRICE_CENTS` | `25` | Price floor — skip cheaper contracts |
| `KELLY_ENABLED` | `true` | Use Kelly sizing (overrides flat stakes) |
| `KELLY_FRACTION` | `0.25` | Fraction of full Kelly to bet |
| `KELLY_ZERO_EDGE_STAKE` | `1.0` | Exploratory stake when Kelly says no edge |
| `EARLY_EXIT_ENABLED` | `true` | Monitor Conviction trades for BTC reversal |
| `EARLY_EXIT_REVERSAL_PCT` | `0.0015` | BTC reversal threshold to trigger exit (0.15%) |
| `EARLY_EXIT_MIN_HOLD_S` | `120` | Min seconds to hold before exit eligible |
| `EARLY_EXIT_MAX_LOSS_PCT` | `0.40` | Max acceptable loss % to execute exit |
| `STRONG_MOMENTUM_THRESHOLD` | `0.00065` | Above this, trade is flagged as "strong" (0.065%) |

## Version history

- **v1.0** — Initial strategy from 136-fill edge analysis. 0.03% momentum
  floor, no 60s confirmation, flat sizing.
- **v2.0** — Added Kelly sizing, auto-scoring throttle, early exit monitor,
  cheap-contract cap, disagreement gating.
- **v2.2** — 25c price floor (killed LOTTERY in practice). Cheap-cap
  threshold raised from 25c to 45c.
- **v3.0** — Momentum floor raised 0.03% → 0.06%. Added mandatory 60s
  confirmation. Kelly prior lowered to 0.52 WR (conservative).
- **v3.1** — Fixed Kelly zero-edge fallback ($25 → $1 exploratory stake).
- **v3.2** — Momentum floor raised 0.06% → 0.07% (two days of data showed
  ±0.060% signals producing both max-losses while all winners were ≥ 0.075%).
  Early exit reversal threshold lowered 0.3% → 0.15% (old threshold never
  triggered in 43+ fills across two days).
