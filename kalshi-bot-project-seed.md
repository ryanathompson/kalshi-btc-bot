# Kalshi BTC Bot — Project Instructions

## What This Project Is
A Python trading bot deployed on Render that trades Kalshi BTC 15-minute prediction markets (`KXBTC15M` series) using two strategies running in parallel. The repo is `ryanathompson/kalshi-btc-bot` on GitHub, `main` branch.

## Repo Structure
```
kalshi-btc-bot/
├── bot.py               # Main bot loop + both strategies + KalshiClient
├── app.py               # Flask dashboard / status API
├── Procfile             # Render process config
├── requirements.txt
├── gunicorn.conf.py
└── templates/           # Dashboard HTML templates
```

`bot_trades.json` is written at runtime on Render (ephemeral — resets on redeploy).

## API Details
- **Base URL:** `https://api.elections.kalshi.com/trade-api/v2`
- **Order endpoint:** `POST /portfolio/orders`
- **Auth:** RSA private key + API key ID
- **Market series:** `KXBTC15M` (BTC 15-minute markets)

## Strategies

**LAG** — detects when Kalshi contract prices haven't caught up to a sharp BTC move on Coinbase. Trades into the mispricing window before the market reprices.

**CONSENSUS** — combines two signals:
- MOMENTUM: BTC price direction over the last 60 seconds
- PREVIOUS: result of the last settled 15-min market
Only fires when both signals agree AND price is ≤ threshold. Historically ~73% win rate in backtests.

## Environment Variables
```
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY_BASE64=      # base64-encoded .pem contents
KALSHI_PRIVATE_KEY_PATH=./kalshi.key
LAG_STAKE=25                    # dollar stake per LAG trade
CONSENSUS_STAKE=25              # dollar stake per CONSENSUS trade
DAILY_LOSS_LIMIT=100
DRY_RUN=false
```

## Risk Manager
- Halts trading if daily P&L loss exceeds `DAILY_LOSS_LIMIT`
- Caps open trades at `max_open` (default 3)
- Checks balance before each cycle; halts if balance < $10
- `resolve_trades()` runs every 60s to settle open positions against Kalshi API

## Known Bugs Fixed (commit `455377f`)
1. **Wrong API path** — `place_order()` was calling `/trade-api/v2/portfolio/orders`, which doubled the path prefix since BASE_URL already contains `/trade-api/v2`. Fixed to `/portfolio/orders`.
2. **Ghost trade logging** — `_log_signal()` was called even when `order_result` contained an error key, creating phantom open trades in `bot_trades.json`. Fixed by checking for errors before logging.

## Deployment Notes (Render)
- Filesystem is ephemeral — `bot_trades.json` resets on every redeploy
- Render auto-deploys on push to `main`
- Ghost trades auto-clear when their market settles via `resolve_trades()`, or on next redeploy
- Dashboard exposed via Flask / gunicorn on the Render web service URL

## Upload Method (for future bot.py updates)
GitHub's file editor uses CodeMirror 6. To replace content programmatically via browser console:
```javascript
const view = document.querySelector('.cm-content').cmTile.view;
view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: decodedText}});
```
Large files are base64-encoded, injected in chunks via `window._b64 += "..."`, then decoded with `atob()` before dispatch.
