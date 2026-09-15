# Schwab FastAPI Skeleton

A minimal FastAPI service that handles Charles Schwab OAuth, runs a live K/D-signal monitor against a watchlist during market hours, places orders (with pre-trade risk checks), and shows all of it on a built-in dashboard.

## Project layout
```
app/
  main.py                 # FastAPI app; wires up routers, starts/stops the monitor loop and activity log DB
  core/config.py          # Settings loaded from env/.env via pydantic (dry-run, watchlist, risk, poll cadence)
  core/activity_log.py    # SQLite-backed log of every order/signal event (activity_log.db)
  core/order_executor.py  # submit_order(): shared preview/live + logging path for manual and automated orders
  core/monitor.py         # Background loop: polls watchlist during market hours, computes K/D, trades within risk limits
  clients/schwab.py        # Schwab API client: token refresh, code exchange, orders, account balances, price history
  routers/accounts.py      # GET /accounts/login — starts the OAuth flow
  routers/auth.py          # GET /accounts/callback — exchanges code for tokens
  routers/orders.py        # POST /orders/{account_id} — places an order, logs every attempt/result
  routers/dashboard.py     # GET /dashboard, GET /api/activity, GET /api/monitor/status
  schemas/order.py         # OrderRequest/OrderLeg pydantic models
  strategy/kd.py           # Pure-Python stochastic %K/%D + crossover signal (BUY/SELL/HOLD)
  templates/dashboard.html # Dashboard page (vanilla HTML/JS, polls the two API endpoints above)
watchlist.json           # Symbols the monitor loop evaluates every poll cycle
requirements.txt
.env
cert.pem / key.pem      # local TLS certs for the HTTPS redirect URI
activity_log.db          # created on first run; gitignored
```

## Setup
1) Python 3.10+ recommended. Install deps:
```bash
pip install -r requirements.txt
```
2) Fill in Schwab credentials and account details in `.env`.
3) Start the server:
```bash
python run.py
```
Reads host/port/TLS from `SCHWAB_REDIRECT_URI` in `.env`, starts uvicorn with `--reload`, and automatically opens `/accounts/login` in your browser once the server is up. Equivalent manual command:
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --ssl-certfile cert.pem --ssl-keyfile key.pem --reload
```
Visiting the server's root URL (`/`) also redirects to `/accounts/login`.

## API
- `GET /health` — basic health check with configured default account id
- `GET /accounts/login` — returns/renders the Schwab authorize URL to start login
- `GET /accounts/callback?code=...&state=...` — OAuth redirect target; exchanges `code` for access/refresh tokens, persists them to `.schwab_tokens.json`, and updates `SCHWAB_REFRESH_TOKEN` in `.env`
- `POST /orders/{account_id}` — places an order; see [Orders & the dry-run flag](#orders--the-dry-run-flag)
- `GET /dashboard` — activity viewer; see [Dashboard](#dashboard)
- `GET /api/activity` — JSON feed behind the dashboard (`limit`, `symbol`, `status`, `event_type` query params)
- `GET /api/monitor/status` — current monitor state (market open/closed, manual pause, circuit breaker, last poll, per-symbol close/%K/%D/signal)
- `POST /api/monitor/pause` / `POST /api/monitor/resume` — emergency stop: pause/resume all automated trading (BUY/SELL/stop-loss) without restarting the server
- `GET /api/portfolio` — current buying power, account value, and held positions (qty, average cost, current price, market value, unrealized P&L) straight from Schwab's account endpoint

`OrderRequest` payload (example):
```json
{
  "orderType": "MARKET",
  "session": "NORMAL",
  "duration": "DAY",
  "orderStrategyType": "SINGLE",
  "orderLegCollection": [
    {"instruction": "BUY", "quantity": 10, "instrument": {"symbol": "AAPL", "assetType": "EQUITY"}}
  ]
}
```

## Orders & the dry-run flag
`SCHWAB_DRY_RUN` (in `.env`, defaults to `true`) gates whether `POST /orders/{account_id}` actually calls Schwab:
- **`true` (default)**: the order is logged but never sent — the response has `"dry_run": true`. Safe to test against.
- **`false`**: the order is sent to Schwab for real. Only flip this once you've verified behavior through dry runs and the dashboard.

Every call — dry-run or live — writes an `order_submitted` event and an `order_result` event (success/failed/error) to the activity log.

## Dashboard
`GET /dashboard` (e.g. `https://127.0.0.1:8000/dashboard`) has three parts:
- **Portfolio** — buying power, total account value, and every held position with average cost, live current price, market value, and unrealized P&L ($ and %), pulled straight from Schwab's account endpoint (`GET /api/portfolio`).
- **Live Monitor** — market open/closed badge, circuit-breaker badge, a Pause/Resume button, last poll time + today's P&L, and a per-symbol snapshot (close, %K, %D, current signal) from `app/core/monitor.py`'s in-memory state.
- **Activity table** — every order attempt and its outcome — status (`success`/`failed`/`error`/`dry_run`/`sent`/`skipped`/`BUY`/`SELL`/`HOLD`), symbol, account, and the full request/response as expandable JSON. Filter by symbol, status, or event type.

