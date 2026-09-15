from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
import httpx

from app.clients.schwab import SchwabClient
from app.core.config import Settings, get_settings
from app.core.deps import get_schwab_client
from app.core.order_executor import submit_order
from app.schemas.order import OrderRequest

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post("/{account_id}")
async def place_order(
    account_id: str,
    order: OrderRequest,
    settings: Settings = Depends(get_settings),
    client: SchwabClient = Depends(get_schwab_client),
):
    order_body = order.dict(exclude_none=True)
    try:
        return await submit_order(
            client=client,
            settings=settings,
            account_id=account_id,
            order_body=order_body,
            source="manual",
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except Exception as exc:  # pragma: no cover - unexpected issues
        raise HTTPException(status_code=500, detail=str(exc))
