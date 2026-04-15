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
       for BTC reversals >0.3%. Sells at small loss vs riding to $0 settlement.

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
CONSENSUS_ENABLED = os.getenv("CONSENSUS_ENABLED", "true").lower() == "true"   # re-enabled v2.0 for dry-run validation
SNIPER_ENABLED    = os.getenv("SNIPER_ENABLED",    "true").lower()  == "true"

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
SNIPER_5M_MIN_MOMENTUM = float(os.getenv("SNIPER_5M_MIN_MOMENTUM", "0.0003"))  # 0.03%
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

# Known edge parameters (from analyze_edge.py ground-truth data).
# These are used as priors until enough live trades accumulate for
# rolling stats to take over via StrategyScorer.
KELLY_PRIORS = {
    "SNIPER_LOTTERY":    {"win_rate": 0.105, "avg_price": 0.055},  # +4.8pp edge
    "SNIPER_CONVICTION": {"win_rate": 0.615, "avg_price": 0.525},  # +9.1pp edge
    "CONSENSUS":         {"win_rate": 0.55,  "avg_price": 0.40},   # conservative prior
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
EARLY_EXIT_REVERSAL_PCT  = float(os.getenv("EARLY_EXIT_REVERSAL_PCT", "0.003"))  # 0.3% BTC reversal
EARLY_EXIT_MIN_HOLD_S    = int(os.getenv("EARLY_EXIT_MIN_HOLD_S", "120"))        # 2 min minimum hold
EARLY_EXIT_MAX_LOSS_PCT  = float(os.getenv("EARLY_EXIT_MAX_LOSS_PCT", "0.10"))   # exit if can recover 90%+

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
            "strategy":  recovered_strategy,
            "ticker":    ticker,
            "side":      side,
            "price":     price_cents,
            "count":     int(total_count) if total_count == int(total_count) else total_count,
            "dollars":   dollars,
            "reason":    f"Rebuilt from Kalshi API (order {order_id[:8]}...)",
            "timestamp": ts,
            "dry_run":   False,
            "order":     {"order_id": order_id, "recovered": True},
            "outcome":   outcome,
            "result":    result,
            "pnl":       pnl,
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
                               exit_reason=""):
    """Finalize the most recent open entry trade matching (ticker,side,count)
    with realized P&L from an early-exit sell.

    This is what we do **instead of** save_trade()-ing a new strategy="EARLY_EXIT"
    row: the dashboard's per-strategy stats were counting the exit separately
    from the entry, so early-exit wins appeared as "None final position" and
    never got credited to the SNIPER/CONSENSUS bucket that originated the
    position. Here we update the originating record in-place.

    Semantics:
      - Finds the trade by (ticker, side, count, result is None).
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
    liquidity to sell back. If BTC reverses > 0.3%, selling at small loss
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
    def __init__(self, stake_dollars):
        self.stake    = stake_dollars
        self.kelly_mode = "LAG"      # [v2.0] Kelly prior key
        self.name     = "LAG"
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
    def __init__(self, stake_dollars):
        self.stake              = stake_dollars
        self.name               = "CONSENSUS"
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
            effective_stake = ks if ks > 0 else base_stake
            if ks > 0:
                kelly_stake = round(ks, 2)
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
      1. 5-min BTC momentum must exceed SNIPER_5M_MIN_MOMENTUM (0.03%).
         Aligned trades:  n=48, WR=35.4%, edge=+2.5pp, +22% ROI.
         Counter trades:  n=88, WR=15.9%, edge=-15.9pp, -27% ROI.
      2. 60-second momentum must not contradict 5-min direction.
         Counter to 60s momentum was 0% WR across 12 trades.
      3. Market must have >= SNIPER_MIN_MINS_LEFT minutes remaining.
         Too little time = no room for momentum continuation.
    """

    def __init__(self, lottery_stake=None, conviction_stake=None):
        self.lottery_stake    = lottery_stake or SNIPER_LOTTERY_STAKE
        self.conviction_stake = conviction_stake or SNIPER_CONVICTION_STAKE
        self.name             = "SNIPER"
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
        kelly_stake = None
        if KELLY_ENABLED and balance:
            kelly_key = f"SNIPER_{mode}"
            prior = KELLY_PRIORS.get(kelly_key, {})
            if prior:
                ks = kelly_size(balance, prior["win_rate"], price_dollars,
                               fallback_stake=stake)
                effective_stake = ks if ks > 0 else stake
                if ks > 0:
                    kelly_stake = round(ks, 2)
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


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
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
        today_settled = [
            t for t in trades
            if t.get("result") and trade_date_et(t.get("timestamp")) == today
        ]
        today_open = [
            t for t in trades
            if not t.get("result") and trade_date_et(t.get("timestamp")) == today
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

        # Open trade count
        open_trades = [t for t in trades if not t.get("result")]
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
        self.risk      = RiskManager(daily_loss_limit,
                                     gross_daily_loss_limit=gross_daily_loss_limit)
        self.dry       = dry_run
        # [v2.0] New components
        self.scorer    = StrategyScorer()
        self.monitor   = PositionMonitor()
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
                if not t.get("result") and t.get("ticker"):
                    self.traded_this_market.add(t["ticker"])
            if self.traded_this_market:
                print(f"  [bot] Pre-seeded {len(self.traded_this_market)} market(s) "
                      f"from open positions: {self.traded_this_market}", flush=True)
        except Exception:
            pass
        self._last_markets = []          # exposed for dashboard

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

        record = {
            **signal,
            "timestamp":       now_et().isoformat(),  # placement time (bot-side)
            "dry_run":         self.dry,
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
        tag   = "[DRY]" if dry else "[LIVE]"
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

        # 3. Update consensus previous result
        try:
            self.consensus.update_previous(markets, self.client)
        except Exception:
            pass

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
                    if mins_left < 3 or mins_left > 14:
                        continue
                except Exception:
                    pass

            active_strategies = []
            if LAG_ENABLED:
                active_strategies.append(self.lag)
            if CONSENSUS_ENABLED:
                active_strategies.append(self.consensus)
            if SNIPER_ENABLED:
                active_strategies.append(self.sniper)

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
        print(f"   Max stake:       ${self.max_stake}/trade (all strategies)")
        print(f"   LAG [{lag_state}]  CONSENSUS [{con_state}]  SNIPER [{snp_state}]")
        print(f"   SNIPER overrides: lottery=${self.sniper.lottery_stake}  "
              f"conviction=${self.sniper.conviction_stake}")
        print(f"   Sniper 5m momentum floor: {SNIPER_5M_MIN_MOMENTUM*100:.3f}%  "
              f"cooldown: {SNIPER_COOLDOWN}s  min time: {SNIPER_MIN_MINS_LEFT:.0f}m")
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
