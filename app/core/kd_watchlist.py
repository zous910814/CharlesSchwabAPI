"""Daily KD (stochastic %K/%D) golden/death-cross swing strategy — AUTO-TRADES.

Decides what to do today (buy a specific symbol, sell the current holding,
or nothing) and, unlike the first cut of this module, submits the order
itself through app.core.order_executor.submit_order — the same function and
safety gate the ORB live-trading path uses. THIS MEANS submit_order's
SCHWAB_DRY_RUN gate is what stands between this and real fills: dry_run=true
sends every order to Schwab's previewOrder endpoint only — validated, never
executed — while dry_run=false places it live. As of when this was written,
the VPS's real .env (not tracked by git — see docs/VPS_OPERATIONS.md) already
has SCHWAB_DRY_RUN=false, so this strategy goes live on real fills the moment
this code is deployed there and a golden-cross signal fires — there is no
separate "go live" step after deploying. That was a deliberate, informed
choice made in the conversation this came from, not an accident; if that's
no longer the intent, set SCHWAB_DRY_RUN=true on the VPS before deploying
future changes to this file. A LINE message
(app.core.line_notify.format_kd_recommendation_message) reports what
happened either way.

v2: a live BUY also places a real resting SELL STOP at the broker (see
_stop_order) for the ATR(14)*2 stop, cancelled and replaced by a market
SELL on a death-cross exit. Before this, the stop was only a value this
module rechecked once a day against the prior day's low — real protection
only as timely as the next scheduled run, not the moment the price actually
crossed it intraday.

v3: the stop is no longer placed synchronously right after the BUY. This
module runs once daily at PRICE_HISTORY_RUN_TIME_ET (04:30 ET by default) —
well before the 09:30 ET open — so a NORMAL-session MARKET BUY submitted
here sits WORKING for hours, not seconds; a SELL STOP placed immediately
after it got rejected by Schwab outright, since the account didn't hold the
shares yet (a real production incident, not a hypothetical). The BUY is
still submitted here, but the stop is placed by
monitor.py's poll loop calling ensure_stop_order every ~60s during market
hours, once the account's actual holdings confirm the fill (see
ensure_stop_order's docstring).

Why this exists (see the conversation this came from): across everything
backtested — ORB long/short, a return-prediction regression, this same KD
rule with several variants — app/strategy/kd.py's golden-cross-in-oversold
rule, long-only, was the only one with a demonstrated edge on a large sample
(~2,300 trades). This ports that rule to production, sized for a small
single-position account (one holding at a time, matching the $800-account
backtest that motivated adding a stop) with an ATR(14)*2 stop-loss. NONE of
that backtesting involved a single live or paper trade — this is the first
time the rule has ever touched a real (if preview-only, for now) order.

Same-day tie-break: when more than one symbol golden-crosses on the same
day, the pick is whichever one a LogisticRegression — trained fresh each run
on all available history to predict "will this symbol golden-cross
tomorrow" — rated most likely to cross, scored using ITS STATE FROM THE DAY
BEFORE the actual cross (see scripts/backtest_kd_predict.py for the offline
validation: ~0.97 AUC). That AUC is high mostly because %K/%D's own
trajectory is mechanically predictable, not because it reads future price
action — its only job here is ranking same-day ties, not timing entries.

Position sizing/state always reads Schwab's own account data (buying power,
current holdings) at call time rather than trusting activity_log alone —
same reasoning as monitor.py trusting Schwab's reported positions over any
internally tracked count. activity_log's kd_entry/kd_exit rows exist so the
day-to-day "are we currently holding, what did we pay, what's the stop"
question survives a process restart even between polls of the real account.
scripts/ is gitignored and never deployed, so the feature-engineering here
mirrors (does not import) scripts/backtest_kd_predict.py's build_features.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from app.clients.schwab import SchwabClient
from app.core import activity_log, price_history_db
from app.core.account import extract_buying_power, extract_position_qty, extract_round_trips, parse_positions
from app.core.config import Settings
from app.core.order_executor import submit_order
from app.strategy.kd import compute_kd, kd_signal

logger = logging.getLogger(__name__)
MARKET_TZ = ZoneInfo("America/New_York")

K_PERIOD = 14
D_PERIOD = 5
OVERSOLD = 20.0
OVERBOUGHT = 80.0
ATR_WINDOW = 14
VOLUME_BASELINE_DAYS = 20
ATR_STOP_MULT = 2.0
MIN_CLOSE_PRICE = 5.0

# Trend filter: only take a golden-cross entry if the stock's own close is
# above its 200-day SMA - turns the KD rule from "buy any oversold bounce"
# into "buy oversold pullbacks within an existing uptrend", which is what
# backtests of this kind of rule actually rely on for their edge (a golden
# cross in a downtrend just buys into a continuing slide).
TREND_FILTER_SMA_PERIOD = 200

# Market-level filter: block NEW entries (existing positions still exit
# normally) unless this symbol's own close is above its SMA - a proxy for
# "is the broad market itself in an uptrend". SPY over $SPX/$COMPX etc.
# because it's an ordinary equity ticker, so it backfills through the exact
# same price_history path as every other symbol with no special-casing.
MARKET_FILTER_SYMBOL = "SPY"
MARKET_FILTER_SMA_PERIOD = 60

# Time stop: force-exit a position after this many trading days (counted off
# price_history's own daily-bar calendar, not calendar days) if neither a
# death cross nor the ATR stop has fired by then - a mean-reversion entry
# that hasn't worked out within ~2 trading weeks is more likely chopping
# sideways than "about to work", and tying up the account's one position
# slot indefinitely on a stale signal has its own opportunity cost. Checked
# once daily in recommend_action only (not check_intraday_exit) - unlike a
# death cross or a stop being pierced, how many days a position has been
# held doesn't change intraday, so there's nothing to react to faster than
# once a day here.
TIME_STOP_TRADING_DAYS = 10

# Floor on shares bought, checked against current buying power BEFORE
# picking among same-day golden-cross candidates (see recommend_action).
# The real capital-tiering lever for small accounts: on a ~$1,000 account,
# a $400+ stock golden-crossing yields 1-2 shares even at BUY_ALLOCATION_PCT
# - too coarse for the ATR stop to mean anything and impossible to exit
# partially. % based sizing already scales correctly across account sizes
# on its own; this just keeps it from picking a candidate too expensive for
# the account to size sanely, when a cheaper alternative crossed the same day.
MIN_SHARES = 2

# Fraction of current buying power to spend on a single BUY, sized off
# yesterday's close as a price ESTIMATE (a market order's real fill price
# isn't known yet) — kept below 1.0 so a small overnight move up doesn't
# make the order cost more than the account actually has.
BUY_ALLOCATION_PCT = 0.97

PREDICT_FEATURE_COLUMNS = [
    "k", "d", "k_minus_d", "k_minus_d_change", "k_slope_3d", "days_in_oversold",
    "volume_ratio", "daily_range_pct", "overnight_gap",
]


def _market_order(symbol: str, instruction: str, quantity: float) -> dict:
    """Same shape as app/core/monitor.py:_market_order (duplicated rather
    than imported - that one is module-private to monitor.py's ORB path)."""
    return {
        "orderType": "MARKET",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {"instruction": instruction, "quantity": quantity, "instrument": {"symbol": symbol, "assetType": "EQUITY"}}
        ],
    }


