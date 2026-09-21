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
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from app.clients.schwab import SchwabClient
from app.core import activity_log, price_history_db
from app.core.account import extract_buying_power, extract_position_qty, parse_positions
from app.core.config import Settings
from app.core.order_executor import submit_order
from app.strategy.kd import compute_kd

logger = logging.getLogger(__name__)

K_PERIOD = 14
D_PERIOD = 5
OVERSOLD = 20.0
OVERBOUGHT = 80.0
ATR_WINDOW = 14
VOLUME_BASELINE_DAYS = 20
ATR_STOP_MULT = 2.0
MIN_CLOSE_PRICE = 5.0

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


async def _current_kd_position() -> Optional[dict]:
    """Most recent kd_entry event, unless a later kd_exit already closed it."""
    entries = await activity_log.fetch_events(event_type="kd_entry", limit=1)
    if not entries:
        return None
    latest_entry = entries[0]
    exits = await activity_log.fetch_events(event_type="kd_exit", limit=1)
    if exits and exits[0]["id"] > latest_entry["id"]:
        return None
    detail = latest_entry.get("detail") or {}
    return {
        "symbol": latest_entry.get("symbol"),
        "entry_price": detail.get("entry_price"),
        "stop_price": detail.get("stop_price"),
        "entry_date": detail.get("entry_date"),
    }


async def recommend_action(settings: Settings, client: SchwabClient) -> dict:
    """Decides today's action AND submits the order via order_executor
    (dry-run or live per SCHWAB_DRY_RUN - see module docstring). Returns one of:
      {"action": "BUY", "symbol": ..., "entry_price": ..., "stop_price": ..., "shares": ...,
       "candidates": [...], "order": <submit_order's return>}
      {"action": "SELL", "symbol": ..., "reason": "death_cross"|"stop_hit", "shares": ...,
       "order": <submit_order's return>}
      {"action": "HOLD", "symbol": ...}        - already holding, no exit signal today
      {"action": "NONE"}                        - flat, no golden cross today (or nothing tradable)
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
            if bool(row["death_cross"]) or stop_hit:
                reason = "stop_hit" if stop_hit else "death_cross"
                shares = extract_position_qty(account, symbol)
                result = {"action": "SELL", "symbol": symbol, "reason": reason, "shares": shares,
                          "trade_date": str(latest_date)}
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
                    detail={"reason": reason, "trade_date": str(latest_date)},
                )
                return result
        return {"action": "HOLD", "symbol": symbol, "trade_date": str(latest_date)}

    candidates = today_rows[today_rows["golden_cross"] & (today_rows["close"] >= MIN_CLOSE_PRICE)]
    if candidates.empty:
        return {"action": "NONE", "trade_date": str(latest_date)}

    chosen = candidates.iloc[0]
    if len(candidates) > 1 and len(all_dates) > 1:
        model = train_cross_predictor(df[df["trade_date"] < latest_date])
        if model is not None:
            prior_day = all_dates[-2]
            scored = df[(df["trade_date"] == prior_day) & df["symbol"].isin(candidates["symbol"])]
            scored = scored.dropna(subset=PREDICT_FEATURE_COLUMNS)
            if not scored.empty:
                scored = scored.copy()
                scored["predicted_proba"] = model.predict_proba(scored[PREDICT_FEATURE_COLUMNS])[:, 1]
                best_symbol = scored.sort_values("predicted_proba", ascending=False).iloc[0]["symbol"]
                match = candidates[candidates["symbol"] == best_symbol]
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

    buying_power = extract_buying_power(account)
    shares = math.floor((buying_power * BUY_ALLOCATION_PCT) / entry_price) if buying_power else 0

    result = {
        "action": "BUY", "symbol": symbol, "entry_price": entry_price, "stop_price": stop_price,
        "atr14": float(atr), "shares": shares, "buying_power": buying_power,
        "candidates": candidates["symbol"].tolist(), "trade_date": str(latest_date),
    }
    if shares >= 1:
        order_body = _market_order(symbol, "BUY", shares)
        try:
            result["order"] = await submit_order(
                client=client, settings=settings, account_id=settings.schwab_account_id,
                order_body=order_body, source="kd_watchlist",
            )
        except Exception as exc:  # noqa: BLE001
            result["order_error"] = str(exc)
    else:
        result["reason"] = "insufficient_buying_power"

    await activity_log.log_event(
        event_type="kd_entry", symbol=symbol, status="signal",
        detail={"entry_price": entry_price, "stop_price": stop_price, "atr14": float(atr),
                "entry_date": str(latest_date), "candidates": candidates["symbol"].tolist(), "shares": shares},
    )
    return result
