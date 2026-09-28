from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from app.clients.schwab import SchwabClient
from app.core import activity_log, fundamentals_db, kd_watchlist, price_history_db
from app.core.config import Settings, get_settings
from app.core.deps import get_schwab_client
from app.core.line_notify import (
    format_daily_watchlist_message,
    format_kd_recommendation_message,
    send_line_message,
)

BASE_DIR = Path(__file__).resolve().parents[2]
MARKET_TZ = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

# In-memory snapshot the dashboard reads via GET /api/price-history/status.
# Resets on process restart, same as monitor.STATE.
STATE: dict = {"last_run_date": None, "last_run_at": None, "last_result": None, "last_error": None}


async def fetch_candidate_universe(client: SchwabClient, settings: Settings) -> list[str]:
    indices = [s.strip() for s in settings.price_history_movers_indices.split(",") if s.strip()]
    seen: dict[str, None] = {}
    for index_symbol in indices:
        try:
            data = await client.get_movers(
                index_symbol,
                sort=settings.price_history_movers_sort,
                frequency=settings.price_history_movers_frequency,
            )
        except Exception as exc:
            logger.warning("movers fetch failed for %s: %s", index_symbol, exc)
            await activity_log.log_event(
                event_type="price_history_movers", symbol=index_symbol, status="error", error=str(exc)
            )
            continue
        for item in data.get("screeners", []) or []:
            symbol = (item.get("symbol") or "").upper()
            if symbol:
                seen.setdefault(symbol, None)
    return list(seen.keys())


def load_fixed_universe(settings: Settings) -> list[str]:
    """Hand-maintained training-data universe (e.g. sp500_constituents.json),
    same flat-JSON-array format and loader convention as monitor.load_watchlist."""
    path = Path(settings.price_history_universe_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [s.strip().upper() for s in data if isinstance(s, str) and s.strip()]


async def backfill_symbol(client: SchwabClient, settings: Settings, symbol: str) -> int:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=settings.price_history_backfill_days)
    history = await client.get_price_history(
        symbol,
        periodType="year",
        frequencyType="daily",
        frequency=1,
        startDate=int(start.timestamp() * 1000),
        endDate=int(end.timestamp() * 1000),
        needExtendedHoursData=False,
    )
    return await price_history_db.upsert_daily_bars(symbol, history.get("candles") or [])


async def fetch_fundamentals_for_universe(client: SchwabClient, universe: list[str]) -> dict:
    """One get_fundamentals() call per symbol -> upsert into fundamentals_db,
    keyed to today's date so repeated same-day runs overwrite rather than
    duplicate. Same fail-soft contract as backfill_symbol: one symbol's
    failure never aborts the batch."""
    today = datetime.now(MARKET_TZ).date()
    result = {"symbols_ok": 0, "symbols_failed": []}
    for symbol in universe:
        try:
            response = await client.get_fundamentals(symbol)
            parsed = fundamentals_db.parse_instrument(response)
            if parsed is None:
                raise ValueError("symbol_not_found")
            await fundamentals_db.upsert_fundamental(symbol, today, parsed)
            result["symbols_ok"] += 1
        except Exception as exc:
            result["symbols_failed"].append(symbol)
            await activity_log.log_event(
                event_type="fundamentals_backfill", symbol=symbol, status="error", error=str(exc)
            )
    return result


async def run_once(settings: Optional[Settings] = None) -> dict:
    """Fetch movers -> candidate universe, plus the fixed training-data
    universe -> per-symbol daily-bar backfill -> upsert (deduped union, each
    symbol fetched once). Idempotent (safe to call more than once same day);
    one symbol's failure never aborts the batch."""
    settings = settings or get_settings()
    if not price_history_db.is_ready():
        return {"skipped": "pg_not_configured"}

    client = get_schwab_client(settings)
    result: dict = {
        "movers_universe_size": 0,
        "fixed_universe_size": 0,
        "combined_universe_size": 0,
        "symbols_ok": 0,
        "symbols_failed": [],
        "rows_upserted": 0,
    }
    try:
        movers_universe = await fetch_candidate_universe(client, settings)
        fixed_universe = load_fixed_universe(settings)
        result["movers_universe_size"] = len(movers_universe)
        result["fixed_universe_size"] = len(fixed_universe)

        combined: dict[str, None] = {}
        for symbol in [*movers_universe, *fixed_universe]:
            combined.setdefault(symbol, None)
        # kd_watchlist's market-level filter (recommend_action) needs this
        # symbol's own daily bars in price_history to compute its SMA - added
        # here so it's backfilled like any other symbol, then explicitly
        # excluded from the candidate pool in kd_watchlist so it's never
        # itself traded as a golden-cross signal.
        combined.setdefault(kd_watchlist.MARKET_FILTER_SYMBOL, None)
        universe = list(combined.keys())
        result["combined_universe_size"] = len(universe)

        for symbol in universe:
            try:
                result["rows_upserted"] += await backfill_symbol(client, settings, symbol)
                result["symbols_ok"] += 1
            except Exception as exc:
                result["symbols_failed"].append(symbol)
                await activity_log.log_event(
                    event_type="price_history_backfill", symbol=symbol, status="error", error=str(exc)
                )

        if fundamentals_db.is_ready():
            result["fundamentals"] = await fetch_fundamentals_for_universe(client, universe)
        else:
            result["fundamentals"] = {"skipped": "pg_not_configured"}
    finally:
        await client.aclose()

    if settings.kd_watchlist_enabled and price_history_db.is_ready():
        kd_client = get_schwab_client(settings)
        try:
            result["kd_recommendation"] = await kd_watchlist.recommend_action(settings, kd_client)
        except Exception as exc:  # noqa: BLE001
            logger.exception("kd_watchlist recommendation failed")
            result["kd_recommendation"] = {"error": str(exc)}
        finally:
            await kd_client.aclose()
        await send_line_message(settings, format_kd_recommendation_message(result["kd_recommendation"]))
    else:
        result["kd_recommendation"] = {"skipped": "disabled_or_pg_not_configured"}

    STATE.update(
        last_run_date=datetime.now(MARKET_TZ).date().isoformat(),
        last_run_at=datetime.now(timezone.utc).isoformat(),
        last_result=result,
    )
    await activity_log.log_event(event_type="price_history_run", status="ok", detail=result)
    await send_line_message(settings, format_daily_watchlist_message(result))
    return result


def _seconds_until_next_run(settings: Settings) -> float:
    run_time = dt_time.fromisoformat(settings.price_history_run_time_et)
    now = datetime.now(MARKET_TZ)
    target = now.replace(hour=run_time.hour, minute=run_time.minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run_daily_forever() -> None:
    settings = get_settings()
    if not settings.pg_dsn:
        logger.warning("PG_DSN not set; price_history daily job disabled")
        return
    while True:
        await asyncio.sleep(_seconds_until_next_run(settings))
        now = datetime.now(MARKET_TZ)
        if now.weekday() >= 5:
            # No trading Sat/Sun, so the most recent daily bar (and thus any
            # golden-cross signal) is unchanged from Friday's close - running
            # anyway just re-submits the same BUY recommend_action already
            # tried on the prior weekend day, duplicating a real (rejected or
            # not) broker order for no new information. Same Mon-Fri check
            # monitor.is_market_open uses.
            continue
        today = now.date().isoformat()
        if STATE.get("last_run_date") == today:
            continue
        try:
            await run_once(settings)
        except Exception as exc:
            logger.exception("price_history daily job failed")
            STATE["last_error"] = str(exc)
            await activity_log.log_event(event_type="price_history_run", status="error", error=str(exc))
