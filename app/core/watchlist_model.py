"""Daily watchlist recommendation: a linear regression on price_history ranks
symbols by predicted next-day range/ATR (a proxy for ORB candidate quality),
validated offline in scripts/backtest_watchlist_regression.py (~1.26x top-10
lift vs. daily average). This module is the production port of that logic —
called from app.core.data_pipeline right after the daily backfill, so
watchlist.json is refreshed before each day's 09:30 ET open.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression

from app.core import price_history_db
from app.core.config import Settings

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[2]

ATR_WINDOW = 14  # matches app/strategy/orb.py:compute_atr
VOLUME_BASELINE_DAYS = 20  # matches app/core/monitor.py:RVOL_BASELINE_DAYS

FEATURE_COLUMNS = [
    "open_close_ratio", "high_close_ratio", "low_close_ratio",
    "atr14", "volume_ratio", "overnight_gap", "daily_range_pct", "range_atr",
]


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    g = df.groupby("symbol", group_keys=False)

    prev_close = g["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["atr14"] = true_range.groupby(df["symbol"]).transform(
        lambda s: s.rolling(ATR_WINDOW, min_periods=ATR_WINDOW).mean()
    )
    df["volume_ratio"] = df["volume"] / g["volume"].transform(
        lambda s: s.rolling(VOLUME_BASELINE_DAYS, min_periods=VOLUME_BASELINE_DAYS).mean()
    )
    df["overnight_gap"] = df["open"] / prev_close - 1
    df["daily_range_pct"] = (df["high"] - df["low"]) / df["close"]
    df["range_atr"] = (df["high"] - df["low"]) / df["atr14"]
    # Open/high/low relative to that day's own close, not raw dollar amounts —
    # keeps the shape of the candle comparable across a $5 stock and a $500
    # one (close/close would always be exactly 1, so it's omitted).
    df["open_close_ratio"] = df["open"] / df["close"]
    df["high_close_ratio"] = df["high"] / df["close"]
    df["low_close_ratio"] = df["low"] / df["close"]

    # Label: NEXT trading day's range_atr, so today's features (day t) predict
    # tomorrow's (day t+1) volatility — causal, no look-ahead.
    df["target"] = g["range_atr"].shift(-1)

    # Only feature columns are required here — the most recent trading day per
    # symbol has no next-day target yet (that's exactly the row this module
    # needs to score), so target NaNs are left in and filtered out by callers
    # that need a label (see score_latest below).
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=FEATURE_COLUMNS)
    return df


def score_latest(df: pd.DataFrame, *, min_close_price: float, top_n: int) -> list[str]:
    """Train on every historical row where the next day's outcome is already
    known; score the single most recent row per symbol, whose next-day
    outcome is exactly what's unknown and being predicted."""
    train = df[df["target"].notna()]
    latest = df.loc[df.groupby("symbol")["trade_date"].idxmax()].copy()
    latest = latest[latest["close"] >= min_close_price]

    if train.empty or latest.empty:
        raise ValueError("not_enough_data")

    model = LinearRegression()
    model.fit(train[FEATURE_COLUMNS], train["target"])
    latest["predicted_range_atr"] = model.predict(latest[FEATURE_COLUMNS])

    # Keep nlargest's descending-by-score order (not alphabetical): monitor.py
    # walks watchlist.json in file order and uses that order as an implicit
    # priority whenever two symbols compete for a shared resource — the daily
    # BUY budget (_reserve_daily_order_value) or a correlation-group slot
    # (only one open position per correlated group at a time). Alphabetizing
    # here would silently hand that priority to whichever symbol's ticker
    # happens to start with an earlier letter, regardless of model score.
    top = latest.nlargest(top_n, "predicted_range_atr")
    return top["symbol"].tolist()


def write_watchlist(settings: Settings, symbols: list[str]) -> None:
    path = Path(settings.watchlist_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    path.write_text(json.dumps(symbols, indent=2) + "\n", encoding="utf-8")


async def generate_and_write(settings: Settings) -> dict:
    """Regenerate watchlist.json from the latest price_history data. Never
    raises — any failure (no DB, insufficient history, etc.) is reported in
    the returned dict and leaves the existing watchlist.json untouched, so a
    bad run never blanks out the live trading watchlist."""
    if not settings.watchlist_auto_update:
        return {"skipped": "auto_update_disabled"}
    if not price_history_db.is_ready():
        return {"skipped": "pg_not_configured"}

    try:
        raw_rows = await price_history_db.fetch_all_daily_bars()
        if not raw_rows:
            return {"skipped": "no_price_history"}

        df = build_features(pd.DataFrame(raw_rows))
        symbols = score_latest(df, min_close_price=settings.watchlist_min_price, top_n=settings.watchlist_top_n)
        write_watchlist(settings, symbols)
        return {"symbols": symbols, "count": len(symbols)}
    except Exception as exc:  # noqa: BLE001
        logger.exception("watchlist auto-update failed")
        return {"error": str(exc)}
