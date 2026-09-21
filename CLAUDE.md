# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI service that authenticates against the Charles Schwab trading API, runs a fully
automated **KD (stochastic %K/%D) golden-cross/death-cross swing strategy** (one position at a
time), places real broker-side stop orders, and exposes a dashboard + JSON API for monitoring.
It runs 24/7 on a Vultr VPS via systemd, auto-deployed from `main` — see
`docs/VPS_OPERATIONS.md` (gitignored, local-only) for connection details/ops runbook.

**ORB (Opening Range Breakout) was ripped out** (`623afc1`, `3ed8f19`) after backtesting showed
only the KD rule had a demonstrated edge (~2,300 trades). `README.md` still describes the old
ORB-based architecture in detail — treat it as historical/inaccurate for anything strategy- or
dashboard-related; trust the code (esp. `app/core/kd_watchlist.py`, `app/core/monitor.py`) over
the README.

## Commands

```bash
pip install -r requirements.txt
python run.py                 # dev server: reads host/port/TLS from SCHWAB_REDIRECT_URI in .env,
                               # starts uvicorn --reload, opens /accounts/login in the browser
```
Manual equivalent: `uvicorn app.main:app --host 127.0.0.1 --port 8000 --ssl-certfile cert.pem --ssl-keyfile key.pem --reload`

There is no test suite and no lint config in this repo. `scripts/*.py` (all gitignored, never
deployed) are one-off offline analysis tools, run directly with `python scripts/<name>.py`; several
take `--days-ago` / `--top` flags — check the file's docstring before running one.

On the VPS: don't `python run.py` manually — it's `systemctl {status,restart,stop,start}
schwab-trading`, auto-deployed every 5 min from `origin/main` by `schwab-deploy.timer`. Full
command list in `docs/VPS_OPERATIONS.md`.

## Architecture

### Two independent background loops, started in `app/main.py`'s lifespan

