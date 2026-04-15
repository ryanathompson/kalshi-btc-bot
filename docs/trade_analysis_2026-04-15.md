# 24h Trade Analysis — 2026-04-15 07:07 ET

Window: 2026-04-14 07:07 ET → 2026-04-15 07:07 ET (cutoff at `last_cycle`).
Source: `/api/status` history (83 attempts in window, 74 filled, 9 NO_FILLs).

## Headline numbers (last 24h)

| Strategy | Attempts | Filled | Wins | Losses | Win rate (filled) | PnL |
| --- | --- | --- | --- | --- | --- | --- |
| CONSENSUS | 43 | 38 | 16 | 22 | 42.1% | **+$35.55** |
| SNIPER | 40 | 36 | 17 | 19 | 47.2% | **−$4.84** |
| **Total** | **83** | **74** | **33** | **41** | **44.6%** | **+$30.71** |

Fill rate: 89.2%. Balance is $79.10 against a $80 daily-loss limit — one bad cluster from a halt.

Caveat: trade-level PnL sums to ~+$31, but `pnl_points` shows the curve only moved from ~−$378 to −$386.54 over the same window (~−$8). The deltas in `pnl_points` for individual trades match `history.pnl`, so the discrepancy likely comes from pre-restart points that got retroactively reconciled when `bot_trades.json` was rebuilt from Kalshi fills after the 21:29 ET restart. Treat +$30 as the upper-bound read and −$10 as the lower-bound; reality is somewhere between. Worth running `reconcile_pnl.py` before trusting either.

## Strategy-level ROI (cumulative, not 24h)

From `all_stats`, `con_stats`, `snp_stats`:
- CONSENSUS: 102 filled, 27.5% WR, ROI **−38.7%**, PnL −$324.16.
- SNIPER: 91 filled, 45.1% WR, ROI **−13.7%**, PnL −$34.47.
- CONSENSUS is carrying ~84% of the total drawdown. It's the bleeder.

## The biggest insight: price-band collapse

Cumulative `entry_price_stats`:

| Price band | Trades | WR | Verdict |
| --- | --- | --- | --- |
| 1–24¢ | 65 | **6.2%** | Catastrophic. Breakeven needs ~1/price ≈ 8–100%. Can't win at 6%. |
| 25–39¢ | 55 | **18.2%** | Still losing. Breakeven in this band is 32–40%. |
| 40–55¢ | 157 | **48.4%** | Roughly breakeven after fees; the only healthy zone. |

SNIPER's 1–24¢ book is **0 wins in 12 trades** (entry_price_stats_snp). The LOTTERY backtest assumed WR=10.5% with +4.8pp edge and +237% ROI — live WR is 0%. Backtest is dead.

CONSENSUS 1–24¢ is 2/31 (6.5%) — same story. CHEAP_CAP at $5 (via env override; default is $2) is softening bleed but not stopping it. In the 24h window the cheap CON entries hit two home runs (+$26 at 16¢, +$15 at 24¢) but lost repeatedly in between (−$8.27 at 28¢, −$7.98 at 38¢, −$5 at 2¢, −$4.92 at 12¢, −$4.83 at 21¢, −$4.48 at 8¢, −$4.52 at 4¢, −$4.22 at 7¢, −$4.95 twice at 15¢, −$3.12 at 1¢, −$5.44 at 32¢…). The wins exist but the long tail of small-count losses dominates.

## Mid-price (40–55¢) is where the money is — but only on CONSENSUS STRONG

In 24h, the large-PnL wins are all STRONG CONSENSUS at 40–55¢:
- 11:46 ET (04-14): +$24.30 @ 55¢ YES, STRONG, 54 contracts
- 16:01 ET (04-14): +$24.30 @ 55¢ YES, STRONG, 54 contracts
- 15:33 ET (04-14): +$12.92 @ 32¢ NO (not STRONG-flagged)
- 04:02 ET (04-15): +$6.96 @ 42¢ YES, STRONG

The SNIPER CONVICTION band (50–55¢) is close to coin-flip in 24h: ~17 wins vs ~15 losses, with small stakes (~$2–$5). It's not obviously losing money but not obviously making it either. Net −$4.84 is essentially noise.

## v2 features are mostly idle

`v2_counters`: disagreements_skipped=0, early_exits_triggered=0, cheap_caps_applied=6.

`disagreement_gating=true` and `early_exit_enabled=true` but neither has fired this session. Either the conditions are too narrow or there's a bug — worth a 5-minute read through the gating logic to confirm they're wired up to the live path. With CON at −39% ROI cumulative, any extra filter that catches disagreements between momentum and prev-result would be worth real money.

`autoscore_enabled=true` but all three strategies are showing `mult=1.0, status=FULL` right now — the throttling never kicked in despite the drawdown. Check the scorer thresholds; CON scoring 65.4 with −39% ROI feels too generous.

## Concrete suggestions to consider

1. **Kill the 1–24¢ band entirely** (for both strategies). Override `CHEAP_CONTRACT_PRICE` to skip instead of cap, or add a hard filter `if price_cents < 25: return None`. 6.2% WR across 65 trades is a statistically meaningful signal (95% CI roughly 2–14%), and breakeven is far above that.
2. **Tighten 25–39¢ CONSENSUS.** 18.2% WR overall; require the STRONG flag (1.5× momentum) or skip. The STRONG-only CON trades have been the actual winners this session.
3. **Investigate why disagreement/early-exit counters are zero.** Either feature is broken or thresholds are so tight they never trigger. Both should be firing multiple times a day given how often momentum flips inside the 15-min window.
4. **Recheck the auto-scorer.** CONSENSUS is at 65.4 on a strategy running −39% ROI. That's not throttling — that's full-throttle losing.
5. **Before any parameter change, run `reconcile_pnl.py --json`** to get ground-truth 24h PnL from Kalshi fills. The history-vs-pnl_points gap above means I can't be certain whether today netted +$30 or −$10.
