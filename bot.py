"""
Kalshi BTC 15-Minute Multi-Strategy Bot v2.0 "Edge Engine"
===========================================================
Runs multiple strategies in parallel on Kalshi's KXBTC15M markets:

  STRATEGY 1 â LAG BOT
    Detects when Kalshi contract prices haven't caught up to live BTC
    price moves on Coinbase. When BTC moves sharply but Kalshi hasn't
    repriced yet, trades into the mispricing.

  STRATEGY 2 â CONSENSUS BOT v1.2
    Combines two signals:
      - MOMENTUM: BTC price direction over last 60 seconds
      - PREVIOUS: Result of the last settled 15-min market
    Only trades when both signals agree AND price is <= threshold.
    Historically ~73% win rate in backtests (not validated OOS).

    v1.1 improvements:
      1. Momentum dead zone â ignores noise below 0.03% BTC move
      2. Previous-result staleness â signal expires after 30 min
      3. Dynamic price cap â scales with momentum strength (0.45â0.55)
      4. Time-aware entry â tightens threshold as market nears close
      5. Trade cooldown â 300s minimum between consensus trades
      6. API throttle â polls settled markets every 60s, not every 5s

    v1.2 improvements (2026-04-10, post live post-mortem):
      1. Dead zone re-tuned to 0.05% (was 0.20% emergency floor; live
         wins clustered at |momentum| 0.065-0.092%, suggesting 0.03% was
         too permissive but 0.20% killed the signal entirely)
      2. Strong-momentum tier: when |momentum| >= STRONG_MOMENTUM_THRESHOLD
         (0.065%) the strategy applies a stake multiplier and a small
         price-cap bonus, leaning into the band where wins concentrate

  STRATEGY 3 — SNIPER v1.0 (2026-04-12)
    Data-driven directional strategy derived from edge analysis of 136
    ground-truth fills. Exploits two price zones where the market
    systematically misprices:
      - LOTTERY (1-10c):   Deep OTM, +4.8pp edge, +237% ROI
      - CONVICTION (50-55c): ATM+, +9.1pp edge, +18.5% ROI
    Primary filter: 5-minute BTC momentum alignment (+2.5pp edge, n=48).
    Avoids 11-49c "kill zone" where all sub-bands show -13pp to -15pp edge.

  v2.0 "EDGE ENGINE" (2026-04-13)
    Four cross-cutting upgrades inspired by ensemble-AI trading systems:
    1. KELLY CRITERION SIZING — Quarter-Kelly bankroll-proportional position
       sizing replaces flat-dollar stakes. Each strategy/mode uses measured
       win rates and payoff ratios to size optimally.
    2. AUTO-SCORING THROTTLE — Rolling 20-trade scorer (ROI/trend/WR) that
       auto-blocks strategies scoring <25 and throttles to 0.5x below 50.
    3. SIGNAL DISAGREEMENT GATING — When multiple strategies fire on the
       same market but disagree on direction, the market is skipped entirely.
    4. DYNAMIC EARLY EXIT — Sniper Conviction trades (50-55c) are monitored
       for BTC reversals >0.15%. Sells at small loss vs riding to $0 settlement.

SETUP:
    pip install requests cryptography colorama tabulate python-dotenv

    Create a .env file:
        KALSHI_API_KEY_ID=your-key-id-here
        KALSHI_PRIVATE_KEY_BASE64=<base64-encoded contents of your .pem file>
        LAG_STAKE=25
        CONSENSUS_STAKE=25
        DRY_RUN=false

    Get Coinbase BTC price (free, no auth):
        https://api.coinbase.com/v2/prices/BTC-USD/spot

GETTING YOUR API KEY:
    1. Log in to kalshi.com
    2. Account & Security â API Keys â Create Key
    3. Save the .key file and Key ID to your .env

â ï¸  RISK WARNING:
    This bot places REAL trades with REAL money.
    Start with small stakes. Never risk more than you can afford to lose.
    Past strategy performance does not guarantee future results.
    Run in DRY_RUN=true mode first to verify behavior.
"""

import os
import math
import re
import time
import uuid
import json
import base64
import datetime
import threading
import requests
try:
    from zoneinfo import ZoneInfo
except ImportError:                                    # pragma: no cover
    from backports.zoneinfo import ZoneInfo            # type: ignore

# ─── Trading timezone ───────────────────────────────────────────────────
# All human-facing times, trade log timestamps, and daily-rollover logic
# use US Eastern Time. ZoneInfo handles EDT/EST transitions automatically.
ET = ZoneInfo("America/New_York")


def now_et():
    """Current time as an aware datetime in US Eastern (EDT/EST)."""
    return datetime.datetime.now(ET)


def today_et():
    """Current calendar date in US Eastern."""
    return now_et().date()


def parse_trade_ts(ts):
    """Parse a trade-log timestamp to a tz-aware datetime.

    Trade records come in three flavors:
      - New bot-logged trades:  ET-aware ISO ('...-04:00' or '-05:00')
      - Legacy bot-logged:      Naive UTC from datetime.utcnow().isoformat()
      - Recovered via rebuild:  UTC with 'Z' suffix from Kalshi API

    Returns None if the string cannot be parsed.
    """
    if not ts:
        return None
    try:
        s = ts
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
        # Naive legacy records → assume UTC (that's what utcnow() produced)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt
    except Exception:
        return None


def trade_date_et(ts):
    """Calendar date (in ET) that a trade timestamp falls on, or None."""
    dt = parse_trade_ts(ts)
    return dt.astimezone(ET).date() if dt else None
import argparse
import websocket
from collections import deque
from dotenv import load_dotenv
from tabulate import tabulate
from colorama import Fore, Style, init
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding

load_dotenv()
init(autoreset=True)

# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# CONFIGURATION
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

BASE_URL       = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_URL       = "https://demo-api.kalshi.co/trade-api/v2"
BTC_TICKER     = "KXBTC15M"
ETH_TICKER     = "KXETH15M"
# BTC price sources tried in order until one succeeds
PRICE_SOURCES = [
    ("binance",   "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
     lambda r: float(r.json()["price"])),
    ("kraken",    "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
     lambda r: float(r.json()["result"]["XXBTZUSD"]["c"][0])),
    ("coingecko", "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
     lambda r: float(r.json()["bitcoin"]["usd"])),
]
POLL_INTERVAL  = 5          # seconds between polls (used for market/resolve checks)
COINBASE_WS_URL = "wss://advanced-trade-ws.coinbase.com"
# LOG_FILE — overridable so production can point at a Render persistent disk
# (e.g. TRADES_LOG_PATH=/var/data/bot_trades.json) instead of the ephemeral
# project filesystem. Defaults to repo-local file for backwards compatibility.
LOG_FILE       = os.getenv("TRADES_LOG_PATH", "bot_trades.json")

# Strategy thresholds (tune these after paper trading)
LAG_THRESHOLD_PCT   = 0.003  # BTC must move >0.3% in 90s for lag signal
LAG_MAX_REPRICE_AGE = 90     # seconds since last Kalshi price change to consider stale
CONSENSUS_MAX_PRICE = 0.55   # only trade consensus when price <= 55¢
MOMENTUM_WINDOW     = 60     # seconds for BTC momentum calculation
MIN_BOOK_SUM        = 0.97   # yes+no must sum >= 0.97 (liquid market check)

# ── Strategy enable flags ─────────────────────────────────────────────
# CONSENSUS was disabled 2026-04-09 after a live post-mortem (33% WR /
# -23.6% ROI over 18 trades). Re-enabled 2026-04-10 as v1.2 with a tighter
# dead zone (0.05%) and a strong-momentum tier. The 2026-04-09 losing band
# was |momentum| 0.030-0.043% with entry prices below 40c, both of which
# v1.2 filters out. Kill-switch via env: CONSENSUS_ENABLED=false (no redeploy).
LAG_ENABLED       = os.getenv("LAG_ENABLED",       "true").lower() == "true"
CONSENSUS_ENABLED = os.getenv("CONSENSUS_ENABLED", "false").lower() == "true"  # [v3.2] retired — replaced by CONSENSUS_V2 beta
SNIPER_ENABLED    = os.getenv("SNIPER_ENABLED",    "true").lower()  == "true"

# ── Beta models (Phase 3+) ──────────────────────────────────────────
# Beta strategies run alongside live, always dry-run, tagged with
# beta_model_id for isolated reporting. See /beta on the dashboard.
# Enable each independently via its own env var.
# CONSENSUS_V1_BETA_ENABLED retired 2026-04-29. Only one consensus model is
# ever active; the canonical switch is CONSENSUS_BETA_ENABLED below, which
# currently drives CONSENSUS_V2.
# EXPIRY_DECAY_V2 PROMOTED to live 2026-04-30 (commit message has full stats
# snapshot per docs/beta_promotion.md). Canonical env name going forward is
# EXPIRY_DECAY_ENABLED. The two BETA_ENABLED forms are honored as fallbacks
# so the existing Render deploy doesn't silently turn the strategy off when
# the new code rolls out — rename `EXPIRY_DECAY_V2_BETA_ENABLED=true` to
# `EXPIRY_DECAY_ENABLED=true` on Render at your convenience.
EXPIRY_DECAY_ENABLED = (
    os.getenv("EXPIRY_DECAY_ENABLED",
              os.getenv("EXPIRY_DECAY_V2_BETA_ENABLED",
                        os.getenv("EXPIRY_DECAY_V1_BETA_ENABLED", "false"))).lower() == "true"
)

# One-shot diagnostic: dump the first Kalshi market JSON to stdout so we can
# see exactly what fields are available (reference price, subtitle, etc.).
# Flip to true on Render, restart, grab the log line, flip back to false.
LOG_MARKET_SAMPLE = os.getenv("LOG_MARKET_SAMPLE", "false").lower() == "true"

# Expiry-decay tunables. All env-overridable so we can iterate without code
# changes. Defaults chosen conservatively for v1: narrow time window, wide
# vol-distance floor, meaningful edge floor. Tighten later after data.
EXPIRY_DECAY_MAX_SECS_LEFT   = int(os.getenv("EXPIRY_DECAY_MAX_SECS_LEFT",   "180"))
EXPIRY_DECAY_MIN_SECS_LEFT   = int(os.getenv("EXPIRY_DECAY_MIN_SECS_LEFT",    "20"))
EXPIRY_DECAY_MIN_DIST_SIGMAS = float(os.getenv("EXPIRY_DECAY_MIN_DIST_SIGMAS", "2.0"))
EXPIRY_DECAY_MIN_EDGE_CENTS  = int(os.getenv("EXPIRY_DECAY_MIN_EDGE_CENTS",     "3"))
EXPIRY_DECAY_VOL_WINDOW_S    = int(os.getenv("EXPIRY_DECAY_VOL_WINDOW_S",     "900"))
EXPIRY_DECAY_COOLDOWN_S      = int(os.getenv("EXPIRY_DECAY_COOLDOWN_S",        "60"))
# v2: contrarian-lottery filter. Sub-5c entries in v1 dry-run sample
# (n=76, 2026-04-23 → 2026-04-25) were 24-for-24 losers — the GBM model
# under-estimates pinning at the extremes and consistently fights a
# near-certain market in the final minutes. Floor at 5c is the cleanest
# cut: there were zero trades priced 5–10c in the sample, and floors of
# 11c+ start dropping real winners (+$161 at 11c, +$85 at 19c). See
# docs/expiry_decay_v2.md for the full breakdown.
EXPIRY_DECAY_MIN_PRICE_CENTS = int(os.getenv("EXPIRY_DECAY_MIN_PRICE_CENTS",     "5"))

# v2.1 (2026-04-30): Kelly sizing toggle. Mirrors BRIDGE_KELLY_ENABLED.
# Uses per-fire GBM model probability `p_side` as the win-rate input — same
# pattern as BRIDGE — rather than a static prior. Quarter-Kelly (global
# KELLY_FRACTION=0.25) and the global KELLY_MAX_BET_PCT=10% bankroll cap
# both apply, but ExpiryDecay additionally caps at self.stake
# (= MAX_STAKE_PER_TRADE) to preserve the single-stake invariant the
# dashboard advertises. When disabled, falls back to v2's flat self.stake.
EXPIRY_DECAY_KELLY_ENABLED   = os.getenv("EXPIRY_DECAY_KELLY_ENABLED", "true").lower() == "true"

# Diagnostic: when true, ExpiryDecayStrategy.evaluate() prints a single line
# per market evaluation showing which gate blocked (or "FIRE") with the
# relevant numbers. Sampled at most once per ticker per N seconds to keep
# the log volume sane.
EXPIRY_DECAY_VERBOSE          = os.getenv("EXPIRY_DECAY_VERBOSE", "false").lower() == "true"
EXPIRY_DECAY_VERBOSE_EVERY_S  = int(os.getenv("EXPIRY_DECAY_VERBOSE_EVERY_S", "30"))

# ── BRIDGE beta tunables ────────────────────────────────────────────
# Hybrid model covering the 3-5 min gap between SNIPER (5-14m) and
# EXPIRY_DECAY (0-3m). Requires BOTH momentum confirmation (SNIPER thesis)
# AND vol-normalized mispricing (EXPIRY_DECAY thesis) to fire.
# Strategy on/off switch. Renamed to BRIDGE_BETA_ENABLED for V2 since the
# V1/V2 distinction is a beta_model_id tag, not a separate strategy class.
# Legacy BRIDGE_V1_BETA_ENABLED still honored for backward compat — you can
# leave it set on Render until you swap to the new name.
BRIDGE_BETA_ENABLED         = (
    os.getenv("BRIDGE_BETA_ENABLED")
    or os.getenv("BRIDGE_V1_BETA_ENABLED", "false")
).lower() == "true"
# Keep the old constant name as an alias so any other modules / scripts
# that import it still work without a code change.
BRIDGE_V1_BETA_ENABLED      = BRIDGE_BETA_ENABLED
BRIDGE_MIN_SECS_LEFT        = int(os.getenv("BRIDGE_MIN_SECS_LEFT",        "180"))  # 3 min
BRIDGE_MAX_SECS_LEFT        = int(os.getenv("BRIDGE_MAX_SECS_LEFT",        "300"))  # 5 min
BRIDGE_5M_MIN_MOMENTUM      = float(os.getenv("BRIDGE_5M_MIN_MOMENTUM",  "0.0006"))  # was same as SNIPER v3; SNIPER raised to 0.07% in v3.2
BRIDGE_60S_CONFIRM_MIN      = float(os.getenv("BRIDGE_60S_CONFIRM_MIN",  "0.0001"))  # same as SNIPER v3
BRIDGE_MIN_DIST_SIGMAS      = float(os.getenv("BRIDGE_MIN_DIST_SIGMAS",    "1.5"))   # lighter than ED's 2.0
BRIDGE_MIN_EDGE_CENTS       = int(os.getenv("BRIDGE_MIN_EDGE_CENTS",         "2"))   # lighter than ED's 3
BRIDGE_VOL_WINDOW_S         = int(os.getenv("BRIDGE_VOL_WINDOW_S",         "900"))
BRIDGE_COOLDOWN_S           = int(os.getenv("BRIDGE_COOLDOWN_S",           "120"))
# v2 sizing toggle: route through kelly_size() with the GBM model probability
# (p_side) and apply CHEAP_CONTRACT_MAX_STAKE the same way Consensus/Sniper do.
# v1 used `count = stake / price`, which made a 2c NO and a 90c YES carry the
# same downside but ~400x different upside — equal-cost / unequal-variance.
BRIDGE_KELLY_ENABLED        = os.getenv("BRIDGE_KELLY_ENABLED", "true").lower() == "true"

# ── FADE beta tunables ──────────────────────────────────────────────
# Mean-reversion model that fades overextended BTC moves. Contrarian to
# SNIPER — trades AGAINST strong 5m momentum when 60s shows weakening.
FADE_V1_BETA_ENABLED        = os.getenv("FADE_V1_BETA_ENABLED", "false").lower() == "true"
FADE_MIN_SECS_LEFT          = int(os.getenv("FADE_MIN_SECS_LEFT",          "300"))  # 5 min
FADE_MAX_SECS_LEFT          = int(os.getenv("FADE_MAX_SECS_LEFT",          "720"))  # 12 min
FADE_5M_MIN_MOMENTUM        = float(os.getenv("FADE_5M_MIN_MOMENTUM",    "0.0010"))  # 0.10% — strong move required
FADE_60S_DECAY_THRESHOLD    = float(os.getenv("FADE_60S_DECAY_THRESHOLD", "0.0003"))  # 60s must be below this (weakening)
FADE_MIN_PRICE_CENTS        = int(os.getenv("FADE_MIN_PRICE_CENTS",         "40"))
FADE_MAX_PRICE_CENTS        = int(os.getenv("FADE_MAX_PRICE_CENTS",         "50"))
FADE_VOL_PERCENTILE_MIN     = float(os.getenv("FADE_VOL_PERCENTILE_MIN",  "0.50"))   # only in elevated vol
FADE_COOLDOWN_S             = int(os.getenv("FADE_COOLDOWN_S",             "300"))

# ── CONSENSUS_V2 beta (momentum-regime hybrid for 36-45c zone) ───────
# Uses SNIPER-grade 5m momentum + previous-result regime agreement to
# trade the 36-45c OTM band that SNIPER's kill-zone filter rejects.
# V1 data showed this band at 63.6% WR / 71.3% ROI across 33 trades.
# Switch is CONSENSUS_BETA_ENABLED (canonical, version-agnostic — only one
# consensus model is ever active). Legacy CONSENSUS_V2_BETA_ENABLED is
# honored as a fallback so a Render env rename isn't required immediately.
CONSENSUS_BETA_ENABLED      = (
    os.getenv("CONSENSUS_BETA_ENABLED",
              os.getenv("CONSENSUS_V2_BETA_ENABLED", "true")).lower() == "true"
)
CONSENSUS_V2_5M_MIN_MOMENTUM = float(os.getenv("CONSENSUS_V2_5M_MIN_MOMENTUM", "0.0007"))  # same floor as SNIPER v3.2
CONSENSUS_V2_60S_CONFIRM_MIN = float(os.getenv("CONSENSUS_V2_60S_CONFIRM_MIN", "0.0001"))  # same as SNIPER v3
CONSENSUS_V2_MIN_PRICE_CENTS = int(os.getenv("CONSENSUS_V2_MIN_PRICE_CENTS",     "36"))
CONSENSUS_V2_MAX_PRICE_CENTS = int(os.getenv("CONSENSUS_V2_MAX_PRICE_CENTS",     "45"))
CONSENSUS_V2_COOLDOWN_S      = int(os.getenv("CONSENSUS_V2_COOLDOWN_S",         "300"))  # 5 min
CONSENSUS_V2_MIN_MINS_LEFT   = float(os.getenv("CONSENSUS_V2_MIN_MINS_LEFT",      "5"))

# ── v2.0 Unified stake ───────────────────────────────────────────────
# Single max-stake-per-trade replaces the old per-strategy LAG_STAKE /
# CONSENSUS_STAKE split.  Every strategy draws from the same budget;
# Kelly sizing then adjusts within this ceiling based on edge.
MAX_STAKE_PER_TRADE = float(os.getenv("MAX_STAKE_PER_TRADE",
                            os.getenv("LAG_STAKE",
                            os.getenv("CONSENSUS_STAKE", "25"))))

# Consensus v1.2 tuning
CONSENSUS_BASE_PRICE = 0.45  # base price cap before momentum scaling
# MOMENTUM_DEAD_ZONE history:
#   v1.0  = 0.0003 (0.03%) - too permissive, fired on noise
#   v1.1+ = 0.0020 (0.20%) - emergency floor 2026-04-09, killed all signal
#   v1.2  = 0.0005 (0.05%) - 2026-04-10, tuned from screen of live trades
#                            (15 losses with momentum 0.030-0.043%, 4 wins
#                            with |momentum| 0.065-0.092%; 0.05% cleanly
#                            separates the loser cluster from the winners).
MOMENTUM_DEAD_ZONE  = float(os.getenv("MOMENTUM_DEAD_ZONE", "0.0005"))

# v1.2 strong-momentum tier: when |momentum| >= STRONG_MOMENTUM_THRESHOLD
# the trade is sized larger and the price cap is extended slightly. This
# leans into the momentum band where wins clustered in live trading.
STRONG_MOMENTUM_THRESHOLD    = float(os.getenv("STRONG_MOMENTUM_THRESHOLD",    "0.00065"))
CONSENSUS_STRONG_STAKE_MULT  = float(os.getenv("CONSENSUS_STRONG_STAKE_MULT",  "1.5"))
CONSENSUS_STRONG_PRICE_BONUS = float(os.getenv("CONSENSUS_STRONG_PRICE_BONUS", "0.05"))

PREV_RESULT_MAX_AGE = 1800   # previous-result signal expires after 30 min (seconds)
CONSENSUS_COOLDOWN  = 300    # min seconds between consensus trades
PREV_CHECK_INTERVAL = 60     # only poll settled markets every 60s (not every cycle)

# Sniper v1.0 tuning — derived from edge analysis of 136 ground-truth fills
# See analyze_edge.py output for the full data backing these thresholds.
# [v3.0] Raised from 0.0003 (0.03%) → 0.0006 (0.06%).  Four days of SNIPER-
# only data (193 fills, 50% WR) showed the 0.03% floor caught noise trades
# that diluted the edge.  Top-5 winners consistently had 5m moves ≥ 0.04%;
# losers clustered at the 0.03-0.05% floor.  Doubling the threshold filters
# the weakest signals.  Override via env: SNIPER_5M_MIN_MOMENTUM=0.0003.
# [v3.2] Raised from 0.0006 (0.06%) → 0.0007 (0.07%).  Two days of live
# data (43 fills) showed both $1.00 max-losses had 5m signals at exactly
# ±0.060% — right at the old floor.  All top-5 winners had |5m| ≥ 0.075%.
# The 0.01% bump filters the weakest conviction entries while preserving
# every observed winner.  Also creates real separation from
# STRONG_MOMENTUM_THRESHOLD (0.065%).
SNIPER_5M_MIN_MOMENTUM = float(os.getenv("SNIPER_5M_MIN_MOMENTUM", "0.0007"))  # 0.07%
SNIPER_COOLDOWN        = int(os.getenv("SNIPER_COOLDOWN",   "180"))   # seconds between sniper trades
# Sniper stake defaults — fall back to MAX_STAKE_PER_TRADE when not explicitly set.
SNIPER_LOTTERY_STAKE    = float(os.getenv("SNIPER_LOTTERY_STAKE")    or MAX_STAKE_PER_TRADE)
SNIPER_CONVICTION_STAKE = float(os.getenv("SNIPER_CONVICTION_STAKE") or MAX_STAKE_PER_TRADE)
SNIPER_MIN_MINS_LEFT   = float(os.getenv("SNIPER_MIN_MINS_LEFT", "5"))  # skip markets with < N min left

# ── v2.0 "Edge Engine" configuration ─────────────────────────────────
# Kelly Criterion sizing: replaces flat-dollar stakes with bankroll-
# proportional sizing based on measured edge per strategy/mode.
KELLY_ENABLED       = os.getenv("KELLY_ENABLED", "true").lower() == "true"
KELLY_FRACTION      = float(os.getenv("KELLY_FRACTION", "0.25"))   # quarter-Kelly
KELLY_MAX_BET_PCT   = float(os.getenv("KELLY_MAX_BET_PCT", "0.10"))  # never bet >10% of bankroll
KELLY_MIN_TRADES    = int(os.getenv("KELLY_MIN_TRADES", "10"))     # need N trades before trusting rolling stats

