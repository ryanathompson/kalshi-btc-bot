"""
Kalshi BTC Bot — Flask Dashboard
=================================
Runs the dual-strategy bot in a background thread and serves a live
dashboard at / with auto-refreshing stats, open trades, and trade history.

Deploy on Render as a Web Service (not Background Worker).
Procfile:  web: gunicorn app:app --workers 1 --threads 4 --timeout 120
"""

import os
import time
import threading
import datetime

from flask import Flask, render_template, jsonify
from dotenv import load_dotenv

from bot import (
    KalshiBot, KalshiClient, load_private_key, load_trades, POLL_INTERVAL
)

load_dotenv()

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────
# SHARED STATE  (written by bot thread, read by Flask routes)
# ─────────────────────────────────────────────────────────────

class BotState:
    def __init__(self):
        self.running      = False
        self.dry_run      = True
        self.btc_price    = None
        self.btc_change   = None   # 1-min % as decimal
        self.market_count = 0
        self.prev_result  = None   # consensus previous signal
        self.halted       = False
        self.halt_reason  = ""
        self.lag_stake    = 0.0
        self.con_stake    = 0.0
        self.daily_limit  = 0.0
        self.started_at   = None
        self.last_cycle   = None
        self.error        = None

_state = BotState()
_bot   = None   # KalshiBot instance (set once the thread starts successfully)


# ─────────────────────────────────────────────────────────────
# HELPER: compute per-strategy stats from trade log
# ─────────────────────────────────────────────────────────────

def _strat_stats(trades, name=None):
    """Return stats dict for settled trades. name=None → all strategies."""
    if name:
        subset = [t for t in trades if t.get("result") and t.get("strategy") == name]
    else:
        subset = [t for t in trades if t.get("result")]

    if not subset:
        return None

    wins    = sum(1 for t in subset if t["result"] == "WIN")
    total   = len(subset)
    wagered = sum(t.get("dollars", 0) for t in subset)
    pnl     = sum(t.get("pnl", 0) for t in subset if t.get("pnl") is not None)
    wwins   = sum(t.get("dollars", 0) for t in subset if t["result"] == "WIN")
    wwr     = wwins / wagered * 100 if wagered else 0

    return {
        "wins":    wins,
        "losses":  total - wins,
        "total":   total,
        "wr":      round(wins / total * 100, 1),
        "wwr":     round(wwr, 1),
        "wagered": round(wagered, 2),
        "pnl":     round(pnl, 2),
        "roi":     round(pnl / wagered * 100, 2) if wagered else 0,
    }


# ─────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/status")
def api_status():
    trades = load_trades()

    open_trades = [t for t in trades if not t.get("result")]
    history = sorted(
        [t for t in trades if t.get("result")],
        key=lambda t: t.get("timestamp", ""),
        reverse=True,
    )[:100]

    # Format open trades for display
    def fmt_open(t):
        ts = t.get("timestamp", "")
        try:
            dt = datetime.datetime.fromisoformat(ts)
            age_s = (datetime.datetime.utcnow() - dt).total_seconds()
            if age_s < 3600:
                age = f"{int(age_s // 60)}m ago"
            else:
                age = f"{int(age_s // 3600)}h ago"
        except Exception:
            age = ts[:16] if ts else "—"
        return {**t, "age": age}

    def fmt_hist(t):
        ts = t.get("timestamp", "")
        return {**t, "ts_short": ts[:16].replace("T", " ") if ts else "—"}

    return jsonify({
        "running":      _state.running,
        "dry_run":      _state.dry_run,
        "btc_price":    _state.btc_price,
        "btc_change":   round(_state.btc_change * 100, 3) if _state.btc_change is not None else None,
        "market_count": _state.market_count,
        "prev_result":  _state.prev_result,
        "halted":       _state.halted,
        "halt_reason":  _state.halt_reason,
        "lag_stake":    _state.lag_stake,
        "con_stake":    _state.con_stake,
        "daily_limit":  _state.daily_limit,
        "started_at":   _state.started_at,
        "last_cycle":   _state.last_cycle,
        "error":        _state.error,
        "open_trades":  [fmt_open(t) for t in open_trades],
        "history":      [fmt_hist(t) for t in history],
        "lag_stats":    _strat_stats(trades, "LAG"),
        "con_stats":    _strat_stats(trades, "CONSENSUS"),
        "all_stats":    _strat_stats(trades),
    })


# ─────────────────────────────────────────────────────────────
# BOT BACKGROUND THREAD
# ─────────────────────────────────────────────────────────────

def _bot_thread():
    global _bot
    _state.running    = True
    _state.started_at = datetime.datetime.utcnow().isoformat()

    api_key_id = os.getenv("KALSHI_API_KEY_ID")
    key_path   = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi.key")
    dry_run    = os.getenv("DRY_RUN", "true").lower() == "true"
    lag_stake  = float(os.getenv("LAG_STAKE",        "25"))
    con_stake  = float(os.getenv("CONSENSUS_STAKE",  "25"))
    daily_lim  = float(os.getenv("DAILY_LOSS_LIMIT", "100"))

    _state.dry_run     = dry_run
    _state.lag_stake   = lag_stake
    _state.con_stake   = con_stake
    _state.daily_limit = daily_lim

    try:
        pkey   = load_private_key(key_path)
        client = KalshiClient(api_key_id, pkey, dry_run=dry_run)
        _bot   = KalshiBot(client, lag_stake, con_stake, daily_lim, dry_run)

        # Warm up BTC feed (15 seconds)
        for _ in range(3):
            p = _bot.btc.fetch()
            if p:
                _state.btc_price = p
            time.sleep(5)

        while True:
            try:
                _bot.run_once()

                # Push state to shared object
                _state.btc_price    = _bot.btc.current()
                _state.btc_change   = _bot.btc.pct_change(60)
                _state.market_count = len(_bot._last_markets)
                _state.prev_result  = _bot.consensus.last_result
                _state.halted       = _bot.risk._halted
                _state.halt_reason  = _bot.risk._halt_reason
                _state.last_cycle   = datetime.datetime.utcnow().isoformat()
                _state.error        = None

            except Exception as e:
                _state.error = str(e)

            time.sleep(POLL_INTERVAL)

    except Exception as e:
        _state.error   = str(e)
        _state.running = False


# Start bot thread automatically when credentials are present
if os.getenv("KALSHI_API_KEY_ID"):
    _t = threading.Thread(target=_bot_thread, daemon=True)
    _t.start()
else:
    _state.error = "KALSHI_API_KEY_ID not set — bot is not running."


# ─────────────────────────────────────────────────────────────
# ENTRY POINT  (dev server — production uses gunicorn)
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
