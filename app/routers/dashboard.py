from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
import httpx

from app.clients.schwab import SchwabClient
from app.core import activity_log, data_pipeline, fundamentals_db, kd_watchlist, monitor, pnl, price_history_db
from app.core.account import extract_buying_power, extract_equity, parse_positions
from app.core.config import Settings, get_settings
from app.core.deps import get_schwab_client
from app.core.kd_watchlist import D_PERIOD, K_PERIOD
from app.strategy.kd import compute_kd

router = APIRouter(tags=["dashboard"])

SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9.\-/]{0,9}")
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "dashboard.html"


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


@router.get("/api/activity")
async def api_activity(
    limit: int = Query(100, ge=1, le=1000),
    symbol: Optional[str] = None,
    status: Optional[str] = None,
    event_type: Optional[str] = None,
) -> list[dict]:
    return await activity_log.fetch_events(limit=limit, symbol=symbol, status=status, event_type=event_type)


@router.get("/api/monitor/status")
async def monitor_status() -> dict:
    return monitor.STATE


@router.post("/api/monitor/pause")
async def monitor_pause(settings: Settings = Depends(get_settings)) -> dict:
    monitor.STATE["manual_pause"] = True
    await activity_log.log_event(
        event_type="monitor_control", account_id=settings.schwab_account_id, status="paused"
    )
    return {"manual_pause": True}


@router.post("/api/monitor/resume")
async def monitor_resume(settings: Settings = Depends(get_settings)) -> dict:
    monitor.STATE["manual_pause"] = False
    await activity_log.log_event(
        event_type="monitor_control", account_id=settings.schwab_account_id, status="resumed"
    )
    return {"manual_pause": False}


@router.post("/api/price-history/run-now")
async def price_history_run_now() -> dict:
    return await data_pipeline.run_once()


@router.get("/api/price-history/status")
async def price_history_status() -> dict:
    return data_pipeline.STATE


@router.get("/api/portfolio")
async def api_portfolio(
    settings: Settings = Depends(get_settings),
    client: SchwabClient = Depends(get_schwab_client),
) -> dict:
    try:
        account = await client.get_account(settings.schwab_account_id)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except Exception as exc:  # pragma: no cover - unexpected issues
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        await client.aclose()

    return {
        "buying_power": extract_buying_power(account),
        "account_value": extract_equity(account),
        "positions": parse_positions(account),
    }


async def _live_daily_bars(client: SchwabClient, symbol: str, days: int) -> list[dict]:
    """Daily bars straight from Schwab, shaped like price_history_db.get_ohlc_bars.
    Not persisted: price_history is the KD candidate universe, and a one-off
    dashboard lookup shouldn't add a symbol to it."""
    end = datetime.now(timezone.utc)
    history = await client.get_price_history(
        symbol,
        periodType="year",
        frequencyType="daily",
        frequency=1,
        startDate=int((end - timedelta(days=int(days * 1.5) + 5)).timestamp() * 1000),
        endDate=int(end.timestamp() * 1000),
        needExtendedHoursData=False,
    )
    bars = [
        {
            "trade_date": datetime.fromtimestamp(c["datetime"] / 1000, tz=timezone.utc).date(),
            "open": c["open"],
            "high": c["high"],
            "low": c["low"],
            "close": c["close"],
            "volume": c["volume"],
        }
        for c in history.get("candles") or []
    ]
    return bars[-days:]


@router.get("/api/candles/{symbol}")
async def api_candles(
    symbol: str,
    days: int = Query(90, ge=10, le=250),
    client: SchwabClient = Depends(get_schwab_client),
) -> dict:
    """Daily OHLCV for the dashboard's position chart. `position` is only
    populated when the requested symbol is the currently-held KD position -
    it gives the chart the entry/stop reference lines to draw. Each bar also
    carries the strategy's `k`/`d` for the KD sub-chart. `name` is the
    company description (None if unavailable).

    Bars come from Postgres when the symbol is in the price_history universe
    (S&P 500 + daily movers); anything else - the dashboard's symbol search -
    falls back to a live Schwab fetch, so any listed ticker can be looked up."""
    symbol = symbol.strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        await client.aclose()
        raise HTTPException(status_code=400, detail="invalid symbol")

    try:
        bars = await price_history_db.get_ohlc_bars(symbol, limit=days)
        name = await fundamentals_db.get_description(symbol)

        if not bars:
            try:
                bars = await _live_daily_bars(client, symbol, days)
            except httpx.HTTPStatusError as exc:
                # Schwab answers an unknown ticker with a 4xx - that's "no such
                # symbol", not a dashboard failure. Auth/rate-limit/5xx still surface.
                if exc.response.status_code not in (400, 404):
                    raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
            if bars and not name:
                try:
                    name = (fundamentals_db.parse_instrument(await client.get_fundamentals(symbol)) or {}).get("description")
                except Exception:  # noqa: BLE001 - the name is cosmetic
                    name = None
    finally:
        await client.aclose()

    # Same %K/%D the strategy trades on (K_PERIOD/D_PERIOD), attached per bar as
    # `k`/`d` (None during warm-up). Computed over every returned bar so the
    # lines are already warm at the left edge of a short chart window.
    for bar in bars:
        for field in ("open", "high", "low", "close"):
            bar[field] = float(bar[field])
    compute_kd(bars, k_period=K_PERIOD, d_period=D_PERIOD)

    position = await kd_watchlist._current_kd_position()
    if position and position.get("symbol") == symbol:
        position_out = {
            "entry_price": position.get("entry_price"),
            "stop_price": position.get("stop_price"),
            "entry_date": position.get("entry_date"),
        }
    else:
        position_out = None

    return {"symbol": symbol, "name": name, "bars": bars, "position": position_out}


@router.get("/api/pnl/monthly")
async def api_pnl_monthly(
    months: int = Query(12, ge=1, le=60),
    settings: Settings = Depends(get_settings),
    client: SchwabClient = Depends(get_schwab_client),
) -> dict:
    try:
        return await pnl.compute_monthly_pnl(client, settings, months_back=months)
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except Exception as exc:  # pragma: no cover - unexpected issues
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        await client.aclose()