# Kelly correlation adjustment — phase-1 log-only instrumentation.
# ──────────────────────────────────────────────────────────────────
# Kelly in bot.py is currently sized per-trade with no awareness of
# other open positions, so stacked correlated bets (e.g. back-to-back
# 15-min markets same side) effectively oversize the portfolio. This
# block computes a correlation shrinkage factor and decorates every
# signal record with both raw and adjusted Kelly stakes — but does
# NOT mutate the actual order in phase 1. Flip to apply-mode later by
# setting CORR_ADJ_ENABLED=true and CORR_ADJ_LOG_ONLY=false (phase 2
# wiring lives in annotate_correlation; search "phase 2").
#
# Buckets (against open same-side positions, by close-time delta):
#   ≤15 min → OVERLAP   (back-to-back window, highest correlation)
#   15–60   → CLOSE     (same hour)
#   >60 same ET-day → SAME_DAY
# Factors compose multiplicatively; floored at CORR_ADJ_MIN_FLOOR.
CORR_ADJ_ENABLED         = os.getenv("CORR_ADJ_ENABLED",  "false").lower() == "true"
CORR_ADJ_LOG_ONLY        = os.getenv("CORR_ADJ_LOG_ONLY", "true").lower()  == "true"
CORR_ADJ_OVERLAP_FACTOR  = float(os.getenv("CORR_ADJ_OVERLAP_FACTOR",  "0.5"))
CORR_ADJ_CLOSE_FACTOR    = float(os.getenv("CORR_ADJ_CLOSE_FACTOR",    "0.7"))
CORR_ADJ_SAME_DAY_FACTOR = float(os.getenv("CORR_ADJ_SAME_DAY_FACTOR", "0.9"))
CORR_ADJ_MIN_FLOOR       = float(os.getenv("CORR_ADJ_MIN_FLOOR",       "0.25"))

# Known edge parameters (from analyze_edge.py ground-truth data).
# These are used as priors until enough live trades accumulate for
# rolling stats to take over via StrategyScorer.
KELLY_PRIORS = {
    "SNIPER_LOTTERY":    {"win_rate": 0.105, "avg_price": 0.055},  # +4.8pp edge (backtest — zone unreachable with 25c floor)
    # [v3.0] Conviction prior lowered from 0.615 → 0.52.  Four days of live
    # SNIPER-only data showed 50% WR on 193 fills — the backtest 61.5% WR
    # was optimistic.  0.52 is a conservative estimate for post-v3-filter WR
    # (tighter momentum floor + 60s confirm should lift above coin-flip, but
    # we don't want Kelly oversizing before we prove it).  Rolling stats via
    # StrategyScorer will override this prior once KELLY_MIN_TRADES fills.
    "SNIPER_CONVICTION": {"win_rate": 0.52,  "avg_price": 0.525},  # conservative live-adjusted
    "CONSENSUS":         {"win_rate": 0.55,  "avg_price": 0.40},   # conservative prior (V1, retired)
    "CONSENSUS_V2":      {"win_rate": 0.60,  "avg_price": 0.41},   # from V1 36-45c band: 63.6% WR, conservative at 60%
    "LAG":               {"win_rate": 0.50,  "avg_price": 0.50},   # structural edge, flat sizing
}

# Auto-scoring: rolling performance tracker that throttles or kills
# strategies when trailing performance drops below thresholds.
AUTOSCORE_ENABLED         = os.getenv("AUTOSCORE_ENABLED", "true").lower() == "true"
AUTOSCORE_WINDOW          = int(os.getenv("AUTOSCORE_WINDOW", "20"))      # rolling trade window
AUTOSCORE_MIN_TRADES      = int(os.getenv("AUTOSCORE_MIN_TRADES", "5"))   # need N trades before scoring
AUTOSCORE_BLOCK_THRESHOLD = float(os.getenv("AUTOSCORE_BLOCK_THRESHOLD", "25"))   # score < this = blocked
AUTOSCORE_THROTTLE_THRESHOLD = float(os.getenv("AUTOSCORE_THROTTLE_THRESHOLD", "50"))  # score < this = 0.5x stake

# Signal disagreement gating: when multiple strategies fire on the
# same market but disagree on direction, skip the market entirely.
DISAGREEMENT_GATING = os.getenv("DISAGREEMENT_GATING", "true").lower() == "true"

# Dynamic early exit: sell Conviction-zone positions if BTC reverses
# hard after entry, instead of riding to settlement at $0.
EARLY_EXIT_ENABLED       = os.getenv("EARLY_EXIT_ENABLED", "true").lower() == "true"
# [v3.2] Lowered from 0.003 (0.3%) → 0.0015 (0.15%).  The old 0.3%
# threshold almost never triggered in 15-min BTC markets — early_exits
# counter was 0 across 43+ fills over two days.  Most losing Conviction
# trades bleed from momentum stalling/drifting, not violent reversals.
# 0.15% is reachable in 13 min (15m contract minus 2m min hold) and
# catches moderate reversals before they ride to zero settlement.
EARLY_EXIT_REVERSAL_PCT  = float(os.getenv("EARLY_EXIT_REVERSAL_PCT", "0.0015"))  # 0.15% BTC reversal
EARLY_EXIT_MIN_HOLD_S    = int(os.getenv("EARLY_EXIT_MIN_HOLD_S", "120"))        # 2 min minimum hold
EARLY_EXIT_MAX_LOSS_PCT  = float(os.getenv("EARLY_EXIT_MAX_LOSS_PCT", "0.40"))   # [v2.5] widened from 0.10 — see docs/v2_feature_audit_2026-04-15.md

# Consensus edge-gone exit — beta only in phase 1.
# ──────────────────────────────────────────────────────────────────
# Unlike the Conviction tracker (which exits on raw BTC reversal),
# this tracker exits when the Consensus entry *signal* would no
# longer fire. Two trigger classes:
#   HARD — signal inverted:
#     * current momentum_side flipped vs entry (still outside dead_zone)
#     * last_result flipped vs entry
#     Action: close regardless of loss, capped at HARD_MAX_LOSS.
#   SOFT — signal decayed:
#     * |momentum| fell into dead zone
#     * last_result aged past PREV_RESULT_MAX_AGE
#     Action: close only if current loss <= SOFT_MAX_LOSS.
#
# Runs as a self-contained beta model (CONSENSUS_V1_WITH_EXIT) that
# mirrors CONSENSUS_V1 entries and simulates exits against current
# top-of-book bid. Zero live order activity. Compare net P&L vs the
# CONSENSUS_V1 ride-to-resolution baseline to measure exit alpha.
CONSENSUS_EDGE_EXIT_BETA_ENABLED = os.getenv("CONSENSUS_EDGE_EXIT_BETA_ENABLED", "false").lower() == "true"
CONSENSUS_EDGE_EXIT_MIN_HOLD_S   = int(os.getenv("CONSENSUS_EDGE_EXIT_MIN_HOLD_S",   "90"))
CONSENSUS_EDGE_EXIT_SOFT_MAX_LOSS = float(os.getenv("CONSENSUS_EDGE_EXIT_SOFT_MAX_LOSS", "0.25"))
CONSENSUS_EDGE_EXIT_HARD_MAX_LOSS = float(os.getenv("CONSENSUS_EDGE_EXIT_HARD_MAX_LOSS", "0.90"))

# Cheap-contract stake cap: lottery-ticket entries (<25c) keep their
# asymmetric upside but stakes are capped to limit bleed during dry
# streaks.  Kelly tends to oversize these because the payout multiple
# is huge, even when the actual win rate doesn't support it.
# [v2.2] Cheap-contract cap raised from 25c/$5 → 45c/$2 after 2026-04-14
# post-mortem: Consensus blew up on 31–41c positions sized at 15–20ct,
# all below the prior 25c threshold, so the cap never fired. Every losing
# Consensus cheap trade that day was in the 30–45c band. The new default
# covers the full cheap-tail entry zone where Kelly was over-sizing.
# Override via env if needed — e.g. CHEAP_CONTRACT_PRICE=35 for a softer cap.
CHEAP_CONTRACT_PRICE     = int(''.join(c for c in os.getenv("CHEAP_CONTRACT_PRICE", "45") if c.isdigit()) or "45")  # cents threshold
CHEAP_CONTRACT_MAX_STAKE = float(os.getenv("CHEAP_CONTRACT_MAX_STAKE", "2.0"))  # max $ on cheap entries

# [v2.2] Hard floor on entry price — sub-25c band was bleeding badly in live trading.
# Cumulative 24h analysis (docs/trade_analysis_2026-04-15.md):
#   - Overall 1-24c:    4 wins / 65 trades = 6.2% WR (need ~10%+ to break even)
#   - SNIPER 1-24c:     0 wins / 12 trades = 0% WR (LOTTERY backtest was wrong)
#   - CONSENSUS 1-24c:  2 wins / 31 trades = 6.5% WR
# Applied to BOTH strategies before any sizing logic. Set to 0 via env to disable.
MIN_ENTRY_PRICE_CENTS = int(''.join(c for c in os.getenv("MIN_ENTRY_PRICE_CENTS", "25") if c.isdigit()) or "25")

# [v2.4] CONSENSUS STRONG-only gate. In a choppy-BTC regime on 2026-04-15,
# non-STRONG CONSENSUS was the dominant bleed:
#   - CON STRONG    (x1.5 momentum): 14 trades, 50.0% WR, PnL  -$0.73  (breakeven)
#   - CON no-STRONG:                 26 trades, 23.1% WR, PnL -$53.62  (100% of CON loss)
# With this flag enabled (default), CONSENSUS returns no signal unless
# strong_momentum is True. Set CONSENSUS_STRONG_ONLY=false via env to
# restore the pre-v2.4 behavior and re-admit non-STRONG CONSENSUS entries.
CONSENSUS_STRONG_ONLY = os.getenv("CONSENSUS_STRONG_ONLY", "true").lower() == "true"

# [v3.0] 60-second momentum confirmation for SNIPER.
# The existing 60s check only *blocks* contradictions (60s opposes 5m).
# This upgrade *requires* positive confirmation: 60s must move in the same
# direction as 5m above a minimum magnitude.  Without confirmation, many
# SNIPER entries fire on decaying or stalled momentum that happened to
# clear 5m threshold but isn't actively continuing — these are coin flips.
# Set SNIPER_REQUIRE_60S_CONFIRM=false via env to disable.
SNIPER_REQUIRE_60S_CONFIRM = os.getenv("SNIPER_REQUIRE_60S_CONFIRM", "true").lower() == "true"
SNIPER_60S_CONFIRM_MIN     = float(os.getenv("SNIPER_60S_CONFIRM_MIN", "0.0001"))  # 0.01% — same direction required

# [v3.2] Momentum divergence filter — block trades where the 5m momentum is
# strong but 60s has largely faded, indicating the move already exhausted.
# Gap = |5m| - |60s|.  When gap > threshold, the 5m signal is stale — the
# bulk of the move happened > 60s ago and isn't continuing.
# Three days of live data (30 SNIPER fills with both 5m and 60s):
#   gap > 0.08%: blocks 4 trades (1W/3L, net PnL saved $1.06)
#   gap > 0.06%: blocks 5 trades (1W/4L, net PnL saved $1.60) — tighter but costs one $1 winner
# Default 0.08% is conservative — catches the worst exhausted-momentum trades
# while minimizing false positives. Set to 999 via env to effectively disable.
MOMENTUM_DIVERGENCE_MAX = float(os.getenv("MOMENTUM_DIVERGENCE_MAX", "0.0008"))  # 0.08%


# ═══════════════════════════════════════════════════════════
# KEEP-ALIVE (prevents Render free-tier spin-down)
# ═══════════════════════════════════════════════════════════

def start_keep_alive(interval=780):
    """
    Ping our own Render URL every `interval` seconds (default 13 min)
    to prevent the free-tier 15-min inactivity spin-down.

    Uses RENDER_EXTERNAL_URL (set automatically by Render) or
    SELF_URL as a manual fallback.  Does nothing if neither is set
    (i.e. running locally).
    """
    url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_URL")
    if not url:
        print("  [keep-alive] No RENDER_EXTERNAL_URL or SELF_URL — skipping.")
        return

    if not url.startswith("http"):
        url = "https://" + url

    def _ping():
        while True:
            try:
                requests.get(url + "/health", timeout=10)
            except Exception:
                pass
            time.sleep(interval)

    t = threading.Thread(target=_ping, daemon=True, name="keep-alive")
    t.start()
    print(f"  [keep-alive] Pinging {url}/health every {interval}s")


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# AUTH
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def load_private_key(path=None):
    """
    Load RSA private key. Tries three sources in order:

    1. KALSHI_PRIVATE_KEY_PEM  â raw PEM content (paste the key directly,
       with real newlines or literal \\n).  Easiest to set in Render.
    2. KALSHI_PRIVATE_KEY_BASE64 â base64-encoded PEM (legacy option).
    3. File at KALSHI_PRIVATE_KEY_PATH (default: ./kalshi.key).
    """
    # ââ Option 1: raw PEM pasted directly into Render env var ââââââââââââââ
    pem_raw = os.getenv("KALSHI_PRIVATE_KEY_PEM")
    if pem_raw:
        # Render may store literal \n as the two-char sequence; normalise both.
        pem_bytes = pem_raw.replace("\\n", "\n").encode()
        return serialization.load_pem_private_key(
            pem_bytes, password=None, backend=default_backend()
        )

    # ââ Option 2: base64-encoded PEM âââââââââââââââââââââââââââââââââââââââ
    b64 = os.getenv("KALSHI_PRIVATE_KEY_BASE64")
    if b64:
        pem_bytes = base64.b64decode(b64.strip())
        return serialization.load_pem_private_key(
            pem_bytes, password=None, backend=default_backend()
        )

    # ââ Option 3: key file âââââââââââââââââââââââââââââââââââââââââââââââââ
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )

    raise RuntimeError(
        "No private key found. Set KALSHI_PRIVATE_KEY_PEM (paste PEM content) "
        "or KALSHI_PRIVATE_KEY_BASE64 (base64-encoded PEM) in Render env vars, "
        "or provide a valid KALSHI_PRIVATE_KEY_PATH file."
    )


def sign(private_key, timestamp, method, path):
    path_clean = path.split("?")[0]
    message = f"{timestamp}{method}{path_clean}".encode("utf-8")
    sig = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(sig).decode("utf-8")


def auth_headers(private_key, api_key_id, method, path):
    # Kalshi API signing timestamp must be POSIX epoch ms — use an
    # explicitly-UTC datetime so this is correct regardless of server TZ.
    ts = str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))
    from urllib.parse import urlparse
    sign_path = urlparse(BASE_URL + path).path
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sign(private_key, ts, method, sign_path),
        "Content-Type": "application/json",
    }


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# KALSHI CLIENT
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

class KalshiClient:
    def __init__(self, api_key_id, private_key, dry_run=True):
        self.key_id = api_key_id
        self.pkey   = private_key
        self.dry    = dry_run
        self.url    = BASE_URL

    def get(self, path, params=None):
        r = requests.get(
            self.url + path,
            headers=auth_headers(self.pkey, self.key_id, "GET", path),
            params=params,
            timeout=10
        )
        r.raise_for_status()
        return r.json()

    def post(self, path, data):
        r = requests.post(
            self.url + path,
            headers=auth_headers(self.pkey, self.key_id, "POST", path),
            json=data,
            timeout=10
        )
        return r

    def get_market(self, ticker):
        r = requests.get(f"{self.url}/markets/{ticker}", timeout=10)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("market")

    def get_markets_by_series(self, series, status="open"):
        r = requests.get(
            f"{self.url}/markets",
            params={"series_ticker": series, "status": status, "limit": 10},
            timeout=10
        )
        r.raise_for_status()
        return r.json().get("markets", [])

    def get_balance(self):
        d = self.get("/portfolio/balance")
        return d.get("balance", 0) / 100  # cents â dollars

    def place_order(self, ticker, side, price_cents, count, strategy_tag=""):
        # client_order_id serves two purposes:
        #   1. Strategy attribution after Render redeploy — the 3-letter prefix
        #      is preserved by Kalshi on /portfolio/orders so rebuild_trades_from_api()
        #      can recover which strategy placed each fill even after bot_trades.json
        #      is wiped by Render's ephemeral filesystem.
        #   2. Idempotency — Kalshi treats client_order_id as an idempotency key:
        #      "If an order with this client_order_id has already been placed, this
        #      request will be idempotent and return the existing order." Using a
        #      deterministic id keyed on (strategy, ticker, date) means a repeat call
        #      within the same day (from a retry, clock-boundary re-fire, or other
        #      race) CANNOT create a duplicate fill server-side. Previously this used
        #      a fresh uuid4 per call, which bypassed idempotency entirely and let
        #      bugs like the 15-min-boundary dedup clear generate real duplicate
        #      fills on Kalshi.
        prefix          = (strategy_tag or "UNK")[:3].upper()
        date_tag        = now_et().strftime("%Y%m%d")
        client_order_id = f"{prefix}-{ticker}-{date_tag}"
        order = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": count,
            "type": "limit",
            f"{'yes' if side == 'yes' else 'no'}_price": price_cents,
            "client_order_id": client_order_id,
        }
        if self.dry:
            return {"dry_run": True, "order": order}
        r = self.post("/portfolio/orders", order)
        if r.status_code == 201:
            return r.json()
        print(f"  Order failed: HTTP {r.status_code} â {r.text}", flush=True)
        return {"error": r.text, "status": r.status_code}

    def sell_order(self, ticker, side, price_cents, count, strategy_tag=""):
        """Sell an existing position (for early exit). Same as place_order but action=sell."""
        prefix = (strategy_tag or "EXT")[:3].upper()
        client_order_id = f"{prefix}-{uuid.uuid4()}"
        order = {
            "ticker": ticker,
            "action": "sell",
            "side": side,
            "count": count,
            "type": "limit",
            f"{'yes' if side == 'yes' else 'no'}_price": price_cents,
            "client_order_id": client_order_id,
        }
        if self.dry:
            return {"dry_run": True, "order": order}
        r = self.post("/portfolio/orders", order)
        if r.status_code == 201:
            return r.json()
        print(f"  [sell] Order failed: HTTP {r.status_code} -- {r.text}", flush=True)
        return {"error": r.text, "status": r.status_code}


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# PRICE DATA
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

