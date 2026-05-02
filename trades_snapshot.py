"""
trades_snapshot.py — Daily commit of bot_trades.json to GitHub.

The reconcile snapshot (reconcile_snapshot.py) captures the per-strategy
P&L rollup from Kalshi's API ground truth. That's enough for accounting,
but the strategy `reason` strings (e.g. "CONVICTION / 5m +0.085% -> YES
@ 52c / 60s: +0.062%") live only in bot_trades.json and the live
/api/status payload — neither of which is committed to git.

Without the reasons, post-hoc analysis of *why* a trade fired (5m
momentum, 60s confirmation magnitude, time-to-expiry, etc.) is
impossible at any horizon longer than the bot's in-memory history. This
module fixes that by snapshotting the full trade log to
trades_archive/YYYY-MM-DD.json once per day, alongside the reconcile
snapshot.

Mirrors reconcile_snapshot.py's design: in-process daemon thread, no
external scheduler, no Chrome / onrender egress dependency. Reuses the
GitHub Contents API plumbing from reconcile_snapshot.commit_snapshot.

Environment variables
---------------------
TRADES_SNAPSHOT_ENABLED      "true" / "false"           (default: "true")
TRADES_SNAPSHOT_HOUR_UTC     hour of day, 24h, UTC      (default: "5")
TRADES_SNAPSHOT_MIN_UTC      minute of hour, UTC        (default: "15")
GITHUB_PAT                   reused from reconcile env  (REQUIRED)
GITHUB_REPO_OWNER            reused from reconcile env  (default: "ryanathompson")
GITHUB_REPO_NAME             reused from reconcile env  (default: "kalshi-btc-bot")
GITHUB_REPO_BRANCH           reused from reconcile env  (default: "main")

The default schedule sits 1h after the reconcile snapshot (default
04:15 UTC) so the two commits don't race for the same branch tip.
"""
from __future__ import annotations

import datetime
import os
import threading
import time
import traceback
from typing import Any, Callable, List, Optional

from reconcile_snapshot import run_snapshot_once as _run_snapshot_once


SNAPSHOT_DIR = "trades_archive"
LOG_PREFIX   = "[trades-snapshot]"


# ─────────────────────────────────────────────────────────────
# Small env helpers (duplicated so this module stays standalone;
# reconcile_snapshot's are private)
# ─────────────────────────────────────────────────────────────

def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v is not None and v != "" else default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").strip().lower() == "true"


def _next_run_at_utc(
    hour: int,
    minute: int,
    now: Optional[datetime.datetime] = None,
) -> datetime.datetime:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run <= now:
        run += datetime.timedelta(days=1)
    return run


# ─────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────

def run_trades_snapshot_once(
    load_trades: Callable[[], List[dict]],
    *,
    date_str: Optional[str] = None,
) -> dict[str, Any]:
    """Snapshot the current trade log to trades_archive/{date_str}.json."""
    return _run_snapshot_once(
        load_trades,
        date_str=date_str,
        snapshot_dir=SNAPSHOT_DIR,
        commit_kind="trades",
        wrap_list=True,
    )


def _scheduler_loop(load_trades: Callable[[], List[dict]]) -> None:
    hour   = int(_env("TRADES_SNAPSHOT_HOUR_UTC", "5"))
    minute = int(_env("TRADES_SNAPSHOT_MIN_UTC",  "15"))

    while True:
        next_run = _next_run_at_utc(hour, minute)
        sleep_s  = (
            next_run - datetime.datetime.now(datetime.timezone.utc)
        ).total_seconds()
        sleep_s = max(sleep_s, 30.0)
        print(f"{LOG_PREFIX} next run at {next_run.isoformat()} "
              f"(sleeping {int(sleep_s)}s)", flush=True)
        time.sleep(sleep_s)

        try:
            res = run_trades_snapshot_once(load_trades)
            commit_sha = (res.get("commit") or {}).get("sha", "") or ""
            path       = (res.get("content") or {}).get("path", "") or ""
            print(f"{LOG_PREFIX} committed {path} sha={commit_sha[:7]}",
                  flush=True)
        except Exception as e:
            print(f"{LOG_PREFIX} FAILED: {e}\n{traceback.format_exc()}",
                  flush=True)

        # Cushion so we don't immediately re-trigger today
        time.sleep(60)


def start_trades_snapshot_scheduler(
    load_trades: Callable[[], List[dict]],
) -> Optional[threading.Thread]:
    """Start the daily trades snapshot scheduler thread.

    No-op (returns None) if disabled via env or if GITHUB_PAT is missing.
    Both cases are logged so the operator can tell the difference from
    outside the process.
    """
    if not _env_bool("TRADES_SNAPSHOT_ENABLED", True):
        print(f"{LOG_PREFIX} disabled via TRADES_SNAPSHOT_ENABLED", flush=True)
        return None
    if not _env("GITHUB_PAT", ""):
        print(f"{LOG_PREFIX} GITHUB_PAT not set — scheduler not started",
              flush=True)
        return None

    t = threading.Thread(
        target=_scheduler_loop,
        args=(load_trades,),
        name="trades-snapshot",
        daemon=True,
    )
    t.start()
    print(f"{LOG_PREFIX} scheduler started", flush=True)
    return t
