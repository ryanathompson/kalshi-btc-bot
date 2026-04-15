# v2 Feature Wiring Audit — 2026-04-15

Both `disagreements_skipped` and `early_exits_triggered` read 0 after ~10h of live trading (since the 21:29 ET restart on 04-14). This audit explains why, and whether it's a bug or a design issue.

## 1. Disagreement gating — structurally rare, likely by design

**Wiring is correct.** `bot.py:2326–2332`:

```python
if DISAGREEMENT_GATING and len(signals) > 1:
    sides = set(s["side"] for s in signals)
    if len(sides) > 1:
        ...
        self.v2_stats["disagreements_skipped"] += 1
        continue
```

**Why it never fires:** The guard requires CONSENSUS and SNIPER to BOTH emit a signal for the SAME market in the SAME cycle, on OPPOSITE sides. That's a narrow intersection because:

- Each strategy has different price zones. SNIPER CONVICTION only fires at 50–55¢. CONSENSUS often fires on cheap tails (now blocked by the 25¢ floor) or mid-price entries. For both to fire on one ticker, the market price has to be inside the SNIPER CONVICTION band AND pass the CONSENSUS dynamic cap.
- Once ONE strategy fires on a ticker, `self.traded_this_market.add(ticker)` at `bot.py:2365` permanently blocks any other strategy from trading that ticker for the market's lifetime. So the window is the very first cycle a market opens — which is typically a single cycle for a strategy to win the race.
- Both strategies' direction signals are derived from the same BTC momentum reading. If momentum is up, both will lean yes. Momentum and previous-result disagreeing on direction (the only way CON side differs from SNI side) is rare.

**Recommendation:** Don't fix this — the feature's gating condition is essentially unreachable under the current architecture. Either repurpose it (e.g., gate when ANY signal emits while an opposing open position exists in a NEIGHBORING strike) or remove the counter from the dashboard to avoid implying it's live.

## 2. Early exit — wiring is correct, logic threshold is wrong

**Wiring is correct.** `bot.py:2208–2254` runs `check_exits` at the top of each cycle (after `self._last_markets = markets` is set at 2187). Positions are tracked at 2370 on every SNIPER CONVICTION signal. In 24h the bot opened 17 SNIPER CONVICTION positions, so the monitor had inventory to check on every subsequent cycle.

**The logic bug is in `PositionMonitor.check_exits`** (`bot.py:1508–1548`):

```python
EARLY_EXIT_REVERSAL_PCT  = 0.003   # 0.3% BTC reversal to trigger check
EARLY_EXIT_MIN_HOLD_S    = 120     # 2 min minimum hold
EARLY_EXIT_MAX_LOSS_PCT  = 0.10    # exit only if loss_pct <= 10%

loss_pct = (entry_price - current_bid) / entry_price
if loss_pct <= EARLY_EXIT_MAX_LOSS_PCT:
    exits.append(...)
```

The chain of conditions is:

1. BTC has reversed ≥0.3% against entry (`adverse` check)
2. Current bid is still within 10% of entry price (`loss_pct <= 0.10`)

**These two conditions rarely co-occur.** When BTC reverses 0.3% on a 15-min KXBTC market, the Kalshi book repriced fast — a 55¢ entry typically gets marked down to 30–45¢ (loss_pct 18–45%), which fails check (2). By the time the BTC reversal is detectable (condition 1), the market has already priced in too much damage for condition (2).

The comment at 1535 reads "exit if can recover 90%+", which suggests the intent was to only exit when the damage is still small. But the effect is "never exit because damage is always already large by the time we look."

**Recommendation:** Either widen the window or invert the check:

- **Widen** (least invasive): raise `EARLY_EXIT_MAX_LOSS_PCT` to 0.50 (allow exits in the 0–50% loss zone).
- **Invert** (more correct): exit whenever (condition 1) holds, unless bid is already 0/illiquid. The adverse BTC move IS the signal; salvaging 50¢ on a 100¢ contract headed to 0 is the whole point.

Low-risk first step: set `EARLY_EXIT_MAX_LOSS_PCT=0.40` via env on Render and watch whether `early_exits_triggered` starts incrementing. If it does, review the outcomes before making it permanent.

## 3. Auto-scorer — not audited yet

Separate task. CON showing `score=65.4, mult=1.0, status=FULL` while running −39% ROI cumulative is suspicious but the scorer likely uses a short lookback window that hides the cumulative bleed. Worth a read pass when you have time.

## Summary

| Feature | Status | Action |
| --- | --- | --- |
| Disagreement gating | Wired correctly, unreachable under current market/cycle architecture | Repurpose or remove the counter |
| Early exit | Wired correctly, `MAX_LOSS_PCT` threshold too tight (blocks all realistic exits) | Raise `EARLY_EXIT_MAX_LOSS_PCT` to 0.40 via env; observe for 24h |
| Auto-scorer | Not audited | Read code when available |