Both auto-refresh every 5s. Data comes from `activity_log.db` (SQLite, created automatically on startup) via `app/core/activity_log.py`; there's nothing else to configure.

## Live monitor & auto-trading
`app/core/monitor.py` runs as a background task for the life of the server (started/stopped in `app/main.py`'s lifespan). Every `MONITOR_POLL_SECONDS` (default 60) while the market is open (Mon-Fri 09:30-16:00 America/New_York — **does not account for market holidays**), it:
1. Reads symbols from `watchlist.json` (`WATCHLIST_PATH` to change the file).
2. Takes one account snapshot (balances + positions) and runs the risk controls below (circuit breaker, stop-loss) across *everything currently held*, not just watchlist symbols.
3. Fetches today's 1-minute bars per watchlist symbol and computes stochastic %K/%D (14/5 periods, in `app/strategy/kd.py`).
4. Derives a signal: **BUY** when %K crosses above %D while oversold (<20), **SELL** when %K crosses below %D while overbought (>80), else **HOLD**. Every check is logged as a `signal_check` event.
5. On a BUY/SELL that differs from the last signal already acted on for that symbol (so it won't refire every poll while the crossover state persists), it applies the per-trade risk checks before trading.

Orders that clear all risk checks go through the same `submit_order()` path as the manual `/orders` endpoint — so they respect `SCHWAB_DRY_RUN` identically (dry-run sends to Schwab's `previewOrder`, live sends a real order) and log identically, tagged `"source": "monitor"` / `"stop_loss"` vs `"manual"` in the detail JSON.

### Risk controls
| Control | Env var (default) | Behavior |
|---|---|---|
| Per-trade spend cap | `SCHWAB_MAX_POSITION_PCT` (0.05) | A BUY spends at most this fraction of current buying power. `quantity = floor(buying_power * pct / close)`; if that's 0, skipped (`insufficient_funds`) — nothing sent to Schwab. |
| Per-symbol exposure cap | `SCHWAB_MAX_SYMBOL_EXPOSURE_PCT` (0.15) | Across repeated BUY signals, one symbol's *total* position value is capped at this fraction of account value. The buy quantity is shrunk to fit remaining headroom, or skipped entirely (`symbol_exposure_cap_reached`) if there's none left — prevents averaging down without limit. |
| Stop-loss | `SCHWAB_STOP_LOSS_PCT` (0.10) | Every poll, any held position down more than this fraction from average cost is force-sold at market, independent of the K/D signal (logged as `stop_loss_triggered`). Fires at most once per symbol per day. Bypasses the circuit breaker (see below) but not manual pause. |
| Daily loss circuit breaker | `SCHWAB_DAILY_LOSS_LIMIT_PCT` (0.03) | If the sum of today's per-position floating P&L drops below this fraction of account value, new BUY/SELL trades pause for the rest of the trading day (`circuit_breaker` event, `status: tripped`) — stop-loss exits still run. Auto-resets the next calendar day. Note: this sums Schwab's per-position `currentDayProfitLoss`, which doesn't capture P&L already realized by positions closed earlier today. |
| Sell-what-you-hold | *(always on)* | SELL only ever sells the quantity actually held; if there's no open position, it's skipped (`no_position_to_sell`). The bot never shorts. |
| Manual pause | Dashboard button / `POST /api/monitor/pause` | Immediately blocks every automated action — BUY, SELL, and stop-loss — without touching `.env` or restarting. `POST /api/monitor/resume` (or the dashboard button again) re-enables it. **In-memory only**: an app restart (including a `--reload` pickup) clears the pause. For a durable stop, set `SCHWAB_DRY_RUN=true` instead. |

Per-symbol "already acted on this signal" state, the stop-loss daily-fire flag, the manual pause, and the circuit breaker are all in-memory only and reset on restart (including `--reload`) — acceptable for a personal dev setup, worth hardening (persist to `activity_log.db`) before leaving it running unattended for long stretches.

`extract_buying_power()` / `extract_equity()` (`app/core/account.py`) try several known Schwab balance field names in order; verified against a real margin account's response during development, but double-check against yours if it's a cash account (visible in `order_skipped`/`account_lookup_failed` detail on the dashboard).

## Notes
- `SCHWAB_BASE_URL` defaults to `https://api.schwabapi.com`; swap to the sandbox base if provided by Schwab.
- If your Schwab app lists a callback like `https://127.0.0.1:8000/accounts/callback`, run the FastAPI server on that host/port and use the same URL in Schwab app settings. If you're not terminating TLS locally, set the callback to `http://127.0.0.1:8000/accounts/callback` to match the protocol you actually use.
- Order legs require a nested `instrument: {symbol, assetType}` object (see the `OrderRequest` example above) — Schwab's API rejects a flat `symbol`/`assetType` on the leg itself with a generic validation error.
