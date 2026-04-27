# FADE_V1 — How it works (plain English)

**One-liner:** When BTC spikes hard in 5 minutes but the last 60 seconds
show the move is stalling, bet that it snaps back — trade the opposite side.

## The thesis

Every other strategy in the bot is momentum-following: BTC moved up, so buy
YES. FADE is the first **contrarian** strategy. It bets that momentum
overshoots.

Here's the pattern it targets:

1. BTC spikes ≥ 0.10% in 5 minutes — a strong, clear directional move.
2. The Kalshi contract reprices to reflect the spike.
3. But in the last 60 seconds, BTC's momentum is stalling or reversing.
4. By expiry, BTC has mean-reverted toward its pre-spike level.
5. The contract settles closer to 50/50 than the spiked price implied.

FADE captures this by buying the **opposite** side: if BTC spiked up and
the YES contract is expensive, buy NO. If BTC dropped and NO is expensive,
buy YES. The entry point is in the 40-50¢ band — where the payout is
roughly even and the risk/reward is symmetric.

## How the bot decides

Every cycle during the 5-12 minute window:

1. **Time check.** Only active when `FADE_MIN_SECS_LEFT` (300s) ≤
   secs_left ≤ `FADE_MAX_SECS_LEFT` (720s). FADE needs enough time for
   mean-reversion to play out — too close to expiry and the spike price
   sticks.

2. **Cooldown.** Won't re-fire within `FADE_COOLDOWN_S` (300s). Longer
   cooldown than other strategies because mean-reversion is a slower thesis.

3. **5-minute momentum: require a STRONG move.** BTC must have moved ≥ 0.10%
   in 5 minutes (`FADE_5M_MIN_MOMENTUM`). This is higher than SNIPER's
   0.06% threshold — FADE needs an overextension, not just a drift.

4. **60-second decay check.** This is the core signal. The last 60 seconds
   must show the move is **weakening**:
   - If the 60s momentum is small (below `FADE_60S_DECAY_THRESHOLD` of
     0.03%), the spike has stalled — good.
   - If the 60s momentum is strong BUT in the *opposite* direction from
     the 5m move, BTC is already reversing — also good.
   - If the 60s momentum is strong AND in the *same* direction as the 5m
     move, the spike is continuing — **pass**, don't fade a live trend.

5. **Volatility regime gate.** Mean-reversion works in high-vol environments
   (big moves snap back) but not in trending/low-vol ones (drifts persist).
   FADE maintains a rolling history of realized volatility and only fires
   when the current vol is above the `FADE_VOL_PERCENTILE_MIN` (50th
   percentile) of recent history. Needs at least 20 vol samples before it
   activates at all.

6. **Direction: opposite to 5m.** If BTC spiked up → buy NO. If BTC
   dropped → buy YES.

7. **Price band filter.** The entry price must be between
   `FADE_MIN_PRICE_CENTS` (40¢) and `FADE_MAX_PRICE_CENTS` (50¢). This
   keeps FADE in the "coin-flip zone" where:
   - The payout is roughly 2:1 (buy at 40-50¢, win 100¢).
   - The contract isn't already so cheap or expensive that the market has
     strong conviction one way.
   - Risk/reward is symmetric — a wrong bet loses ~45¢, a right bet wins
     ~55¢.

If all gates pass, FADE fires on the contrarian side.

## Why this is different from everything else

The bot's other strategies — SNIPER, EXPIRY_DECAY, BRIDGE — all follow
momentum or price certainty. They say "BTC is going up, buy YES" or "BTC is
clearly above the strike, buy YES." They agree with the market's direction
and bet on it continuing or being underpriced.

FADE says the opposite: "BTC went up too fast, the market overreacted, and
it's going to come back." This is portfolio diversification at the strategy
level. When a momentum strategy loses because the spike reversed, FADE's
thesis is that exact reversal — the two strategies are structurally
uncorrelated.

## Why it might lose

- **Momentum can persist.** "Don't fight the trend" is investing 101. A
  0.10% 5m move could be the start of a 0.30% move, not the end of one.
  The 60s decay check helps, but a brief pause doesn't guarantee reversal.
- **The 40-50¢ price band means ~50% base rate.** At 45¢, breakeven WR is
  45%. That's easy to clear, but the edge is thin — fees and slippage can
  erase a 55% WR.
- **Vol-regime gating needs history.** The first 20 cycles of any session
  are blind (not enough vol samples). If the best FADE opportunities are
  early in a session after a news spike, the model will miss them.
- **Mean-reversion timing is uncertain.** BTC may revert, but not within
  the 15-minute market window. A 12-minute-left entry gives 12 minutes for
  the revert, but if it takes 20 minutes, the trade still loses.

## Why it might win

- The pattern is real and well-documented: short-term BTC overreactions
  (especially on no-news spikes) frequently mean-revert within minutes.
- The 60s decay check is a strong filter — it doesn't fire on active trends,
  only on moves that are already losing steam.
- The price band (40-50¢) is where the payout math is most favorable for
  contrarian bets: symmetric risk, and the market hasn't already priced in
  the revert.
- Portfolio diversification: when SNIPER has a losing streak (momentum
  whipsaws), FADE should be in its element.

## Settings you can tune

All env vars, defaults shown:

| Var | Default | What it controls |
|---|---|---|
| `FADE_V1_BETA_ENABLED` | `false` | Whole strategy on/off |
| `FADE_MIN_SECS_LEFT` | `300` | Window opens at 12 min left |
| `FADE_MAX_SECS_LEFT` | `720` | Window closes at 5 min left |
| `FADE_5M_MIN_MOMENTUM` | `0.0010` | Min 5-min BTC move (0.10%) |
| `FADE_60S_DECAY_THRESHOLD` | `0.0003` | Max 60s momentum to count as "decaying" |
| `FADE_MIN_PRICE_CENTS` | `40` | Low end of price band |
| `FADE_MAX_PRICE_CENTS` | `50` | High end of price band |
| `FADE_VOL_PERCENTILE_MIN` | `0.50` | Min vol percentile (above median) |
| `FADE_COOLDOWN_S` | `300` | Per-market re-fire cooldown |
