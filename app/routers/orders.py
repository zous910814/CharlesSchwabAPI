from fastapi import APIRouter, Depends, HTTPException
import httpx

from app.clients.schwab import SchwabClient
from app.core.config import Settings, get_settings
from app.schemas.order import OrderRequest

router = APIRouter(prefix="/orders", tags=["orders"])


def get_client(settings: Settings = Depends(get_settings)) -> SchwabClient:
    return SchwabClient(
        client_id=settings.schwab_client_id,
        client_secret=settings.schwab_client_secret,
        redirect_uri=settings.schwab_redirect_uri,
        refresh_token=settings.schwab_refresh_token,
        code_verifier=settings.schwab_code_verifier,
        base_url=settings.schwab_base_url,
    )


@router.post("/{account_id}")
async def place_order(
    account_id: str,
    order: OrderRequest,
    client: SchwabClient = Depends(get_client),
):
    try:
        return await client.place_order(account_id, order.dict(exclude_none=True))
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except Exception as exc:  # pragma: no cover - unexpected issues
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/default")
async def place_default_account_order(
    order: OrderRequest,
    settings: Settings = Depends(get_settings),
    client: SchwabClient = Depends(get_client),
):
    try:
        return await client.place_order(settings.schwab_account_id, order.dict(exclude_none=True))
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(exc))
