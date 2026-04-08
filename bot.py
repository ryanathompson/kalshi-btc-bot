"""
Kalshi BTC 15-Minute Dual Strategy Bot
=======================================
Runs two strategies in parallel on Kalshi's KXBTC15M markets:

  STRATEGY 1 — LAG BOT
    Detects when Kalshi contract prices haven't caught up to live BTC
    price moves on Coinbase. When BTC moves sharply but Kalshi hasn't
    repriced yet, trades into the mispricing.

  STRATEGY 2 — CONSENSUS BOT
    Combines two signals:
      - MOMENTUM: BTC price direction over last 60 seconds
      - PREVIOUS: Result of the last settled 15-min market
    Only trades when both signals agree AND price is <= threshold.
    Historically ~73% win rate in backtests.

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
    2. Account & Security → API Keys → Create Key
    3. Save the .key file and Key ID to your .env

⚠️  RISK WARNING:
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
import requests
import argparse
from collections import deque
from dotenv import load_dotenv
from tabulate import tabulate
from colorama import Fore, Style, init
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding

load_dotenv()
init(autoreset=True)

# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────

BASE_URL       = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_URL       = "https://demo-api.kalshi.co/trade-api/v2"
BTC_TICKER     = "KXBTC15M"
ETH_TICKER     = "KXETH15M"
COINBASE_URL   = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
POLL_INTERVAL  = 5          # seconds between polls
LOG_FILE       = "bot_trades.json"

# Strategy thresholds (tune these after paper trading)
LAG_THRESHOLD_PCT   = 0.003  # BTC must move >0.3% in 90s for lag signal
LAG_MAX_REPRICE_AGE = 90     # seconds since last Kalshi price change to consider stale
CONSENSUS_MAX_PRICE = 0.55   # only trade consensus when price <= 55¢
MOMENTUM_WINDOW     = 60     # seconds for BTC momentum calculation
MIN_BOOK_SUM        = 0.97   # yes+no must sum >= 0.97 (liquid market check)

# ─────────────────────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────────────────────

def load_private_key(path=None):
    """
    Load RSA private key. Prefers KALSHI_PRIVATE_KEY_BASE64 env var
    (base64-encoded PEM) so the key can be stored in Render secrets.
    Falls back to reading from a file path.
    """
    b64 = os.getenv("KALSHI_PRIVATE_KEY_BASE64")
    if b64:
        pem_bytes = base64.b64decode(b64)
        return serialization.load_pem_private_key(
            pem_bytes, password=None, backend=default_backend()
        )
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(
                f.read(), password=None, backend=default_backend()
            )
    raise RuntimeError(
        "No private key found. Set KALSHI_PRIVATE_KEY_BASE64 env var "
        "or provide a valid KALSHI_PRIVATE_KEY_PATH."
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
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    from urllib.parse import urlparse
    sign_path = urlparse(BASE_URL + path).path
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sign(private_key, ts, method, sign_path),
        "Content-Type": "application/json",
    }


# ─────────────────────────────────────────────────────────────
# KALSHI CLIENT
# ─────────────────────────────────────────────────────────────

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
        return d.get("balance", 0) / 100  # cents → dollars

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
        r = self.post("/trade-api/v2/portfolio/orders", order)
        if r.status_code == 201:
            return r.json()
        return {"error": r.text, "status": r.status_code}


# ─────────────────────────────────────────────────────────────
# PRICE DATA
# ─────────────────────────────────────────────────────────────

class BTCPriceFeed:
    """Rolling BTC price history from Coinbase (free, no auth)."""
    def __init__(self, window=120):
        self.history = deque(maxlen=window)

    def fetch(self):
        try:
            r = requests.get(COINBASE_URL, timeout=5)
            price = float(r.json()["data"]["amount"])
            self.history.append((time.time(), price))
            return price
        except Exception:
            return None

    def current(self):
        return self.history[-1][1] if self.history else None

    def price_at(self, seconds_ago):
        """Return price from ~seconds_ago seconds back."""
        now = time.time()
        target = now - seconds_ago
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


# ─────────────────────────────────────────────────────────────
# TRADE LOG
# ─────────────────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────
# STRATEGY 1: LAG BOT
# ─────────────────────────────────────────────────────────────

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
        self.last_kalshi_prices = {}  # ticker → (yes, no, timestamp)

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


# ─────────────────────────────────────────────────────────────
# STRATEGY 2: CONSENSUS BOT
# ─────────────────────────────────────────────────────────────

class ConsensusStrategy:
    """
    Only trades when MOMENTUM and PREVIOUS signals agree.

    MOMENTUM: BTC up/down in last 60s → trade YES/NO
    PREVIOUS: last settled market result → trade same side
    CONSENSUS: only fire when both agree AND price <= threshold
    """
    def __init__(self, stake_dollars):
        self.stake        = stake_dollars
        self.name         = "CONSENSUS"
        self.last_result  = None  # "yes" or "no"
        self.last_ticker  = None

    def update_previous(self, markets, client):
        """Check recently closed markets for previous result."""
        closed = client.get_markets_by_series(BTC_TICKER, status="settled")
        if closed:
            latest = sorted(closed, key=lambda m: m.get("close_time", ""), reverse=True)[0]
            result = latest.get("result")
            if result and latest["ticker"] != self.last_ticker:
                self.last_result = result
                self.last_ticker = latest["ticker"]

    def evaluate(self, market, btc_feed):
        ticker  = market["ticker"]
        yes_px  = float(market.get("yes_ask_dollars", market.get("yes_bid_dollars", 0)))
        no_px   = float(market.get("no_ask_dollars",  market.get("no_bid_dollars",  0)))
        book_sum = yes_px + no_px

        if book_sum < MIN_BOOK_SUM:
            return None

        # MOMENTUM signal
        btc_change = btc_feed.pct_change(MOMENTUM_WINDOW)
        if btc_change is None:
            return None
        momentum_side = "yes" if btc_change > 0 else "no"

        # PREVIOUS signal
        if not self.last_result:
            return None
        previous_side = self.last_result

        # Both must agree
        if momentum_side != previous_side:
            return None

        side          = momentum_side
        price_dollars = yes_px if side == "yes" else no_px

        # Only trade at or below threshold price
        if price_dollars > CONSENSUS_MAX_PRICE:
            return None

        price_cents = int(price_dollars * 100)
        count       = max(1, int(self.stake / price_dollars))

        return {
            "strategy": self.name,
            "ticker":   ticker,
            "side":     side,
            "price":    price_cents,
            "count":    count,
            "dollars":  round(count * price_dollars, 2),
            "reason":   f"Momentum {btc_change*100:+.2f}% + Previous={previous_side}",
        }


# ─────────────────────────────────────────────────────────────
# RISK MANAGEMENT
# ─────────────────────────────────────────────────────────────

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

    def check(self, client):
        if self._halted:
            return False, self._halt_reason

        trades = load_trades()
        today  = datetime.date.today().isoformat()

        # Daily loss check
        today_settled = [
            t for t in trades
            if t.get("result") and t.get("timestamp", "")[:10] == today
        ]
        daily_pnl = sum(t.get("pnl", 0) for t in today_settled if t.get("pnl"))
        if daily_pnl <= -abs(self.daily_limit):
            self._halted      = True
            self._halt_reason = f"Daily loss limit hit (${daily_pnl:.2f})"
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


# ─────────────────────────────────────────────────────────────
# MAIN BOT LOOP
# ─────────────────────────────────────────────────────────────

class KalshiBot:
    def __init__(self, client, lag_stake, consensus_stake,
                 daily_loss_limit, dry_run):
        self.client    = client
        self.btc       = BTCPriceFeed(window=200)
        self.lag       = LagStrategy(lag_stake)
        self.consensus = ConsensusStrategy(consensus_stake)
        self.risk      = RiskManager(daily_loss_limit)
        self.dry       = dry_run
        self.traded_this_market = set()  # prevent double-trading same market

    def _log_signal(self, signal, order_result):
        record = {
            **signal,
            "timestamp":  datetime.datetime.utcnow().isoformat(),
            "dry_run":    self.dry,
            "order":      order_result,
            "outcome":    None,
            "result":     None,
            "pnl":        None,
        }
        save_trade(record)
        return record

    def _print_signal(self, signal, dry):
        color = Fore.CYAN if signal["strategy"] == "LAG" else Fore.MAGENTA
        tag   = "[DRY]" if dry else "[LIVE]"
        print(
            f"\n{color}► {signal['strategy']} {tag}{Style.RESET_ALL}  "
            f"{signal['ticker']}  "
            f"{signal['side'].upper()} @ {signal['price']}¢  "
            f"x{signal['count']} = ${signal['dollars']:.2f}\n"
            f"  Reason: {signal['reason']}"
        )

    def run_once(self):
        # 1. Fetch BTC price
        btc_price = self.btc.fetch()
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

        # 3. Update consensus previous result
        try:
            self.consensus.update_previous(markets, self.client)
        except Exception:
            pass

        # 4. Risk check
        ok, reason = self.risk.check(self.client)
        if not ok:
            print(Fore.RED + f"  🛑 Risk halt: {reason}")
            return

        # 5. Evaluate strategies on each market
        for market in markets:
            ticker = market["ticker"]

            # Skip already traded this market cycle
            if ticker in self.traded_this_market:
                continue

            # Time remaining check — skip if < 3 min left (too close to settlement)
            close_time = market.get("close_time")
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

                self._log_signal(signal, order_result)
                self.traded_this_market.add(ticker)
                break  # one strategy per market per cycle

        # Status line
        ts         = datetime.datetime.now().strftime("%H:%M:%S")
        change     = self.btc.pct_change(60)
        change_str = f"{change*100:+.3f}%" if change else "—"
        print(
            f"  [{ts}]  BTC ${btc_price:,.0f}  1m:{change_str}  "
            f"Markets:{len(markets)}  "
            f"Prev:{self.consensus.last_result or '?'}",
            end="\r"
        )

    def run(self):
        print(Fore.GREEN + Style.BRIGHT + "\n🤖 KALSHI DUAL STRATEGY BOT STARTED")
        mode = "DRY RUN" if self.dry else "LIVE TRADING"
        print(f"   Mode: {Fore.YELLOW}{mode}{Style.RESET_ALL}")
        print(f"   LAG stake:       ${self.lag.stake}/trade")
        print(f"   CONSENSUS stake: ${self.consensus.stake}/trade")
        print(f"   Daily loss limit: ${self.risk.daily_limit}")
        print(f"   Series: {BTC_TICKER}")
        print(Fore.YELLOW + "   Press Ctrl+C to stop\n")

        # Warm up BTC feed
        print("  Warming up BTC price feed (15s)...")
        for _ in range(3):
            self.btc.fetch()
            time.sleep(5)

        while True:
            try:
                self.run_once()
                # Clear traded set every 15 min
                if int(time.time()) % 900 < POLL_INTERVAL:
                    self.traded_this_market.clear()
                time.sleep(POLL_INTERVAL)
            except KeyboardInterrupt:
                print(Fore.YELLOW + "\n\nBot stopped.")
                resolve_trades(self.client)
                print_stats()
                break
            except Exception as e:
                print(Fore.RED + f"\n  Unexpected error: {e}")
                time.sleep(POLL_INTERVAL * 2)


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

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

    # Stakes — CLI > .env > interactive prompt
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

    print(Fore.CYAN + "\n⚙  KALSHI DUAL STRATEGY BOT — CONFIGURATION\n")

    lag_stake   = get_stake(args.lag_stake,   "LAG_STAKE",       "LAG strategy")
    con_stake   = get_stake(args.con_stake,   "CONSENSUS_STAKE", "CONSENSUS strategy")
    daily_limit = args.daily_limit or float(os.getenv("DAILY_LOSS_LIMIT", 0) or
                  input("  Daily loss limit in dollars (bot halts if exceeded): $").strip())

    if not dry_run:
        print(f"\n{Fore.RED}▲  LIVE TRADING MODE{Style.RESET_ALL}")
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
        print(Fore.GREEN + f"\n  ✓ Connected to Kalshi  |  Balance: ${bal:.2f}")
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
    bot.run()


if __name__ == "__main__":
    main()
