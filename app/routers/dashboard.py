from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
import httpx

from app.clients.schwab import SchwabClient
from app.core import activity_log, data_pipeline, monitor
from app.core.account import extract_buying_power, extract_equity, parse_positions
from app.core.config import Settings, get_settings
from app.core.deps import get_schwab_client

router = APIRouter(tags=["dashboard"])

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
