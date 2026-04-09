"""
Kalshi BTC 15-Minute Dual Strategy Bot
=======================================
Runs two strategies in parallel on Kalshi's KXBTC15M markets:

  STRATEGY 1 â LAG BOT
    Detects when Kalshi contract prices haven't caught up to live BTC
    price moves on Coinbase. When BTC moves sharply but Kalshi hasn't
    repriced yet, trades into the mispricing.

  STRATEGY 2 â CONSENSUS BOT v1.1
    Combines two signals:
      - MOMENTUM: BTC price direction over last 60 seconds
      - PREVIOUS: Result of the last settled 15-min market
    Only trades when both signals agree AND price is <= threshold.
    Historically ~73% win rate in backtests.

    v1.1 improvements:
      1. Momentum dead zone â ignores noise below 0.03% BTC move
      2. Previous-result staleness â signal expires after 30 min
      3. Dynamic price cap â scales with momentum strength (0.45â0.55)
      4. Time-aware entry â tightens threshold as market nears close
      5. Trade cooldown â 300s minimum between consensus trades
      6. API throttle â polls settled markets every 60s, not every 5s

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
LOG_FILE       = "bot_trades.json"

# Strategy thresholds (tune these after paper trading)
LAG_THRESHOLD_PCT   = 0.003  # BTC must move >0.3% in 90s for lag signal
LAG_MAX_REPRICE_AGE = 90     # seconds since last Kalshi price change to consider stale
CONSENSUS_MAX_PRICE = 0.55   # only trade consensus when price <= 55Â¢
MOMENTUM_WINDOW     = 60     # seconds for BTC momentum calculation
MIN_BOOK_SUM        = 0.97   # yes+no must sum >= 0.97 (liquid market check)

# Consensus v1.1 tuning
CONSENSUS_BASE_PRICE = 0.45  # base price cap before momentum scaling
MOMENTUM_DEAD_ZONE  = 0.0003 # ignore BTC moves < 0.03% (noise filter)
PREV_RESULT_MAX_AGE = 1800   # previous-result signal expires after 30 min (seconds)
CONSENSUS_COOLDOWN  = 300    # min seconds between consensus trades
PREV_CHECK_INTERVAL = 60     # only poll settled markets every 60s (not every cycle)


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
        order = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "count": count,
            "type": "limit",
            f"{'yes' if side == 'yes' else 'no'}_price": price_cents,
            "client_order_id": str(uuid.uuid4()),
        }
        if self.dry:
            return {"dry_run": True, "order": order}
        r = self.post("/portfolio/orders", order)
        if r.status_code == 201:
            return r.json()
        print(f"  Order failed: HTTP {r.status_code} â {r.text}", flush=True)
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
    existing_tickers_ts = {
        (t.get("ticker", ""), t.get("timestamp", "")[:16])
        for t in existing
    }

    new_trades = []
    for order_id, fills in orders.items():
        # Use first fill for metadata
        f0 = fills[0]
        ticker = f0.get("ticker", "")
        side = f0.get("side", "")  # "yes" or "no"
        ts = f0.get("created_time") or f0.get("ts", "")

        # Skip if we already have this trade logged
        if (ticker, ts[:16]) in existing_tickers_ts:
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

        trade = {
            "strategy":  "RECOVERED",
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


def resolve_trades(client):
    """Check settled markets and update trade outcomes."""
    trades = load_trades()
    changed = 0
    for t in trades:
        if t.get("result"):
            continue
        mkt = client.get_market(t["ticker"])
        if not mkt:
            continue
        result = mkt.get("result")
        if not result:
            continue
        t["outcome"] = result
        won = (t["side"] == result)
        t["result"] = "WIN" if won else "LOSS"
        price = t["price"] / 100
        size  = t["dollars"]
        if won:
            t["pnl"] = round(size / price * (1 - price), 2)
        else:
            t["pnl"] = -round(size, 2)
        changed += 1
    if changed:
        with open(LOG_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    return changed


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
    settled = [t for t in trades if t.get("result")]
    if not settled:
        print(Fore.YELLOW + "No settled trades yet.")
        return

    for strategy in ["LAG", "CONSENSUS", "ALL"]:
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
        self.name     = "LAG"
        self.last_kalshi_prices = {}  # ticker â (yes, no, timestamp)

    def evaluate(self, market, btc_feed):
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
        count         = max(1, int(self.stake / price_dollars))

        return {
            "strategy": self.name,
            "ticker":   ticker,
            "side":     side,
            "price":    price_cents,
            "count":    count,
            "dollars":  round(count * price_dollars, 2),
            "reason":   f"BTC {btc_change*100:+.2f}% in {LAG_MAX_REPRICE_AGE}s, Kalshi stale {staleness:.0f}s",
        }


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

    def evaluate(self, market, btc_feed, mins_left=None):
        """
        Evaluate consensus signal for a single market.

        Args:
            market:    Kalshi market dict
            btc_feed:  BTCPriceFeed instance
            mins_left: minutes remaining in this market (passed from main loop)

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

        # ── [v1.1] Dynamic price cap ──────────────────────────
        # Base threshold of 0.45, scales up to CONSENSUS_MAX_PRICE with
        # stronger momentum.  abs(btc_change)*100 maps ~0.03%â0.13%+
        # into a 0â0.10 bonus on top of CONSENSUS_BASE_PRICE.
        momentum_bonus = min(abs(btc_change) * 100, CONSENSUS_MAX_PRICE - CONSENSUS_BASE_PRICE)
        dynamic_max    = CONSENSUS_BASE_PRICE + momentum_bonus

        # [v1.1] Time-aware: tighten cap when < 8 min remain
        if mins_left is not None and mins_left < 8:
            time_penalty = 0.05 * max(0, 8 - mins_left) / 8  # up to 5Â¢ tighter
            dynamic_max -= time_penalty

        # Clamp to hard ceiling
        dynamic_max = min(dynamic_max, CONSENSUS_MAX_PRICE)

        if price_dollars > dynamic_max:
            return None

        price_cents = int(price_dollars * 100)
        count       = max(1, int(self.stake / price_dollars))

        # [v1.1] Record trade time for cooldown
        self.last_trade_time = now

        return {
            "strategy": self.name,
            "ticker":   ticker,
            "side":     side,
            "price":    price_cents,
            "count":    count,
            "dollars":  round(count * price_dollars, 2),
            "reason":   (f"Momentum {btc_change*100:+.3f}% + Previous={previous_side} "
                         f"| cap={dynamic_max:.2f} mins_left={mins_left or '?'}"),
        }


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# RISK MANAGEMENT
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

