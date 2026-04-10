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
import traceback

from flask import Flask, render_template, jsonify, request
from dotenv import load_dotenv

from bot import (
    KalshiBot, KalshiClient, load_private_key, load_trades, POLL_INTERVAL,
    resolve_trades, start_keep_alive, rebuild_trades_from_api,
    ET, now_et, today_et, parse_trade_ts,
)

from backtest_consensus import (
    fetch_history, run_sweep, interpret_sweep,
    DEFAULT_SWEEP_DEAD_ZONES, DEFAULT_BASE_PRICE, DEFAULT_MAX_PRICE,
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


def _to_epoch_ms(ts):
    """Parse any trade-log timestamp format to epoch ms (UTC).

    Delegates to bot.parse_trade_ts which knows about ET-aware, naive-UTC,
    and Kalshi 'Z'-suffixed formats. Returns None if unparseable.
    """
    dt = parse_trade_ts(ts)
    return int(dt.timestamp() * 1000) if dt else None


def _fmt_et(ts, short=True):
    """Format a trade timestamp as a human-readable ET string.

    short=True  → 'YYYY-MM-DD HH:MM ET'  (for tables)
    short=False → 'YYYY-MM-DD HH:MM:SS ET' (for detail views)
    """
    dt = parse_trade_ts(ts)
    if not dt:
        return "—"
    et = dt.astimezone(ET)
    fmt = "%Y-%m-%d %H:%M" if short else "%Y-%m-%d %H:%M:%S"
    return et.strftime(fmt) + " ET"


def _pnl_points(trades):
    """Chronological cumulative-P&L series across ALL settled trades.

    Returns [{"ts": epoch_ms_utc, "pnl": cumulative_pnl, "delta": trade_pnl}, ...]
    sorted by timestamp ascending. Timestamps are normalized to epoch ms so
    the frontend doesn't have to worry about mixed ISO formats across
    bot-logged trades (naive UTC) and API-rebuilt trades (ISO with 'Z').
    """
    settled = []
    for t in trades:
        if not t.get("result"):
            continue
        if t.get("pnl") is None:
            continue
        ms = _to_epoch_ms(t.get("timestamp"))
        if ms is None:
            continue
        settled.append((ms, float(t.get("pnl") or 0)))

    settled.sort(key=lambda pair: pair[0])

    points = []
    cum = 0.0
    for ms, delta in settled:
        cum += delta
        points.append({
            "ts":    ms,
            "pnl":   round(cum, 2),
            "delta": round(delta, 2),
        })
    return points


# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# FLASK ROUTES
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/health")
def health():
    return "ok", 200


@app.route("/api/status")
def api_status():
    trades = load_trades()

    open_trades = [t for t in trades if not t.get("result")]

    # Sort by parsed epoch ms, not raw string — bot-logged (ET-aware),
    # legacy (naive UTC), and recovered (UTC 'Z') timestamps do NOT sort
    # correctly lexicographically against one another.
    def _sort_key(t):
        ms = _to_epoch_ms(t.get("timestamp"))
        return ms if ms is not None else -1
    history = sorted(
        [t for t in trades if t.get("result")],
        key=_sort_key,
        reverse=True,
    )[:100]

    # Format open trades for display — 'age' is wall-clock delta,
    # independent of storage TZ, so we compute it from parsed UTC.
    now_utc = datetime.datetime.now(datetime.timezone.utc)

    def fmt_open(t):
        dt = parse_trade_ts(t.get("timestamp"))
        if dt is None:
            age = "—"
        else:
            age_s = (now_utc - dt).total_seconds()
            if age_s < 0:
                age = "just now"
            elif age_s < 3600:
                age = f"{int(age_s // 60)}m ago"
            else:
                age = f"{int(age_s // 3600)}h ago"
        return {**t, "age": age, "ts_et": _fmt_et(t.get("timestamp"))}

    def fmt_hist(t):
        # Always show ET so bot-logged and recovered trades use the same clock.
        return {**t, "ts_short": _fmt_et(t.get("timestamp"))}

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
        "pnl_points":   _pnl_points(trades),
    })


@app.route("/api/resolve", methods=["POST"])
def api_resolve():
    """Manually trigger resolve_trades() â useful for debugging stuck trades."""
    if _bot is None:
        return jsonify({"error": "Bot not initialized"}), 503
    try:
        n = resolve_trades(_bot.client)
        trades = load_trades()
        open_count = sum(1 for t in trades if not t.get("result"))
        return jsonify({"resolved": n, "open_remaining": open_count})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/reset_halt", methods=["POST"])
def api_reset_halt():
    """Manually clear the RiskManager halt state.

    The halt is normally auto-cleared at ET midnight (new trading day).
    This endpoint lets the operator force an immediate reset from the
    dashboard after reviewing the halt reason.
    """
    if _bot is None:
        return jsonify({"error": "Bot not initialized"}), 503
    try:
        prev_reason = _bot.risk._halt_reason
        _bot.risk._halted      = False
        _bot.risk._halt_reason = ""
        _bot.risk._halt_date   = None
        # CRITICAL: also set the manual override for today, otherwise the
        # main loop's next risk.check() (within POLL_INTERVAL seconds) will
        # recompute today's PnL from the trade log and re-halt immediately
        # with the same reason. The override auto-clears at ET midnight.
        _bot.risk._override_date = today_et()
        # Mirror into shared state so the dashboard updates immediately
        _state.halted      = False
        _state.halt_reason = ""
        print(f"[bot] Halt manually reset via dashboard (was: {prev_reason}) "
              f"— daily-loss check bypassed until next ET midnight", flush=True)
        return jsonify({"ok": True, "was": prev_reason})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ────────────────────────────────────────────────────────────