def _stop_order(symbol: str, quantity: float, stop_price: float) -> dict:
    """A real resting SELL STOP at the broker — unlike the once-a-day
    low-vs-stop_price check this module also does, this fires the instant
    Schwab prints a trade at or below stop_price, not the next time the
    daily pipeline happens to run. GOOD_TILL_CANCEL so it survives the many
    days this swing strategy typically holds a position, not just today."""
    return {
        "orderType": "STOP",
        "session": "NORMAL",
        "duration": "GOOD_TILL_CANCEL",
        "orderStrategyType": "SINGLE",
        "stopPrice": f"{stop_price:.2f}",
        "orderLegCollection": [
            {"instruction": "SELL", "quantity": quantity, "instrument": {"symbol": symbol, "assetType": "EQUITY"}}
        ],
    }


def _extract_order_id(order_result: Optional[dict]) -> Optional[str]:
    """submit_order's return: dry-run previews carry no real order id (no
    order was actually created); a live placement's id is the last path
    segment of the Location header submit_order/place_order passes through."""
    if not order_result or order_result.get("dry_run"):
        return None
    location = order_result.get("location")
    if location:
        return location.rstrip("/").rsplit("/", 1)[-1]
    order_id = order_result.get("orderId")
    return str(order_id) if order_id is not None else None


