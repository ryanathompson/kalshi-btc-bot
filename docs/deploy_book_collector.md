# Deploying `book_collector.py` as a Render Background Worker

This runs the passive KXBTC15M orderbook collector alongside the live
trading bot. It is **read-only** against Kalshi — it never places,
modifies, or cancels orders — so it is safe to run at the same time.

## What it does

Every `POLL_INTERVAL_S` seconds (default 3s), for every active
`KXBTC15M` contract currently open on Kalshi, it snapshots the top of
book on the ATM ± `STRIKE_BAND` strikes (default ±3, so 7 strikes per
contract), together with Coinbase BTC/USD spot, and writes one row per
strike to SQLite *and* appends a JSONL line to a tail log.

Rate budget at defaults (1 active contract, 7 strikes, 3s poll):
~7 orderbook GETs + 1 `/markets` list every 30s + 1 Coinbase GET per
tick ≈ 3 req/s sustained. Well under Kalshi's limits and under
Coinbase's anonymous-IP limits.

## Files it uses

- Imports `KalshiClient` and `load_private_key` from `bot.py` directly.
  If `bot.py` moves, `book_collector.py` must move with it.
- Writes SQLite to `BOOK_DB_PATH` (default `/tmp/book_data.db`).
- Appends JSONL to `BOOK_JSONL_PATH` (default `/tmp/book_data.jsonl`).

`/tmp` is **ephemeral** on Render — it is wiped on every redeploy and
can be evicted under memory pressure. That's fine for a 24-hour
measurement run, but pull the `.db` off the worker before you push a
new deploy, or you will lose the data.

For a long-running collector, attach a Render Disk and point
`BOOK_DB_PATH` at a path on the disk (e.g. `/var/data/book_data.db`).

## Render service configuration

1. Go to the Render dashboard → **New → Background Worker**.
2. Select the same GitHub repo as the main `kalshi-btc-bot` web service.
3. Fill in:
   - **Name**: `kalshi-btc-book-collector`
   - **Branch**: `main`
   - **Runtime**: `Python`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python book_collector.py`
   - **Instance Type**: `Starter` is fine (the worker uses ~50 MB RAM).
4. Add **environment variables** — these must match the main bot's:
   - `KALSHI_API_KEY_ID`
   - `KALSHI_PRIVATE_KEY_PEM` *(preferred — paste the PEM body,
     literal `\n` is fine; the loader normalizes it)*
     — or `KALSHI_PRIVATE_KEY_BASE64` — or `KALSHI_PRIVATE_KEY_PATH`
   - `POLL_INTERVAL_S` (optional, default `3`)
   - `STRIKE_BAND` (optional, default `3`)
   - `BOOK_DB_PATH` (optional, default `/tmp/book_data.db`)
   - `BOOK_JSONL_PATH` (optional, default `/tmp/book_data.jsonl`)
5. Click **Create Background Worker**. Render builds, starts, and tails
   logs automatically.

## Verifying it is running

In the Render dashboard → **Logs** you should see lines like:

    2026-04-11 14:02:17,433 [INFO] book_collector starting | db=/tmp/book_data.db interval=3.0s band=ATM±3
    2026-04-11 14:02:17,801 [INFO] active events: 1  (total strikes: 21)
    2026-04-11 14:02:18,112 [INFO] tick: btc=84512.30 events=1 rows=7

If you see `refresh_active_contracts failed` or `coinbase spot failed`
occasionally, that's fine — the next tick will retry. If you see
either on *every* tick, check network egress / API key validity.

## Pulling the data off for analysis

After letting it run for ~24 hours, open the Render **Shell** on the
worker and:

    # Quick sanity — how many rows have we recorded?
    sqlite3 /tmp/book_data.db 'select count(*), min(ts_ms), max(ts_ms) from book_snapshots;'

    # Spot check a slice
    sqlite3 /tmp/book_data.db 'select ts_ms, ticker, yes_bid, yes_ask, btc_price from book_snapshots order by ts_ms desc limit 10;'

Download the file from the Shell UI, then locally:

    python analyze_book_data.py book_data.db

The analyzer produces a GO / MARGINAL / NO_GO / RERUN verdict plus
spread-by-minute-to-close and stale-residence histograms.

## Stopping it

Render → the worker service → **Suspend** (keeps env and disk) or
**Delete** (wipes everything). If you only want to pause for a redeploy
of the main bot, Suspend is enough — the collector is completely
decoupled from the web service.

## Gotchas

- **Do not share DB path with the main bot**. Nothing in the main bot
  writes to `book_data.db`, but keep them separate anyway so a bug in
  one can never corrupt the other's file.
- **`/trade-api/v2` prefix**: `book_collector.py` calls `client.get()`
  with paths like `/markets` and `/markets/{ticker}/orderbook` — NOT
  `/trade-api/v2/markets`. `KalshiClient.url` already contains the
  prefix. If you see 404s on otherwise-valid tickers, check that you
  haven't added the prefix back in.
- **Redeploy wipes `/tmp`**. If you are mid-run and need to redeploy
  the main web service, that is fine — the worker is a separate
  service. But if you redeploy *the worker*, the SQLite file is gone.
  Pull it first.