class BTCPriceFeed:
    """
    Real-time BTC price feed via Coinbase Advanced Trade WebSocket.

    Connects to the public `ticker` channel (no auth required) and receives
    a push every time a BTC-USD trade matches on Coinbase.  Falls back to
    REST polling if the WebSocket drops.

    The public interface (current, price_at, pct_change, fetch) is unchanged
    so both LAG and CONSENSUS strategies work without modification.
    """
    def __init__(self, window=500):
        self.history   = deque(maxlen=window)
        self._ws       = None
        self._ws_alive = False
        self._lock     = threading.Lock()
        self._stop     = threading.Event()

    # ── WebSocket lifecycle ────────────────────────────────
    def start(self):
        """Launch the WebSocket listener in a background thread."""
        t = threading.Thread(target=self._ws_loop, daemon=True, name="btc-ws")
        t.start()
        print("  [price] WebSocket feed starting...", flush=True)
        # Wait up to 10s for first price to arrive
        deadline = time.time() + 10
        while not self.history and time.time() < deadline:
            time.sleep(0.2)
        if self.history:
            print(f"  [price] WebSocket connected  |  BTC ${self.current():,.0f}", flush=True)
        else:
            print("  [price] WebSocket slow to connect, falling back to REST", flush=True)
            self.fetch()  # seed with one REST call

    def stop(self):
        """Cleanly shut down the WebSocket."""
        self._stop.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _ws_loop(self):
        """Connect + reconnect loop with exponential backoff."""
        self._backoff = 1
        while not self._stop.is_set():
            try:
                print(f"  [price] WS connecting to {COINBASE_WS_URL}...", flush=True)
                self._connect()
            except Exception as e:
                print(f"  [price] WS connection failed: {type(e).__name__}: {e}", flush=True)
            self._ws_alive = False
            if self._stop.is_set():
                break
            wait = min(self._backoff, 30)
            print(f"  [price] WS disconnected, retrying in {wait}s (REST fallback active)...", flush=True)
            self._stop.wait(wait)
            self._backoff = min(self._backoff * 2, 30)

    def _connect(self):
        """Single WebSocket session: subscribe, read messages, handle pings."""
        ws = websocket.WebSocketApp(
            COINBASE_WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws = ws
        ws.run_forever(ping_interval=20, ping_timeout=10)

    def _on_open(self, ws):
        sub = json.dumps({
            "type": "subscribe",
            "product_ids": ["BTC-USD"],
            "channel": "ticker",
        })
        ws.send(sub)
        # Also subscribe to heartbeats to keep connection alive during
        # quiet periods (Coinbase closes idle sockets after ~90s)
        hb = json.dumps({
            "type": "subscribe",
            "product_ids": ["BTC-USD"],
            "channel": "heartbeats",
        })
        ws.send(hb)
        self._ws_alive = True
        self._backoff  = 1  # reset backoff on successful connect
        print("  [price] WS connected, subscribed to ticker + heartbeats", flush=True)

    def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("channel") != "ticker":
            return
        for event in msg.get("events", []):
            for ticker in event.get("tickers", []):
                if ticker.get("product_id") == "BTC-USD":
                    try:
                        price = float(ticker["price"])
                    except (KeyError, ValueError):
                        continue
                    with self._lock:
                        self.history.append((time.time(), price))

    def _on_error(self, ws, error):
        if not self._stop.is_set():
            print(f"  [price] WS error: {error}", flush=True)

    def _on_close(self, ws, close_status, close_msg):
        self._ws_alive = False
        if close_status or close_msg:
            print(f"  [price] WS closed: status={close_status} msg={close_msg}", flush=True)

    # ── REST fallback ──────────────────────────────────────
    def fetch(self):
        """
        One-shot REST price fetch.  Used for:
          - Initial seed if WS is slow to connect
          - Fallback when WS has been down for a while
        """
        for name, url, parser in PRICE_SOURCES:
            try:
                r = requests.get(url, timeout=(3, 4))
                price = parser(r)
                with self._lock:
                    self.history.append((time.time(), price))
                return price
            except Exception:
                continue
        return None

    # ── Public interface (unchanged for strategies) ────────
    def current(self):
        with self._lock:
            return self.history[-1][1] if self.history else None

    def price_at(self, seconds_ago):
        """Return price from ~seconds_ago seconds back."""
        now = time.time()
        target = now - seconds_ago
        with self._lock:
            for ts, px in reversed(self.history):
                if ts <= target:
                    return px
        return None

    def pct_change(self, seconds=60):
        """% change over last N seconds. Positive = up."""
        now_px = self.current()
        old_px = self.price_at(seconds)
        if now_px and old_px:
            return (now_px - old_px) / old_px
        return None

    @property
    def is_live(self):
        """True if WebSocket is actively receiving data."""
        return self._ws_alive


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# TRADE LOG
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ


def rebuild_trades_from_api(client):
    """
    Pull fill & settlement history from Kalshi API and rebuild bot_trades.json.
    Called on startup so deploys never lose trade history.

    - GET /portfolio/fills  -> every matched trade
    - GET /portfolio/settlements -> market outcomes + revenue
    - Cross-references by ticker to compute result/pnl

    Fills placed outside the bot (manual UI trades) will show strategy="MANUAL".
    """
    print("[rebuild] Pulling trade history from Kalshi API...", flush=True)

    # ── Fetch all fills (paginated) filtered to BTC 15-min markets ──────
    all_fills = []
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = client.get("/portfolio/fills", params=params)
        except Exception as e:
            print(f"[rebuild] Error fetching fills: {e}", flush=True)
            return []
        fills = resp.get("fills", [])
        all_fills.extend(fills)
        cursor = resp.get("cursor")
        if not cursor or not fills:
            break

    # Filter to KXBTC15M tickers only
    btc_fills = [f for f in all_fills if BTC_TICKER in (f.get("ticker", "") or "")]
    print(f"[rebuild] Found {len(btc_fills)} BTC 15-min fills out of {len(all_fills)} total.", flush=True)

    if not btc_fills:
        return []

    # ── Fetch orders to recover strategy from client_order_id prefix ────
    # client_order_id is set to "{PREFIX}-{uuid}" where PREFIX is the
    # strategy tag (LAG / CON / etc.) — see KalshiClient.place_order().
    # This is the persistence story for strategy attribution: even after a
    # Render redeploy wipes bot_trades.json, rebuilding from Kalshi orders
    # restores the strategy field correctly instead of bucketing everything
    # as "RECOVERED".
    PREFIX_TO_STRATEGY = {"LAG": "LAG", "CON": "CONSENSUS", "TAI": "TAIL", "EXT": "EARLY_EXIT"}
    order_id_to_strategy = {}
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = client.get("/portfolio/orders", params=params)
        except Exception as e:
            print(f"[rebuild] Error fetching orders (strategy attribution unavailable): {e}", flush=True)
            break
        page = resp.get("orders", []) or []
        for o in page:
            oid  = o.get("order_id")
            coid = o.get("client_order_id") or ""
            if not oid:
                continue
            prefix = coid.split("-", 1)[0].upper() if "-" in coid else ""
            strat = PREFIX_TO_STRATEGY.get(prefix)
            if strat:
                order_id_to_strategy[oid] = strat
        cursor = resp.get("cursor")
        if not cursor or not page:
            break
    print(f"[rebuild] Recovered strategy tags for {len(order_id_to_strategy)} orders.", flush=True)

    # ── Fetch settlements (paginated) ───────────────────────────────────
    all_settlements = []
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = client.get("/portfolio/settlements", params=params)
        except Exception as e:
            print(f"[rebuild] Error fetching settlements: {e}", flush=True)
            break
        setts = resp.get("settlements", [])
        all_settlements.extend(setts)
        cursor = resp.get("cursor")
        if not cursor or not setts:
            break

    # Build lookup: ticker -> settlement info
    settlement_map = {}
    for s in all_settlements:
        ticker = s.get("ticker") or s.get("market_ticker", "")
        if BTC_TICKER in ticker:
            settlement_map[ticker] = s

    # ── Reconstruct trade records ───────────────────────────────────────
    # Group fills by order_id to consolidate partial fills
    from collections import defaultdict
    orders = defaultdict(list)
    for f in btc_fills:
        orders[f.get("order_id", f.get("fill_id", ""))].append(f)

    existing = load_trades()
    # Build a set of known order IDs for dedup.  Bot-logged trades store
    # the order_id at the top level OR nested inside order.order.order_id
    # or order.order_id (recovered).  We normalise all three paths.
    existing_order_ids = set()
    for t in existing:
        oid = t.get("order_id")
        if oid:
            existing_order_ids.add(oid)
        inner = t.get("order") or {}
        if isinstance(inner, dict):
            oid2 = inner.get("order_id")
            if oid2:
                existing_order_ids.add(oid2)
            inner2 = inner.get("order") or {}
            if isinstance(inner2, dict):
                oid3 = inner2.get("order_id")
                if oid3:
                    existing_order_ids.add(oid3)
    # Legacy records without an order_id: match on (ticker, FULL timestamp)
    # as a last-resort fallback. The previous version truncated the
    # timestamp to minute precision (ts[:16]), which silently collapsed
    # distinct orders that happened on the same ticker within the same
    # minute — e.g. the paired duplicate fills caused by the boundary
    # race. Using the full timestamp keeps legacy-record dedup working
    # while allowing multiple distinct fills on the same ticker.
    existing_tickers_ts = {
        (t.get("ticker", ""), t.get("timestamp", ""))
        for t in existing
        if not (
            t.get("order_id")
            or (t.get("order") or {}).get("order_id")
            or ((t.get("order") or {}).get("order") or {}).get("order_id")
        )
    }

    new_trades = []
    for order_id, fills in orders.items():
        # Use first fill for metadata
        f0 = fills[0]
        ticker = f0.get("ticker", "")
        side = f0.get("side", "")  # "yes" or "no"
        ts = f0.get("created_time") or f0.get("ts", "")

        # Skip if we already have this trade logged. order_id is the
        # primary key — Kalshi guarantees it's unique per order. The
        # (ticker, timestamp) fallback only covers truly legacy records
        # that were logged before order_id storage existed (the set is
        # filtered to those records above, so it can't accidentally
        # swallow distinct fills on the same ticker).
        if order_id in existing_order_ids:
            continue
        if existing_tickers_ts and (ticker, ts) in existing_tickers_ts:
            continue

        # Aggregate across partial fills
        total_count = sum(float(fi.get("count_fp", fi.get("count", 0))) for fi in fills)
        if side == "yes":
            price_dollars = float(f0.get("yes_price_dollars", 0))
        else:
            price_dollars = float(f0.get("no_price_dollars", 0))
        price_cents = int(round(price_dollars * 100))
        dollars = round(total_count * price_dollars, 2)

        # Check settlement
        sett = settlement_map.get(ticker)
        outcome = None
        result = None
        pnl = None
        if sett:
            outcome = sett.get("market_result", None)
            if outcome:
                won = (side == outcome)
                result = "WIN" if won else "LOSS"
                if won and price_dollars > 0:
                    pnl = round(dollars / price_dollars * (1 - price_dollars), 2)
                else:
                    pnl = -round(dollars, 2)

        # Recover strategy from client_order_id prefix if available;
        # fall back to "RECOVERED" for legacy orders without a prefix.
        recovered_strategy = order_id_to_strategy.get(order_id, "RECOVERED")
        trade = {
            "strategy":      recovered_strategy,
            "ticker":        ticker,
            "side":          side,
            "price":         price_cents,
            "count":         int(total_count) if total_count == int(total_count) else total_count,
            "dollars":       dollars,
            "reason":        f"Rebuilt from Kalshi API (order {order_id[:8]}...)",
            "timestamp":     ts,
            "dry_run":       False,
            "is_beta":       False,  # Kalshi-sourced fills are always live
            "beta_model_id": None,
            "order":         {"order_id": order_id, "recovered": True},
            "outcome":       outcome,
            "result":        result,
            "pnl":           pnl,
        }
        new_trades.append(trade)

    if new_trades:
        all_trades = existing + new_trades
        # Sort by parsed epoch (handles mixed ET-aware, naive-UTC, and
        # Kalshi 'Z' timestamps correctly — string sort would not).
        def _chrono(t):
            dt = parse_trade_ts(t.get("timestamp"))
            return dt.timestamp() if dt else 0
        all_trades.sort(key=_chrono)
        with open(LOG_FILE, "w") as f:
            json.dump(all_trades, f, indent=2)
        wins = sum(1 for t in new_trades if t.get("result") == "WIN")
        losses = sum(1 for t in new_trades if t.get("result") == "LOSS")
        pending = sum(1 for t in new_trades if not t.get("result"))
        total_pnl = sum(t.get("pnl", 0) for t in new_trades if t.get("pnl"))
        print(
            f"[rebuild] Recovered {len(new_trades)} trades: "
            f"{wins}W / {losses}L / {pending} pending  |  P&L: ${total_pnl:.2f}",
            flush=True,
        )
    else:
        print("[rebuild] No new trades to recover (log is up to date).", flush=True)

    return new_trades


def dedup_trades():
    """Remove duplicate trade records that share the same Kalshi order_id.

    After rebuild_trades_from_api() recovers trades, the log may contain
    both the original bot-logged record AND a 'recovered' record for the
    same order (due to timezone mismatches in the old ticker+ts dedup).
    This pass keeps the richer bot-logged record and drops the recovered
    duplicate.
    """
    trades = load_trades()
    if not trades:
        return

    def _extract_oid(t):
        """Return the Kalshi order_id from any nesting depth."""
        oid = t.get("order_id")
        if oid:
            return oid
        inner = t.get("order") or {}
        if isinstance(inner, dict):
            oid = inner.get("order_id")
            if oid:
                return oid
            inner2 = inner.get("order") or {}
            if isinstance(inner2, dict):
                return inner2.get("order_id")
        return None

    seen = {}      # order_id -> index of best record
    to_drop = set()

    for i, t in enumerate(trades):
        oid = _extract_oid(t)
        if not oid:
            continue
        if oid in seen:
            prev_idx = seen[oid]
            prev = trades[prev_idx]
            # Prefer the non-recovered record (has richer data)
            prev_recovered = (prev.get("order") or {}).get("recovered", False)
            curr_recovered = (t.get("order") or {}).get("recovered", False)
            if curr_recovered and not prev_recovered:
                to_drop.add(i)       # drop current (recovered dupe)
            elif prev_recovered and not curr_recovered:
                to_drop.add(prev_idx)  # drop previous (recovered dupe)
                seen[oid] = i
            else:
                to_drop.add(i)       # both same type, drop later one
        else:
            seen[oid] = i

    if to_drop:
        cleaned = [t for i, t in enumerate(trades) if i not in to_drop]
        with open(LOG_FILE, "w") as f:
            json.dump(cleaned, f, indent=2)
        print(f"[dedup] Removed {len(to_drop)} duplicate trades from log.", flush=True)
    else:
        print("[dedup] No duplicates found.", flush=True)


def load_trades():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE) as f:
            return json.load(f)
    return []


def save_trade(trade):
    trades = load_trades()
    trades.append(trade)
    with open(LOG_FILE, "w") as f:
        json.dump(trades, f, indent=2)


def close_trade_by_early_exit(ticker, side, count, entry_cents, exit_cents,
                               exit_reason="", beta_model_id=None):
    """Finalize the most recent open entry trade matching (ticker,side,count)
    with realized P&L from an early-exit sell.

    This is what we do **instead of** save_trade()-ing a new strategy="EARLY_EXIT"
    row: the dashboard's per-strategy stats were counting the exit separately
    from the entry, so early-exit wins appeared as "None final position" and
    never got credited to the SNIPER/CONSENSUS bucket that originated the
    position. Here we update the originating record in-place.

    Semantics:
      - Finds the trade by (ticker, side, count, result is None).
      - If beta_model_id is provided, also requires t.beta_model_id match.
        This keeps beta exit simulations from accidentally closing a live
        position that happens to share the same (ticker, side, count).
        Default None preserves pre-existing live-trade behavior.
      - Sets result=WIN if exit_proceeds > entry_cost else LOSS. We use
        strict P&L sign (not "exit > entry" as a policy flag) so a small
        stop-loss exit correctly shows as a LOSS, not a WIN.
      - Marks closed_by_exit=True so resolve_trades() skips this row when
        the market later settles.

    Returns True on success, False if no matching open trade was found.
    """
    trades = load_trades()
    entry_cost    = round(count * entry_cents / 100.0, 4)
    exit_proceeds = round(count * exit_cents  / 100.0, 4)
    pnl           = round(exit_proceeds - entry_cost, 2)

    # Iterate most-recent-first: if the bot happened to open the same
    # (ticker,side,count) twice in a row, we want to close the newest one.
    for t in reversed(trades):
        if t.get("result"):
            continue
        if t.get("ticker") != ticker:
            continue
        if t.get("side")   != side:
            continue
        if t.get("count")  != count:
            continue
        if beta_model_id is not None and t.get("beta_model_id") != beta_model_id:
            continue
        t["result"]         = "WIN" if pnl > 0 else "LOSS"
        t["pnl"]            = pnl
        t["exit_reason"]    = "EARLY_EXIT"
        t["exit_price"]     = exit_cents
        t["exit_payout"]    = exit_proceeds
        t["closed_by_exit"] = True
        if exit_reason:
            t["exit_detail"] = exit_reason
        with open(LOG_FILE, "w") as f:
            json.dump(trades, f, indent=2)
        return True
    return False


def is_positioned(t):
    """True if the trade resulted in a real held position (WIN or LOSS).

    NO_FILL trades are logged locally for observability — the bot submitted
    an order but Kalshi returned no matching fills — but they should be
    excluded from win-rate, ROI, and win/loss counts because no capital was
    at risk and no outcome was realized. Use this helper wherever stats
    assume a binary WIN/LOSS universe.
    """
    return t.get("result") in ("WIN", "LOSS")