def _add_consecutive_oversold(df: pd.DataFrame) -> pd.DataFrame:
    counts: list[int] = []
    for _, g in df.groupby("symbol", sort=False):
        streak = 0
        for k in g["k"]:
            streak = streak + 1 if pd.notna(k) and k < OVERSOLD else 0
            counts.append(streak)
    df = df.copy()
    df["days_in_oversold"] = counts
    return df


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)

    k_all, d_all = [], []
    for _, g in df.groupby("symbol", sort=False):
        candles = g[["high", "low", "close"]].to_dict("records")
        compute_kd(candles, k_period=K_PERIOD, d_period=D_PERIOD)
        k_all.extend(c["k"] for c in candles)
        d_all.extend(c["d"] for c in candles)
    df["k"] = k_all
    df["d"] = d_all

    g = df.groupby("symbol", group_keys=False)
    df["k_minus_d"] = df["k"] - df["d"]
    df["k_minus_d_change"] = df["k_minus_d"] - g["k_minus_d"].shift(1)
    df["k_slope_3d"] = df["k"] - g["k"].shift(3)

    prev_close = g["close"].shift(1)
    df["volume_ratio"] = df["volume"] / g["volume"].transform(
        lambda s: s.rolling(VOLUME_BASELINE_DAYS, min_periods=VOLUME_BASELINE_DAYS).mean()
    )
    df["daily_range_pct"] = (df["high"] - df["low"]) / df["close"]
    df["overnight_gap"] = df["open"] / prev_close - 1

    # NaN (not enough history yet) compares False against a real close, so
    # trend_ok/market filter checks below naturally fail closed during
    # warm-up without any extra isna() handling.
    df["sma_trend"] = g["close"].transform(
        lambda s: s.rolling(TREND_FILTER_SMA_PERIOD, min_periods=TREND_FILTER_SMA_PERIOD).mean()
    )
    df["trend_ok"] = df["close"] > df["sma_trend"]
    df["sma_market_filter"] = g["close"].transform(
        lambda s: s.rolling(MARKET_FILTER_SMA_PERIOD, min_periods=MARKET_FILTER_SMA_PERIOD).mean()
    )

    true_range = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    df["atr14"] = true_range.groupby(df["symbol"]).transform(
        lambda s: s.rolling(ATR_WINDOW, min_periods=ATR_WINDOW).mean()
    )

    df = _add_consecutive_oversold(df)

    prev_k, prev_d = g["k"].shift(1), g["d"].shift(1)
    has_prev = prev_k.notna() & prev_d.notna() & df["k"].notna() & df["d"].notna()
    df["golden_cross"] = has_prev & (prev_k <= prev_d) & (df["k"] > df["d"]) & (df["k"] < OVERSOLD)
    df["death_cross"] = has_prev & (prev_k >= prev_d) & (df["k"] < df["d"]) & (df["k"] > OVERBOUGHT)

    # Label for the tie-break predictor: did a golden cross happen on the
    # NEXT row - see train_cross_predictor.
    df["label"] = df.groupby("symbol")["golden_cross"].shift(-1)

    return df.replace([np.inf, -np.inf], np.nan)


