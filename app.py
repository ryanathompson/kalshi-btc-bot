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

from pnl_windows import compute_windows

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


def _entry_price_stats(trades, strategy=None):
    """Bucket settled trades by entry price (cents) and return win-rate per band.

    Bands match the dashboard widget the user designed:
        1-24c   (deep underdog — historically 0% WR in live trading)
        25-39c  (mid underdog — historically ~18% WR)
        40-55c  (favored band — historically 75% WR; the only profitable zone)

    The 'price' field on a trade is stored as integer cents (1-99). Trades
    without a settled `result` are skipped.

    strategy=None aggregates across all strategies; pass "CONSENSUS" or "LAG"
    to filter. Returns a list of dicts in band order so the dashboard can
    iterate without resorting:

        [{"label":"1-24c", "lo":1, "hi":24, "trades":N, "wins":W,
          "losses":L, "wr":pct, "warn":bool}, ...]

    `warn` is True for bands whose live win-rate has historically been below
    50% — the UI uses it to render the warning triangle from the screenshot.
    """
    bands = [
        {"label": "1-24c",  "lo": 1,  "hi": 24, "warn": True},
        {"label": "25-39c", "lo": 25, "hi": 39, "warn": True},
        {"label": "40-55c", "lo": 40, "hi": 55, "warn": False},
    ]
    for b in bands:
        b["trades"] = 0
        b["wins"]   = 0
        b["losses"] = 0
        b["wr"]     = None

    for t in trades:
        if not t.get("result"):
            continue
        if strategy and t.get("strategy") != strategy:
            continue
        try:
            price = int(t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if price < 1:
            continue
        for b in bands:
            if b["lo"] <= price <= b["hi"]:
                b["trades"] += 1
                if t["result"] == "WIN":
                    b["wins"] += 1
                else:
                    b["losses"] += 1
                break

    for b in bands:
        if b["trades"]:
            b["wr"] = round(b["wins"] / b["trades"] * 100, 1)
    return bands


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
        "snp_stats":    _strat_stats(trades, "SNIPER"),
        "all_stats":    _strat_stats(trades),
        "pnl_points":   _pnl_points(trades),
        # Win-rate breakdown by entry price band — used by the dashboard
        # widget. We expose both an "all-strategy" view and a CONSENSUS-only
        # view since the price-band signal is most actionable for Consensus.
        "entry_price_stats":     _entry_price_stats(trades),
        "entry_price_stats_con": _entry_price_stats(trades, "CONSENSUS"),
        "entry_price_stats_snp": _entry_price_stats(trades, "SNIPER"),
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


@app.route("/api/pnl_windows")
def api_pnl_windows():
    """Windowed P&L for 1D/1W/1M/ALL, calendar-aligned to ET.

    Replaces the client-side rolling-window math in dashboard.html with
    one server-side computation so (a) the 1D display matches the halt
    timer's ET-midnight reset, and (b) DST transitions are handled
    correctly regardless of the viewer's browser timezone.

    The response also includes an ``_invariants`` block flagging any
    monotonicity violations across nested windows. Those failures are a
    real-bug detector (double-count / bad timestamp / boundary drift),
    NOT a math artifact, and are also logged at WARNING level to Render
    so they surface even if the dashboard is closed.
    """
    trades = load_trades()
    # Pass bot.py's own parse_trade_ts so the dashboard buckets trades
    # into the same calendar day as the RiskManager halt timer does.
    result = compute_windows(
        trades,
        now=now_et(),
        ts_parser=parse_trade_ts,
    )

    # Annotate with current halt state if the bot is running so the
    # dashboard can badge the window that's currently frozen.
    try:
        if _bot is not None:
            halted = bool(_bot.risk._halted)
            for k in ("1D", "1W", "1M", "ALL"):
                result[k]["halt_active"] = halted
            if halted:
                result["_halt_reason"] = _bot.risk._halt_reason
    except Exception:
        pass

    inv = result.get("_invariants", {})
    if not inv.get("ok", True):
        app.logger.warning(
            "pnl_windows invariant failures: %s", inv.get("failures"),
        )
    return jsonify(result)


# ────────────────────────────────────────────────────────────
# ANALYSIS PAGE — wraps backtest_consensus.py for non-technical use
# ─────────────────────────────────────────────────────────────
#
# The page answers one question: "Is Consensus safe to re-enable?"
#
# Why a background thread + polling instead of a blocking request:
# Render runs gunicorn with --timeout 120. A fresh 14-day @1s fetch
# hits Binance with ~1200 paginated requests and takes well over two
# minutes, so a synchronous request gets killed mid-flight and the
# client sees an HTML error page instead of JSON. Running in a
# background thread decouples fetch duration from the HTTP timeout.

# Cap 1-second interval to prevent accidental multi-minute fetches that
# Binance will rate-limit anyway. 1-minute can span much longer.
MAX_DAYS_1S = 3
MAX_DAYS_1M = 60

# Analysis state — mutated only by the background worker thread.
# All reads from Flask routes go through the lock.
_analysis_lock = threading.Lock()
_analysis_state = {
    "state":      "idle",   # "idle" | "running" | "done" | "error"
    "progress":   "",       # human-readable status line
    "started_at": None,     # ISO timestamp
    "finished_at": None,    # ISO timestamp
    "result":     None,     # final result dict when state == "done"
    "error":      None,     # error string when state == "error"
    "params":     None,     # {start, end, interval}
}
_analysis_thread = None     # Thread handle for the current/last run


def _set_progress(msg: str):
    """Thread-safe progress string update."""
    with _analysis_lock:
        _analysis_state["progress"] = msg
    print(f"[analysis] {msg}", flush=True)


def _run_analysis_worker(start: str, end: str, interval: str):
    """
    Background worker that runs the full fetch+sweep+interpret pipeline
    and mutates _analysis_state as it progresses. Must not raise — it
    captures all exceptions into the state dict.
    """
    global _analysis_state
    try:
        # Build a Kalshi client — reuse the bot's if available.
        _set_progress("Building Kalshi client…")
        client = _bot.client if _bot is not None else None
        if client is None:
            api_key_id = os.getenv("KALSHI_API_KEY_ID")
            key_path   = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi.key")
            if not api_key_id:
                raise RuntimeError(
                    "Bot isn't initialized and no Kalshi credentials are "
                    "set. Analysis needs Kalshi API access.")
            pkey = load_private_key(key_path)
            client = KalshiClient(api_key_id, pkey, dry_run=True)

        _set_progress(
            f"Fetching BTC klines and Kalshi markets ({start} → {end}, {interval})…"
        )
        history = fetch_history(
            start=start,
            end=end,
            interval=interval,
            with_kalshi=True,
            cache_dir="data",
            max_cache_age_hours=6.0,
            kalshi_client=client,
        )

        btc_n    = len(history.get("btc") or [])
        market_n = len(history.get("kalshi_markets") or [])
        cache_hit = bool(history.get("_cache_hit"))
        _set_progress(
            f"Fetched {btc_n:,} BTC samples and {market_n:,} Kalshi markets"
            + (" (from cache)" if cache_hit else "")
            + ". Running sweep…"
        )

        rows = run_sweep(
            history,
            dead_zones=DEFAULT_SWEEP_DEAD_ZONES,
            base_price=DEFAULT_BASE_PRICE,
            max_price=DEFAULT_MAX_PRICE,
        )
        _set_progress(f"Sweep done ({len(rows)} thresholds). Computing verdict…")
        verdict = interpret_sweep(rows)

        result = {
            "status":       "ok",
            "start":        start,
            "end":          end,
            "interval":     interval,
            "cache_hit":    cache_hit,
            "cache_age_s":  history.get("_cache_age_s"),
            "btc_count":    btc_n,
            "market_count": market_n,
            "sweep":        rows,
            "verdict":      verdict,
            "ran_at":       datetime.datetime.utcnow().isoformat() + "Z",
        }

        with _analysis_lock:
            _analysis_state["state"]       = "done"
            _analysis_state["progress"]    = "Complete."
            _analysis_state["result"]      = result
            _analysis_state["finished_at"] = datetime.datetime.utcnow().isoformat() + "Z"
        print(f"[analysis] Done: verdict={verdict.get('verdict')}", flush=True)

    except BaseException as e:
        # BaseException (not Exception) so we also catch SystemExit / KeyboardInterrupt
        # — otherwise a SystemExit from the replay engine would silently kill the
        # worker thread and leave the state stuck in "running" forever.
        traceback.print_exc()
        with _analysis_lock:
            _analysis_state["state"]       = "error"
            _analysis_state["error"]       = str(e) or type(e).__name__
            _analysis_state["progress"]    = f"Failed: {e}"
            _analysis_state["finished_at"] = datetime.datetime.utcnow().isoformat() + "Z"


@app.route("/analysis")
def analysis_page():
    return render_template("analysis.html")


@app.route("/api/analysis/status")
def api_analysis_status():
    """Poll endpoint. Returns the full state dict."""
    with _analysis_lock:
        snap = dict(_analysis_state)
    return jsonify(snap)


@app.route("/api/analysis/run", methods=["POST"])
def api_analysis_run():
    """
    Kick off an analysis run in a background thread and return immediately.

    Returns 202 with {state: "running"} on success, or 409/400 on errors.
    The client then polls /api/analysis/status until state is "done" or "error".
    """
    global _analysis_thread, _analysis_state

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
        d_start = datetime.date.fromisoformat(start)
        d_end   = datetime.date.fromisoformat(end)
    except ValueError as e:
        return jsonify({
            "status": "error",
            "error": f"Invalid date format (expected YYYY-MM-DD): {e}",
        }), 400

    if d_start >= d_end:
        return jsonify({
            "status": "error",
            "error": "Start date must be before end date.",
        }), 400

    span_days = (d_end - d_start).days
    if interval == "1s" and span_days > MAX_DAYS_1S:
        return jsonify({
            "status": "error",
            "error": (
                f"1-second precision is capped at {MAX_DAYS_1S} days "
                f"(you requested {span_days}). Use 1-minute precision for "
                f"longer windows — it's nearly as good for this strategy and "
                f"runs in seconds instead of minutes."
            ),
        }), 400
    if interval == "1m" and span_days > MAX_DAYS_1M:
        return jsonify({
            "status": "error",
            "error": (
                f"Date range is capped at {MAX_DAYS_1M} days "
                f"(you requested {span_days})."
            ),
        }), 400

    # Reject if another run is already in flight.
    with _analysis_lock:
        if _analysis_state["state"] == "running":
            return jsonify({
                "status": "error",
                "state":  "running",
                "error": "Another analysis run is already in progress. "
                         "Wait for it to finish and try again.",
            }), 409

        # Reset state for the new run.
        _analysis_state.update({
            "state":       "running",
            "progress":    "Starting…",
            "started_at":  datetime.datetime.utcnow().isoformat() + "Z",
            "finished_at": None,
            "result":      None,
            "error":       None,
            "params":      {"start": start, "end": end, "interval": interval},
        })

    _analysis_thread = threading.Thread(
        target=_run_analysis_worker,
        args=(start, end, interval),
        name="analysis-worker",
        daemon=True,
    )
    _analysis_thread.start()

    return jsonify({
        "status": "started",
        "state":  "running",
        "params": {"start": start, "end": end, "interval": interval},
    }), 202




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