# ANALYSIS PAGE — wraps backtest_consensus.py for non-technical use
# ─────────────────────────────────────────────────────────────
#
# The page answers one question: "Is Consensus safe to re-enable?"
# It runs the same paranoid replay the CLI does, but in-process from
# the dashboard, with file-based caching so repeated clicks are fast.

# Lock so two concurrent clicks don't both blast Binance/Kalshi at once.
_analysis_lock = threading.Lock()

# In-memory cache of the LAST run, lets us re-render on refresh.
_last_analysis_result = None


@app.route("/analysis")
def analysis_page():
    return render_template("analysis.html")


@app.route("/api/analysis/last")
def api_analysis_last():
    """Return the most recent analysis result, if any."""
    if _last_analysis_result is None:
        return jsonify({"status": "empty"})
    return jsonify({"status": "ok", "result": _last_analysis_result})


@app.route("/api/analysis/run", methods=["POST"])
def api_analysis_run():
    """Run a fetch+sweep+interpret pipeline for the analysis page."""
    global _last_analysis_result

    if not _analysis_lock.acquire(blocking=False):
        return jsonify({
            "status": "error",
            "error": "Another analysis run is already in progress. "
                     "Wait a few seconds and try again.",
        }), 429

    try:
        body = request.get_json(silent=True) or {}
        today = datetime.date.today()
        default_start = (today - datetime.timedelta(days=14)).isoformat()
        default_end   = today.isoformat()

        start    = body.get("start")    or default_start
        end      = body.get("end")      or default_end
        interval = body.get("interval") or "1m"

        if interval not in ("1s", "1m"):
            return jsonify({
                "status": "error",
                "error": f"Invalid interval {interval!r}. Must be '1s' or '1m'.",
            }), 400

        try:
            datetime.date.fromisoformat(start)
            datetime.date.fromisoformat(end)
        except ValueError as e:
            return jsonify({
                "status": "error",
                "error": f"Invalid date format (expected YYYY-MM-DD): {e}",
            }), 400

        if start >= end:
            return jsonify({
                "status": "error",
                "error": "Start date must be before end date.",
            }), 400

        # Kalshi client — reuse the bot's authenticated client if available,
        # otherwise build one from env. If no creds at all we cannot proceed.
        client = _bot.client if _bot is not None else None
        if client is None:
            api_key_id = os.getenv("KALSHI_API_KEY_ID")
            key_path   = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi.key")
            if not api_key_id:
                return jsonify({
                    "status": "error",
                    "error": "Bot isn't initialized and no Kalshi credentials "
                             "are set. Analysis needs Kalshi API access to fetch "
                             "historical markets.",
                }), 503
            try:
                pkey = load_private_key(key_path)
                client = KalshiClient(api_key_id, pkey, dry_run=True)
            except Exception as e:
                return jsonify({
                    "status": "error",
                    "error": f"Failed to build Kalshi client: {e}",
                }), 500

        try:
            history = fetch_history(
                start=start,
                end=end,
                interval=interval,
                with_kalshi=True,
                cache_dir="data",
                max_cache_age_hours=6.0,
                kalshi_client=client,
            )
        except Exception as e:
            traceback.print_exc()
            return jsonify({
                "status": "error",
                "error": f"Failed to fetch history: {e}",
            }), 500

        try:
            rows = run_sweep(
                history,
                dead_zones=DEFAULT_SWEEP_DEAD_ZONES,
                base_price=DEFAULT_BASE_PRICE,
                max_price=DEFAULT_MAX_PRICE,
            )
            verdict = interpret_sweep(rows)
        except Exception as e:
            traceback.print_exc()
            return jsonify({
                "status": "error",
                "error": f"Sweep/interpretation failed: {e}",
            }), 500

        result = {
            "status":       "ok",
            "start":        start,
            "end":          end,
            "interval":     interval,
            "cache_hit":    bool(history.get("_cache_hit")),
            "cache_age_s":  history.get("_cache_age_s"),
            "btc_count":    len(history.get("btc") or []),
            "market_count": len(history.get("kalshi_markets") or []),
            "sweep":        rows,
            "verdict":      verdict,
            "ran_at":       datetime.datetime.utcnow().isoformat() + "Z",
        }
        _last_analysis_result = result
        return jsonify(result)

    finally:
        _analysis_lock.release()




# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
# BOT BACKGROUND THREAD
# âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def _bot_thread():
    global _bot
    _state.running    = True
    _state.started_at = now_et().isoformat()

    # Start self-ping to prevent Render free-tier spin-down
    start_keep_alive()

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

        # Rebuild trade log from Kalshi API (survives Render redeploys)
        try:
            rebuild_trades_from_api(client)
        except Exception as e:
            print(f"[bot] Trade rebuild warning: {e}", flush=True)

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

        _last_resolve = 0

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
                _state.last_cycle   = now_et().isoformat()
                _state.error        = None

            except Exception as e:
                _state.error = str(e)

            # Resolve settled trades every 60 s
            if time.time() - _last_resolve >= 60:
                try:
                    n = resolve_trades(_bot.client)
                    if n:
                        print(f"[bot] Resolved {n} trade(s).", flush=True)
                except Exception as re:
                    print(f"[bot] resolve error: {re}", flush=True)
                _last_resolve = time.time()

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