def train_cross_predictor(df: pd.DataFrame) -> Optional[LogisticRegression]:
    """Trained fresh on ALL available history each run - live use, not
    evaluation (see scripts/backtest_kd_predict.py for the held-out
    validation that found ~0.97 AUC / 15x precision@10 lift)."""
    labeled = df[df["label"].notna() & (df["close"] >= MIN_CLOSE_PRICE)].dropna(subset=PREDICT_FEATURE_COLUMNS)
    if labeled.empty or labeled["label"].nunique() < 2 or len(labeled) < 200:
        return None
    model = LogisticRegression(class_weight="balanced", max_iter=1000)
    model.fit(labeled[PREDICT_FEATURE_COLUMNS], labeled["label"].astype(int))
    return model


async def _pdt_guard(
    settings: Settings, account: dict, position: dict, reason: str, trade_date: str,
) -> Optional[dict]:
    """Defers a same-day KD exit that would push the account over the PDT
    round-trip limit, UNLESS reason is stop_hit - a hard price stop stays
    protective-exits-are-never-blocked (matching monitor.py's blanket
    stop-loss philosophy: losing more capital to dodge a PDT flag is worse
    than the flag). Only death_cross can be deferred, because it's a signal
    exit, not an immediate-loss-prevention one - the resting broker-side
    stop order isn't cancelled while deferred, so real downside is still
    covered; the position just holds one more day and recommend_action's
    once-daily check (a different calendar day - never a day trade) gets
    another shot at exiting it. Only fires same-day: entry and exit on
    different days was never going to be a day trade in the first place."""
    if reason == "stop_hit" or position.get("entry_date") != trade_date:
        return None
    round_trips = extract_round_trips(account)
    if round_trips is None or round_trips < settings.pdt_round_trip_limit:
        return None

    symbol = position["symbol"]
    already = await activity_log.fetch_events(event_type="kd_exit_deferred_pdt", symbol=symbol, limit=1)
    if not (already and (already[0].get("detail") or {}).get("trade_date") == trade_date):
        await activity_log.log_event(
            event_type="kd_exit_deferred_pdt", symbol=symbol, status=reason,
            detail={"round_trips": round_trips, "limit": settings.pdt_round_trip_limit, "trade_date": trade_date},
        )
    return {
        "action": "HOLD", "symbol": symbol, "reason": "pdt_guard_deferred",
        "would_have_exited_for": reason, "trade_date": trade_date,
    }


async def _exit_kd_position(
    settings: Settings, client: SchwabClient, account: dict, position: dict, reason: str, trade_date: str,
) -> dict:
    """Cancels the resting broker-side stop (if any) and market-sells the
    full held quantity. Shared by recommend_action's once-daily SELL branch
    and check_intraday_exit's faster same-day check, so both exit paths stay
    in sync instead of drifting apart."""
    deferred = await _pdt_guard(settings, account, position, reason, trade_date)
    if deferred is not None:
        return deferred

    symbol = position["symbol"]
    shares = extract_position_qty(account, symbol)
    result = {"action": "SELL", "symbol": symbol, "reason": reason, "shares": shares, "trade_date": trade_date}

    # Cancel the resting broker-side stop before market-selling - otherwise a
    # stale SELL STOP for shares we no longer hold stays working. If reason
    # is stop_hit, that order may have already filled/vanished on its own;
    # cancelling a nonexistent order just fails harmlessly here.
    stop_order_id = position.get("stop_order_id")
    if stop_order_id:
        try:
            await client.cancel_order(settings.schwab_account_id, stop_order_id)
        except Exception as exc:  # noqa: BLE001
            logger.info("kd_watchlist: stop-order cancel for %s (%s) did not succeed: %s",
                        symbol, stop_order_id, exc)

    if shares > 0:
        order_body = _market_order(symbol, "SELL", shares)
        try:
            result["order"] = await submit_order(
                client=client, settings=settings, account_id=settings.schwab_account_id,
                order_body=order_body, source="kd_watchlist",
            )
        except Exception as exc:  # noqa: BLE001
            result["order_error"] = str(exc)

    await activity_log.log_event(
        event_type="kd_exit", symbol=symbol, status="signal",
        detail={"reason": reason, "trade_date": trade_date},
    )
    return result