1. **`app/core/monitor.py::run_forever()`** — polls every `MONITOR_POLL_SECONDS` (default 60)
   while the market is open (Mon-Fri 09:30-16:00 America/New_York, no holiday awareness). Each
   poll: account-wide risk controls (circuit breaker, blanket stop-loss — strategy-agnostic, run
   no matter what bought the position) + `kd_watchlist.check_intraday_exit()` (death-cross/stop
   re-checked off today's still-forming daily bar, not just once a day).
2. **`app/core/data_pipeline.py::run_daily_forever()`** — once a day at `PRICE_HISTORY_RUN_TIME_ET`
   (default 04:30 ET, before market open): builds a candidate universe (Schwab movers + fixed
   `sp500_constituents.json`), backfills daily OHLCV into Postgres (`price_history` table), backfills
   fundamentals, then calls `kd_watchlist.recommend_action()` — the **authoritative** once-daily
   BUY/SELL/HOLD/NONE decision, which submits the real order itself.

Both loops are independent of Postgres being configured: if `PG_DSN` is unset, `price_history_db`
no-ops and KD is effectively disabled (`kd_watchlist_enabled` checks `price_history_db.is_ready()`
everywhere) — nothing crashes, the app just runs with monitoring/risk-controls only.

### KD strategy (`app/core/kd_watchlist.py` + `app/strategy/kd.py`)

- **Entry**: golden cross (K crosses above D while K < 20) on the daily bar. If multiple symbols
  cross the same day, a `LogisticRegression` trained fresh each run (on all history, scored using
  the prior day's state) picks the one most likely to have crossed — a tie-break only, not a
  timing signal (see the module's long docstring for the reasoning and `scripts/backtest_kd_predict.py`
  for the offline validation, ~0.97 AUC).
- **Sizing**: `BUY_ALLOCATION_PCT` (0.97) of current buying power / signal-day close as a price
  estimate (order is a MARKET order, so real fill price is unknown ahead of time).
- **Stop**: `entry_price - ATR_STOP_MULT(2.0) * ATR(14)`. v2 places a **real resting SELL STOP
  order at the broker** (`GOOD_TILL_CANCEL`), not just a value re-checked once a day — see
  `_stop_order()`.
- **Exit**: death cross (K crosses below D while K > 80) or the stop being hit, checked once daily
  off the finalized close (`recommend_action`) *and* every monitor poll off today's still-forming
  bar (`check_intraday_exit`) — the intraday check exists so a death cross/stop-pierce is acted on
  within ~a minute, not up to a day later.
- **State**: always re-derived from Schwab's own account data (buying power, current holdings) at
  call time, never trusted from `activity_log` alone — `activity_log`'s `kd_entry`/`kd_exit` rows
  exist only so "are we holding, what did we pay, what's the stop" survives a process restart.
- `SCHWAB_DRY_RUN` (`.env`) is what stands between this and real fills — dry-run sends every order
  to Schwab's `previewOrder` (validated, never executed); it gates KD identically to manual orders.
  There is deliberately no separate "go live" step once `SCHWAB_DRY_RUN=false` and a golden cross fires.

### Order path (single choke point)

Both the manual `POST /orders/{account_id}` endpoint and every automated order (KD entry/exit/stop,
blanket stop-loss) go through **`app/core/order_executor.py::submit_order()`** — the one place that
checks `SCHWAB_DRY_RUN`, calls Schwab, and logs `order_submitted` + `order_result` to
`activity_log.db` (SQLite). Anything touching order placement should go through this function, not
call `SchwabClient` directly, to keep dry-run gating and logging consistent.

### Risk controls (account-wide, strategy-agnostic, all in `app/core/monitor.py`)

Blanket stop-loss (`SCHWAB_STOP_LOSS_PCT`, force-sells any position down that much from average
cost) and a daily-loss circuit breaker (`SCHWAB_DAILY_LOSS_LIMIT_PCT`, pauses new BUY/SELL for the
rest of the day) apply across *everything held*, independent of what bought it. Manual pause
(`POST /api/monitor/pause`) blocks everything except EOD-type protective exits; both pause and
circuit-breaker state are **in-memory only** — reset on restart (including `--reload`).

### Data layer

- `activity_log.db` (SQLite, via `app/core/activity_log.py`) — every order/signal/risk event; feeds
  the dashboard's activity table and `GET /api/activity`. Created automatically on startup.
- Postgres (`PG_DSN`, via `app/core/price_history_db.py` + `app/core/fundamentals_db.py`) — daily
  OHLCV (`price_history` table) and daily fundamentals snapshots. Optional; KD and the backtest
  scripts depend on it, monitoring/risk-controls don't.
- `app/clients/schwab.py::SchwabClient` — all Schwab API calls (OAuth refresh, orders, account,
  quotes, price history, movers, fundamentals) go through here; self-throttles to 110 req/min
  against Schwab's 120/min limit.

### Routers

`app/routers/{accounts,auth}.py` handle the OAuth login/callback (persists tokens to
`.schwab_tokens.json` + rewrites `SCHWAB_REFRESH_TOKEN` in `.env`, and hot-applies the new token to
the running process — no restart needed). `orders.py` is the manual order endpoint. `dashboard.py`
serves `GET /dashboard` (static HTML polling the JSON endpoints) plus `/api/activity`,
`/api/monitor/{status,pause,resume}`, `/api/portfolio`, `/api/price-history/{run-now,status}`.
`line_webhook.py` is a one-time-use endpoint for discovering your own `LINE_USER_ID` (see
`app/core/line_notify.py` for outbound risk-event push notifications — `stop_loss_triggered`,
KD `stop_hit`/death-cross exits, circuit breaker trip/reset, order failures).

## Working in this repo

- `scripts/` is entirely gitignored — local-only offline backtests/analysis, never deployed and
  never wired into any live code path. They intentionally duplicate small bits of production logic
  (e.g. `kd_watchlist._market_order`) rather than import module-private helpers.
- `watchlist.json` is also gitignored on the VPS side (see `docs/VPS_OPERATIONS.md`) even though
  it's git-tracked in this repo — the VPS's auto-deploy does `git reset --hard`, so editing it
  locally and pushing is the only way changes stick.
- Order legs require a nested `instrument: {symbol, assetType}` object — Schwab rejects a flat
  `symbol`/`assetType` on the leg itself with a generic validation error.
- Config is centralized in `app/core/config.py` (pydantic-settings, reads `.env`); `get_settings()`
  is `lru_cache`d and only invalidated by `auth.py`'s callback on a fresh login, so other env
  changes need a process restart to take effect.
