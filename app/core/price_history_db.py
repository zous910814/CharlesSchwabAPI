from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from app.core.config import Settings

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.pool.Pool] = None

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS price_history (
    symbol      TEXT NOT NULL,
    trade_date  DATE NOT NULL,
    open        DOUBLE PRECISION NOT NULL,
    high        DOUBLE PRECISION NOT NULL,
    low         DOUBLE PRECISION NOT NULL,
    close       DOUBLE PRECISION NOT NULL,
    volume      BIGINT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'schwab',
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, trade_date)
)
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_price_history_trade_date ON price_history (trade_date)
"""

UPSERT_SQL = """
INSERT INTO price_history (symbol, trade_date, open, high, low, close, volume, source)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (symbol, trade_date) DO UPDATE SET
    open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
    close = EXCLUDED.close, volume = EXCLUDED.volume,
    source = EXCLUDED.source, fetched_at = now()
"""


async def init_pool(settings: Settings) -> None:
    global _pool
    if not settings.pg_dsn:
        logger.warning("PG_DSN not set; price_history pipeline disabled")
        return
    try:
        _pool = await asyncpg.create_pool(dsn=settings.pg_dsn, min_size=1, max_size=5)
        async with _pool.acquire() as conn:
            await conn.execute(CREATE_TABLE_SQL)
            await conn.execute(CREATE_INDEX_SQL)
    except Exception:
        logger.exception("price_history_db pool init failed, pipeline disabled")
        _pool = None


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def is_ready() -> bool:
    return _pool is not None


def get_pool() -> Optional[asyncpg.pool.Pool]:
    """Shared pool accessor for sibling *_db modules (e.g. fundamentals_db)
    that persist to the same Postgres instance but own their own table."""
    return _pool


async def get_recent_daily_bars(symbol: str, limit: int = 15) -> list[dict]:
    """Most recent completed daily bars for `symbol` (strictly before today,
    so a not-yet-finalized "today" row is never included), oldest first —
    ready to feed straight into app.strategy.kd.compute_kd (via
    kd_watchlist.check_intraday_exit), which expects that ordering and just
    needs high/low/close per bar."""
    if _pool is None:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT high, low, close FROM price_history "
            "WHERE symbol = $1 AND trade_date < CURRENT_DATE "
            "ORDER BY trade_date DESC LIMIT $2",
            symbol,
            limit,
        )
    return [dict(r) for r in reversed(rows)]


async def get_ohlc_bars(symbol: str, limit: int = 90) -> list[dict]:
    """Most recent completed daily bars for `symbol`, oldest first, full OHLCV
    - feeds the dashboard's per-position candlestick chart (app/routers/
    dashboard.py's /api/candles/{symbol})."""
    if _pool is None:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT trade_date, open, high, low, close, volume FROM price_history "
            "WHERE symbol = $1 ORDER BY trade_date DESC LIMIT $2",
            symbol,
            limit,
        )
    return [dict(r) for r in reversed(rows)]


async def fetch_all_daily_bars() -> list[dict]:
    """Every symbol's full daily-bar history, oldest first per symbol — the
    training set for app.core.kd_watchlist's golden/death-cross features."""
    if _pool is None:
        return []
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT symbol, trade_date, open, high, low, close, volume "
            "FROM price_history ORDER BY symbol, trade_date"
        )
    return [dict(r) for r in rows]


async def upsert_daily_bars(symbol: str, candles: list[dict], *, source: str = "schwab") -> int:
    if _pool is None or not candles:
        return 0
    rows = [
        (
            symbol,
            datetime.fromtimestamp(candle["datetime"] / 1000, tz=timezone.utc).date(),
            float(candle["open"]),
            float(candle["high"]),
            float(candle["low"]),
            float(candle["close"]),
            int(candle["volume"]),
            source,
        )
        for candle in candles
    ]
    async with _pool.acquire() as conn:
        await conn.executemany(UPSERT_SQL, rows)
    return len(rows)