class RiskManager:
    """
    Hard stops to prevent runaway losses.
    Tracks daily and rolling-window P&L from the log file.
    """
    def __init__(self, daily_loss_limit, max_open_trades=4):
        self.daily_limit  = daily_loss_limit
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

        # Daily loss check — skipped for the rest of today if the operator
        # manually reset the halt via the dashboard (Reset Now button).
        if self._override_date != today:
            today_settled = [
                t for t in trades
                if t.get("result") and trade_date_et(t.get("timestamp")) == today
            ]
            daily_pnl = sum(t.get("pnl", 0) for t in today_settled if t.get("pnl"))
            if daily_pnl <= -abs(self.daily_limit):
                self._halted      = True
                self._halt_reason = f"Daily loss limit hit (${daily_pnl:.2f})"
                self._halt_date   = today
                return False, self._halt_reason

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
    def __init__(self, client, lag_stake, consensus_stake,
                 daily_loss_limit, dry_run):
        self.client    = client
        self.btc       = BTCPriceFeed(window=500)
        self.lag       = LagStrategy(lag_stake)
        self.consensus = ConsensusStrategy(consensus_stake)
        self.risk      = RiskManager(daily_loss_limit)
        self.dry       = dry_run
        self.traded_this_market = set()  # prevent double-trading same market
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

        # 5. Evaluate strategies on each market
        for market in markets:
            ticker = market["ticker"]

            # Skip already traded this market cycle
            if ticker in self.traded_this_market:
                continue

            # Time remaining check â skip if < 3 min left (too close to settlement)
            close_time = market.get("close_time")
            mins_left = None
            if close_time:
                try:
                    ct = datetime.datetime.fromisoformat(
                        close_time.replace("Z", "+00:00")
                    )
                    mins_left = (ct - datetime.datetime.now(datetime.timezone.utc)).total_seconds() / 60
                    if mins_left < 3 or mins_left > 14:
                        continue  # too late or too early
                except Exception:
                    pass

            for strategy in [self.lag, self.consensus]:
                try:
                    # [v1.1] Pass mins_left to consensus for time-aware entry
                    if isinstance(strategy, ConsensusStrategy):
                        signal = strategy.evaluate(market, self.btc, mins_left=mins_left)
                    else:
                        signal = strategy.evaluate(market, self.btc)
                except Exception as e:
                    print(Fore.RED + f"  Strategy error ({strategy.name}): {e}")
                    continue

                if not signal:
                    continue

                self._print_signal(signal, self.dry)

                # Place order
                if self.dry:
                    order_result = {"dry_run": True}
                else:
                    try:
                        resp = self.client.place_order(
                            ticker      = signal["ticker"],
                            side        = signal["side"],
                            price_cents = signal["price"],
                            count       = signal["count"],
                            strategy_tag= signal["strategy"],
                        )
                        order_result = resp
                    except Exception as e:
                        order_result = {"error": str(e)}
                        print(Fore.RED + f"  Order error: {e}")

                # Only log the trade if the order was actually placed
                if "error" in order_result and not order_result.get("dry_run"):
                    print(Fore.RED + f"  Order rejected â not logging as open trade.")
                    continue

                self._log_signal(signal, order_result)
                self.traded_this_market.add(ticker)
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
        print(Fore.GREEN + Style.BRIGHT + "\nð¤ KALSHI DUAL STRATEGY BOT STARTED")
        mode = "DRY RUN" if self.dry else "LIVE TRADING"
        print(f"   Mode: {Fore.YELLOW}{mode}{Style.RESET_ALL}")
        print(f"   LAG stake:       ${self.lag.stake}/trade")
        print(f"   CONSENSUS stake: ${self.consensus.stake}/trade")
        print(f"   Daily loss limit: ${self.risk.daily_limit}")
        print(f"   Series: {BTC_TICKER}")
        print(Fore.YELLOW + "   Press Ctrl+C to stop\n")

        # Start real-time WebSocket price feed
        self.btc.start()

        _last_resolve = 0

        while True:
            try:
                self.run_once()
                # Clear traded set every 15 min
                if int(time.time()) % 900 < POLL_INTERVAL:
                    self.traded_this_market.clear()
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
    parser = argparse.ArgumentParser(description="Kalshi BTC 15-min Dual Strategy Bot")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Simulate trades without placing real orders")
    parser.add_argument("--lag-stake",   type=float, default=None,
                        help="Dollar stake per LAG trade (overrides .env)")
    parser.add_argument("--con-stake",   type=float, default=None,
                        help="Dollar stake per CONSENSUS trade (overrides .env)")
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
    def get_stake(arg_val, env_key, label):
        if arg_val is not None:
            return arg_val
        env_val = os.getenv(env_key)
        if env_val:
            return float(env_val)
        val = input(f"  Enter {label} stake per trade in dollars (e.g. 25): $").strip()
        return float(val)

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

    print(Fore.CYAN + "\nâ  KALSHI DUAL STRATEGY BOT â CONFIGURATION\n")

    lag_stake   = get_stake(args.lag_stake,   "LAG_STAKE",       "LAG strategy")
    con_stake   = get_stake(args.con_stake,   "CONSENSUS_STAKE", "CONSENSUS strategy")
    daily_limit = args.daily_limit or float(os.getenv("DAILY_LOSS_LIMIT", 0) or
                  input("  Daily loss limit in dollars (bot halts if exceeded): $").strip())

    if not dry_run:
        print(f"\n{Fore.RED}â²  LIVE TRADING MODE{Style.RESET_ALL}")
        print(f"   LAG stake:       ${lag_stake}/trade")
        print(f"   CONSENSUS stake: ${con_stake}/trade")
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
        client          = client,
        lag_stake       = lag_stake,
        consensus_stake = con_stake,
        daily_loss_limit= daily_limit,
        dry_run         = dry_run,
    )
    start_keep_alive()
    bot.run()


if __name__ == "__main__":
    main()
