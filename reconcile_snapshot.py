"""
reconcile_snapshot.py — Daily reconcile snapshot, committed to GitHub.

Runs a background thread inside the Render web process that, once per day,
builds a reconcile payload (same content as /api/reconcile) and commits it
to reconcile_archive/YYYY-MM-DD.json in the GitHub repo via the Contents API.

Replaces the previous external scheduled task that reached Render through
the Claude-in-Chrome extension. The bot now owns its own record-keeping:
no Chrome, no onrender.com egress dependency, no laptop-must-be-awake
assumption. Failures log to stdout and retry on the next schedule.

Environment variables
---------------------
RECONCILE_SNAPSHOT_ENABLED     "true" / "false"          (default: "true")
RECONCILE_SNAPSHOT_HOUR_UTC    hour of day, 24h, UTC     (default: "4")
RECONCILE_SNAPSHOT_MIN_UTC     minute of hour, UTC       (default: "15")
GITHUB_PAT                     fine-grained PAT with
                                contents: read/write     (REQUIRED)
GITHUB_REPO_OWNER              repo owner login          (default: "ryanathompson")
GITHUB_REPO_NAME               repo name                 (default: "kalshi-btc-bot")
GITHUB_REPO_BRANCH             branch to commit to       (default: "main")
"""
from __future__ import annotations

import base64
import datetime
import json
import os
import threading
import time
import traceback
from typing import Any, Callable, Optional

import requests


SNAPSHOT_DIR = "reconcile_archive"
LOG_PREFIX   = "[reconcile-snapshot]"


# ─────────────────────────────────────────────────────────────
# Small env helpers
# ─────────────────────────────────────────────────────────────

def _env(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v is not None and v != "" else default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "true" if default else "false").strip().lower() == "true"


# ─────────────────────────────────────────────────────────────
# Date / scheduling helpers
# ─────────────────────────────────────────────────────────────

def _today_utc_date() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _next_run_at_utc(
    hour: int,
    minute: int,
    now: Optional[datetime.datetime] = None,
) -> datetime.datetime:
    """Return the next UTC datetime at the given hour/minute, strictly in the future."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run <= now:
        run += datetime.timedelta(days=1)
    return run


# ─────────────────────────────────────────────────────────────
# GitHub Contents API
# ─────────────────────────────────────────────────────────────

def _github_headers(pat: str) -> dict[str, str]:
    return {
        "Authorization":        f"Bearer {pat}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent":           "kalshi-btc-bot-reconcile-snapshot",
    }


def _contents_url(owner: str, repo: str, path: str) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"


def _get_existing_sha(
    owner: str, repo: str, path: str, branch: str, pat: str
) -> Optional[str]:
    """Return the blob SHA of the file at `path` on `branch`, or None if it doesn't exist."""
    r = requests.get(
        _contents_url(owner, repo, path),
        params={"ref": branch},
        headers=_github_headers(pat),
        timeout=15,
    )
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json().get("sha")


def commit_snapshot(
    payload: dict[str, Any],
    date_str: str,
    *,
    pat: Optional[str]    = None,
    owner: Optional[str]  = None,
    repo: Optional[str]   = None,
    branch: Optional[str] = None,
) -> dict[str, Any]:
    """Commit `payload` as reconcile_archive/{date_str}.json to GitHub.

    If the file already exists, update it in place (the GitHub API requires
    the current blob SHA for updates). Returns the parsed API response.
    """
    pat    = pat    or _env("GITHUB_PAT",         "")
    owner  = owner  or _env("GITHUB_REPO_OWNER",  "ryanathompson")
    repo   = repo   or _env("GITHUB_REPO_NAME",   "kalshi-btc-bot")
    branch = branch or _env("GITHUB_REPO_BRANCH", "main")

    if not pat:
        raise RuntimeError("GITHUB_PAT not set; cannot commit snapshot")

    path = f"{SNAPSHOT_DIR}/{date_str}.json"
    content_bytes = json.dumps(payload, indent=2, default=str, sort_keys=True).encode("utf-8")
    content_b64   = base64.b64encode(content_bytes).decode("ascii")

    sha = _get_existing_sha(owner, repo, path, branch, pat)

    body: dict[str, Any] = {
        "message": f"reconcile: daily snapshot {date_str}",
        "content": content_b64,
        "branch":  branch,
    }
    if sha:
        body["sha"]     = sha
        body["message"] = f"reconcile: refresh snapshot {date_str}"

    r = requests.put(
        _contents_url(owner, repo, path),
        headers=_github_headers(pat),
        json=body,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ─────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────

def run_snapshot_once(
    build_payload: Callable[[], dict[str, Any]],
    *,
    date_str: Optional[str] = None,
) -> dict[str, Any]:
    """Build the reconcile payload and commit it. Returns the GitHub response."""
    date_str = date_str or _today_utc_date()
    payload  = build_payload()
    if not isinstance(payload, dict):
        raise RuntimeError(f"build_payload returned non-dict: {type(payload)}")
    payload = dict(payload)  # don't mutate caller's cache entry
    payload["_snapshot_captured_at_utc"] = (
        datetime.datetime.now(datetime.timezone.utc).isoformat()
    )
    payload["_snapshot_source"] = "render-snapshot"
    return commit_snapshot(payload, date_str)


def _scheduler_loop(build_payload: Callable[[], dict[str, Any]]) -> None:
    hour   = int(_env("RECONCILE_SNAPSHOT_HOUR_UTC", "4"))
    minute = int(_env("RECONCILE_SNAPSHOT_MIN_UTC",  "15"))

    while True:
        next_run = _next_run_at_utc(hour, minute)
        sleep_s  = (
            next_run - datetime.datetime.now(datetime.timezone.utc)
        ).total_seconds()
        # Never tight-loop, even if clock skews weird
        sleep_s = max(sleep_s, 30.0)
        print(f"{LOG_PREFIX} next run at {next_run.isoformat()} "
              f"(sleeping {int(sleep_s)}s)", flush=True)
        time.sleep(sleep_s)

        try:
            res = run_snapshot_once(build_payload)
            commit_sha = (res.get("commit") or {}).get("sha", "") or ""
            path       = (res.get("content") or {}).get("path", "") or ""
            print(f"{LOG_PREFIX} committed {path} sha={commit_sha[:7]}",
                  flush=True)
        except Exception as e:
            print(f"{LOG_PREFIX} FAILED: {e}\n{traceback.format_exc()}",
                  flush=True)

        # Cushion so the next loop iteration doesn't immediately schedule today
        # again when the commit completed just past the trigger minute.
        time.sleep(60)


def start_snapshot_scheduler(
    build_payload: Callable[[], dict[str, Any]],
) -> Optional[threading.Thread]:
    """Start the daily snapshot scheduler thread.

    No-op (returns None) if disabled via env or if GITHUB_PAT is missing —
    both cases are logged so the operator can tell the difference from
    outside the process.
    """
    if not _env_bool("RECONCILE_SNAPSHOT_ENABLED", True):
        print(f"{LOG_PREFIX} disabled via RECONCILE_SNAPSHOT_ENABLED", flush=True)
        return None
    if not _env("GITHUB_PAT", ""):
        print(f"{LOG_PREFIX} GITHUB_PAT not set — scheduler not started",
              flush=True)
        return None

    t = threading.Thread(
        target=_scheduler_loop,
        args=(build_payload,),
        name="reconcile-snapshot",
        daemon=True,
    )
    t.start()
    print(f"{LOG_PREFIX} scheduler started", flush=True)
    return t
