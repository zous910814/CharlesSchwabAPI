# Schwab FastAPI Skeleton

A FastAPI service that authenticates against the Charles Schwab trading API, runs a fully automated KD (stochastic %K/%D golden-cross/death-cross) swing strategy (one position at a time), places real broker-side stop orders, and exposes a dashboard + JSON API for monitoring. Runs 24/7 on a VPS via systemd, auto-deployed from `main`.

## Project layout
| Path | Description |
| --- | --- |
| `app/main.py` | FastAPI app; wires up routers, starts/stops the monitor loop and the daily data-pipeline loop, opens the activity log DB |
| `app/core/config.py` | Settings loaded from env/.env via pydantic-settings (dry-run, risk limits, KD/ATR params, Postgres DSN, LINE creds) |
| `app/core/activity_log.py` | SQLite-backed log of every order/signal/risk event (`activity_log.db`); feeds the dashboard's activity table |
| `app/core/order_executor.py` | `submit_order()`: single choke point for manual + automated orders — dry-run gate, Schwab call, logging |
| `app/core/monitor.py` | `run_forever()`: polls every `MONITOR_POLL_SECONDS` while the market is open; blanket stop-loss, daily-loss circuit breaker, KD intraday exit check |
| `app/core/data_pipeline.py` | `run_daily_forever()`: once daily pre-market, builds the candidate universe, backfills price history + fundamentals into Postgres, then calls `kd_watchlist.recommend_action()` |
| `app/core/kd_watchlist.py` | Authoritative once-daily BUY/SELL/HOLD/NONE decision for the KD strategy; submits real entry/exit/stop orders itself |
| `app/core/account.py` | Parses Schwab account responses: buying power, equity, positions, day P&L |
| `app/core/pnl.py` | Derives realized P&L per round trip from Schwab transactions (pairs each opening buy against its closing sell) |
| `app/core/price_history_db.py` | Postgres-backed daily OHLCV storage (`price_history` table); no-ops if `PG_DSN` is unset |
| `app/core/fundamentals_db.py` | Postgres-backed daily fundamentals snapshot, one row per (symbol, day) |
| `app/core/deps.py` | Shared `SchwabClient` FastAPI dependency |
| `app/core/line_notify.py` | LINE push notifications for risk events + webhook signature verification |
| `app/clients/schwab.py` | Schwab API client: token refresh, code exchange, orders, account, quotes, price history, movers, fundamentals; self-throttles to 110 req/min |
| `app/routers/accounts.py` | `GET /accounts/login` — starts the OAuth flow |
| `app/routers/auth.py` | `GET /accounts/callback` — exchanges code for tokens, persists them, hot-applies the new token without a restart |
| `app/routers/orders.py` | `POST /orders/{account_id}` — manual order endpoint, goes through `order_executor.submit_order` |
| `app/routers/dashboard.py` | `GET /dashboard` plus `/api/activity`, `/api/monitor/{status,pause,resume}`, `/api/portfolio`, `/api/candles/{symbol}`, `/api/pnl/monthly`, `/api/price-history/{run-now,status}` |
| `app/routers/line_webhook.py` | `POST /line/webhook` — one-time-use endpoint for discovering your own `LINE_USER_ID` |
| `app/schemas/order.py` | `OrderRequest`/`OrderLeg`/`Instrument` pydantic models |
| `app/strategy/kd.py` | `compute_kd()`: pure-Python stochastic %K/%D indicator calculation |
| `app/templates/dashboard.html` | Dashboard page (vanilla HTML/JS, polls the API endpoints above) |
| `watchlist.json` | Git-tracked but gitignored on the VPS side — the VPS's auto-deploy does `git reset --hard`, so editing locally and pushing is the only way changes stick |
| `sp500_constituents.json` | Fixed S&P 500 constituent list, one input to the daily candidate universe alongside Schwab movers |
| `scripts/` | Gitignored, local-only offline backtest/analysis tools; never deployed or imported by live code |
| `docs/stock trading strategy.md` | The old ORB spec — ORB has been ripped out; kept only as historical reference |
| `docs/VPS_OPERATIONS.md` | VPS deployment/ops runbook (gitignored, local reference only) |
| `docs/BACKTEST_REPORT.md` | Watchlist regression + KD backtest findings |
| `requirements.txt` | Python dependencies |
| `.env` | Environment/config values |
| `cert.pem` / `key.pem` | Local TLS certs for the HTTPS redirect URI |
| `activity_log.db` | Created on first run; gitignored |

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
- `GET /api/monitor/status` — current monitor state (market open/closed, manual pause, circuit breaker, daily order-value ledger, last poll, per-symbol close/OR/VWAP/RVOL/spread/open position)
- `POST /api/monitor/pause` / `POST /api/monitor/resume` — emergency stop: pause/resume all automated trading (BUY/SELL/stop-loss) without restarting the server
- `GET /api/portfolio` — current buying power, account value, and held positions (qty, average cost, current price, market value, unrealized P&L) straight from Schwab's account endpoint
- `POST /line/webhook` — receives LINE Messaging API events; see [LINE risk notifications](#line-risk-notifications)

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
- **Live Monitor** — market open/closed badge, circuit-breaker badge, a Pause/Resume button, last poll time + today's P&L, and a per-symbol snapshot (close, opening-range low–high, VWAP, RVOL, spread%, and any open ORB position with its current R-multiple) from `app/core/monitor.py`'s in-memory state.
- **Activity table** — every order/strategy event — status (`success`/`failed`/`error`/`dry_run`/`sent`/`skipped`/`BUY`/`HOLD`/`target1`/`target2`/`stop_hit`/`time_stop`/`eod_flatten`/…), symbol, account, and the full request/response as expandable JSON. Filter by symbol, status, or event type.

Both auto-refresh every 5s. Data comes from `activity_log.db` (SQLite, created automatically on startup) via `app/core/activity_log.py`; there's nothing else to configure.

## Live monitor & the ORB strategy
`app/core/monitor.py` runs as a background task for the life of the server (started/stopped in `app/main.py`'s lifespan), implementing the Opening Range Breakout strategy specified in **`docs/stock trading strategy.md`** — read that file for the full rationale; this section covers what's actually implemented and how it maps to config.

Every `MONITOR_POLL_SECONDS` (default 60) while the market is open (Mon-Fri 09:30-16:00 America/New_York — **does not account for market holidays**):
1. Reads symbols from `watchlist.json` (`WATCHLIST_PATH` to change the file); SPY and QQQ are always evaluated too (even if not listed) since they're the market-direction reference for every other symbol.
2. Takes one account snapshot and runs the account-wide risk controls (circuit breaker, blanket stop-loss) across *everything currently held*, not just watchlist symbols.
3. For each symbol, fetches today's 1-minute bars and computes: **opening range** (high/low of the 09:30:00-09:34:59 bars), **VWAP** (cumulative from 09:30, bar-based typical price), **ATR(14)** (simple average of true range, not Wilder-smoothed), and **RVOL** (today's cumulative volume-by-minute vs. the average of the last 20 trading days' curves at the same minute — fetched once per symbol per day and cached).
4. Manages any open position first (see **Exits** below), then — only inside the 09:35-11:00 entry window — fetches a batched real-time quote for the whole watchlist and evaluates each symbol without a position against the **entry rules**.

### Entry rules (long only — see [What's not implemented](#whats-not-implemented))
All of the following must hold (`app/strategy/orb.py::evaluate_long_entry`), logged as a `signal_check` event either way so you can see exactly which condition failed:
1. Latest 1-minute close breaks above the opening-range high.
2. Close is above VWAP.
3. RVOL ≥ `SCHWAB_ORB_RVOL_MIN` (default 1.5).
4. Live bid/ask spread ≤ `SCHWAB_ORB_SPREAD_MAX_PCT` (default 0.05%).
5. Market direction aligned: Nasdaq-linked symbols (`QQQ, AAPL, NVDA, MSFT, AMD, TSLA`) confirm against QQQ's own VWAP; everything else confirms against SPY's.
6. Opening-range width is sane relative to volatility: between `SCHWAB_ORB_OR_WIDTH_MIN_ATR` and `SCHWAB_ORB_OR_WIDTH_MAX_ATR` × ATR(14) (default 0.3–1.5×) — rejects gappy/anomalous ranges.

A symbol is also skipped (no entry) if: it already has an open position, it's already hit `SCHWAB_ORB_MAX_TRADES_PER_SYMBOL` (default 2) entries today, today's consecutive-loss count has hit `SCHWAB_ORB_MAX_CONSECUTIVE_LOSSES` (default 3), its quote shows `securityStatus != "Normal"` (halt/anomaly), or another symbol in the same correlation group (see below) already has an open position.

**Position size** is risk-based, then clamped by the existing account-wide caps:
```
stop_price   = entry_price - max(SCHWAB_ORB_STOP_ATR_MULT * ATR, entry_price - OR_High)
risk_shares  = floor(account_equity * SCHWAB_ORB_RISK_PCT / (entry_price - stop_price))
final_shares = min(risk_shares, per-trade cap, per-symbol exposure cap, daily order-value cap)
```
(the last three caps are the same account-wide ones listed in [Risk controls](#risk-controls) below).

### Exits (`app/core/monitor.py::_manage_position`, logged as `orb_exit`)
Checked in this order every poll, on the current 1-minute close, against the *original* stop distance (R doesn't reset when the stop is later moved):
1. **`eod_flatten`** — past 15:55 ET: close the whole position. Never blocked by manual pause (a paused bot should still not hold overnight).
2. **`stop_hit`** — close ≤ stop price: close the whole position. Blocked only by manual pause (bypasses the circuit breaker, matching the account-wide stop-loss's precedent).
3. **`back_in_range`** — close drops back below the opening-range low: close the whole position.
4. **`vwap_cross_back`** — close drops back below VWAP: close the whole position.
5. **`target2`** — R-multiple ≥ `SCHWAB_ORB_TARGET2_R` (default 1.5): close whatever remains.
6. **`target1`** — R-multiple ≥ `SCHWAB_ORB_TARGET1_R` (default 1.0), not already taken, and more than 1 share held: sell half (rounded down), move the stop to breakeven on the rest. A 1-share position skips straight to target2/stop/time instead of trying to split.
7. **`time_stop`** — open ≥ `SCHWAB_ORB_TIME_STOP_MINUTES` (default 15) and still below `SCHWAB_ORB_TIME_STOP_R` (default 0.5): close the whole position.

Before every exit sell, the quantity is clamped to whatever Schwab actually reports as held (not just our own tracked count) — if it's already zero, the tracked position is cleared instead of erroring.

Orders that clear all checks go through the same `submit_order()` path as the manual `/orders` endpoint — so they respect `SCHWAB_DRY_RUN` identically (dry-run sends to Schwab's `previewOrder`, live sends a real order) and log identically, tagged `"source": "orb_entry"` / `"orb_<exit_reason>"` / `"stop_loss"` vs `"manual"` in the detail JSON.

### What's not implemented
The spec (`docs/stock trading strategy.md` §7, §10-13) covers more than this pass does — treat these as known gaps, not bugs:
- **Short-selling (§7)** — long-only for now; the spec itself suggests this fallback when reliable short-locate data isn't available, which it isn't here.
- **Correlated-position grouping (§9.4)** — simplified to two static groups (Nasdaq-linked vs. SPY/broad-market) capped at one open position per group, not a real correlation calculation.
- **Halt/anomaly detection (§10)** — best-effort via the quote's `securityStatus` field; no earnings/event calendar exclusion.
- **Execution model (§8)** — market orders only ("Mode A"); no marketable-limit-with-30s-cancel ("Mode B").
- **Granularity** — decisions run on completed 1-minute bars each `MONITOR_POLL_SECONDS` (default 60s), not the 5-second/tick-level timing the spec's stress-test section implies; ATR is a simple moving average, not Wilder-smoothed.
- **Backtesting, walk-forward validation, stress testing, and the 20-day paper-trading rollout (§11-13)** — not built this round; the spec is explicit that going live without them is skipping real diligence, so treat `SCHWAB_DRY_RUN=true` as the current substitute, not a formality.

### Risk controls
| Control | Env var (default) | Behavior |
|---|---|---|
| Per-trade spend cap | `SCHWAB_MAX_POSITION_PCT` (0.05) | A BUY spends at most this fraction of current buying power. `quantity = floor(buying_power * pct / close)`; if that's 0, skipped (`insufficient_funds`) — nothing sent to Schwab. |
| Per-symbol exposure cap | `SCHWAB_MAX_SYMBOL_EXPOSURE_PCT` (0.15) | Across repeated BUY signals, one symbol's *total* position value is capped at this fraction of account value. The buy quantity is shrunk to fit remaining headroom, or skipped entirely (`symbol_exposure_cap_reached`) if there's none left — prevents averaging down without limit. |
| Blanket stop-loss | `SCHWAB_STOP_LOSS_PCT` (0.10) | Every poll, any held position down more than this fraction from average cost is force-sold at market — an account-wide backstop independent of, and looser than, ORB's own ATR-based per-trade stop (logged as `stop_loss_triggered`). Fires at most once per symbol per day. Bypasses the circuit breaker but not manual pause. |
| Daily loss circuit breaker | `SCHWAB_DAILY_LOSS_LIMIT_PCT` (0.03) | If the sum of today's per-position floating P&L drops below this fraction of account value, new BUY/SELL trades pause for the rest of the trading day (`circuit_breaker` event, `status: tripped`) — stop-loss exits still run. Auto-resets the next calendar day. Note: this sums Schwab's per-position `currentDayProfitLoss`, which doesn't capture P&L already realized by positions closed earlier today. |
| Daily order-value cap | `SCHWAB_MAX_DAILY_ORDER_VALUE_PCT` (0.20) | Cumulative BUY notional submitted today can never exceed the account's **actual buying power**, even when Schwab's reported balance hasn't yet caught up with an order just submitted. `app/core/monitor.py`'s `daily_order_value` tracker keeps a FIFO list of unconfirmed BUY reservations and reconciles them on every check: if buying power has dropped since the last check, that drop is assumed to be Schwab catching up with the oldest pending reservations, which are then cleared — so a confirmed buy stops being double-subtracted, and a SELL raising buying power flows straight into more same-day BUY budget. The effective cap is `min(buying_power, account_value * pct)` minus whatever's still unconfirmed-pending; `SCHWAB_MAX_DAILY_ORDER_VALUE_PCT` can only pull that tighter, never past the real balance. Skipped as `daily_order_value_cap_reached` once exhausted; resets the next calendar day. Only counts BUYs — SELLs reduce risk, so they aren't capped. |
| Sell clamped to actual holdings | *(always on)* | Every ORB exit re-checks Schwab's reported position quantity right before selling and clamps to it (never just trusts the internally tracked share count); the bot never shorts. |
| Manual pause | Dashboard button / `POST /api/monitor/pause` | Immediately blocks every automated action — new entries, discretionary exits (target/VWAP-cross/time-stop), and the blanket stop-loss — without touching `.env` or restarting. `eod_flatten` is the one exception and always fires regardless, so a paused bot still won't hold overnight. `POST /api/monitor/resume` (or the dashboard button again) re-enables it. **In-memory only**: an app restart (including a `--reload` pickup) clears the pause. For a durable stop, set `SCHWAB_DRY_RUN=true` instead. |

Per-symbol ORB day-state (opening range, entries-today count, open position, RVOL baseline cache), the stop-loss daily-fire flag, the manual pause, and the circuit breaker are all in-memory only and reset on restart (including `--reload`) — acceptable for a personal dev setup, worth hardening (persist to `activity_log.db`) before leaving it running unattended for long stretches.

`extract_buying_power()` / `extract_equity()` (`app/core/account.py`) try several known Schwab balance field names in order; verified against a real margin account's response during development, but double-check against yours if it's a cash account (visible in `order_skipped`/`account_lookup_failed` detail on the dashboard).

## LINE risk notifications
`app/core/activity_log.py::log_event()` pushes a LINE message via `app/core/line_notify.py` whenever it logs one of these (`app/core/line_notify.py::is_risk_event`):
- `stop_loss_triggered` — the blanket account-wide stop-loss fired
- `orb_exit` with status `stop_hit` — ORB's own ATR-based stop fired
- `circuit_breaker` (`tripped` or `reset`) — the daily loss limit paused/resumed trading
- `daily_trading_halted` — consecutive losses hit `SCHWAB_ORB_MAX_CONSECUTIVE_LOSSES`, no more entries today
- `monitor_error` — the monitor loop itself hit an error (account/quote/price-history lookup failed, etc.)
- `order_result` with status `failed` or `error` — an order was rejected or errored, manual or automated

It's a fire-and-forget background task (`asyncio.create_task`) so a slow or unreachable LINE API never adds latency to the trading loop, and delivery failures are only printed to the console — never re-logged through `log_event` itself, which would risk a retry loop if LINE is what's down. With `LINE_CHANNEL_ACCESS_TOKEN`/`LINE_USER_ID` unset (the default), `send_line_message` is a silent no-op — nothing breaks, you just don't get pushes yet.

### Setup
1. **Create a channel**: go to the [LINE Developers Console](https://developers.line.biz/console/), create a Provider if you don't have one, then create a new **Messaging API** channel under it.
2. **Get the Channel Access Token**: open the channel → **Messaging API** tab → issue a long-lived channel access token. Put it in `.env` as `LINE_CHANNEL_ACCESS_TOKEN`.
3. **Get the Channel Secret**: same channel → **Basic settings** tab → copy the Channel secret. Put it in `.env` as `LINE_CHANNEL_SECRET` (optional but recommended — without it, `/line/webhook` accepts requests without verifying they actually came from LINE).
4. **Add the bot as a friend**: scan the QR code shown on the channel's Messaging API tab from your own LINE app.
5. **Find your own User ID** (LINE doesn't show this anywhere in the console — you have to receive one webhook event to see it):
   - Temporarily expose your local server publicly, e.g. `ngrok http https://127.0.0.1:8000` (or any tunnel tool you prefer) — LINE's servers need to reach the webhook over the internet, `127.0.0.1` won't work directly.
   - In the channel's **Messaging API** tab, set the Webhook URL to `https://<your-tunnel-domain>/line/webhook` and enable "Use webhook".
   - Send the bot any message from your LINE app.
   - Check `GET /api/activity?event_type=line_webhook` (or the dashboard, filtered to that event type) — the logged detail's `source.userId` is your `LINE_USER_ID`. Copy it into `.env`.
   - You can turn the tunnel off after this — outbound risk notifications use the Push Message API and don't need a live webhook at all; the webhook is purely a one-time way to discover your own ID (or leave it running if you might want two-way bot commands later).
6. Restart the server (`python run.py`) so the new `.env` values load, then trigger any risk event (or just wait for one) to confirm a message arrives.

## Notes
- `SCHWAB_BASE_URL` defaults to `https://api.schwabapi.com`; swap to the sandbox base if provided by Schwab.
- If your Schwab app lists a callback like `https://127.0.0.1:8000/accounts/callback`, run the FastAPI server on that host/port and use the same URL in Schwab app settings. If you're not terminating TLS locally, set the callback to `http://127.0.0.1:8000/accounts/callback` to match the protocol you actually use.
- Order legs require a nested `instrument: {symbol, assetType}` object (see the `OrderRequest` example above) — Schwab's API rejects a flat `symbol`/`assetType` on the leg itself with a generic validation error.