async def check_intraday_exit(settings: Settings, client: SchwabClient) -> Optional[dict]:
    """Same death-cross/ATR-stop exit check recommend_action does once a day
    off last night's finalized close, but run from monitor.py's ~60s poll
    loop off TODAY'S still-forming daily bar instead — so a death cross (or
    a low piercing the stop) gets acted on within a minute or so of
    happening, not up to a day later when the once-daily job next runs.

    Today's bar is built from the day's 1-minute candles so far (open/high/
    low/close as they stand right now), appended after price_history_db's
    already-finalized prior bars, then run through the exact same
    compute_kd/kd_signal used everywhere else. Because that bar keeps
    changing every poll until the real close prints, the death-cross state
    can flicker in and out intraday before settling - this trades a bit of
    whipsaw risk for reacting same-day instead of next-day; the once-daily
    recommend_action check still runs too and is authoritative on the
    finalized bar.
    """
    if not price_history_db.is_ready():
        return None
    position = await _current_kd_position()
    if position is None:
        return None
    symbol = position["symbol"]

    try:
        account = await client.get_account(settings.schwab_account_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kd_watchlist intraday check: account fetch failed: %s", exc)
        return None
    if symbol not in {p["symbol"] for p in parse_positions(account)}:
        return None

    daily_bars = await price_history_db.get_recent_daily_bars(symbol, limit=K_PERIOD + D_PERIOD + 10)
    if len(daily_bars) < K_PERIOD:
        return None

    try:
        history = await client.get_price_history(
            symbol, periodType="day", period=1, frequencyType="minute", frequency=1,
            needExtendedHoursData=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("kd_watchlist intraday check: price history fetch failed for %s: %s", symbol, exc)
        return None
    candles = history.get("candles") or []
    if not candles:
        return None

    today_bar = {
        "high": max(c["high"] for c in candles),
        "low": min(c["low"] for c in candles),
        "close": candles[-1]["close"],
    }
    combined = [*daily_bars, today_bar]
    compute_kd(combined, k_period=K_PERIOD, d_period=D_PERIOD)
    is_death_cross = kd_signal(combined, oversold=OVERSOLD, overbought=OVERBOUGHT) == "SELL"

    stop_price = position.get("stop_price")
    stop_hit = stop_price is not None and today_bar["low"] <= stop_price
    if not (is_death_cross or stop_hit):
        return None

    reason = "stop_hit" if stop_hit else "death_cross"
    trade_date = datetime.now(MARKET_TZ).date().isoformat()
    result = await _exit_kd_position(settings, client, account, position, reason, trade_date)
    result["intraday"] = True
    return result


async def _current_kd_position() -> Optional[dict]:
    """Most recent kd_entry event, unless a later kd_exit already closed it.

    stop_order_id comes from a later kd_stop_placed event (see
    ensure_stop_order), not kd_entry's own detail - the stop is no longer
    placed synchronously right after the BUY (see ensure_stop_order's
    docstring for why), so at the moment kd_entry is logged there is no
    stop order yet. kd_entry still carries a stop_order_id key for backward
    compatibility with rows logged before this change; it's always None on
    rows logged after."""
    entries = await activity_log.fetch_events(event_type="kd_entry", limit=1)
    if not entries:
        return None
    latest_entry = entries[0]
    exits = await activity_log.fetch_events(event_type="kd_exit", limit=1)
    if exits and exits[0]["id"] > latest_entry["id"]:
        return None

    detail = latest_entry.get("detail") or {}
    symbol = latest_entry.get("symbol")
    stop_order_id = detail.get("stop_order_id")
    if stop_order_id is None:
        stop_events = await activity_log.fetch_events(
            event_type="kd_stop_placed", symbol=symbol, status="placed", limit=1
        )
        if stop_events and stop_events[0]["id"] > latest_entry["id"]:
            stop_order_id = (stop_events[0].get("detail") or {}).get("stop_order_id")

    return {
        "symbol": symbol,
        "entry_price": detail.get("entry_price"),
        "stop_price": detail.get("stop_price"),
        "entry_date": detail.get("entry_date"),
        "stop_order_id": stop_order_id,
        "dry_run": bool(detail.get("dry_run")),
    }


async def ensure_stop_order(settings: Settings, client: SchwabClient) -> Optional[dict]:
    """Places the resting broker-side SELL STOP for the current KD position
    once its entry BUY has actually filled - called every monitor poll
    (~60s during market hours) rather than right after submitting the BUY.

    recommend_action runs once daily at PRICE_HISTORY_RUN_TIME_ET, which
    defaults to 04:30 ET - well before the 09:30 ET open. A NORMAL-session
    MARKET order submitted then sits WORKING for hours, not seconds; placing
    the stop immediately afterward (the old behavior) meant Schwab almost
    always rejected it outright, since the account didn't hold the shares
    yet - exactly the "stop order didn't land" failure this function fixes.
    Uses the account's actual held quantity (not the entry's estimated
    `shares`) so a partial fill still gets a correctly-sized stop."""
    position = await _current_kd_position()
    if position is None or position.get("stop_order_id") or position.get("dry_run"):
        return None
    if position.get("stop_price") is None:
        return None

    symbol = position["symbol"]
    try:
        account = await client.get_account(settings.schwab_account_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kd_watchlist ensure_stop_order: account fetch failed: %s", exc)
        return None

    quantity = extract_position_qty(account, symbol)
    if quantity <= 0:
        return None  # BUY hasn't filled yet (or never will) - retry next poll

    stop_body = _stop_order(symbol, quantity, position["stop_price"])
    try:
        stop_result = await submit_order(
            client=client, settings=settings, account_id=settings.schwab_account_id,
            order_body=stop_body, source="kd_watchlist_stop",
        )
        stop_order_id = _extract_order_id(stop_result)
        await activity_log.log_event(
            event_type="kd_stop_placed", symbol=symbol, status="placed",
            detail={"stop_order_id": stop_order_id, "quantity": quantity, "stop_price": position["stop_price"]},
        )
        return {"symbol": symbol, "stop_order_id": stop_order_id, "quantity": quantity}
    except Exception as exc:  # noqa: BLE001
        await activity_log.log_event(
            event_type="kd_stop_placed", symbol=symbol, status="error",
            detail={"quantity": quantity, "stop_price": position["stop_price"]}, error=str(exc),
        )
        return None


def _trading_days_held(all_dates: list, entry_date: Optional[str], latest_date) -> Optional[int]:
    """Trading-day count from entry_date to latest_date (0 on the entry day
    itself), counted off price_history's own daily-bar calendar (so weekends/
    holidays are never counted) rather than calendar days. None if entry_date
    isn't found in all_dates - e.g. a kd_entry logged before entry_date was
    added, or entry_date has since aged out of the backfill window - in which
    case the time stop just doesn't fire rather than raising."""
    if not entry_date:
        return None
    date_strs = [str(d) for d in all_dates]
    latest_str = str(latest_date)
    if entry_date not in date_strs or latest_str not in date_strs:
        return None
    return date_strs.index(latest_str) - date_strs.index(entry_date)


def _market_filter_ok(today_rows: pd.DataFrame) -> bool:
    """True unless MARKET_FILTER_SYMBOL's own close is confirmed BELOW its
    SMA today - fails open (permits entries) if that symbol's row is simply
    missing or its SMA hasn't warmed up yet (NaN), since a data gap here
    shouldn't silently disable all new entries the way an actual bear signal
    should. Only gates new entries (recommend_action's candidate branch);
    existing positions still exit normally regardless of market condition."""
    row = today_rows[today_rows["symbol"] == MARKET_FILTER_SYMBOL]
    if row.empty:
        return True
    row = row.iloc[0]
    if pd.isna(row["sma_market_filter"]):
        return True
    return bool(row["close"] > row["sma_market_filter"])


async def recommend_action(settings: Settings, client: SchwabClient) -> dict:
    """Decides today's action AND submits the order via order_executor
    (dry-run or live per SCHWAB_DRY_RUN - see module docstring). Returns one of:
      {"action": "BUY", "symbol": ..., "entry_price": ..., "stop_price": ..., "shares": ...,
       "candidates": [...], "order": <submit_order's return>}
      {"action": "SELL", "symbol": ..., "reason": "death_cross"|"stop_hit"|"time_stop", "shares": ...,
       "order": <submit_order's return>}
      {"action": "HOLD", "symbol": ...}        - already holding, no exit signal today
      {"action": "NONE"}                        - flat, no golden cross today (or nothing tradable)
      {"action": "NONE", "reason": "market_filter_blocked"} - golden cross(es) fired, but
                                                 MARKET_FILTER_SYMBOL is below its own SMA
      {"skipped": "..."}                        - couldn't evaluate (no data yet, etc.)
      {"action": ..., "order_error": "..."}     - decision made, but the order submission raised
    """
    if not price_history_db.is_ready():
        return {"skipped": "pg_not_configured"}

    raw_rows = await price_history_db.fetch_all_daily_bars()
    if not raw_rows:
        return {"skipped": "no_price_history"}

    df = build_features(pd.DataFrame(raw_rows))
    all_dates = sorted(df["trade_date"].unique())
    latest_date = all_dates[-1]
    today_rows = df[df["trade_date"] == latest_date]

    position = await _current_kd_position()

    try:
        account = await client.get_account(settings.schwab_account_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("kd_watchlist: account fetch failed: %s", exc)
        return {"skipped": f"account_fetch_failed: {exc}"}
    held_symbols = {p["symbol"] for p in parse_positions(account)}

    if position is not None and position["symbol"] in held_symbols:
        symbol = position["symbol"]
        row = today_rows[today_rows["symbol"] == symbol]
        if not row.empty:
            row = row.iloc[0]
            stop_price = position.get("stop_price")
            stop_hit = stop_price is not None and row["low"] <= stop_price
            days_held = _trading_days_held(all_dates, position.get("entry_date"), latest_date)
            time_stop = days_held is not None and days_held >= TIME_STOP_TRADING_DAYS
            if bool(row["death_cross"]) or stop_hit or time_stop:
                reason = "stop_hit" if stop_hit else "death_cross" if bool(row["death_cross"]) else "time_stop"
                return await _exit_kd_position(settings, client, account, position, reason, str(latest_date))
        return {"action": "HOLD", "symbol": symbol, "trade_date": str(latest_date)}

    if not _market_filter_ok(today_rows):
        return {"action": "NONE", "trade_date": str(latest_date), "reason": "market_filter_blocked"}

    candidates = today_rows[
        today_rows["golden_cross"]
        & (today_rows["close"] >= MIN_CLOSE_PRICE)
        & today_rows["trend_ok"]
        & (today_rows["symbol"] != MARKET_FILTER_SYMBOL)
    ]
    if candidates.empty:
        return {"action": "NONE", "trade_date": str(latest_date)}

    # `candidates` stays the FULL set of today's crosses for reporting
    # (kd_entry's "candidates" field, the "other crosses not picked" LINE
    # line) - `pool` is what's actually ranked/picked from, narrowed to
    # what the account can size to MIN_SHARES.
    buying_power = extract_buying_power(account)
    pool = candidates
    if buying_power:
        # If NONE clear MIN_SHARES, fall through with the full set
        # unfiltered so behavior for a too-small account is unchanged
        # (resolves to "insufficient_buying_power" below, same as always).
        affordable = candidates[np.floor((buying_power * BUY_ALLOCATION_PCT) / candidates["close"]) >= MIN_SHARES]
        if not affordable.empty:
            pool = affordable

    chosen = pool.iloc[0]
    if len(pool) > 1 and len(all_dates) > 1:
        model = train_cross_predictor(df[df["trade_date"] < latest_date])
        if model is not None:
            prior_day = all_dates[-2]
            scored = df[(df["trade_date"] == prior_day) & df["symbol"].isin(pool["symbol"])]
            scored = scored.dropna(subset=PREDICT_FEATURE_COLUMNS)
            if not scored.empty:
                scored = scored.copy()
                scored["predicted_proba"] = model.predict_proba(scored[PREDICT_FEATURE_COLUMNS])[:, 1]
                best_symbol = scored.sort_values("predicted_proba", ascending=False).iloc[0]["symbol"]
                match = pool[pool["symbol"] == best_symbol]
                if not match.empty:
                    chosen = match.iloc[0]

    atr = chosen["atr14"]
    if pd.isna(atr) or atr <= 0:
        return {"action": "NONE", "trade_date": str(latest_date), "reason": "no_atr_yet"}

    # entry_price is the SIGNAL day's close, a proxy for sizing only - the
    # order below is a MARKET order, so the real fill price is whatever the
    # market gives once it opens, same "fills at next day's open" assumption
    # the backtest used. BUY_ALLOCATION_PCT<1 leaves room for that estimate
    # being slightly off.
    entry_price = float(chosen["close"])
    stop_price = entry_price - ATR_STOP_MULT * float(atr)
    symbol = str(chosen["symbol"])

    shares = math.floor((buying_power * BUY_ALLOCATION_PCT) / entry_price) if buying_power else 0

    result = {
        "action": "BUY", "symbol": symbol, "entry_price": entry_price, "stop_price": stop_price,
        "atr14": float(atr), "shares": shares, "buying_power": buying_power,
        "candidates": candidates["symbol"].tolist(), "trade_date": str(latest_date),
    }
    is_dry_run = False
    if shares >= 1:
        order_body = _market_order(symbol, "BUY", shares)
        try:
            result["order"] = await submit_order(
                client=client, settings=settings, account_id=settings.schwab_account_id,
                order_body=order_body, source="kd_watchlist",
            )
            is_dry_run = bool(result["order"].get("dry_run"))
        except Exception as exc:  # noqa: BLE001
            result["order_error"] = str(exc)

        # The stop is NOT placed here anymore - see ensure_stop_order's
        # docstring. This runs pre-market (PRICE_HISTORY_RUN_TIME_ET,
        # default 04:30 ET), so a NORMAL-session MARKET BUY submitted above
        # won't fill for hours; a SELL STOP placed immediately after it used
        # to get rejected outright since the account didn't hold the shares
        # yet. monitor.py's poll loop calls ensure_stop_order every ~60s
        # during market hours and places the stop the moment the account
        # actually shows the fill.
    else:
        result["reason"] = "insufficient_buying_power"

    await activity_log.log_event(
        event_type="kd_entry", symbol=symbol, status="signal",
        detail={"entry_price": entry_price, "stop_price": stop_price, "atr14": float(atr),
                "entry_date": str(latest_date), "candidates": candidates["symbol"].tolist(), "shares": shares,
                "stop_order_id": None, "dry_run": is_dry_run},
    )
    return result
