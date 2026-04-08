"""
Kalshi BTC Bot â Flask Dashboard
=================================
Runs the dual-strategy bot in a background thread and serves a live
dashboard at / with auto-refreshing stats, open trades, and trade history.

Deploy on Render as a Web Service (not Background Worker).
Procfile:  web: gunicorn app:app --workers 1 --threads 4 --timeout 120
"""

import os
import sys
import time
import threading
import datetime
import concurrent.futures

from flask import Flask, render_template, jsonify
from dotenv import load_dotenv

from bot import (
    KalshiBot, KalshiClient, load_private_key, load_trades, POLL_INTERVAL
)

load_dotenv()

app = Flask(__name__)

# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# SHARED STATE  (written by bot thread, read by Flask routes)
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# HELPER: compute per-strategy stats from trade log
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def _strat_stats(trades, name=None):
    """Return stats dict for settled trades. name=None â all strategies."""
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


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# FLASK ROUTES
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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
            age = ts[:16] if ts else "â"
        return {**t, "age": age}

    def fmt_hist(t):
        ts = t.get("timestamp", "")
        return {**t, "ts_short": ts[:16].replace("T", " ") if ts else "â"}

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


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# BOT BACKGROUND THREAD
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

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

    print("[bot] Thread started â initializing...", flush=True)
    try:
        pkey   = load_private_key(key_path)
        client = KalshiClient(api_key_id, pkey, dry_run=dry_run)
        _bot   = KalshiBot(client, lag_stake, con_stake, daily_lim, dry_run)
        print("[bot] Bot initialized â warming up BTC price feed...", flush=True)

        # Warm up BTC feed with a hard per-fetch deadline.
        # IMPORTANT: do NOT use 'with ThreadPoolExecutor as ex:' â its __exit__
        # calls shutdown(wait=True), which blocks forever if the fetch thread
        # is stuck in an OS-level TCP hang.  Use shutdown(wait=False) instead.
        for i in range(3):
            ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            fut = ex.submit(_bot.btc.fetch)
            try:
                p = fut.result(timeout=8)   # hard wall-clock deadline
            except Exception:
                p = None
            finally:
                ex.shutdown(wait=False)     # abandon hung thread, don't block
            if p:
                _state.btc_price = p
                print(f"[bot] Warmup fetch {i+1}/3: BTC=${p:,.0f}", flush=True)
            else:
                print(f"[bot] Warmup fetch {i+1}/3: failed (will retry in main loop)", flush=True)
            if i < 2:
                time.sleep(5)
        print("[bot] Warmup done â entering main loop", flush=True)

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
        print(f"[bot] FATAL: {e}", flush=True)
        _state.error   = str(e)
        _state.running = False


# NOTE: The bot thread is NOT started here at module level.
# Starting threads during gunicorn's master import causes them to run in the
# master process, not the forked worker.  Threads don't survive fork(), so the
# worker ends up serving HTTP with _state.running=True but no bot thread.
# Instead, the thread is started in gunicorn.conf.py via post_worker_init,
# which fires inside each forked worker after it has fully initialized.
if not os.getenv("KALSHI_API_KEY_ID"):
    _state.error = "KALSHI_API_KEY_ID not set â bot is not running."


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# ENTRY POINT  (dev server â production uses gunicorn)
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