def resolve_trades(client):
    """
    Mark settled trades with their outcome and realized P/L.

    Previously this trusted the locally-logged `dollars` / `price` / `count`
    (copied from the strategy's placement *intent*) and computed synthetic
    P/L from the market result alone. That produced phantom +$X WINs on
    orders that Kalshi never filled — e.g. the bot logged a 22-contract
    @ 22¢ order, Kalshi rejected or matched zero, the YES market settled,
    and resolve marked it a $17.16 WIN even though no position was ever
    held.

    This version reconciles against `/portfolio/fills` before assigning a
    result:

      * No fill rows for this order_id → `result = "NO_FILL"`, `pnl = 0`
        (position never existed; market outcome is irrelevant).
      * One or more fills → aggregate partials, compute `filled_count`
        and `filled_dollars` from Kalshi's real numbers, then set P/L as
        `filled_count − filled_dollars` on WIN (each winning contract
        pays $1) or `−filled_dollars` on LOSS.

    Dry-run records and legacy records without an `order_id` fall back to
    the old intent-based calc since there's nothing to reconcile against.
    """
    trades = load_trades()
    changed = 0

    # Batch-fetch fills once per resolve pass and index by order_id. We
    # paginate a bit so long-running pending trades (e.g. after a Render
    # restart) can still be reconciled; in steady state the first page
    # covers everything that settled since the previous 60s tick.
    fills_by_order = {}
    cursor = None
    for _ in range(5):  # cap at ~1000 fills
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = client.get("/portfolio/fills", params=params)
        except Exception as e:
            print(f"[resolve] Fills fetch error: {e}", flush=True)
            break
        for f in resp.get("fills") or []:
            oid = f.get("order_id")
            if oid:
                fills_by_order.setdefault(oid, []).append(f)
        cursor = resp.get("cursor")
        if not cursor:
            break

    for t in trades:
        if t.get("result"):
            continue

        mkt = client.get_market(t["ticker"])
        if not mkt:
            continue
        outcome = mkt.get("result")
        if not outcome:
            continue

        # ── dry-run + legacy records: old intent-based calc ──────────
        # These records have no real Kalshi order_id to join against
        # (dry runs never placed an order; legacy trades predate the
        # order_id field). The best we can do is treat the logged size
        # as the filled size.
        is_dry   = bool(t.get("dry_run"))
        order_id = t.get("order_id")
        if is_dry or not order_id:
            t["outcome"] = outcome
            won          = (t["side"] == outcome)
            t["result"]  = "WIN" if won else "LOSS"
            price        = (t.get("price") or 0) / 100
            size         = t.get("dollars") or 0
            if won and price > 0:
                t["pnl"] = round(size / price * (1 - price), 2)
            else:
                t["pnl"] = -round(size, 2)
            changed += 1
            continue

        # ── live path: reconcile against real Kalshi fills ───────────
        order_fills = fills_by_order.get(order_id, [])

        # Aggregate across partial fills on this order_id.
        side        = t["side"]
        price_key   = "yes_price_dollars" if side == "yes" else "no_price_dollars"
        total_count = 0.0
        total_cost  = 0.0
        for f in order_fills:
            # Kalshi sometimes exposes fractional fills under count_fp; fall
            # back to count (integer) for older responses.
            try:
                c = float(f.get("count_fp") or f.get("count") or 0)
            except (TypeError, ValueError):
                c = 0.0
            try:
                px = float(f.get(price_key) or 0)
            except (TypeError, ValueError):
                px = 0.0
            total_count += c
            total_cost  += c * px

        # Record what actually filled so the dashboard / analysis scripts
        # can see it without re-querying Kalshi.
        t["filled_count"]   = (int(total_count)
                               if total_count == int(total_count)
                               else round(total_count, 4))
        t["filled_dollars"] = round(total_cost, 2)
        t["outcome"]        = outcome

        if total_count <= 0:
            # Order was submitted but never matched — no position was held.
            # pnl stays at 0 and downstream helpers (is_positioned) exclude
            # it from win-rate / ROI.
            t["result"] = "NO_FILL"
            t["pnl"]    = 0
            changed += 1
            continue

        won = (side == outcome)
        t["result"] = "WIN" if won else "LOSS"
        if won:
            # Each winning contract pays $1; cost was total_cost.
            t["pnl"] = round(total_count * 1.0 - total_cost, 2)
        else:
            t["pnl"] = -round(total_cost, 2)
        changed += 1

    if changed:
        with open(LOG_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    return changed


def reconcile_trades(client):
    """
    Re-verify already-resolved trades against real Kalshi fills and flip
    any that never actually filled from WIN/LOSS to NO_FILL.

    resolve_trades() only touches records where result is None, so trades
    that were marked WIN/LOSS by the pre-reconciliation logic stay stuck
    with phantom P/L even after the fix shipped. This pass sweeps them:

      * For each non-dry-run trade with an order_id and result in
        (WIN, LOSS), fetch its actual fills.
      * If no fills → flip to result="NO_FILL", pnl=0.
      * If fills exist but the logged `filled_*` fields are absent
        (pre-fix records), back-fill them from the real numbers and
        recompute pnl on the true basis.

    Safe to run on a healthy log — it only rewrites records whose
    ground-truth state contradicts their logged state.

    Returns a dict with counts: {"flipped_to_nofill", "recomputed", "scanned"}.
    """
    trades = load_trades()

    # Batch-fetch fills once (same strategy as resolve_trades).
    fills_by_order = {}
    cursor = None
    for _ in range(10):  # cap at ~2000 fills to be thorough on first run
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = client.get("/portfolio/fills", params=params)
        except Exception as e:
            print(f"[reconcile] Fills fetch error: {e}", flush=True)
            break
        for f in resp.get("fills") or []:
            oid = f.get("order_id")
            if oid:
                fills_by_order.setdefault(oid, []).append(f)
        cursor = resp.get("cursor")
        if not cursor:
            break

    flipped   = 0
    recomputed = 0
    scanned   = 0
    changed   = False

    for t in trades:
        if t.get("dry_run"):
            continue
        if t.get("result") not in ("WIN", "LOSS"):
            continue
        order_id = t.get("order_id")
        if not order_id:
            continue
        scanned += 1

        order_fills = fills_by_order.get(order_id, [])
        side        = t.get("side")
        price_key   = "yes_price_dollars" if side == "yes" else "no_price_dollars"
        total_count = 0.0
        total_cost  = 0.0
        for f in order_fills:
            try:
                c = float(f.get("count_fp") or f.get("count") or 0)
            except (TypeError, ValueError):
                c = 0.0
            try:
                px = float(f.get(price_key) or 0)
            except (TypeError, ValueError):
                px = 0.0
            total_count += c
            total_cost  += c * px

        if total_count <= 0:
            # Logged as WIN/LOSS but Kalshi shows no fills — phantom.
            t["result"]         = "NO_FILL"
            t["pnl"]            = 0
            t["filled_count"]   = 0
            t["filled_dollars"] = 0.0
            flipped += 1
            changed = True
            continue

        # Back-fill real fill data if pre-fix resolve logged only intent.
        if "filled_count" not in t or "filled_dollars" not in t:
            t["filled_count"]   = (int(total_count)
                                   if total_count == int(total_count)
                                   else round(total_count, 4))
            t["filled_dollars"] = round(total_cost, 2)
            # Recompute P/L on the real basis — the old synthetic calc
            # used the intent price/count which can differ from fills.
            won = (t.get("result") == "WIN")
            if won:
                t["pnl"] = round(total_count * 1.0 - total_cost, 2)
            else:
                t["pnl"] = -round(total_cost, 2)
            recomputed += 1
            changed = True

    if changed:
        with open(LOG_FILE, "w") as f:
            json.dump(trades, f, indent=2)

    print(
        f"[reconcile] scanned={scanned} flipped_to_NO_FILL={flipped} "
        f"recomputed={recomputed}",
        flush=True,
    )
    return {
        "scanned":           scanned,
        "flipped_to_nofill": flipped,
        "recomputed":        recomputed,
    }


def update_fill_times(client):
    """
    Back-fill fill_timestamp + fill_latency_s on any live-logged trades that
    have been placed but whose fill metadata hasn't been recorded yet.

    Runs on the same 60s cadence as resolve_trades() and pulls only the most
    recent page of fills (limit=200) so it's cheap at steady state.  Kalshi
    emits fill.created_time which is the actual match time — joined against
    the trade's bot-side placement timestamp, that gives us per-trade resting
    latency without any post-hoc API reconciliation.

    Side effects: writes back to bot_trades.json if anything changed.
    Returns the number of trades updated.

    Skips:
      - dry-run trades (never had a real order_id)
      - trades already back-filled (fill_timestamp is not None)
      - trades without a recorded order_id (legacy / rebuilt records)
    """
    trades = load_trades()
    pending = [
        t for t in trades
        if not t.get("dry_run")
        and t.get("order_id")
        and not t.get("fill_timestamp")
    ]
    if not pending:
        return 0

    try:
        resp = client.get("/portfolio/fills", params={"limit": 200})
    except Exception as e:
        print(f"[fill-sync] Error fetching fills: {e}", flush=True)
        return 0

    fills = resp.get("fills", []) or []

    # For each order_id, keep the earliest fill timestamp (handles partials).
    first_fill_ts = {}
    for f in fills:
        oid = f.get("order_id")
        ts  = f.get("created_time")
        if not oid or not ts:
            continue
        prev = first_fill_ts.get(oid)
        if prev is None or ts < prev:
            first_fill_ts[oid] = ts

    updated = 0
    for t in pending:
        fill_ts = first_fill_ts.get(t.get("order_id"))
        if not fill_ts:
            continue
        t["fill_timestamp"] = fill_ts
        placed_dt = parse_trade_ts(t.get("timestamp"))
        fill_dt   = parse_trade_ts(fill_ts)
        if placed_dt and fill_dt:
            t["fill_latency_s"] = round((fill_dt - placed_dt).total_seconds(), 2)
        updated += 1

    if updated:
        with open(LOG_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    return updated


def print_stats():
    trades = load_trades()
    # Exclude NO_FILL from stats — they never held a position, so win-rate /
    # ROI calcs would be distorted if we included them.
    settled = [t for t in trades if is_positioned(t)]
    if not settled:
        print(Fore.YELLOW + "No settled trades yet.")
        return

    for strategy in ["LAG", "CONSENSUS", "SNIPER", "ALL"]:
        subset = settled if strategy == "ALL" else [
            t for t in settled if t.get("strategy") == strategy
        ]
        if not subset:
            continue

        wins    = sum(1 for t in subset if t["result"] == "WIN")
        total   = len(subset)
        wagered = sum(t.get("dollars", 0) for t in subset)
        pnl     = sum(t.get("pnl", 0) for t in subset if t.get("pnl"))
        wr      = wins / total * 100
        wwins   = sum(t.get("dollars", 0) for t in subset if t["result"] == "WIN")
        wwr     = wwins / wagered * 100 if wagered else 0
        roi     = pnl / wagered * 100 if wagered else 0

        print(f"\n{Fore.CYAN}{strategy} STRATEGY")
        rows = [
            ["Settled Trades", total],
            ["Wins / Losses", f"{wins} / {total - wins}"],
            ["Win Rate", f"{wr:.1f}%"],
            ["Weighted Win Rate", f"{wwr:.1f}%"],
            ["Total Wagered", f"${wagered:,.2f}"],
            ["Total P&L", f"${pnl:,.2f}"],
            ["ROI", f"{roi:.2f}%"],
        ]
        print(tabulate(rows, tablefmt="rounded_outline"))



# ═══════════════════════════════════════════════════════════
# v2.0 "EDGE ENGINE" COMPONENTS
# ═══════════════════════════════════════════════════════════

def kelly_size(balance, win_rate, price_dollars, fraction=KELLY_FRACTION,
               max_pct=KELLY_MAX_BET_PCT, fallback_stake=None):
    """Quarter-Kelly position sizing for binary contracts.

    For binary outcomes that pay $1 on win:
      b = (1 - price) / price   (payoff ratio)
      f* = (b*p - q) / b
    """
    if not KELLY_ENABLED or balance is None or balance <= 0:
        return fallback_stake or 0
    if price_dollars <= 0.005 or price_dollars >= 0.995:
        return fallback_stake or 0
    b = (1.0 - price_dollars) / price_dollars
    p = win_rate
    q = 1.0 - p
    f_star = (b * p - q) / b
    if f_star <= 0:
        return 0
    kelly_stake = balance * fraction * f_star
    max_stake = balance * max_pct
    kelly_stake = min(kelly_stake, max_stake)
    kelly_stake = max(kelly_stake, 1.0) if kelly_stake > 0 else 0
    return round(kelly_stake, 2)


class StrategyScorer:
    """Rolling performance scorer that auto-throttles losing strategies.

    Score 0-100 based on ROI (40%), trend (25%), sample (20%), WR (15%).
    Allocation: >=50 → 1.0x, 25-49 → 0.5x, <25 → 0.0x (blocked).
    """
    def __init__(self, window=AUTOSCORE_WINDOW, min_trades=AUTOSCORE_MIN_TRADES):
        self.window = window
        self.min_trades = min_trades
        self._cache = {}
        self._cache_ttl = 60

    def score(self, strategy_name):
        if not AUTOSCORE_ENABLED:
            return 100.0, 1.0
        now = time.time()
        cached = self._cache.get(strategy_name)
        if cached and (now - cached[2]) < self._cache_ttl:
            return cached[0], cached[1]
        trades = load_trades()
        # NO_FILL trades shouldn't influence auto-scoring: no capital at risk,
        # no outcome. is_positioned() filters them out of win-rate / ROI.
        settled = [t for t in trades
                   if is_positioned(t) and t.get("strategy") == strategy_name]
        recent = settled[-self.window:]
        if len(recent) < self.min_trades:
            self._cache[strategy_name] = (100.0, 1.0, now)
            return 100.0, 1.0
        wins = sum(1 for t in recent if t["result"] == "WIN")
        total = len(recent)
        wr = wins / total
        wagered = sum(t.get("dollars", 0) for t in recent)
        pnl = sum(t.get("pnl", 0) for t in recent if t.get("pnl"))
        roi = pnl / wagered if wagered > 0 else 0
        roi_score = min(100, max(0, (roi + 0.5) * 100))
        wr_score = min(100, max(0, wr * 200))
        if len(recent) >= 10:
            last5_pnl = sum(t.get("pnl", 0) for t in recent[-5:] if t.get("pnl"))
            prev5_pnl = sum(t.get("pnl", 0) for t in recent[-10:-5] if t.get("pnl"))
            trend_score = 70 if last5_pnl > prev5_pnl else 30
        else:
            trend_score = 50
        sample_score = min(100, len(recent) * 5)
        score = 0.40 * roi_score + 0.25 * trend_score + 0.20 * sample_score + 0.15 * wr_score
        if score < AUTOSCORE_BLOCK_THRESHOLD:
            mult = 0.0
        elif score < AUTOSCORE_THROTTLE_THRESHOLD:
            mult = 0.5
        else:
            mult = 1.0
        self._cache[strategy_name] = (score, mult, now)
        return score, mult

    def get_rolling_win_rate(self, strategy_name, mode=None):
        """Get rolling win rate for Kelly updates. None = use priors."""
        trades = load_trades()
        settled = [t for t in trades
                   if is_positioned(t) and t.get("strategy") == strategy_name]
        if mode:
            settled = [t for t in settled if mode in (t.get("reason") or "")]
        recent = settled[-self.window:]
        if len(recent) < KELLY_MIN_TRADES:
            return None
        wins = sum(1 for t in recent if t["result"] == "WIN")
        return wins / len(recent)


class PositionMonitor:
    """Tracks open Conviction-zone positions for early exit on BTC reversal.

    Only Sniper Conviction trades (50-55c) are tracked — they have enough
    liquidity to sell back. If BTC reverses > 0.15%, selling at small loss
    beats riding to $0 settlement (~50% loss).
    """
    def __init__(self):
        self.positions = {}

    def track(self, signal, btc_price):
        reason = signal.get("reason", "")
        if signal.get("strategy") == "SNIPER" and "CONVICTION" in reason:
            self.positions[signal["ticker"]] = {
                "side": signal["side"],
                "entry_btc": btc_price,
                "entry_time": time.time(),
                "entry_price_cents": signal["price"],
                "count": signal["count"],
            }

    def check_exits(self, btc_feed, markets):
        if not EARLY_EXIT_ENABLED or not self.positions:
            return []
        exits = []
        now = time.time()
        btc_now = btc_feed.current()
        if btc_now is None:
            return []
        mkt_map = {m["ticker"]: m for m in markets}
        for ticker, pos in list(self.positions.items()):
            if now - pos["entry_time"] < EARLY_EXIT_MIN_HOLD_S:
                continue
            if ticker not in mkt_map:
                self.positions.pop(ticker, None)
                continue
            btc_change = (btc_now - pos["entry_btc"]) / pos["entry_btc"]
            adverse = ((pos["side"] == "yes" and btc_change < -EARLY_EXIT_REVERSAL_PCT) or
                       (pos["side"] == "no"  and btc_change >  EARLY_EXIT_REVERSAL_PCT))
            if not adverse:
                continue
            market = mkt_map[ticker]
            bid_key = f"{pos['side']}_bid_dollars"
            current_bid = float(market.get(bid_key, 0))
            entry_price = pos["entry_price_cents"] / 100.0
            if current_bid <= 0:
                continue
            loss_pct = (entry_price - current_bid) / entry_price
            if loss_pct <= EARLY_EXIT_MAX_LOSS_PCT:
                exits.append({
                    "ticker": ticker,
                    "side": pos["side"],
                    "count": pos["count"],
                    "sell_price_cents": max(1, int(current_bid * 100)),
                    "entry_price_cents": pos["entry_price_cents"],
                    "btc_reversal": btc_change,
                    "loss_pct": loss_pct,
                    "reason": (f"EARLY EXIT: BTC {btc_change*100:+.2f}% reversal, "
                               f"sell @ {int(current_bid*100)}c (loss {loss_pct*100:.1f}%) "
                               f"vs ride-to-zero"),
                })
        return exits

    def remove(self, ticker):
        self.positions.pop(ticker, None)


class ConsensusExitTracker:
    """Shadow-tracks CONSENSUS_V1_WITH_EXIT beta entries and emits simulated
    exit signals when the Consensus entry logic would no longer fire in the
    same direction.

    This differs from PositionMonitor (Conviction tracker) in that the exit
    trigger is *signal-state-based*, not BTC-drift-based:

      HARD — signal inverted:
        * current momentum_side flipped vs entry (still outside dead zone)
        * last_result flipped vs entry
        Action: close regardless of loss, capped at HARD_MAX_LOSS.
      SOFT — signal decayed:
        * |momentum| fell into MOMENTUM_DEAD_ZONE
        * last_result aged past PREV_RESULT_MAX_AGE
        Action: close only if current loss <= SOFT_MAX_LOSS.
      HOLD — everything else (including STRONG→non-STRONG weakening while
             still in the original direction — logged but not exited in v1).

    Beta-only: exits are simulated against current top-of-book bid; no real
    sell orders are placed. close_trade_by_early_exit is called with
    beta_model_id='CONSENSUS_V1_WITH_EXIT' to mutate the correct beta trade
    record without touching any live positions.

    Entry state only captures what's needed for signal-state evaluation —
    no entry_btc, because BTC drift isn't a trigger here. That makes
    rehydration after a Render restart cheap: we can reconstruct state from
    the trade record alone.
    """
    BETA_MODEL_ID = "CONSENSUS_V1_WITH_EXIT"

    def __init__(self):
        self.positions = {}

    def track(self, signal, btc_price=None):
        """Capture entry state for an emitted CONSENSUS_V1_WITH_EXIT signal.

        btc_price accepted for signature parity with PositionMonitor.track
        but ignored here — this tracker doesn't do BTC-drift exits.
        """
        if signal.get("beta_model_id") != self.BETA_MODEL_ID:
            return
        if signal.get("strategy") != "CONSENSUS":
            return
        reason = signal.get("reason", "")
        # Consensus only fires when momentum_side == previous_side, and
        # signal["side"] equals both. So entry_momentum_side and
        # entry_previous are captured as signal["side"] directly.
        self.positions[signal["ticker"]] = {
            "side":                signal["side"],
            "entry_time":          time.time(),
            "entry_price_cents":   signal["price"],
            "count":               signal["count"],
            "entry_momentum_side": signal["side"],
            "entry_previous":      signal["side"],
            "strong_at_entry":     "[STRONG" in reason,
        }

    def rehydrate_from_trades(self):
        """On startup, repopulate tracker state from any open beta trade
        records with beta_model_id == CONSENSUS_V1_WITH_EXIT that haven't
        yet been closed. Uses the trade record's timestamp for entry_time
        so min-hold gating stays honest across restarts.
        """
        try:
            trades = load_trades()
        except Exception:
            return 0
        restored = 0
        for t in trades:
            if t.get("result"):
                continue
            if t.get("closed_by_exit"):
                continue
            if not t.get("is_beta"):
                continue
            if t.get("beta_model_id") != self.BETA_MODEL_ID:
                continue
            # Derive entry_time from placement timestamp so min-hold gating
            # works sensibly after restart.
            entry_ts = t.get("timestamp")
            entry_time = time.time()
            if entry_ts:
                try:
                    dt = datetime.datetime.fromisoformat(entry_ts)
                    entry_time = dt.timestamp()
                except (ValueError, TypeError):
                    pass
            reason = t.get("reason", "")
            self.positions[t["ticker"]] = {
                "side":                t["side"],
                "entry_time":          entry_time,
                "entry_price_cents":   t.get("price", 0),
                "count":               t.get("count", 0),
                "entry_momentum_side": t["side"],
                "entry_previous":      t["side"],
                "strong_at_entry":     "[STRONG" in reason,
            }
            restored += 1
        return restored

    def _classify(self, pos, btc_change, current_previous, prev_age_s):
        """Decide HARD / SOFT / HOLD for a tracked position given the
        current signal state. Returns (trigger_type, reason_str).

        Pure function — no BTC feed or market dict access, so unit-testable
        without any runtime dependencies.
        """
        momentum_side = None
        if btc_change is not None and abs(btc_change) >= MOMENTUM_DEAD_ZONE:
            momentum_side = "yes" if btc_change > 0 else "no"

        # HARD: entry signal explicitly inverted
        if momentum_side is not None and momentum_side != pos["entry_momentum_side"]:
            pct = (btc_change or 0) * 100
            return "HARD", (f"momentum flipped {pos['entry_momentum_side']}->"
                            f"{momentum_side} (d={pct:+.3f}%)")
        if current_previous and current_previous != pos["entry_previous"]:
            return "HARD", (f"previous flipped {pos['entry_previous']}->"
                            f"{current_previous}")

        # SOFT: entry signal has decayed but not inverted
        if momentum_side is None:
            pct = (btc_change or 0) * 100
            return "SOFT", f"momentum decayed into dead zone (d={pct:+.3f}%)"
        if (current_previous is not None and prev_age_s is not None
                and prev_age_s > PREV_RESULT_MAX_AGE):
            return "SOFT", (f"previous stale (age={prev_age_s:.0f}s > "
                            f"{PREV_RESULT_MAX_AGE}s)")

        return "HOLD", ""

    def check_exits(self, btc_feed, markets, consensus_ref):
        """Evaluate exit conditions for all tracked positions.

        Args:
            btc_feed: BTCPriceFeed instance for current momentum
            markets: current open market list (for top-of-book bid lookup)
            consensus_ref: ConsensusStrategy instance whose last_result is
                the source of truth for the previous-result signal. In
                practice self.consensus — update_previous runs every cycle
                regardless of CONSENSUS_ENABLED, so last_result stays fresh.

        Returns a list of exit signal dicts suitable for feeding to
        close_trade_by_early_exit. Empty list on no-op cycles.
        """
        if not CONSENSUS_EDGE_EXIT_BETA_ENABLED or not self.positions:
            return []
        exits = []
        now = time.time()
        btc_change = btc_feed.pct_change(MOMENTUM_WINDOW)
        current_previous = consensus_ref.last_result if consensus_ref else None
        prev_ts = consensus_ref.last_result_time if consensus_ref else 0
        prev_age_s = (now - prev_ts) if prev_ts else None
        mkt_map = {m["ticker"]: m for m in markets}

        for ticker, pos in list(self.positions.items()):
            # Min hold
            if now - pos["entry_time"] < CONSENSUS_EDGE_EXIT_MIN_HOLD_S:
                continue
            # Market no longer open — likely already resolved. Drop
            # silently; the underlying trade record will settle naturally
            # via resolve_trades().
            if ticker not in mkt_map:
                self.positions.pop(ticker, None)
                continue

            trigger, why = self._classify(pos, btc_change, current_previous,
                                          prev_age_s)
            if trigger == "HOLD":
                continue

            market = mkt_map[ticker]
            bid_key = f"{pos['side']}_bid_dollars"
            current_bid = float(market.get(bid_key, 0))
            if current_bid <= 0:
                # No liquidity to exit into — wait it out. This can happen
                # in the final seconds of a market.
                continue
            entry_price = pos["entry_price_cents"] / 100.0
            loss_pct = (entry_price - current_bid) / entry_price

            # Gate on per-trigger max-loss. SOFT is tighter because the
            # signal is weaker — we should only exit cheaply.
            if trigger == "HARD" and loss_pct > CONSENSUS_EDGE_EXIT_HARD_MAX_LOSS:
                continue
            if trigger == "SOFT" and loss_pct > CONSENSUS_EDGE_EXIT_SOFT_MAX_LOSS:
                continue

            exits.append({
                "ticker":             ticker,
                "side":               pos["side"],
                "count":              pos["count"],
                "sell_price_cents":   max(1, int(current_bid * 100)),
                "entry_price_cents":  pos["entry_price_cents"],
                "loss_pct":           loss_pct,
                "trigger":            trigger,
                "reason":             (f"EDGE_GONE {trigger}: {why} | sell "
                                       f"@ {int(current_bid*100)}c "
                                       f"(loss {loss_pct*100:+.1f}%)"),
            })
        return exits

    def remove(self, ticker):
        self.positions.pop(ticker, None)


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# STRATEGY 1: LAG BOT
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

class LagStrategy:
    """
    Detects stale Kalshi prices vs live BTC.

    Signal: BTC moved >LAG_THRESHOLD_PCT in last 90s,
            but Kalshi yes+no prices haven't updated recently.
    Trade:  Buy YES if BTC went up (prices will catch up upward)
            Buy NO  if BTC went down
    """
    def __init__(self, stake_dollars, *, is_beta=False, beta_model_id=None):
        self.stake    = stake_dollars
        self.kelly_mode = "LAG"      # [v2.0] Kelly prior key
        self.name     = "LAG"
        self.is_beta       = is_beta
        self.beta_model_id = beta_model_id
        self.last_kalshi_prices = {}  # ticker â (yes, no, timestamp)

    def evaluate(self, market, btc_feed, balance=None):
        ticker  = market["ticker"]
        yes_px  = float(market.get("yes_ask_dollars", market.get("yes_bid_dollars", 0)))
        no_px   = float(market.get("no_ask_dollars",  market.get("no_bid_dollars",  0)))
        book_sum = yes_px + no_px

        # Skip illiquid markets
        if book_sum < MIN_BOOK_SUM:
            return None

        # Track when Kalshi last changed price
        prev = self.last_kalshi_prices.get(ticker)
        now  = time.time()
        if prev and (prev[0] == yes_px and prev[1] == no_px):
            staleness = now - prev[2]
        else:
            staleness = 0
            self.last_kalshi_prices[ticker] = (yes_px, no_px, now)

        # BTC momentum over lag window
        btc_change = btc_feed.pct_change(LAG_MAX_REPRICE_AGE)
        if btc_change is None:
            return None

        moved = abs(btc_change) >= LAG_THRESHOLD_PCT
        stale = staleness >= LAG_MAX_REPRICE_AGE * 0.5  # half the window is enough

        if not (moved and stale):
            return None

        # Determine direction
        side          = "yes" if btc_change > 0 else "no"
        price_dollars = yes_px if side == "yes" else no_px
        price_cents   = int(price_dollars * 100)
        # [v2.0] Kelly sizing (Lag uses flat stake — structural edge, not statistical)
        effective_stake = self.stake
        kelly_stake = None
        if KELLY_ENABLED and balance:
            ks = kelly_size(balance, KELLY_PRIORS["LAG"]["win_rate"],
                           price_dollars, fallback_stake=self.stake)
            if ks > 0:
                effective_stake = ks
                kelly_stake = round(ks, 2)
        count = max(1, int(effective_stake / price_dollars))

        signal = {
            "strategy": self.name,
            "ticker":   ticker,
            "side":     side,
            "price":    price_cents,
            "count":    count,
            "dollars":  round(count * price_dollars, 2),
            "reason":   f"BTC {btc_change*100:+.2f}% in {LAG_MAX_REPRICE_AGE}s, Kalshi stale {staleness:.0f}s",
        }
        if kelly_stake is not None:
            signal["kelly_stake"] = kelly_stake
        return signal


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# STRATEGY 2: CONSENSUS BOT
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

class ConsensusStrategy:
    """
    Consensus Logic v1.1
    ====================
    Only trades when MOMENTUM and PREVIOUS signals agree.

    MOMENTUM: BTC up/down in last 60s â trade YES/NO
    PREVIOUS: last settled market result â trade same side
    CONSENSUS: only fire when both agree AND price <= dynamic threshold

    v1.1 improvements over v1.0:
      1. Dead zone filter   â momentum below MOMENTUM_DEAD_ZONE is ignored (noise)
      2. Signal staleness   â previous result expires after PREV_RESULT_MAX_AGE
      3. Dynamic price cap  â base 0.45 scales up to 0.55 with stronger momentum
      4. Time-aware entry   â tightens price cap as market nears close
      5. Trade cooldown     â CONSENSUS_COOLDOWN seconds between trades
      6. API throttle       â update_previous polls every PREV_CHECK_INTERVAL, not every cycle
    """
    def __init__(self, stake_dollars, *, is_beta=False, beta_model_id=None):
        self.stake              = stake_dollars
        self.name               = "CONSENSUS"
        self.is_beta            = is_beta
        self.beta_model_id      = beta_model_id
        self.last_result        = None   # "yes" or "no"
        self.last_result_time   = 0      # timestamp when last_result was set
        self.last_ticker        = None
        self.last_trade_time    = 0      # [v1.1] cooldown tracking
        self._last_prev_check   = 0      # [v1.1] API throttle timestamp

    def update_previous(self, markets, client):
        """
        Check recently closed markets for previous result.
        [v1.1] Throttled: only hits the API every PREV_CHECK_INTERVAL seconds.
        """
        now = time.time()
        if now - self._last_prev_check < PREV_CHECK_INTERVAL:
            return  # too soon, skip API call
        self._last_prev_check = now

        closed = client.get_markets_by_series(BTC_TICKER, status="settled")
        if closed:
            latest = sorted(closed, key=lambda m: m.get("close_time", ""), reverse=True)[0]
            result = latest.get("result")
            if result and latest["ticker"] != self.last_ticker:
                self.last_result      = result
                self.last_result_time = now   # [v1.1] timestamp the signal
                self.last_ticker      = latest["ticker"]

    def evaluate(self, market, btc_feed, mins_left=None, balance=None, score_mult=1.0):
        """
        Evaluate consensus signal for a single market.

        Args:
            market:    Kalshi market dict
            btc_feed:  BTCPriceFeed instance
            mins_left: minutes remaining in this market (passed from main loop)
            balance:   Current account balance (for Kelly sizing)
            score_mult: Auto-score multiplier (throttle factor)

        Returns signal dict or None.
        """
        ticker  = market["ticker"]
        yes_px  = float(market.get("yes_ask_dollars", market.get("yes_bid_dollars", 0)))
        no_px   = float(market.get("no_ask_dollars",  market.get("no_bid_dollars",  0)))
        book_sum = yes_px + no_px

        if book_sum < MIN_BOOK_SUM:
            return None

        # ── [v1.1] Cooldown check ──────────────────────────────
        now = time.time()
        if now - self.last_trade_time < CONSENSUS_COOLDOWN:
            return None

        # ── MOMENTUM signal ────────────────────────────────────
        btc_change = btc_feed.pct_change(MOMENTUM_WINDOW)
        if btc_change is None:
            return None

        # [v1.1] Dead zone: ignore noise-level moves
        if abs(btc_change) < MOMENTUM_DEAD_ZONE:
            return None

        momentum_side = "yes" if btc_change > 0 else "no"

        # ── PREVIOUS signal ────────────────────────────────────
        if not self.last_result:
            return None

        # [v1.1] Staleness: expire the previous signal after PREV_RESULT_MAX_AGE
        if self.last_result_time and (now - self.last_result_time > PREV_RESULT_MAX_AGE):
            return None

        previous_side = self.last_result

        # Both must agree
        if momentum_side != previous_side:
            return None

        side          = momentum_side
        price_dollars = yes_px if side == "yes" else no_px

        # ── [v1.2] Strong-momentum tier ────────────────────
        # When |momentum| crosses STRONG_MOMENTUM_THRESHOLD (0.065%), the
        # signal is in the band where live wins concentrated. Apply a stake
        # multiplier and extend the price cap by CONSENSUS_STRONG_PRICE_BONUS.
        strong_momentum = abs(btc_change) >= STRONG_MOMENTUM_THRESHOLD

        # [v2.4] STRONG-only gate — skip when non-STRONG momentum is all we
        # have. Live data showed non-STRONG CON at 23% WR vs STRONG CON at
        # 50% WR; non-STRONG was the whole bleed. See CONSENSUS_STRONG_ONLY
        # config block for the underlying numbers.
        if CONSENSUS_STRONG_ONLY and not strong_momentum:
            return None

        # ── [v1.1] Dynamic price cap ──────────────────────────
        # Base threshold of 0.45, scales up to CONSENSUS_MAX_PRICE with
        # stronger momentum.  abs(btc_change)*100 maps ~0.03%â0.13%+
        # into a 0â0.10 bonus on top of CONSENSUS_BASE_PRICE.
        momentum_bonus = min(abs(btc_change) * 100, CONSENSUS_MAX_PRICE - CONSENSUS_BASE_PRICE)
        dynamic_max    = CONSENSUS_BASE_PRICE + momentum_bonus
        # [v1.2] Strong-momentum cap bonus (clamped to CONSENSUS_MAX_PRICE below)
        if strong_momentum:
            dynamic_max += CONSENSUS_STRONG_PRICE_BONUS

        # [v1.1] Time-aware: tighten cap when < 8 min remain
        if mins_left is not None and mins_left < 8:
            time_penalty = 0.05 * max(0, 8 - mins_left) / 8  # up to 5Â¢ tighter
            dynamic_max -= time_penalty

        # Clamp to hard ceiling
        dynamic_max = min(dynamic_max, CONSENSUS_MAX_PRICE)

        if price_dollars > dynamic_max:
            return None

        price_cents = int(price_dollars * 100)

        # [v2.2] Hard floor — skip sub-25c entries (live WR 6.5%, bleed too fast).
        # See MIN_ENTRY_PRICE_CENTS config block for rationale.
        if price_cents < MIN_ENTRY_PRICE_CENTS:
            return None

        # [v2.0] Kelly sizing with strong-momentum and auto-score multipliers
        base_stake = self.stake * (CONSENSUS_STRONG_STAKE_MULT if strong_momentum else 1.0)
        kelly_stake = None
        if KELLY_ENABLED and balance:
            # Use rolling win rate if enough data, else prior
            rolling_wr = None  # populated by caller via scorer
            prior = KELLY_PRIORS["CONSENSUS"]
            ks = kelly_size(balance, prior["win_rate"], price_dollars,
                           fallback_stake=base_stake)
            if ks > 0:
                effective_stake = ks
                kelly_stake = round(ks, 2)
            else:
                # [v3.1] Same fix as SNIPER: small exploratory stake when Kelly says no edge
                effective_stake = float(os.getenv("KELLY_ZERO_EDGE_STAKE", "1.0"))
        else:
            effective_stake = base_stake
        # [v2.0] Auto-score throttle multiplier
        effective_stake *= score_mult
        # [v2.1] Cheap-contract stake cap: limit bleed on lottery-priced entries
        cheap_capped = False
        if price_cents < CHEAP_CONTRACT_PRICE and effective_stake > CHEAP_CONTRACT_MAX_STAKE:
            effective_stake = CHEAP_CONTRACT_MAX_STAKE
            cheap_capped = True
        count = max(1, int(effective_stake / price_dollars))

        # [v1.1] Record trade time for cooldown
        self.last_trade_time = now

        signal = {
            "strategy": self.name,
            "ticker":   ticker,
            "side":     side,
            "price":    price_cents,
            "count":    count,
            "dollars":  round(count * price_dollars, 2),
            "reason":   (f"Momentum {btc_change*100:+.3f}% + Previous={previous_side} "
                         f"| cap={dynamic_max:.2f} mins_left={mins_left or '?'}"
                         f"{' [STRONG x' + str(CONSENSUS_STRONG_STAKE_MULT) + ']' if strong_momentum else ''}"
                         f"{' [CHEAP_CAP $' + str(CHEAP_CONTRACT_MAX_STAKE) + ']' if cheap_capped else ''}"),
        }
        if kelly_stake is not None:
            signal["kelly_stake"] = kelly_stake
        return signal



# ═══════════════════════════════════════════════════════════
# STRATEGY 2b: CONSENSUS V2 (momentum-regime hybrid, 36-45c)
# ═══════════════════════════════════════════════════════════

class ConsensusV2Strategy:
    """
    Consensus V2 — momentum-regime hybrid for the 36-45c zone
    ==========================================================
    Evolved from V1 after 100-trade beta showed the 36-45c band at 63.6% WR
    / 71.3% ROI while other bands bled. V2 strips down to just that zone and
    upgrades the signal pipeline.

    THESIS:
      The 36-45c OTM band is underserved — SNIPER's kill-zone rejects it, but
      when strong 5-minute momentum aligns with regime continuation (previous
      market settled the same way), contracts in this band are mispriced.

    SIGNAL PIPELINE (all must pass):
      1. 5-minute BTC momentum >= CONSENSUS_V2_5M_MIN_MOMENTUM (0.07%).
         Upgraded from V1's weak 60-second window to SNIPER-grade 5m signal.
      2. 60-second confirmation: recent price action must confirm 5m direction
         (same gate as SNIPER v3.0 — filters stale/decaying momentum).
      3. Previous-result agreement: the last settled 15-min market must have
         resolved in the same direction as current momentum. This is regime
         awareness — continuation is more likely than reversal.
      4. Price must be in the 36-45c band (hard floor and cap).
      5. Market must have >= 5 minutes remaining.
      6. Cooldown of 300s between fires.

    SIZING:
      Kelly with corrected priors (60% WR, 0.41 avg price) — conservative
      estimate below the raw 63.6% observed in V1's 36-45c band.
      Hard-capped at $3/trade while sample accumulates.

    DIFFERENCES FROM V1:
      - 5m momentum replaces 60s momentum (much stronger signal)
      - 60s confirmation added (filters decaying momentum)
      - Hard 36-45c zone (no dynamic cap, no 25-35c or 46-55c)
      - Kelly priors based on actual band performance, not whole-strategy avg
      - No STRONG-only gate needed (0.07% floor is already above STRONG)
    """

    def __init__(self, stake_dollars, *, is_beta=False, beta_model_id=None):
        self.stake          = stake_dollars
        self.name           = "CONSENSUS_V2"
        self.is_beta        = is_beta
        self.beta_model_id  = beta_model_id
        self.last_result    = None   # "yes" or "no"
        self.last_result_time = 0
        self.last_ticker    = None
        self.last_trade_time = 0
        self._last_prev_check = 0

    def update_previous(self, markets, client):
        """Check recently closed markets for previous result.
        Throttled: only hits the API every PREV_CHECK_INTERVAL seconds.
        """
        now = time.time()
        if now - self._last_prev_check < PREV_CHECK_INTERVAL:
            return
        self._last_prev_check = now

        closed = client.get_markets_by_series(BTC_TICKER, status="settled")
        if closed:
            latest = sorted(closed, key=lambda m: m.get("close_time", ""), reverse=True)[0]
            result = latest.get("result")
            if result and latest["ticker"] != self.last_ticker:
                self.last_result      = result
                self.last_result_time = now
                self.last_ticker      = latest["ticker"]

    def evaluate(self, market, btc_feed, mins_left=None, balance=None, score_mult=1.0):
        """
        Evaluate Consensus V2 signal for a single market.

        Returns signal dict or None.
        """
        ticker  = market["ticker"]
        yes_px  = float(market.get("yes_ask_dollars", market.get("yes_bid_dollars", 0)))
        no_px   = float(market.get("no_ask_dollars",  market.get("no_bid_dollars",  0)))
        book_sum = yes_px + no_px

        if book_sum < MIN_BOOK_SUM:
            return None

        # -- Time filter: need enough runway ---------------------------------
        if mins_left is not None and mins_left < CONSENSUS_V2_MIN_MINS_LEFT:
            return None

        # -- Cooldown --------------------------------------------------------
        now = time.time()
        if now - self.last_trade_time < CONSENSUS_V2_COOLDOWN_S:
            return None

        # -- 5-minute momentum (primary signal) ------------------------------
        mom_5m = btc_feed.pct_change(300)
        if mom_5m is None:
            return None

        if abs(mom_5m) < CONSENSUS_V2_5M_MIN_MOMENTUM:
            return None

        # -- 60-second contradiction check -----------------------------------
        mom_60s = btc_feed.pct_change(60)
        if mom_60s is not None and abs(mom_60s) > 0.0003:
            if (mom_5m > 0 and mom_60s < -0.0003) or \
               (mom_5m < 0 and mom_60s > 0.0003):
                return None

        # -- 60-second confirmation ------------------------------------------
        if mom_60s is None:
            return None
        same_direction = (mom_5m > 0 and mom_60s > CONSENSUS_V2_60S_CONFIRM_MIN) or \
                         (mom_5m < 0 and mom_60s < -CONSENSUS_V2_60S_CONFIRM_MIN)
        if not same_direction:
            return None

        # [v3.2] Momentum divergence filter (shared with SNIPER)
        divergence = abs(mom_5m) - abs(mom_60s)
        if divergence > MOMENTUM_DIVERGENCE_MAX:
            return None

        # -- Previous-result agreement (regime signal) -----------------------
        if not self.last_result:
            return None

        if self.last_result_time and (now - self.last_result_time > PREV_RESULT_MAX_AGE):
            return None

        momentum_side = "yes" if mom_5m > 0 else "no"
        previous_side = self.last_result

        if momentum_side != previous_side:
            return None

        # -- Price zone filter: 36-45c only ----------------------------------
        side          = momentum_side
        price_dollars = yes_px if side == "yes" else no_px
        price_cents   = int(price_dollars * 100)

        if price_cents < CONSENSUS_V2_MIN_PRICE_CENTS:
            return None
        if price_cents > CONSENSUS_V2_MAX_PRICE_CENTS:
            return None

        # -- Kelly sizing with corrected priors ------------------------------
        kelly_stake = None
        KELLY_ZERO_EDGE_STAKE = float(os.getenv("KELLY_ZERO_EDGE_STAKE", "1.0"))
        if KELLY_ENABLED and balance:
            prior = KELLY_PRIORS.get("CONSENSUS_V2", {})
            if prior:
                ks = kelly_size(balance, prior["win_rate"], price_dollars,
                               fallback_stake=self.stake)
                if ks > 0:
                    effective_stake = ks
                    kelly_stake = round(ks, 2)
                else:
                    effective_stake = KELLY_ZERO_EDGE_STAKE
            else:
                effective_stake = self.stake
        else:
            effective_stake = self.stake

        # Auto-score throttle
        effective_stake *= score_mult

        # Hard cap at $3 — Kelly priors are from a small sample (33 trades),
        # so limit downside while the beta accumulates more data.
        CONSENSUS_V2_MAX_STAKE = float(os.getenv("CONSENSUS_V2_MAX_STAKE", "3.0"))
        effective_stake = min(effective_stake, CONSENSUS_V2_MAX_STAKE)

        count = max(1, int(effective_stake / price_dollars))

        # Record trade time for cooldown
        self.last_trade_time = now

        signal = {
            "strategy": self.name,
            "ticker":   ticker,
            "side":     side,
            "price":    price_cents,
            "count":    count,
            "dollars":  round(count * price_dollars, 2),
            "reason":   (f"CONVICTION / 5m {mom_5m*100:+.4f}% -> {side.upper()} @ {price_cents}c "
                         f"/ 60s: {mom_60s*100:+.4f}% / prev={previous_side} "
                         f"/ {mins_left:.0f}m left" if mins_left else
                         f"CONVICTION / 5m {mom_5m*100:+.4f}% -> {side.upper()} @ {price_cents}c "
                         f"/ 60s: {mom_60s*100:+.4f}% / prev={previous_side}"),
        }
        if kelly_stake is not None:
            signal["kelly_stake"] = kelly_stake
        return signal



# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# STRATEGY 3: SNIPER (data-driven directional)
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

class SniperStrategy:
    """
    Sniper v1.0 -- data-driven directional strategy
    ================================================
    Derived from edge analysis of 136 ground-truth fills (analyze_edge.py).

    THESIS:
      Two price zones beat fair pricing; everything between is a graveyard.
      5-minute BTC momentum alignment is the strongest exploitable signal.

    ENTRY MODES:
      LOTTERY    (1-10c):  Deep OTM. WR=10.5% vs BE=5.7% -> +4.8pp edge, +237% ROI.
                           Risk $5-10 to win $90-95. Asymmetry does the heavy lifting.
      CONVICTION (50-55c): ATM+.    WR=61.5% vs BE=52.5% -> +9.1pp edge, +18.5% ROI.
                           Higher hit rate, moderate payoff per trade.
      KILL ZONE  (11-49c): NEVER.   All sub-bands show -13pp to -15pp edge.

    FILTERS:
      1. 5-min BTC momentum must exceed SNIPER_5M_MIN_MOMENTUM (0.07%).
         Aligned trades:  n=48, WR=35.4%, edge=+2.5pp, +22% ROI.
         Counter trades:  n=88, WR=15.9%, edge=-15.9pp, -27% ROI.
      2. 60-second momentum must not contradict 5-min direction.
         Counter to 60s momentum was 0% WR across 12 trades.
      3. Market must have >= SNIPER_MIN_MINS_LEFT minutes remaining.
         Too little time = no room for momentum continuation.
    """

    def __init__(self, lottery_stake=None, conviction_stake=None, *, is_beta=False, beta_model_id=None):
        self.lottery_stake    = lottery_stake or SNIPER_LOTTERY_STAKE
        self.conviction_stake = conviction_stake or SNIPER_CONVICTION_STAKE
        self.name             = "SNIPER"
        self.is_beta          = is_beta
        self.beta_model_id    = beta_model_id
        self.last_trade_time  = 0

    def evaluate(self, market, btc_feed, mins_left=None, balance=None, score_mult=1.0):
        """
        Evaluate sniper signal for a single market.

        Args:
            market:    Kalshi market dict
            btc_feed:  BTCPriceFeed instance
            mins_left: minutes remaining in this market
            balance:   Current account balance (for Kelly sizing)
            score_mult: Auto-score multiplier (throttle factor)

        Returns signal dict or None.
        """
        ticker  = market["ticker"]
        yes_px  = float(market.get("yes_ask_dollars", market.get("yes_bid_dollars", 0)))
        no_px   = float(market.get("no_ask_dollars",  market.get("no_bid_dollars",  0)))
        book_sum = yes_px + no_px

        if book_sum < MIN_BOOK_SUM:
            return None

        # -- Time filter: need enough runway for momentum continuation --
        if mins_left is not None and mins_left < SNIPER_MIN_MINS_LEFT:
            return None

        # -- Cooldown -------------------------------------------------------
        now = time.time()
        if now - self.last_trade_time < SNIPER_COOLDOWN:
            return None

        # -- 5-minute BTC momentum (primary signal) -------------------------
        mom_5m = btc_feed.pct_change(300)
        if mom_5m is None:
            return None

        # Must have meaningful 5-min directional move
        if abs(mom_5m) < SNIPER_5M_MIN_MOMENTUM:
            return None

        # -- 60-second momentum cross-check ---------------------------------
        # Counter to 60s momentum = 0% WR (n=12). If short-term momentum
        # has reversed against the 5-min trend, stand aside.
        mom_60s = btc_feed.pct_change(60)
        if mom_60s is not None and abs(mom_60s) > 0.0003:
            if (mom_5m > 0 and mom_60s < -0.0003) or \
               (mom_5m < 0 and mom_60s > 0.0003):
                return None

        # [v3.0] 60-second confirmation: require that short-term momentum is
        # actively moving in the same direction as 5m, not just not-reversed.
        # Many losing entries fired on stale/decaying momentum where 60s was
        # near zero — technically not contradicting but not confirming either.
        if SNIPER_REQUIRE_60S_CONFIRM:
            if mom_60s is None:
                return None  # no 60s data = can't confirm
            same_direction = (mom_5m > 0 and mom_60s > SNIPER_60S_CONFIRM_MIN) or \
                             (mom_5m < 0 and mom_60s < -SNIPER_60S_CONFIRM_MIN)
            if not same_direction:
                return None

        # [v3.2] Momentum divergence filter — exhausted-momentum detection.
        # If the gap between |5m| and |60s| exceeds the threshold, the bulk
        # of the move happened > 60s ago and is fading. Three days of data:
        # trades with gap > 0.08% were 1W/3L (net -$1.06 saved by blocking).
        if mom_60s is not None:
            divergence = abs(mom_5m) - abs(mom_60s)
            if divergence > MOMENTUM_DIVERGENCE_MAX:
                return None

        # -- Direction from 5-min momentum ----------------------------------
        side          = "yes" if mom_5m > 0 else "no"
        price_dollars = yes_px if side == "yes" else no_px
        price_cents   = int(price_dollars * 100)

        # [v2.2] Hard floor — skip sub-25c entries (LOTTERY live WR 0/12).
        # See MIN_ENTRY_PRICE_CENTS config block for rationale.
        if price_cents < MIN_ENTRY_PRICE_CENTS:
            return None

        # -- Entry price zone filter ----------------------------------------
        # LOTTERY: 1-10c | CONVICTION: 50-55c | KILL ZONE: 11-49c, 56c+
        # NOTE: LOTTERY branch is now unreachable given the 25c floor above,
        # but kept for clarity and for restoring if MIN_ENTRY_PRICE_CENTS=0.
        if 1 <= price_cents <= 10:
            mode  = "LOTTERY"
            stake = self.lottery_stake
        elif 50 <= price_cents <= 55:
            mode  = "CONVICTION"
            stake = self.conviction_stake
        else:
            return None  # kill zone -- no edge here

        # [v2.0] Kelly sizing per entry mode
        # [v3.1] CRITICAL FIX: when Kelly returns 0 (no edge at this price),
        # use a small exploratory stake ($1) instead of falling back to the
        # full SNIPER_CONVICTION_STAKE ($25).  The v3.0 prior of 0.52 causes
        # Kelly to return 0 for all trades above 51¢ (breakeven = win_rate),
        # and the old fallback engaged the MAXIMUM flat stake — producing
        # $20 bets on a $36 bankroll (70% per trade).  This caused -$71 PnL
        # on a 60% WR day: losses at $20 dwarfed wins.
        # Override exploratory stake via env: KELLY_ZERO_EDGE_STAKE=1.0
        KELLY_ZERO_EDGE_STAKE = float(os.getenv("KELLY_ZERO_EDGE_STAKE", "1.0"))
        kelly_stake = None
        if KELLY_ENABLED and balance:
            kelly_key = f"SNIPER_{mode}"
            prior = KELLY_PRIORS.get(kelly_key, {})
            if prior:
                ks = kelly_size(balance, prior["win_rate"], price_dollars,
                               fallback_stake=stake)
                if ks > 0:
                    effective_stake = ks
                    kelly_stake = round(ks, 2)
                else:
                    # Kelly says no edge — use small exploratory stake, NOT full flat stake
                    effective_stake = KELLY_ZERO_EDGE_STAKE
            else:
                effective_stake = stake
        else:
            effective_stake = stake
        # [v2.0] Auto-score throttle multiplier
        effective_stake *= score_mult
        # [v2.1] Cheap-contract stake cap: limit bleed on lottery-priced entries
        cheap_capped = False
        if price_cents < CHEAP_CONTRACT_PRICE and effective_stake > CHEAP_CONTRACT_MAX_STAKE:
            effective_stake = CHEAP_CONTRACT_MAX_STAKE
            cheap_capped = True
        count = max(1, int(effective_stake / price_dollars))

        # -- Record trade time for cooldown ---------------------------------
        self.last_trade_time = now

        mom_60s_str = f"{mom_60s*100:+.3f}%" if mom_60s else "n/a"
        signal = {
            "strategy": self.name,
            "ticker":   ticker,
            "side":     side,
            "price":    price_cents,
            "count":    count,
            "dollars":  round(count * price_dollars, 2),
            "reason":   (f"{mode} | 5m {mom_5m*100:+.3f}% -> {side.upper()} @ {price_cents}c "
                         f"| 60s: {mom_60s_str} | {mins_left:.0f}m left"
                         f"{' [CHEAP_CAP $' + str(CHEAP_CONTRACT_MAX_STAKE) + ']' if cheap_capped else ''}"
                         if mins_left else
                         f"{mode} | 5m {mom_5m*100:+.3f}% -> {side.upper()} @ {price_cents}c "
                         f"| 60s: {mom_60s_str}"
                         f"{' [CHEAP_CAP $' + str(CHEAP_CONTRACT_MAX_STAKE) + ']' if cheap_capped else ''}"),
        }
        if kelly_stake is not None:
            signal["kelly_stake"] = kelly_stake
        return signal


# ──────────────────────────────────────────────────────────────
# KELLY CORRELATION INSTRUMENTATION (phase-1: log-only)
# ──────────────────────────────────────────────────────────────

_MONTH_ABBREV = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_ticker_close_time(ticker):
    """Parse Kalshi KXBTC15M ticker → market close datetime (UTC), or None.

    Format observed in reconcile_archive/*.json and live fills:
        KXBTC15M-<YY><MMM><DD><HH><MM>-<close_min>
    e.g. 'KXBTC15M-26APR151945-45' → 2026-04-15 19:45 UTC.

    The trailing '-MM' duplicates the closing minute and isn't used here —
    we parse the full close timestamp from the YYMMMDDHHMM block.

    Returns None on parse failure so callers can fall back to "uncorrelated"
    instead of raising (this function is called on every logged signal).
    """
    if not ticker or not isinstance(ticker, str):
        return None
    parts = ticker.split("-")
    if len(parts) < 2:
        return None
    mid = parts[1]
    if len(mid) != 11:
        return None
    try:
        yy  = int(mid[0:2])
        mon = _MONTH_ABBREV.get(mid[2:5].upper())
        if mon is None:
            return None
        dd  = int(mid[5:7])
        hh  = int(mid[7:9])
        mm  = int(mid[9:11])
        return datetime.datetime(2000 + yy, mon, dd, hh, mm,
                                 tzinfo=datetime.timezone.utc)
    except (ValueError, IndexError):
        return None


def correlation_factor(proposed_signal, open_trades):
    """Compute correlation shrinkage factor ∈ [CORR_ADJ_MIN_FLOOR, 1.0].

    Compares the proposed signal against each open same-side position and
    composes a multiplicative factor based on how close their close times
    are. Ignores settled trades, early-exited trades, and same-ticker
    trades (the latter are already blocked by traded_this_market).

    Returns (factor, diag) where diag is a dict suitable for JSON logging.

    Pure function — no I/O, no global state except the CORR_ADJ_* config.
    """
    ticker = proposed_signal.get("ticker", "")
    side   = proposed_signal.get("side")
    prop_close = _parse_ticker_close_time(ticker)
    if prop_close is None or not side:
        return 1.0, {"skipped": "no_close_time_or_side", "overlap_n": 0,
                     "close_n": 0, "sameday_n": 0, "matched": []}

    overlap_n = close_n = sameday_n = 0
    matched = []
    for t in open_trades or []:
        if t.get("result"):              # settled
            continue
        if t.get("closed_by_exit"):      # already exited early
            continue
        if t.get("side") != side:        # opposite-direction → treated as uncorrelated v1
            continue
        t_ticker = t.get("ticker", "")
        if t_ticker == ticker:           # same market — handled by traded_this_market
            continue
        t_close = _parse_ticker_close_time(t_ticker)
        if t_close is None:
            continue
        delta_min = abs((prop_close - t_close).total_seconds()) / 60.0
        if delta_min <= 15:
            overlap_n += 1
            bucket = "OVERLAP"
        elif delta_min <= 60:
            close_n += 1
            bucket = "CLOSE"
        elif prop_close.date() == t_close.date():
            sameday_n += 1
            bucket = "SAME_DAY"
        else:
            continue  # cross-day → treat as uncorrelated
        matched.append({"ticker": t_ticker, "bucket": bucket,
                        "delta_min": round(delta_min, 1)})

    factor = 1.0
    factor *= CORR_ADJ_OVERLAP_FACTOR  ** overlap_n
    factor *= CORR_ADJ_CLOSE_FACTOR    ** close_n
    factor *= CORR_ADJ_SAME_DAY_FACTOR ** sameday_n
    factor = max(factor, CORR_ADJ_MIN_FLOOR)

    return factor, {
        "overlap_n": overlap_n,
        "close_n":   close_n,
        "sameday_n": sameday_n,
        "matched":   matched,
    }


def annotate_correlation(signal, open_trades):
    """Decorate a signal with correlation diagnostics before it's logged.

    Phase 1 (current default — CORR_ADJ_LOG_ONLY=True):
      Adds kelly_stake_raw / corr_factor / corr_diag / kelly_stake_adjusted.
      Does NOT mutate kelly_stake / count / dollars. Zero behavior change.

    Phase 2 (reserved — set CORR_ADJ_ENABLED=True and
    CORR_ADJ_LOG_ONLY=False): will additionally rewrite kelly_stake, count,
    and dollars. Kept stubbed here so the flip is a config change only.

    No-ops on signals without a Kelly stake (e.g. LAG, which uses flat
    sizing) — we only have a meaningful "adjusted" to report when Kelly
    produced a number.
    """
    if not signal:
        return signal
    raw = signal.get("kelly_stake")
    if raw is None:
        return signal
    factor, diag = correlation_factor(signal, open_trades)
    signal["kelly_stake_raw"]      = raw
    signal["corr_factor"]          = round(factor, 4)
    signal["corr_diag"]            = diag
    signal["kelly_stake_adjusted"] = round(raw * factor, 2)
    # Phase 2 apply-mode (gated so this stays inert until we opt in):
    if CORR_ADJ_ENABLED and not CORR_ADJ_LOG_ONLY:
        # Intentionally left unimplemented in phase 1. See
        # docs/correlation_instrumentation.md when it's time to enable.
        pass
    return signal


# ──────────────────────────────────────────────────────────────
# STRATEGY 4: EXPIRY DECAY (beta)
# ──────────────────────────────────────────────────────────────

def _get_market_strike(market):
    """Return (strike, strike_type) for a Kalshi KXBTC15M market, or (None, None).

    Kalshi exposes the reference directly as `floor_strike` (a float in USD)
    on the market dict; the ticker suffix (-00/-15/-30/-45) is just the close
    minute and does NOT encode the strike. `strike_type` is typically
    "greater_or_equal" for these markets — YES resolves if the final 60s
    BRTI average >= floor_strike. Re-applied 2026-04-23 after revert in
    c450179 (DIAG dump in 0ca940b confirmed the floor_strike schema).
    """
    if not isinstance(market, dict):
        return (None, None)
    raw = market.get("floor_strike")
    if raw is None:
        return (None, None)
    try:
        strike = float(raw)
    except (TypeError, ValueError):
        return (None, None)
    if strike <= 0:
        return (None, None)
    stype = market.get("strike_type") or "greater_or_equal"
    return (strike, stype)


def _phi(x):
    """Standard normal CDF. math.erf only; no scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _realized_vol_per_sqrt_s(btc_feed, lookback_s=900, min_samples=30):
    """Estimate BTC's per-sqrt-second log-return stddev.

    Pulls recent (ts, px) samples from the feed's internal history, builds
    log-returns normalized by sqrt(dt) so they're on a per-sqrt-second scale,
    and returns their stddev.

    Returns None on insufficient data. Short-window vol is noisy; callers
    should expect occasional None and skip those cycles.
    """
    try:
        with btc_feed._lock:
            hist = list(btc_feed.history)
    except AttributeError:
        return None
    if len(hist) < min_samples:
        return None

    now = hist[-1][0]
    cutoff = now - lookback_s
    window = [(ts, px) for ts, px in hist if ts >= cutoff]
    if len(window) < min_samples:
        window = hist[-min_samples:]

    rets_per_sqrt_s = []
    for i in range(1, len(window)):
        dt = window[i][0] - window[i-1][0]
        px_prev = window[i-1][1]
        px_now  = window[i][1]
        if dt <= 0 or px_prev <= 0 or px_now <= 0:
            continue
        r = math.log(px_now / px_prev)
        rets_per_sqrt_s.append(r / math.sqrt(dt))

    if len(rets_per_sqrt_s) < min_samples - 10:
        return None
    mean = sum(rets_per_sqrt_s) / len(rets_per_sqrt_s)
    var  = sum((r - mean) ** 2 for r in rets_per_sqrt_s) / max(len(rets_per_sqrt_s) - 1, 1)
    return math.sqrt(var)


class ExpiryDecayStrategy:
    """
    Expiry-decay strategy (Phase 4). Promoted to live 2026-04-30 after
    110 sim fires at 92.7% WR / +60% sim ROI cleared the docs/beta_promotion.md
    bar. v2.1 added Kelly sizing on top of the v2 entry logic.

    THESIS:
      In the final N seconds of a 15-min market, probability converges
      toward 0 or 1 faster than Kalshi's book reprices. If BTC is deeply
      into the YES (or deeply into the NO) region relative to short-window
      realized vol, the opposite-side contract still carries residual risk
      premium + impatient-seller liquidity. We buy the side the market
      has effectively decided, collecting a few cents of edge into
      settlement.

    ENTRY GATES (all must pass):
      1. Time remaining in [MIN_SECS_LEFT, MAX_SECS_LEFT]
      2. Vol-normalized distance |log(S/K)| / (sigma * sqrt(T))
         >= MIN_DIST_SIGMAS
      3. Model fair value - Kalshi ask >= MIN_EDGE_CENTS (after fees proxy)
      4. Book sum (yes_ask + no_ask) >= MIN_BOOK_SUM
      5. [v2] Chosen-side ask >= EXPIRY_DECAY_MIN_PRICE_CENTS (default 5c)

    FAIR VALUE:
      Simple GBM with zero drift over the remaining window. BTC realized
      vol is estimated from the last VOL_WINDOW_S seconds of the feed's
      own price history, converted to per-sqrt-second. This is the
      cheapest defensible pricing model; good enough to flag obvious
      mispricings without falling apart in minutes-to-expiry conditions.

    SIZING:
      Quarter-Kelly using the per-fire GBM probability p_side as the
      win-rate input — same pattern as BRIDGE_V2. The model's per-fire
      conviction varies a lot (2σ scrapes vs 4σ pinned), and using a
      static prior would either under-size the obvious fires or over-size
      the marginal ones. Hard-capped at self.stake (= MAX_STAKE_PER_TRADE)
      so the dashboard's "Stake $X" header remains the actual ceiling.
      When Kelly returns 0 (no edge at this price), falls back to a $1
      exploratory stake (same v3.1 convention as SNIPER / CONSENSUS_V2).
      EXPIRY_DECAY_KELLY_ENABLED=false reverts to flat self.stake.

    PROMOTION:
      See docs/beta_promotion.md. Expect optimistic-sim inflation; require
      ~2x the edge you'd demand from a live strategy before promoting.

    CHANGELOG:
      v2.1 (2026-04-30): Added Kelly sizing via EXPIRY_DECAY_KELLY_ENABLED
        (default ON). Uses per-fire p_side as the win-rate input — matches
        BRIDGE_V2's pattern. v2 sample of 110 fires at 92.7% WR / +60% sim
        ROI cleared the docs/beta_promotion.md bar; flat sizing is no
        longer justified by "no historical WR yet." Capped at self.stake
        to preserve the single-stake invariant — high-conviction fires
        will saturate the cap rather than scale beyond it. No beta_model_id
        bump: this is a sizing change, not a signal change; entry gates
        and fire pattern are identical to v2. Live-deploy guidance per
        playbook step 3: half-stake first week to validate fills before
        scaling.
      v2 (2026-04-25): Added EXPIRY_DECAY_MIN_PRICE_CENTS price floor.
        v1 dry-run sample (n=76, 2026-04-23 → 04-25): sub-5c entries
        were 24-for-24 losers (-$480) while >=5c entries were 41W/11L
        for +$461 (+45% sim ROI). Failure mode: GBM vol-distance gate
        treats "BTC 2σ above strike, buy NO at 1c" the same as "BTC 2σ
        above strike, buy NO at 80c" — but the contrarian-lottery side
        fights near-certain pinning that the realized-vol model can't
        see in the final minutes. Floor at 5c is the cleanest cut: zero
        v1 trades priced 5–10c, and 11c+ floors start dropping real
        winners. See docs/expiry_decay_v2.md for the full breakdown.
        Records re-tagged beta_model_id=EXPIRY_DECAY_V2 so the v1
        history stays a clean baseline; promotion-bar fire count resets
        per docs/beta_promotion.md ("re-tuning ≡ overfitting" rule).
      v1 (2026-04-23): Initial Phase 4 launch.
    """

    def __init__(self, stake_dollars, *,
                 is_beta=False, beta_model_id=None):
        # Promoted live 2026-04-30 — defaults flipped from is_beta=True. Call
        # site in KalshiBot.__init__ no longer passes beta_model_id; if a
        # future shadow-beta variant of this class is wanted, override at
        # the call site (matches the BRIDGE_V1 → V2 retag pattern).
        self.stake           = stake_dollars
        self.name            = "EXPIRY_DECAY"
        self.is_beta         = is_beta
        self.beta_model_id   = beta_model_id
        self.last_fire_time  = 0.0

    def _vlog(self, ticker, msg):
        """Per-ticker sampled verbose log (rate-limited by VERBOSE_EVERY_S)."""
        if not EXPIRY_DECAY_VERBOSE:
            return
        if not hasattr(self, "_vlog_last"):
            self._vlog_last = {}
        now = time.time()
        prev = self._vlog_last.get(ticker, 0)
        if now - prev < EXPIRY_DECAY_VERBOSE_EVERY_S:
            return
        self._vlog_last[ticker] = now
        print(f"  [exp-decay:{ticker}] {msg}", flush=True)

    def evaluate(self, market, btc_feed, mins_left=None,
                 balance=None, score_mult=1.0):
        ticker = market.get("ticker", "?")

        # ---- Time gate (converts mins_left → seconds) -----------------
        if mins_left is None:
            self._vlog(ticker, "BLOCK time: mins_left=None")
            return None
        secs_left = mins_left * 60.0
        if secs_left < EXPIRY_DECAY_MIN_SECS_LEFT:
            self._vlog(ticker, f"BLOCK time: secs_left={secs_left:.0f} < min={EXPIRY_DECAY_MIN_SECS_LEFT}")
            return None
        if secs_left > EXPIRY_DECAY_MAX_SECS_LEFT:
            self._vlog(ticker, f"BLOCK time: secs_left={secs_left:.0f} > max={EXPIRY_DECAY_MAX_SECS_LEFT}")
            return None

        # ---- Cooldown ---------------------------------------------------
        now_s = time.time()
        if now_s - self.last_fire_time < EXPIRY_DECAY_COOLDOWN_S:
            self._vlog(ticker, f"BLOCK cooldown: {now_s - self.last_fire_time:.0f}s < {EXPIRY_DECAY_COOLDOWN_S}s")
            return None

        # ---- Liquidity --------------------------------------------------
        yes_ask = float(market.get("yes_ask_dollars", 0) or 0)
        no_ask  = float(market.get("no_ask_dollars",  0) or 0)
        if yes_ask <= 0 or no_ask <= 0:
            self._vlog(ticker, f"BLOCK liquidity: yes_ask={yes_ask} no_ask={no_ask} (zero ask)")
            return None
        if yes_ask + no_ask < MIN_BOOK_SUM:
            self._vlog(ticker, f"BLOCK liquidity: book_sum={yes_ask + no_ask:.2f} < {MIN_BOOK_SUM}")
            return None

        # ---- Strike from market.floor_strike --------------------------
        # Kalshi KXBTC15M markets are strike-based: YES resolves if the
        # final 60s BRTI average is >= floor_strike (per strike_type).
        # The strike is NOT in the ticker suffix — that's the close minute.
        strike, strike_type = _get_market_strike(market)
        if strike is None:
            self._vlog(ticker, f"BLOCK strike: floor_strike missing on market dict (keys={sorted(market.keys()) if isinstance(market, dict) else '?'})")
            return None
        if strike_type != "greater_or_equal":
            self._vlog(ticker, f"BLOCK strike: unsupported strike_type={strike_type!r}")
            return None

        # ---- Current BTC + realized vol --------------------------------
        btc = btc_feed.current()
        if btc is None or btc <= 0:
            self._vlog(ticker, f"BLOCK btc: current()={btc}")
            return None
        sigma_s = _realized_vol_per_sqrt_s(btc_feed, lookback_s=EXPIRY_DECAY_VOL_WINDOW_S)
        if sigma_s is None or sigma_s <= 0:
            self._vlog(ticker, f"BLOCK vol: sigma_s={sigma_s} (insufficient history?)")
            return None
        sigma_T = sigma_s * math.sqrt(secs_left)
        if sigma_T <= 0:
            self._vlog(ticker, f"BLOCK vol: sigma_T={sigma_T}")
            return None

        # ---- Fair value P(YES) via zero-drift GBM (YES if BTC_T >= K) --
        z = math.log(strike / btc) / sigma_T
        p_yes = 1.0 - _phi(z)
        dist_sigmas = abs(math.log(btc / strike)) / sigma_T

        # Numerical guardrails — floating-point underflow near certainty
        p_yes = max(0.0, min(1.0, p_yes))

        if dist_sigmas < EXPIRY_DECAY_MIN_DIST_SIGMAS:
            self._vlog(
                ticker,
                f"BLOCK dist: {dist_sigmas:.2f}σ < {EXPIRY_DECAY_MIN_DIST_SIGMAS}σ "
                f"(BTC ${btc:,.0f} vs {strike:,.0f}, sigma_s={sigma_s:.2e}, secs_left={secs_left:.0f})"
            )
            return None

        # ---- Edge check (pick the better side) -------------------------
        yes_edge_cents = int(round((p_yes        - yes_ask) * 100))
        no_edge_cents  = int(round(((1.0 - p_yes) - no_ask)  * 100))

        if yes_edge_cents >= no_edge_cents and yes_edge_cents >= EXPIRY_DECAY_MIN_EDGE_CENTS:
            side, price_dollars, edge_cents, p_side = "yes", yes_ask, yes_edge_cents, p_yes
        elif no_edge_cents >= EXPIRY_DECAY_MIN_EDGE_CENTS:
            side, price_dollars, edge_cents, p_side = "no",  no_ask,  no_edge_cents, 1.0 - p_yes
        else:
            self._vlog(
                ticker,
                f"BLOCK edge: yes_edge={yes_edge_cents}c no_edge={no_edge_cents}c "
                f"< min={EXPIRY_DECAY_MIN_EDGE_CENTS}c "
                f"(p_yes={p_yes:.3f}, yes_ask={yes_ask:.2f}, no_ask={no_ask:.2f})"
            )
            return None

        if price_dollars <= 0:
            self._vlog(ticker, f"BLOCK price: {price_dollars}")
            return None
        price_cents = int(round(price_dollars * 100))

        # [v2] Contrarian-lottery filter. v1 dry-run showed sub-5c
        # entries 24-for-24 losers; the GBM dist_sigmas gate doesn't
        # distinguish "buy consensus side at 80c" from "buy contrarian
        # side at 1c" even though they're opposite trades.
        if price_cents < EXPIRY_DECAY_MIN_PRICE_CENTS:
            self._vlog(
                ticker,
                f"BLOCK price floor: {side.upper()} @ {price_cents}c "
                f"< min={EXPIRY_DECAY_MIN_PRICE_CENTS}c (v2 contrarian-lottery filter)"
            )
            return None

        # ---- [v2.1] Kelly sizing -----------------------------------------
        # Use per-fire GBM probability `p_side` as the win-rate input
        # (same pattern as BRIDGE_V2). Cap at self.stake to keep the
        # dashboard's "Stake $X" header truthful as the per-trade ceiling.
        # When Kelly says no edge despite passing the entry gates, fall
        # back to a small exploratory stake — matches the SNIPER /
        # CONSENSUS_V2 / BRIDGE convention so we still log the fire and
        # learn from the outcome without exposing the full stake.
        kelly_stake = None
        if EXPIRY_DECAY_KELLY_ENABLED and balance is not None and balance > 0:
            ks = kelly_size(balance, p_side, price_dollars,
                            fallback_stake=self.stake)
            if ks > 0:
                effective_stake = min(ks, self.stake)
                kelly_stake = round(effective_stake, 2)
            else:
                effective_stake = float(os.getenv("KELLY_ZERO_EDGE_STAKE", "1.0"))
        else:
            effective_stake = self.stake

        count = max(1, int(effective_stake / price_dollars))

        self.last_fire_time = now_s

        strike_repr = f"{strike:,.0f}"
        kelly_log = f" | kelly ${kelly_stake:.2f}" if kelly_stake is not None else " | flat"
        self._vlog(
            ticker,
            f"FIRE {side.upper()} @ {price_cents}c | {dist_sigmas:.2f}σ | "
            f"fair {p_side*100:.1f}% | edge +{edge_cents}c | secs_left={secs_left:.0f}"
            f"{kelly_log}"
        )
        signal = {
            "strategy": self.name,
            "ticker":   ticker,
            "side":     side,
            "price":    price_cents,
            "count":    count,
            "dollars":  round(count * price_dollars, 2),
            "reason":   (f"BTC ${btc:,.0f} vs {strike_repr} "
                         f"| {dist_sigmas:.2f}σ | {secs_left:.0f}s left "
                         f"| fair {p_side*100:.1f}%, ask {price_cents}c, "
                         f"edge +{edge_cents}c"
                         f"{f' | kelly ${kelly_stake:.2f}' if kelly_stake is not None else ''}"),
        }
        if kelly_stake is not None:
            signal["kelly_stake"] = kelly_stake
        return signal


# ═══════════════════════════════════════════════════════════
# STRATEGY 5: BRIDGE (beta)
# ═══════════════════════════════════════════════════════════

class BridgeStrategy:
    """
    Bridge beta model — fills the 3-5 minute gap between SNIPER and EXPIRY_DECAY.

    THESIS:
      In the 3-5 minute window, pure momentum (SNIPER) is getting stale and
      pure vol-distance (EXPIRY_DECAY) isn't decisive yet. Requiring BOTH
      signals simultaneously — meaningful momentum that's also vol-confirmed
      to be far from strike — should produce higher WR than either alone.

    ENTRY GATES (all must pass):
      1. Time remaining in [BRIDGE_MIN_SECS_LEFT, BRIDGE_MAX_SECS_LEFT]
      2. 5-min BTC momentum >= BRIDGE_5M_MIN_MOMENTUM (same as SNIPER v3)
      3. 60s momentum confirms same direction (same as SNIPER v3)
      4. Vol-normalized distance >= BRIDGE_MIN_DIST_SIGMAS (1.5σ, lighter
         than EXPIRY_DECAY's 2.0σ since more time remains)
      5. GBM model edge >= BRIDGE_MIN_EDGE_CENTS (2¢)
      6. Momentum direction and model direction must AGREE

    SIZING:
      v2: Kelly-fractional sizing using the GBM model probability `p_side`,
      gated by CHEAP_CONTRACT_MAX_STAKE on lottery-priced entries (matches
      the v2.1 Consensus fix). Counters reset to zero by bumping the
      beta_model_id from BRIDGE_V1 -> BRIDGE_V2 (per docs/beta_promotion.md
      "re-tuning ≡ overfitting" rule — sizing change is a model change).
    """

    def __init__(self, stake_dollars, *,
                 is_beta=True, beta_model_id="BRIDGE_V2"):
        self.stake           = stake_dollars
        self.name            = "BRIDGE"
        self.is_beta         = is_beta
        self.beta_model_id   = beta_model_id
        self.last_fire_time  = 0.0

    def evaluate(self, market, btc_feed, mins_left=None,
                 balance=None, score_mult=1.0):
        ticker = market.get("ticker", "?")

        # ---- Time gate (3-5 min window) -----------------------------------
        if mins_left is None:
            return None
        secs_left = mins_left * 60.0
        if secs_left < BRIDGE_MIN_SECS_LEFT or secs_left > BRIDGE_MAX_SECS_LEFT:
            return None

        # ---- Cooldown -----------------------------------------------------
        now_s = time.time()
        if now_s - self.last_fire_time < BRIDGE_COOLDOWN_S:
            return None

        # ---- Liquidity ----------------------------------------------------
        yes_ask = float(market.get("yes_ask_dollars", 0) or 0)
        no_ask  = float(market.get("no_ask_dollars",  0) or 0)
        if yes_ask <= 0 or no_ask <= 0:
            return None
        if yes_ask + no_ask < MIN_BOOK_SUM:
            return None

        # ---- 5-min momentum gate (SNIPER v3 thesis) ----------------------
        mom_5m = btc_feed.pct_change(300)
        if mom_5m is None or abs(mom_5m) < BRIDGE_5M_MIN_MOMENTUM:
            return None

        # ---- 60s confirmation (SNIPER v3 thesis) -------------------------
        mom_60s = btc_feed.pct_change(60)
        if mom_60s is None:
            return None
        same_dir = (mom_5m > 0 and mom_60s > BRIDGE_60S_CONFIRM_MIN) or \
                   (mom_5m < 0 and mom_60s < -BRIDGE_60S_CONFIRM_MIN)
        if not same_dir:
            return None

        momentum_side = "yes" if mom_5m > 0 else "no"

        # ---- Strike + vol (EXPIRY_DECAY thesis) --------------------------
        strike, strike_type = _get_market_strike(market)
        if strike is None or strike_type != "greater_or_equal":
            return None

        btc = btc_feed.current()
        if btc is None or btc <= 0:
            return None
        sigma_s = _realized_vol_per_sqrt_s(btc_feed, lookback_s=BRIDGE_VOL_WINDOW_S)
        if sigma_s is None or sigma_s <= 0:
            return None
        sigma_T = sigma_s * math.sqrt(secs_left)
        if sigma_T <= 0:
            return None

        # ---- Vol-normalized distance gate --------------------------------
        dist_sigmas = abs(math.log(btc / strike)) / sigma_T
        if dist_sigmas < BRIDGE_MIN_DIST_SIGMAS:
            return None

        # ---- GBM fair value + edge check ---------------------------------
        z = math.log(strike / btc) / sigma_T
        p_yes = max(0.0, min(1.0, 1.0 - _phi(z)))

        yes_edge_cents = int(round((p_yes - yes_ask) * 100))
        no_edge_cents  = int(round(((1.0 - p_yes) - no_ask) * 100))

        if yes_edge_cents >= no_edge_cents and yes_edge_cents >= BRIDGE_MIN_EDGE_CENTS:
            model_side, price_dollars, edge_cents, p_side = "yes", yes_ask, yes_edge_cents, p_yes
        elif no_edge_cents >= BRIDGE_MIN_EDGE_CENTS:
            model_side, price_dollars, edge_cents, p_side = "no", no_ask, no_edge_cents, 1.0 - p_yes
        else:
            return None

        # ---- KEY GATE: momentum and model must AGREE on direction --------
        if momentum_side != model_side:
            return None

        if price_dollars <= 0:
            return None
        price_cents = int(round(price_dollars * 100))

        # ---- Sizing (v2) --------------------------------------------------
        # v1 used `count = stake / price`, which produced equal $ downside
        # but wildly different upside / variance per fire (a 2c NO @ $20
        # stake = ~1000 contracts = ~$980 max payout, vs. a 90c YES @ $20
        # stake = ~$2.20 max payout). v2 routes through kelly_size() using
        # the GBM model probability p_side, then applies the same cheap-
        # contract cap as Consensus/Sniper to limit lottery-priced bleed.
        base_stake = self.stake
        kelly_stake = None
        if BRIDGE_KELLY_ENABLED and balance is not None and balance > 0:
            ks = kelly_size(balance, p_side, price_dollars,
                            fallback_stake=base_stake)
            if ks > 0:
                effective_stake = ks
                kelly_stake = round(ks, 2)
            else:
                # Kelly says no edge despite passing entry gates — take a
                # small exploratory stake (matches Consensus/Sniper pattern).
                effective_stake = float(os.getenv("KELLY_ZERO_EDGE_STAKE", "1.0"))
        else:
            effective_stake = base_stake

        # Cheap-contract cap (matches Consensus v2.1 / Sniper): limit dollar
        # exposure on lottery-priced entries — the structural fix for the
        # 2c NO / 800-contract case from BRIDGE_V1.
        cheap_capped = False
        if price_cents < CHEAP_CONTRACT_PRICE and effective_stake > CHEAP_CONTRACT_MAX_STAKE:
            effective_stake = CHEAP_CONTRACT_MAX_STAKE
            cheap_capped = True

        count = max(1, int(effective_stake / price_dollars))

        self.last_fire_time = now_s

        signal = {
            "strategy": self.name,
            "ticker":   ticker,
            "side":     model_side,
            "price":    price_cents,
            "count":    count,
            "dollars":  round(count * price_dollars, 2),
            "reason":   (f"5m {mom_5m*100:+.3f}% + {dist_sigmas:.2f}σ | "
                         f"{secs_left:.0f}s left | fair {p_side*100:.1f}%, "
                         f"ask {price_cents}c, edge +{edge_cents}c"
                         f"{' [CHEAP_CAP $' + str(CHEAP_CONTRACT_MAX_STAKE) + ']' if cheap_capped else ''}"),
        }
        if kelly_stake is not None:
            signal["kelly_stake"] = kelly_stake
        return signal


# ═══════════════════════════════════════════════════════════
# STRATEGY 6: FADE (beta)
# ═══════════════════════════════════════════════════════════

class FadeStrategy:
    """
    Fade beta model — mean-reversion after BTC overextension.

    THESIS:
      SNIPER rides momentum. But momentum overshoots — BTC spikes 0.10%+
      in 5 minutes, the contract reprices, then BTC mean-reverts and the
      contract settles near the original level. FADE sells into these
      overextensions by trading AGAINST the 5m direction when 60s momentum
      shows the move is weakening or reversing.

    ENTRY GATES (all must pass):
      1. Time remaining in [FADE_MIN_SECS_LEFT, FADE_MAX_SECS_LEFT]
      2. 5-min BTC momentum >= FADE_5M_MIN_MOMENTUM (0.10% — strong move)
      3. 60s momentum must be WEAKENING: |mom_60s| < FADE_60S_DECAY_THRESHOLD
         (the spike happened but the last minute shows it's stalling)
      4. Entry price in [FADE_MIN_PRICE_CENTS, FADE_MAX_PRICE_CENTS] range
      5. Realized vol must be elevated (above FADE_VOL_PERCENTILE_MIN of
         recent history) — mean-reversion works in high-vol, not trending

    DIRECTION:
      OPPOSITE to 5-min momentum. If BTC spiked up -> buy NO (fade the spike).
      If BTC dropped -> buy YES (fade the drop).

    SIZING:
      Fixed stake / price_dollars, with CHEAP_CONTRACT_MAX_STAKE applied
      to the 40-44c slice of the entry band (matches Consensus v2.1).
      No Kelly priors yet — FADE has no explicit win-probability model;
      Kelly would require a calibrated mean-reversion p estimate first.
    """

    def __init__(self, stake_dollars, *,
                 is_beta=True, beta_model_id="FADE_V1"):
        self.stake           = stake_dollars
        self.name            = "FADE"
        self.is_beta         = is_beta
        self.beta_model_id   = beta_model_id
        self.last_fire_time  = 0.0
        # Rolling vol samples for percentile gating
        self._vol_history    = deque(maxlen=200)

    def evaluate(self, market, btc_feed, mins_left=None,
                 balance=None, score_mult=1.0):
        ticker = market.get("ticker", "?")

        # ---- Time gate (5-12 min window) ----------------------------------
        if mins_left is None:
            return None
        secs_left = mins_left * 60.0
        if secs_left < FADE_MIN_SECS_LEFT or secs_left > FADE_MAX_SECS_LEFT:
            return None

        # ---- Cooldown -----------------------------------------------------
        now_s = time.time()
        if now_s - self.last_fire_time < FADE_COOLDOWN_S:
            return None

        # ---- Liquidity ----------------------------------------------------
        yes_ask = float(market.get("yes_ask_dollars", 0) or 0)
        no_ask  = float(market.get("no_ask_dollars",  0) or 0)
        if yes_ask <= 0 or no_ask <= 0:
            return None
        if yes_ask + no_ask < MIN_BOOK_SUM:
            return None

        # ---- 5-min momentum: require STRONG move --------------------------
        mom_5m = btc_feed.pct_change(300)
        if mom_5m is None or abs(mom_5m) < FADE_5M_MIN_MOMENTUM:
            return None

        # ---- 60s decay: move must be WEAKENING ----------------------------
        # The spike happened in the 5m window but the last 60s shows it's
        # stalling or reversing. This is the mean-reversion signal.
        mom_60s = btc_feed.pct_change(60)
        if mom_60s is None:
            return None
        # 60s momentum must be weak (near zero or opposing 5m direction)
        if abs(mom_60s) >= FADE_60S_DECAY_THRESHOLD:
            # If 60s is still strong AND in the same direction as 5m,
            # the move is continuing — don't fade it
            if (mom_5m > 0 and mom_60s > 0) or (mom_5m < 0 and mom_60s < 0):
                return None

        # ---- Vol regime gate: only fade in elevated vol -------------------
        sigma_s = _realized_vol_per_sqrt_s(btc_feed, lookback_s=900)
        if sigma_s is not None and sigma_s > 0:
            self._vol_history.append(sigma_s)
        if len(self._vol_history) < 20:
            return None  # not enough vol history to judge regime
        sorted_vols = sorted(self._vol_history)
        vol_percentile_idx = int(FADE_VOL_PERCENTILE_MIN * len(sorted_vols))
        vol_threshold = sorted_vols[min(vol_percentile_idx, len(sorted_vols) - 1)]
        if sigma_s is None or sigma_s < vol_threshold:
            return None  # low-vol regime — momentum may persist, don't fade

        # ---- Direction: OPPOSITE to 5m momentum ---------------------------
        fade_side = "no" if mom_5m > 0 else "yes"
        price_dollars = yes_ask if fade_side == "yes" else no_ask
        price_cents = int(round(price_dollars * 100))

        # ---- Price band filter (40-50c range) -----------------------------
        if price_cents < FADE_MIN_PRICE_CENTS or price_cents > FADE_MAX_PRICE_CENTS:
            return None

        if price_dollars <= 0:
            return None

        # ---- Sizing -------------------------------------------------------
        # Apply the cheap-contract cap consistently with Consensus v2.1 /
        # Sniper. With FADE_MIN_PRICE_CENTS=40 and CHEAP_CONTRACT_PRICE=45
        # this only bites on the 40-44c slice, but it costs nothing to keep
        # the rule uniform across strategies (and protects us if either
        # threshold moves later).
        effective_stake = self.stake
        cheap_capped = False
        if price_cents < CHEAP_CONTRACT_PRICE and effective_stake > CHEAP_CONTRACT_MAX_STAKE:
            effective_stake = CHEAP_CONTRACT_MAX_STAKE
            cheap_capped = True

        count = max(1, int(effective_stake / price_dollars))

        self.last_fire_time = now_s

        return {
            "strategy": self.name,
            "ticker":   ticker,
            "side":     fade_side,
            "price":    price_cents,
            "count":    count,
            "dollars":  round(count * price_dollars, 2),
            "reason":   (f"FADE 5m {mom_5m*100:+.3f}% -> {fade_side.upper()} @ {price_cents}c "
                         f"| 60s: {mom_60s*100:+.3f}% (decaying) "
                         f"| vol {sigma_s:.2e} (p{FADE_VOL_PERCENTILE_MIN:.0%}) "
                         f"| {secs_left:.0f}s left"
                         f"{' [CHEAP_CAP $' + str(CHEAP_CONTRACT_MAX_STAKE) + ']' if cheap_capped else ''}"),
        }


#âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# RISK MANAGEMENT
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

class RiskManager:
    """
    Hard stops to prevent runaway losses.

    Tracks THREE separate daily loss metrics, all in ET-day buckets:

      1. NET daily P&L (existing).      Halts when net <= -DAILY_LOSS_LIMIT.
         This is the headline number but is too lenient on its own — a single
         lucky tail-bet win can let the bot bleed indefinitely without ever
         tripping the net limit (proven by the 2026-04-09 post-mortem).

      2. GROSS daily losses (NEW).      Halts when sum-of-losing-pnl <=
         -DAILY_GROSS_LOSS_LIMIT, regardless of how many wins offset them.
         This is the metric that catches "drip-drip-drip" bleeding strategies.
         Default = 1.5 × net limit; override with env DAILY_GROSS_LOSS_LIMIT.

      3. OPEN-exposure cap (NEW).        Halts new entries when the dollar
         value of currently-OPEN positions plus today's realized losses
         already meets the gross limit. This prevents the bot from stacking
         4 simultaneous max-loss trades that don't show up in pnl until they
         resolve (because t["pnl"] is None on unresolved trades).
    """
    def __init__(self, daily_loss_limit, max_open_trades=4,
                 gross_daily_loss_limit=None):
        self.daily_limit  = daily_loss_limit
        self.gross_daily_limit = (
            gross_daily_loss_limit
            if gross_daily_loss_limit is not None
            else 1.5 * daily_loss_limit
        )
        self.max_open     = max_open_trades
        self._halted      = False
        self._halt_reason = ""
        self._halt_date   = None          # date the halt was triggered
        self._override_date = None        # date of manual daily-loss override (bypass until next ET midnight)

    def check(self, client):
        today = today_et()

        # Auto-reset halt at ET midnight — new trading day, clean slate
        if self._halted and self._halt_date and today > self._halt_date:
            print(f"  ♻ New trading day (ET) — clearing yesterday's halt ({self._halt_reason})")
            self._halted      = False
            self._halt_reason = ""
            self._halt_date   = None

        # Auto-clear manual daily-loss override at ET midnight too
        if self._override_date and today > self._override_date:
            print(f"  ♻ New trading day (ET) — clearing manual daily-loss override")
            self._override_date = None

        if self._halted:
            return False, self._halt_reason

        trades = load_trades()
        # CRITICAL: live trades only. Beta and dry-run records carry
        # synthetic pnl set by resolve_trades (the "best we can do is treat
        # logged size as filled size" path), and beta strategies multiply
        # the rate of synthetic losses every cycle. Counting them toward
        # halt thresholds means imaginary losses can shut off real trading
        # — which is exactly what was happening on 2026-04-23/24/25 when
        # the bot halted at -$80/-$251/-$89 while the dashboard's live-only
        # view showed -$18/-$10/$0. The dashboard's /api/pnl_windows already
        # filters these the same way; this brings RiskManager into
        # agreement so halt logic and displayed P/L can't disagree.
        today_settled = [
            t for t in trades
            if t.get("result")
            and not t.get("is_beta")
            and not t.get("dry_run")
            and trade_date_et(t.get("timestamp")) == today
        ]
        today_open = [
            t for t in trades
            if not t.get("result")
            and not t.get("is_beta")
            and not t.get("dry_run")
            and trade_date_et(t.get("timestamp")) == today
        ]

        # ── Gross loss + net loss limits ─────────────────────────────────
        # Skipped for the rest of today if the operator manually reset the
        # halt via the dashboard (Reset Now button).
        if self._override_date != today:
            net_pnl = sum(t.get("pnl", 0) for t in today_settled if t.get("pnl"))
            gross_loss = sum(t.get("pnl", 0) for t in today_settled
                             if t.get("pnl") and t.get("pnl") < 0)
            # Worst-case loss on open positions = full premium paid (assume
            # they all settle losing). This is intentionally pessimistic.
            open_exposure_loss = -sum(t.get("dollars", 0) for t in today_open)

            if net_pnl <= -abs(self.daily_limit):
                self._halted      = True
                self._halt_reason = f"Daily NET loss limit hit (${net_pnl:.2f})"
                self._halt_date   = today
                return False, self._halt_reason

            if gross_loss <= -abs(self.gross_daily_limit):
                self._halted      = True
                self._halt_reason = (
                    f"Daily GROSS loss limit hit "
                    f"(realized losses ${gross_loss:.2f} >= "
                    f"${self.gross_daily_limit:.2f})"
                )
                self._halt_date   = today
                return False, self._halt_reason

            # Worst-case if every open trade lost
            worst_case = gross_loss + open_exposure_loss
            if worst_case <= -abs(self.gross_daily_limit):
                # Don't permanently halt — block new entries only, since open
                # trades may still resolve favorably. Gets re-evaluated next cycle.
                return False, (
                    f"Open-exposure pause: realized ${gross_loss:.2f} + "
                    f"open ${open_exposure_loss:.2f} = ${worst_case:.2f} "
                    f"would breach gross limit ${self.gross_daily_limit:.2f}"
                )

        # Open trade count — live only, same reason as today_settled above.
        open_trades = [
            t for t in trades
            if not t.get("result")
            and not t.get("is_beta")
            and not t.get("dry_run")
        ]
        if len(open_trades) >= self.max_open:
            return False, f"Max open trades ({self.max_open}) reached"

        # Balance sanity check
        try:
            bal = client.get_balance()
            if bal < 10:
                self._halted      = True
                self._halt_reason = f"Balance too low (${bal:.2f})"
                return False, self._halt_reason
        except Exception:
            pass

        return True, "OK"


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# MAIN BOT LOOP
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

class KalshiBot:
    def __init__(self, client, max_stake, daily_loss_limit, dry_run,
                 gross_daily_loss_limit=None,
                 sniper_lottery_stake=None, sniper_conviction_stake=None):
        self.client    = client
        self.max_stake = max_stake     # [v2.0] unified stake ceiling
        self.btc       = BTCPriceFeed(window=500)
        self.lag       = LagStrategy(max_stake)
        self.consensus = ConsensusStrategy(max_stake)
        self.sniper    = SniperStrategy(
            lottery_stake=sniper_lottery_stake or max_stake,
            conviction_stake=sniper_conviction_stake or max_stake,
        )
        # ExpiryDecay promoted from beta 2026-04-30 (110 fires, 92.7% WR,
        # +60% sim ROI). v2.1 added Kelly sizing on the same entry logic.
        # Lives in the late-window dispatch branch (mins_left < 3) since
        # SNIPER / LAG / CONSENSUS each enforce mins_left >= 3 internally.
        self.expiry_decay = ExpiryDecayStrategy(max_stake)
        self.risk      = RiskManager(daily_loss_limit,
                                     gross_daily_loss_limit=gross_daily_loss_limit)
        self.dry       = dry_run
        # Beta strategy registry. Beta strategies always run dry-run, produce
        # trade records tagged is_beta=True + beta_model_id=<id>, and do NOT
        # participate in auto-scoring, disagreement gating, or position
        # monitoring.
        self.beta_strategies      = []
        # Per-beta-model per-cycle dedup. Separate from self.traded_this_market
        # so beta open positions don't block live strategies and vice versa.
        self.beta_traded_by_model = {}  # model_id -> set of tickers
        # ── CONSENSUS_V1 beta (Phase 3) — RETIRED 2026-04-29 ──────
        # Replaced by CONSENSUS_V2 (momentum-regime hybrid, 36-45c band).
        # V1's existing trade-log entries continue to render under the
        # /beta dashboard's "Retired" tab; no new fires are emitted.
        # ── EXPIRY_DECAY (Phase 4) — PROMOTED LIVE 2026-04-30 ───────────
        # Was beta. Cleared docs/beta_promotion.md bar at 110 sim fires:
        #   WR 92.7% (vs 78.4% breakeven, +14.3pp gap), +60% sim ROI,
        #   worst trade -$19.99 (1.02x avg stake — clear of the 3x cap).
        # Live registration is `self.expiry_decay` in __init__ above; this
        # block intentionally left empty so the surrounding model_id
        # numbering stays stable in commit history. The per-cycle dispatch
        # for ExpiryDecay lives below in the active_strategies loop —
        # gated by EXPIRY_DECAY_ENABLED + late-window time check.
        # ── CONSENSUS_V1_WITH_EXIT beta ─────────────────────────────────
        # Mirrors CONSENSUS_V1's entry logic but adds the edge-gone exit
        # rule (see ConsensusExitTracker). Both models run simultaneously
        # so dashboard can diff net P&L between ride-to-resolution
        # (CONSENSUS_V1) and exit-when-signal-decays (CONSENSUS_V1_WITH_EXIT)
        # over the same entry universe.
        if CONSENSUS_EDGE_EXIT_BETA_ENABLED:
            self.beta_strategies.append(ConsensusStrategy(
                max_stake,
                is_beta=True,
                beta_model_id=ConsensusExitTracker.BETA_MODEL_ID,
            ))
        # ── BRIDGE_V2 beta (gap filler: 3-5 min window) ─────────────────
        # NOTE: model_id bumped V1 -> V2 (Kelly + cheap-cap sizing fix —
        # see commit log). This call-site tag takes precedence over the
        # __init__ default, so it must be updated when the model_id rolls.
        if BRIDGE_BETA_ENABLED:
            self.beta_strategies.append(BridgeStrategy(
                max_stake,
                is_beta=True,
                beta_model_id="BRIDGE_V2",
            ))
        # ── FADE_V1 beta (mean-reversion contrarian) ────────────────────
        if FADE_V1_BETA_ENABLED:
            self.beta_strategies.append(FadeStrategy(
                max_stake,
                is_beta=True,
                beta_model_id="FADE_V1",
            ))
        # ── CONSENSUS_V2 beta (momentum-regime hybrid, 36-45c) ──────────
        # Replaces V1. Uses SNIPER-grade 5m momentum + previous-result
        # regime agreement to trade the 36-45c OTM band. V1 data showed
        # this band at 63.6% WR / 71.3% ROI. Capped at $3/trade while
        # accumulating sample.
        self.consensus_v2 = None
        if CONSENSUS_BETA_ENABLED:
            self.consensus_v2 = ConsensusV2Strategy(
                max_stake,
                is_beta=True,
                beta_model_id="CONSENSUS_V2",
            )
            self.beta_strategies.append(self.consensus_v2)
        # [v2.0] New components
        self.scorer    = StrategyScorer()
        self.monitor   = PositionMonitor()
        # Beta edge-gone tracker. Self-contained from self.monitor (which
        # only touches SNIPER Conviction) so there's no risk of
        # double-tracking or misrouted exits.
        self.consensus_exit_tracker = ConsensusExitTracker()
        if CONSENSUS_EDGE_EXIT_BETA_ENABLED:
            restored = self.consensus_exit_tracker.rehydrate_from_trades()
            if restored:
                print(f"  [consensus-exit-beta] rehydrated {restored} open "
                      f"position(s) from trade log", flush=True)
        self._balance  = None             # cached balance, updated each cycle
        # [v2.0] Telemetry counters for dashboard
        self.v2_stats  = {
            "disagreements_skipped": 0,
            "early_exits_triggered": 0,
            "cheap_caps_applied": 0,
            "kelly_active": KELLY_ENABLED,
            "last_kelly_sizes": {},  # strategy name → last kelly stake
        }
        # Seed from existing open trades so we never double up on a market
        # after a Render restart (rebuild_trades_from_api creates RECOVERED
        # records, but traded_this_market was empty → strategies would fire
        # on markets we already hold positions in).
        self.traded_this_market = set()
        try:
            for t in load_trades():
                # Skip beta trades — they share the market space but live in
                # their own book, so open beta positions must not block live.
                if t.get("is_beta"):
                    continue
                if not t.get("result") and t.get("ticker"):
                    self.traded_this_market.add(t["ticker"])
            if self.traded_this_market:
                print(f"  [bot] Pre-seeded {len(self.traded_this_market)} market(s) "
                      f"from open positions: {self.traded_this_market}", flush=True)
        except Exception:
            pass
        self._last_markets = []          # exposed for dashboard
        self._market_sample_logged = False  # one-shot diagnostic flag

    def _select_open_book_for_correlation(self, is_beta, beta_model_id):
        """Pick the right "book" of open trades for correlation scoring.

        Live signals correlate against live open positions only; beta
        signals correlate against their own beta model's open positions
        (each beta is a self-contained counterfactual, so cross-book
        correlation would be noise).

        Filters out settled trades (`result` set) and early-exited trades
        (`closed_by_exit`). Called from _log_signal on every logged
        signal, so kept O(n) with no API hits.
        """
        try:
            trades = load_trades()
        except Exception:
            return []
        book = []
        for t in trades:
            if t.get("result"):
                continue
            if t.get("closed_by_exit"):
                continue
            if is_beta:
                if not t.get("is_beta"):
                    continue
                if t.get("beta_model_id") != beta_model_id:
                    continue
            else:
                if t.get("is_beta"):
                    continue
            book.append(t)
        return book

    def _log_signal(self, signal, order_result):
        # Extract order_id from Kalshi's response so update_fill_times()
        # can later join this trade against /portfolio/fills by order_id.
        # Kalshi wraps the created order under an "order" key on 201.
        order_id = None
        if isinstance(order_result, dict):
            inner = order_result.get("order")
            if isinstance(inner, dict):
                order_id = inner.get("order_id")
            if not order_id:
                order_id = order_result.get("order_id")

        # Beta signals are ALWAYS dry-run regardless of global self.dry.
        # Live signals inherit self.dry. This invariant is enforced here so no
        # caller can accidentally log a beta trade as live.
        is_beta_signal = bool(signal.get("is_beta"))
        record_dry_run = True if is_beta_signal else self.dry

        # Phase-1 correlation instrumentation: annotate the signal with
        # kelly_stake_raw / corr_factor / corr_diag / kelly_stake_adjusted
        # fields before the record is persisted. Log-only in phase 1 — does
        # not mutate kelly_stake / count / dollars. Wrapped so a parse or
        # I/O hiccup never takes down the live log path.
        try:
            open_book = self._select_open_book_for_correlation(
                is_beta_signal, signal.get("beta_model_id"),
            )
            annotate_correlation(signal, open_book)
        except Exception as _corr_err:
            print(Fore.YELLOW + f"  [corr] annotate skipped: {_corr_err}",
                  flush=True)

        record = {
            **signal,
            "timestamp":       now_et().isoformat(),  # placement time (bot-side)
            "dry_run":         record_dry_run,
            "is_beta":         is_beta_signal,
            "beta_model_id":   signal.get("beta_model_id"),
            "order":           order_result,
            "order_id":        order_id,
            "fill_timestamp":  None,   # populated by update_fill_times()
            "fill_latency_s":  None,   # populated by update_fill_times()
            "outcome":         None,
            "result":          None,
            "pnl":             None,
        }
        save_trade(record)
        return record

    def _print_signal(self, signal, dry):
        color = Fore.CYAN if signal["strategy"] == "LAG" else Fore.MAGENTA
        if signal.get("is_beta"):
            tag = f"[BETA:{signal.get('beta_model_id','')}]"
        else:
            tag = "[DRY]" if dry else "[LIVE]"
        print(
            f"\n{color}âº {signal['strategy']} {tag}{Style.RESET_ALL}  "
            f"{signal['ticker']}  "
            f"{signal['side'].upper()} @ {signal['price']}Â¢  "
            f"x{signal['count']} = ${signal['dollars']:.2f}\n"
            f"  Reason: {signal['reason']}"
        )

    def run_once(self):
        # 1. Read latest BTC price (pushed via WebSocket, REST fallback)
        if not self.btc.is_live:
            # WS is down — poll REST every cycle to keep prices fresh
            self.btc.fetch()
        btc_price = self.btc.current()
        if btc_price is None:
            print(Fore.YELLOW + "  BTC price unavailable, skipping cycle")
            return

        # 2. Fetch active BTC 15-min markets
        try:
            markets = self.client.get_markets_by_series(BTC_TICKER, status="open")
        except Exception as e:
            print(Fore.RED + f"  Market fetch error: {e}")
            return

        if not markets:
            print(Fore.YELLOW + "  No open BTC 15-min markets right now")
            return

        self._last_markets = markets  # expose for dashboard

        # One-shot diagnostic dump so we can see the full Kalshi market shape
        # (looking for reference_price / underlying_value / subtitle fields).
        # Gated by LOG_MARKET_SAMPLE env flag; prints only once per process.
        if LOG_MARKET_SAMPLE and not self._market_sample_logged and markets:
            try:
                sample = markets[0]
                print("\n[DIAG market-sample] Full JSON of markets[0]:", flush=True)
                print(json.dumps(sample, indent=2, default=str), flush=True)
                print(f"[DIAG market-sample] Keys: {sorted(sample.keys())}",
                      flush=True)
                # Also try get_market() for the single-market endpoint —
                # sometimes Kalshi exposes extra fields there vs the list view.
                try:
                    detail = self.client.get_market(sample["ticker"])
                    if detail:
                        print("\n[DIAG market-sample] Full JSON of get_market(ticker):",
                              flush=True)
                        print(json.dumps(detail, indent=2, default=str), flush=True)
                        print(f"[DIAG market-sample] Detail keys: {sorted(detail.keys())}",
                              flush=True)
                except Exception as e:
                    print(f"[DIAG market-sample] get_market detail fetch failed: {e}",
                          flush=True)
            except Exception as e:
                print(f"[DIAG market-sample] dump failed: {e}", flush=True)
            self._market_sample_logged = True

        # 3. Update consensus previous result
        try:
            self.consensus.update_previous(markets, self.client)
        except Exception:
            pass

        # Share the same previous-result ground truth with any beta
        # ConsensusStrategy instances so they don't double-poll the
        # settled-markets API. The previous-result signal is objective
        # ground truth — there's no experimental value in each beta
        # instance querying it independently.
        for _bs in self.beta_strategies:
            if isinstance(_bs, (ConsensusStrategy, ConsensusV2Strategy)):
                _bs.last_result      = self.consensus.last_result
                _bs.last_result_time = self.consensus.last_result_time

        # 4. Risk check
        ok, reason = self.risk.check(self.client)
        if not ok:
            print(Fore.RED + f"  ð Risk halt: {reason}")
            return

        # [v2.0] Cache balance for Kelly sizing
        try:
            self._balance = self.client.get_balance()
        except Exception:
            pass  # use last cached value

        # [v2.0] Check early exits on Conviction positions BEFORE new entries
        if EARLY_EXIT_ENABLED and self._last_markets:
            try:
                exit_signals = self.monitor.check_exits(self.btc, markets)
                for ex in exit_signals:
                    print(f"\n{Fore.YELLOW}>>> {ex['reason']}{Style.RESET_ALL}", flush=True)
                    if not self.dry:
                        resp = self.client.sell_order(
                            ticker=ex["ticker"], side=ex["side"],
                            price_cents=ex["sell_price_cents"],
                            count=ex["count"], strategy_tag="EXT",
                        )
                        if "error" not in resp:
                            self.v2_stats["early_exits_triggered"] += 1
                            # [v2.2] Attribute exit P&L to the ORIGINATING
                            # strategy by finalizing the entry record in-place
                            # instead of saving a separate "EARLY_EXIT" row.
                            # See close_trade_by_early_exit() docstring.
                            closed = close_trade_by_early_exit(
                                ticker=ex["ticker"],
                                side=ex["side"],
                                count=ex["count"],
                                entry_cents=ex["entry_price_cents"],
                                exit_cents=ex["sell_price_cents"],
                                exit_reason=ex.get("reason", ""),
                            )
                            if not closed:
                                # Fallback: no matching open trade found
                                # (shouldn't happen, but don't silently drop
                                # the exit — log it so we can audit later).
                                save_trade({
                                    "strategy":    "EARLY_EXIT",
                                    "ticker":      ex["ticker"],
                                    "side":        ex["side"],
                                    "price":       ex["sell_price_cents"],
                                    "count":       ex["count"],
                                    "dollars":     round(ex["count"] * ex["sell_price_cents"] / 100.0, 2),
                                    "reason":      ex["reason"],
                                    "timestamp":   now_et().isoformat(),
                                    "dry_run":     False,
                                    "order":       resp,
                                    "order_id":    (resp.get("order", {}) or {}).get("order_id"),
                                    "result":      None,
                                    "pnl":         None,
                                    "outcome":     None,
                                    "_orphan_exit": True,
                                })
                    self.monitor.remove(ex["ticker"])
            except Exception as e:
                print(f"  [early-exit] Error: {e}", flush=True)

        # [Beta] Consensus edge-gone exit simulation. Runs each cycle
        # before the live entry pass so beta exit decisions and live
        # entry decisions see the same BTC/market state. Dry-run only —
        # mutates beta trade records in-place via close_trade_by_early_exit
        # with beta_model_id scoping so no live position is touched.
        if CONSENSUS_EDGE_EXIT_BETA_ENABLED and self.consensus_exit_tracker.positions:
            try:
                beta_exits = self.consensus_exit_tracker.check_exits(
                    self.btc, markets, self.consensus,
                )
                for ex in beta_exits:
                    print(f"\n{Fore.CYAN}>>> [BETA:"
                          f"{ConsensusExitTracker.BETA_MODEL_ID}] "
                          f"{ex['reason']}{Style.RESET_ALL}", flush=True)
                    closed = close_trade_by_early_exit(
                        ticker=ex["ticker"],
                        side=ex["side"],
                        count=ex["count"],
                        entry_cents=ex["entry_price_cents"],
                        exit_cents=ex["sell_price_cents"],
                        exit_reason=ex["reason"],
                        beta_model_id=ConsensusExitTracker.BETA_MODEL_ID,
                    )
                    if not closed:
                        # Tracker had a position we couldn't find in the
                        # trade log (e.g. log rotated, or rehydrated from a
                        # partial state). Drop it so we don't keep trying.
                        print(f"  [consensus-exit-beta] WARN: no matching "
                              f"trade record for {ex['ticker']} "
                              f"{ex['side']} x{ex['count']} — dropping "
                              f"tracker entry", flush=True)
                    self.consensus_exit_tracker.remove(ex["ticker"])
            except Exception as e:
                print(f"  [consensus-exit-beta] Error: {e}", flush=True)

        # 5. Evaluate strategies on each market
        for market in markets:
            ticker = market["ticker"]

            # Skip already traded this market cycle
            if ticker in self.traded_this_market:
                continue

            # Time remaining check
            close_time = market.get("close_time")
            mins_left = None
            if close_time:
                try:
                    ct = datetime.datetime.fromisoformat(
                        close_time.replace("Z", "+00:00")
                    )
                    mins_left = (ct - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 60
                    # [2026-04-30] Lower bound dropped from 3 → 0 to admit
                    # EXPIRY_DECAY's 0-3 min window. Each non-late strategy
                    # (LAG/CONSENSUS/SNIPER) re-enforces its own min-mins
                    # below via the `is_late_window` check, so this loosen
                    # is safe.
                    if mins_left < 0 or mins_left > 14:
                        continue
                except Exception:
                    pass

            # Late-window flag: EXPIRY_DECAY's domain. Other live strategies
            # all require mins_left >= 3 (SNIPER's SNIPER_MIN_MINS_LEFT=5,
            # CONSENSUS_V2_MIN_MINS_LEFT=5, LAG is timing-agnostic but has
            # never been validated below 3 min). Keep them out of this band.
            is_late_window = mins_left is not None and mins_left < 3

            active_strategies = []
            if LAG_ENABLED and not is_late_window:
                active_strategies.append(self.lag)
            if CONSENSUS_ENABLED and not is_late_window:
                active_strategies.append(self.consensus)
            if SNIPER_ENABLED and not is_late_window:
                active_strategies.append(self.sniper)
            if EXPIRY_DECAY_ENABLED and is_late_window:
                active_strategies.append(self.expiry_decay)

            # [v2.0] Collect ALL signals for disagreement gating
            signals = []
            for strategy in active_strategies:
                # [v2.0] Auto-score check
                score_val, score_mult = self.scorer.score(strategy.name)
                if score_mult <= 0:
                    print(f"  [{strategy.name}] Auto-blocked (score={score_val:.0f})", flush=True)
                    continue

                try:
                    if isinstance(strategy, LagStrategy):
                        signal = strategy.evaluate(market, self.btc,
                                                   balance=self._balance)
                    elif isinstance(strategy, ConsensusStrategy):
                        signal = strategy.evaluate(market, self.btc,
                                                   mins_left=mins_left,
                                                   balance=self._balance,
                                                   score_mult=score_mult)
                    elif isinstance(strategy, SniperStrategy):
                        signal = strategy.evaluate(market, self.btc,
                                                   mins_left=mins_left,
                                                   balance=self._balance,
                                                   score_mult=score_mult)
                    elif isinstance(strategy, ExpiryDecayStrategy):
                        signal = strategy.evaluate(market, self.btc,
                                                   mins_left=mins_left,
                                                   balance=self._balance,
                                                   score_mult=score_mult)
                    else:
                        signal = strategy.evaluate(market, self.btc)
                except Exception as e:
                    print(Fore.RED + f"  Strategy error ({strategy.name}): {e}")
                    continue

                if signal:
                    signal["_score"] = score_val
                    signal["_score_mult"] = score_mult
                    signals.append(signal)

            if not signals:
                continue

            # [v2.0] Disagreement gating: if strategies disagree on direction, skip
            if DISAGREEMENT_GATING and len(signals) > 1:
                sides = set(s["side"] for s in signals)
                if len(sides) > 1:
                    names = [s["strategy"] for s in signals]
                    print(f"  [{ticker}] Signal disagreement ({names}), skipping market", flush=True)
                    self.v2_stats["disagreements_skipped"] += 1
                    continue

            # Use first (highest-priority) signal
            signal = signals[0]

            # [v2.1] Track cheap-cap usage
            if "CHEAP_CAP" in signal.get("reason", ""):
                self.v2_stats["cheap_caps_applied"] += 1

            self._print_signal(signal, self.dry)

            # Place order
            if self.dry:
                order_result = {"dry_run": True}
            else:
                try:
                    resp = self.client.place_order(
                        ticker=signal["ticker"],
                        side=signal["side"],
                        price_cents=signal["price"],
                        count=signal["count"],
                        strategy_tag=signal["strategy"],
                    )
                    order_result = resp
                except Exception as e:
                    order_result = {"error": str(e)}
                    print(Fore.RED + f"  Order error: {e}")

            if "error" in order_result and not order_result.get("dry_run"):
                print(Fore.RED + f"  Order rejected -- not logging as open trade.")
                continue

            self._log_signal(signal, order_result)
            self.traded_this_market.add(ticker)

            # [v2.0] Track position for early exit monitoring
            btc_now = self.btc.current()
            if btc_now:
                self.monitor.track(signal, btc_now)

            break  # one strategy per market per cycle

        # ─────────────────────────────────────────────────────────
        # BETA PASS — isolated from live.
        # ─────────────────────────────────────────────────────────
        # Beta strategies evaluate independently of the live loop above.
        # They do NOT participate in disagreement gating, auto-scoring,
        # position monitoring (early exit), or the shared traded_this_market
        # dedup set. Each beta model gets its own per-cycle dedup.
        # Orders are ALWAYS treated as dry-run. If a beta strategy raises,
        # we log and continue — one bad beta must not halt the live loop.
        for beta_strategy in self.beta_strategies:
            model_id = getattr(beta_strategy, "beta_model_id", None) or beta_strategy.name
            traded = self.beta_traded_by_model.setdefault(model_id, set())
            for market in markets:
                ticker = market["ticker"]
                if ticker in traded:
                    continue

                # Time remaining check — mirror live pass
                close_time = market.get("close_time")
                mins_left = None
                if close_time:
                    try:
                        ct = datetime.datetime.fromisoformat(
                            close_time.replace("Z", "+00:00")
                        )
                        mins_left = (ct - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 60
                        # Skip freshly-opened markets (>14 min left). No
                        # lower bound here — late-window beta strategies
                        # (e.g. expiry decay) need access to the last few
                        # minutes. Each strategy gates its own time window.
                        if mins_left > 14:
                            continue
                    except Exception:
                        pass

                try:
                    # Best-effort dispatch: newer beta strategies accept balance
                    # and mins_left; fall back to a bare signature if not.
                    try:
                        signal = beta_strategy.evaluate(
                            market, self.btc, mins_left=mins_left,
                            balance=self._balance, score_mult=1.0,
                        )
                    except TypeError:
                        signal = beta_strategy.evaluate(market, self.btc)
                except Exception as e:
                    print(Fore.RED + f"  [beta:{model_id}] evaluate error: {e}", flush=True)
                    continue

                if not signal:
                    continue

                # Stamp beta provenance on the signal before logging.
                signal["is_beta"]       = True
                signal["beta_model_id"] = model_id

                self._print_signal(signal, dry=True)
                order_result = {"dry_run": True, "reason": "beta"}
                self._log_signal(signal, order_result)
                traded.add(ticker)
                # Track CONSENSUS_V1_WITH_EXIT entries for later edge-gone
                # exit simulation. No-op for other beta model_ids.
                if signal.get("beta_model_id") == ConsensusExitTracker.BETA_MODEL_ID:
                    try:
                        self.consensus_exit_tracker.track(signal)
                    except Exception as e:
                        print(Fore.YELLOW + f"  [consensus-exit-beta] "
                              f"track error: {e}", flush=True)
                # No break — beta models evaluate all markets each cycle,
                # subject only to per-model cooldown and per-cycle dedup.

        # Status line
        ts         = now_et().strftime("%H:%M:%S")
        change     = self.btc.pct_change(60)
        change_str = f"{change*100:+.3f}%" if change else "â"
        ws_tag     = "WS" if self.btc.is_live else "REST"
        print(
            f"  [{ts}]  BTC ${btc_price:,.0f}  1m:{change_str}  "
            f"Markets:{len(markets)}  "
            f"Prev:{self.consensus.last_result or '?'}"
            f"  [{ws_tag}]",
            end="\r"
        )

    def run(self):
        print(Fore.GREEN + Style.BRIGHT + "\nð¤ KALSHI MULTI-STRATEGY BOT v2.0 'EDGE ENGINE'")
        mode = "DRY RUN" if self.dry else "LIVE TRADING"
        print(f"   Mode: {Fore.YELLOW}{mode}{Style.RESET_ALL}")
        lag_state = "ENABLED" if LAG_ENABLED else "DISABLED"
        con_state = "ENABLED" if CONSENSUS_ENABLED else "DISABLED"
        snp_state = "ENABLED" if SNIPER_ENABLED else "DISABLED"
        exp_state = "ENABLED" if EXPIRY_DECAY_ENABLED else "DISABLED"
        print(f"   Max stake:       ${self.max_stake}/trade (all strategies)")
        print(f"   LAG [{lag_state}]  CONSENSUS [{con_state}]  SNIPER [{snp_state}]  EXPIRY_DECAY [{exp_state}]")
        if self.beta_strategies:
            beta_ids = ", ".join(
                getattr(bs, "beta_model_id", None) or bs.name
                for bs in self.beta_strategies
            )
            print(f"   BETA models ({len(self.beta_strategies)}): {beta_ids}")
        else:
            print(f"   BETA models: (none registered)")
        print(f"   SNIPER overrides: lottery=${self.sniper.lottery_stake}  "
              f"conviction=${self.sniper.conviction_stake}")
        print(f"   Sniper 5m momentum floor: {SNIPER_5M_MIN_MOMENTUM*100:.3f}%  "
              f"cooldown: {SNIPER_COOLDOWN}s  min time: {SNIPER_MIN_MINS_LEFT:.0f}m")
        print(f"   Momentum divergence max: {MOMENTUM_DIVERGENCE_MAX*100:.3f}%")
        print(f"   Momentum dead zone: {MOMENTUM_DEAD_ZONE*100:.3f}%")
        print(f"   Strong-momentum threshold: {STRONG_MOMENTUM_THRESHOLD*100:.3f}% "
              f"(stake x{CONSENSUS_STRONG_STAKE_MULT}, cap +{CONSENSUS_STRONG_PRICE_BONUS:.2f})")
        print(f"   [v2.0] Kelly sizing:     {'ON ('+str(KELLY_FRACTION)+'x)' if KELLY_ENABLED else 'OFF'}")
        print(f"   [v2.0] Auto-scoring:     {'ON' if AUTOSCORE_ENABLED else 'OFF'}")
        print(f"   [v2.0] Disagreement gate: {'ON' if DISAGREEMENT_GATING else 'OFF'}")
        print(f"   [v2.0] Early exit:       {'ON' if EARLY_EXIT_ENABLED else 'OFF'}")
        print(f"   Daily NET loss limit:   ${self.risk.daily_limit}")
        print(f"   Daily GROSS loss limit: ${self.risk.gross_daily_limit}")
        print(f"   Trade log: {LOG_FILE}")
        print(f"   Series: {BTC_TICKER}")
        print(Fore.YELLOW + "   Press Ctrl+C to stop\n")

        # Start real-time WebSocket price feed
        self.btc.start()

        _last_resolve = 0

        while True:
            try:
                self.run_once()
                # Prune traded_this_market to tickers that are STILL ACTIVE.
                #
                # Previously: `if int(time.time()) % 900 < POLL_INTERVAL: clear()`,
                # which wiped the set for the first few seconds of every :00/:15/:30/:45
                # boundary. But Kalshi 15-min markets settle on those same boundaries
                # and the next window opens — meaning a market purchased at 8:29:55
                # (still ~15m of remaining life) got its dedup entry wiped at 8:30:00
                # and was re-traded at 8:30:05. That race produced actual duplicate
                # fills on Kalshi (same target, same size, same cost, paired in history).
                #
                # Market-list pruning is self-correcting: Kalshi stops returning a
                # ticker once its window settles, so intersecting against the most
                # recent active market set drops only truly-gone markets and never
                # exposes a still-tradeable ticker to a re-entry.
                active_tickers = {m.get("ticker") for m in (self._last_markets or [])}
                if active_tickers:
                    self.traded_this_market.intersection_update(active_tickers)
                # Resolve settled trades + back-fill fill timestamps every 60s
                if time.time() - _last_resolve >= 60:
                    try:
                        n = resolve_trades(self.client)
                        if n:
                            print(f"[bot] Resolved {n} trade(s).", flush=True)
                    except Exception as re:
                        print(f"[bot] resolve error: {re}", flush=True)
                    try:
                        f = update_fill_times(self.client)
                        if f:
                            print(f"[bot] Back-filled fill times on {f} trade(s).", flush=True)
                    except Exception as fe:
                        print(f"[bot] fill-sync error: {fe}", flush=True)
                    _last_resolve = time.time()
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                print(Fore.YELLOW + "\n\nBot stopped.")
                self.btc.stop()
                resolve_trades(self.client)
                print_stats()
                break
            except Exception as e:
                print(Fore.RED + f"\n  Unexpected error: {e}")
                time.sleep(POLL_INTERVAL * 2)


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# ENTRY POINT
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def main():
    parser = argparse.ArgumentParser(description="Kalshi BTC 15-min Multi-Strategy Bot v2.0")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Simulate trades without placing real orders")
    parser.add_argument("--stake",       type=float, default=None,
                        help="Max dollar stake per trade, all strategies (overrides .env MAX_STAKE_PER_TRADE)")
    parser.add_argument("--snp-lottery-stake",    type=float, default=None,
                        help="Override Sniper lottery stake (defaults to --stake)")
    parser.add_argument("--snp-conviction-stake", type=float, default=None,
                        help="Override Sniper conviction stake (defaults to --stake)")
    parser.add_argument("--daily-limit", type=float, default=None,
                        help="Max daily loss in dollars before halting")
    parser.add_argument("--stats",       action="store_true",
                        help="Print performance stats and exit")
    parser.add_argument("--resolve",     action="store_true",
                        help="Resolve settled trades and exit")
    args = parser.parse_args()

    # Load credentials
    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    key_path   = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi.key")
    dry_run    = args.dry_run or os.getenv("DRY_RUN", "false").lower() == "true"

    # Stakes â CLI > .env > interactive prompt
    def get_max_stake(arg_val):
        if arg_val is not None:
            return arg_val
        return MAX_STAKE_PER_TRADE  # already resolved from env chain in config

    # Stats/resolve don't need live credentials
    if args.stats:
        print_stats()
        return
    if args.resolve:
        if not api_key_id or (not os.getenv("KALSHI_PRIVATE_KEY_BASE64") and not os.path.exists(key_path)):
            print(Fore.RED + "API credentials required for resolve.")
            return
        pkey   = load_private_key(key_path)
        client = KalshiClient(api_key_id, pkey, dry_run=True)
        n = resolve_trades(client)
        print(Fore.GREEN + f"Resolved {n} trade(s).")
        print_stats()
        return

    # Validate credentials
    if not api_key_id:
        print(Fore.RED + "KALSHI_API_KEY_ID not set. Add it to your .env file.")
        return
    if not os.getenv("KALSHI_PRIVATE_KEY_BASE64") and not os.path.exists(key_path):
        print(Fore.RED + f"Private key not found. Set KALSHI_PRIVATE_KEY_BASE64 or place key at {key_path}")
        return

    print(Fore.CYAN + "\n  KALSHI v2.0 EDGE ENGINE \u2014 CONFIGURATION\n")

    max_stake   = get_max_stake(args.stake)
    # Sniper stakes fall back to CONSENSUS_STAKE when not explicitly set
    snp_lot     = args.snp_lottery_stake    or float(os.getenv("SNIPER_LOTTERY_STAKE")    or max_stake)
    snp_conv    = args.snp_conviction_stake or float(os.getenv("SNIPER_CONVICTION_STAKE") or max_stake)
    daily_limit = args.daily_limit or float(os.getenv("DAILY_LOSS_LIMIT", 0) or
                  input("  Daily loss limit in dollars (bot halts if exceeded): $").strip())
    # Gross-loss limit defaults to 1.5× the net limit if not explicitly set.
    # This is the value that catches "drip-bleed" losing strategies whose
    # net is propped up by occasional large winners.
    gross_daily_limit = float(os.getenv("DAILY_GROSS_LOSS_LIMIT", 0)) or (1.5 * daily_limit)

    if not dry_run:
        print(f"\n{Fore.RED}â²  LIVE TRADING MODE{Style.RESET_ALL}")
        print(f"   Max stake:       ${max_stake}/trade (all strategies)")
        print(f"   SNIPER overrides: lottery=${snp_lot}  conviction=${snp_conv}")
        print(f"   Daily loss limit: ${daily_limit}")
        confirm = input("\n  Type 'YES' to confirm live trading: ").strip()
        if confirm != "YES":
            print("Aborted.")
            return

    pkey   = load_private_key(key_path)
    client = KalshiClient(api_key_id, pkey, dry_run=dry_run)

    # Verify connection
    try:
        bal = client.get_balance()
        print(Fore.GREEN + f"\n  â Connected to Kalshi  |  Balance: ${bal:.2f}")
    except Exception as e:
        print(Fore.RED + f"  Connection failed: {e}")
        return

    bot = KalshiBot(
        client                   = client,
        max_stake                = max_stake,
        daily_loss_limit         = daily_limit,
        gross_daily_loss_limit   = gross_daily_limit,
        dry_run                  = dry_run,
        sniper_lottery_stake     = snp_lot,
        sniper_conviction_stake  = snp_conv,
    )
    start_keep_alive()
    bot.run()


if __name__ == "__main__":
    main()
