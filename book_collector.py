#!/usr/bin/env python3
"""
book_collector.py — Passive KXBTC15M orderbook + BTC truth-feed collector.

Purpose
-------
Records a time-aligned snapshot of:
  (a) the top-of-book on every ATM±N strike of every active KXBTC15M contract
  (b) Coinbase BTC/USD spot price
every POLL_INTERVAL_S seconds, for as long as the process runs.

Output is written to SQLite (book_data.db) AND appended to a JSONL log
(book_data.jsonl) so you can tail operational state from the Render Shell.

This script is READ-ONLY against Kalshi — it never places, modifies, or
cancels orders. Safe to run alongside the live trading bot.

Deployment
----------
Intended to run as a separate Render background worker in the same repo
as bot.py. Reuses bot.py's KalshiClient + load_private_key directly so
we inherit the exact same auth scheme, base URL, and key-loading paths.

Env vars (same as the main bot):
  KALSHI_API_KEY_ID         - Kalshi RSA key ID (UUID)
  KALSHI_PRIVATE_KEY_PEM    - RSA private key PEM (preferred on Render)
  KALSHI_PRIVATE_KEY_BASE64 - or base64-encoded PEM (legacy)
  KALSHI_PRIVATE_KEY_PATH   - or path to a PEM file (default: ./kalshi.key)
  BOOK_DB_PATH              - defaults to /tmp/book_data.db
  BOOK_JSONL_PATH           - defaults to /tmp/book_data.jsonl
  POLL_INTERVAL_S           - defaults to 3
  STRIKE_BAND               - defaults to 3 (ATM±3 strikes per contract)

Start command on Render:
  python book_collector.py

Notes on ephemeral storage
--------------------------
Render background workers use an ephemeral filesystem — /tmp is wiped on
redeploy and can be evicted under memory pressure. That's fine for a
24-hour measurement run: pull the db before redeploying. For a longer
run, attach a Render disk and point BOOK_DB_PATH at it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

# Reuse bot.py's auth + base URL + key loader. No fallback — if bot.py
# isn't importable from this worker's cwd, the worker is misconfigured
# and we want to fail loudly at startup, not silently drift.
from bot import KalshiClient, load_private_key  # noqa: E402


# ---- config ----
DB_PATH = os.getenv("BOOK_DB_PATH", "/tmp/book_data.db")
JSONL_PATH = os.getenv("BOOK_JSONL_PATH", "/tmp/book_data.jsonl")
POLL_INTERVAL_S = float(os.getenv("POLL_INTERVAL_S", "3"))
STRIKE_BAND = int(os.getenv("STRIKE_BAND", "3"))  # ATM±N
SERIES_TICKER = "KXBTC15M"

log = logging.getLogger("book_collector")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)


# ---- data model ----
@dataclass
class BookSnapshot:
    ts_ms: int
    ticker: str
    strike: float
    contract_close_ts_ms: int | None
    minutes_to_close: float | None
    yes_bid: int | None
    yes_ask: int | None
    yes_bid_size: int | None
    yes_ask_size: int | None
    no_bid: int | None
    no_ask: int | None
    btc_price: float | None

    def as_row(self) -> tuple:
        return (
            self.ts_ms, self.ticker, self.strike, self.contract_close_ts_ms,
            self.minutes_to_close, self.yes_bid, self.yes_ask,
            self.yes_bid_size, self.yes_ask_size, self.no_bid, self.no_ask,
            self.btc_price,
        )

    def as_dict(self) -> dict:
        return {
            "ts_ms": self.ts_ms, "ticker": self.ticker, "strike": self.strike,
            "close_ts_ms": self.contract_close_ts_ms,
            "min_to_close": self.minutes_to_close,
            "yb": self.yes_bid, "ya": self.yes_ask,
            "ybs": self.yes_bid_size, "yas": self.yes_ask_size,
            "nb": self.no_bid, "na": self.no_ask,
            "btc": self.btc_price,
        }


# ---- storage ----
SCHEMA = """
CREATE TABLE IF NOT EXISTS book_snapshots (
    ts_ms INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    strike REAL NOT NULL,
    contract_close_ts_ms INTEGER,
    minutes_to_close REAL,
    yes_bid INTEGER, yes_ask INTEGER,
    yes_bid_size INTEGER, yes_ask_size INTEGER,
    no_bid INTEGER, no_ask INTEGER,
    btc_price REAL
);
CREATE INDEX IF NOT EXISTS idx_book_ticker_ts ON book_snapshots(ticker, ts_ms);
CREATE INDEX IF NOT EXISTS idx_book_ts ON book_snapshots(ts_ms);
"""


def init_db(path: str) -> sqlite3.Connection:
    # Ensure parent dir exists (works for both /tmp/... and ./...)
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # autocommit
    conn.executescript(SCHEMA)
    # Write-ahead so analyze_book_data.py can read concurrently
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


# ---- ticker helpers ----
# KXBTC15M tickers look like 'KXBTC15M-26APR11H10-T85000' (exact strike)
# or 'KXBTC15M-26APR11H10-B85000-85250' (between-strikes). We want the
# LOWER bound of a B-range, not the upper.
_STRIKE_RE = re.compile(r"-([TB])(\d+)(?:-(\d+))?$")


def parse_strike_from_ticker(ticker: str) -> float | None:
    if not ticker:
        return None
    m = _STRIKE_RE.search(ticker)
    if not m:
        return None
    try:
        return float(m.group(2))
    except Exception:
        return None


def iso_to_ms(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def select_atm_band(markets: list[dict], btc_price: float, band: int) -> list[dict]:
    """From a list of markets (all strikes of one contract), pick the
    `band*2 + 1` closest to btc_price by strike. Returns sorted by strike."""
    with_strike = []
    for m in markets:
        k = parse_strike_from_ticker(m.get("ticker", ""))
        if k is not None:
            with_strike.append((k, m))
    if not with_strike:
        return []
    with_strike.sort(key=lambda kv: abs(kv[0] - btc_price))
    chosen = with_strike[: band * 2 + 1]
    chosen.sort(key=lambda kv: kv[0])
    return [m for _, m in chosen]


# ---- feeds ----
def get_btc_spot() -> float | None:
    """Coinbase BTC-USD spot. Same source the LAG strategy uses in bot.py."""
    try:
        r = requests.get(
            "https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=5
        )
        r.raise_for_status()
        return float(r.json()["data"]["amount"])
    except Exception as e:
        log.warning("coinbase spot failed: %s", e)
        return None


def list_active_contracts(client: KalshiClient) -> list[dict]:
    """Return currently-open KXBTC15M markets. Each 15m contract spans
    many strike markets; they share an event_ticker."""
    out: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {
            "series_ticker": SERIES_TICKER,
            "status": "open",
            "limit": 200,
        }
        if cursor:
            params["cursor"] = cursor
        # NOTE: paths must NOT include '/trade-api/v2' — bot.py's
        # KalshiClient already prepends BASE_URL for us.
        resp = client.get("/markets", params=params)
        out.extend(resp.get("markets", []))
        cursor = resp.get("cursor")
        if not cursor:
            break
    return out


def group_by_event(markets: list[dict]) -> dict[str, list[dict]]:
    """Group strike markets by event_ticker (one event = one 15m contract)."""
    by_event: dict[str, list[dict]] = {}
    for m in markets:
        ev = m.get("event_ticker")
        if not ev:
            # Fallback: strip the strike suffix off the market ticker
            ev = _STRIKE_RE.sub("", m.get("ticker", ""))
        by_event.setdefault(ev, []).append(m)
    return by_event


def fetch_orderbook(client: KalshiClient, ticker: str) -> dict | None:
    try:
        return client.get(f"/markets/{ticker}/orderbook")
    except requests.HTTPError as e:
        log.warning("orderbook %s failed: %s", ticker, e)
        return None
    except Exception as e:
        log.warning("orderbook %s error: %s", ticker, e)
        return None


def top_of_book(ob: dict) -> dict:
    """Kalshi orderbook payload: {'orderbook': {'yes': [[px, sz], ...],
    'no': [[px, sz], ...]}}. The 'yes' list is YES-side bids; the 'no'
    list is NO-side bids. There is no explicit 'ask' — the ask on YES is
    implied by the best NO bid (yes_ask = 100 - best_no_bid).

    Returns dict of yes_bid, yes_ask, yes_bid_size, yes_ask_size, no_bid,
    no_ask — integer cents / contract counts. Missing levels are None.
    """
    book = (ob or {}).get("orderbook") or {}
    yes_levels = book.get("yes") or []
    no_levels = book.get("no") or []

    def best(levels: list) -> tuple[int | None, int | None]:
        if not levels:
            return None, None
        try:
            px = int(levels[0][0])
            sz = int(levels[0][1])
            return px, sz
        except Exception:
            return None, None

    yb, ybs = best(yes_levels)
    nb, nbs = best(no_levels)
    ya = (100 - nb) if nb is not None else None
    yas = nbs
    na = (100 - yb) if yb is not None else None
    return {
        "yes_bid": yb, "yes_ask": ya,
        "yes_bid_size": ybs, "yes_ask_size": yas,
        "no_bid": nb, "no_ask": na,
    }


# ---- main loop ----
class Collector:
    def __init__(self) -> None:
        api_key_id = os.environ["KALSHI_API_KEY_ID"]
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi.key")
        pkey = load_private_key(key_path)
        # dry_run doesn't matter for us — we only call GET — but we set
        # it True to make intent explicit: this worker never writes.
        self.client = KalshiClient(api_key_id, pkey, dry_run=True)
        self.db = init_db(DB_PATH)
        self._stop = False
        self._contracts_refresh_ts = 0.0
        self._active_events: dict[str, list[dict]] = {}

        signal.signal(signal.SIGINT, self._graceful_stop)
        signal.signal(signal.SIGTERM, self._graceful_stop)

    def _graceful_stop(self, *_: Any) -> None:
        log.info("stop signal received")
        self._stop = True

    def refresh_active_contracts(self) -> None:
        """Refresh the list of open KXBTC15M events every 30s. Contracts
        roll every 15 min so this cadence is plenty."""
        if time.time() - self._contracts_refresh_ts < 30:
            return
        try:
            markets = list_active_contracts(self.client)
            self._active_events = group_by_event(markets)
            self._contracts_refresh_ts = time.time()
            log.info("active events: %d  (total strikes: %d)",
                     len(self._active_events), len(markets))
        except Exception as e:
            log.warning("refresh_active_contracts failed: %s", e)

    def tick(self) -> None:
        self.refresh_active_contracts()
        if not self._active_events:
            return
        btc = get_btc_spot()
        if btc is None:
            return
        ts_ms = int(time.time() * 1000)
        rows: list[tuple] = []
        jsonl_lines: list[str] = []

        for ev_ticker, markets in self._active_events.items():
            atm = select_atm_band(markets, btc, STRIKE_BAND)
            for m in atm:
                ticker = m.get("ticker", "")
                strike = parse_strike_from_ticker(ticker)
                if strike is None:
                    continue
                close_ts = iso_to_ms(m.get("close_time"))
                mtc = None
                if close_ts is not None:
                    mtc = max(0.0, (close_ts - ts_ms) / 60000.0)
                ob = fetch_orderbook(self.client, ticker)
                if ob is None:
                    continue
                tob = top_of_book(ob)
                snap = BookSnapshot(
                    ts_ms=ts_ms, ticker=ticker, strike=strike,
                    contract_close_ts_ms=close_ts, minutes_to_close=mtc,
                    btc_price=btc, **tob,
                )
                rows.append(snap.as_row())
                jsonl_lines.append(json.dumps(snap.as_dict(), separators=(",", ":")))

        if rows:
            self.db.executemany(
                """INSERT INTO book_snapshots
                   (ts_ms, ticker, strike, contract_close_ts_ms, minutes_to_close,
                    yes_bid, yes_ask, yes_bid_size, yes_ask_size,
                    no_bid, no_ask, btc_price)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        if jsonl_lines:
            try:
                with open(JSONL_PATH, "a") as f:
                    for line in jsonl_lines:
                        f.write(line + "\n")
            except Exception as e:
                log.warning("jsonl append failed: %s", e)

        log.info("tick: btc=%.2f events=%d rows=%d",
                 btc, len(self._active_events), len(rows))

    def run(self) -> None:
        log.info("book_collector starting | db=%s interval=%.1fs band=ATM±%d",
                 DB_PATH, POLL_INTERVAL_S, STRIKE_BAND)
        next_tick = time.time()
        while not self._stop:
            try:
                self.tick()
            except Exception as e:
                log.exception("tick error: %s", e)
            next_tick += POLL_INTERVAL_S
            sleep_s = next_tick - time.time()
            if sleep_s < 0:
                # Fell behind — reset the schedule
                next_tick = time.time()
            else:
                time.sleep(sleep_s)
        log.info("book_collector stopped")


if __name__ == "__main__":
    Collector().run()
