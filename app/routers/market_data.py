from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, Query

from app.clients.schwab import SchwabClient
from app.core.config import get_settings
from app.schemas.history import PriceHistoryQuery

router = APIRouter(prefix="/marketdata", tags=["marketdata"])


def get_client() -> SchwabClient:
    settings = get_settings()
    return SchwabClient(
        client_id=settings.schwab_client_id,
        client_secret=settings.schwab_client_secret,
        redirect_uri=settings.schwab_redirect_uri,
        refresh_token=settings.schwab_refresh_token,
        base_url=settings.schwab_base_url,
    )


@router.get("/{symbol}/history")
async def get_history(
    symbol: str,
    query: PriceHistoryQuery = Depends(),
    client: SchwabClient = Depends(get_client),
) -> dict:
    # Drop None values and convert datetime to epoch millis for the Schwab API
    params = {}
    for key, value in query.dict(exclude_none=True).items():
        if isinstance(value, datetime):
            params[key] = int(value.replace(tzinfo=timezone.utc).timestamp() * 1000)
        else:
            params[key] = value
    raw = await client.get_price_history(symbol, **params)

    # Convert Schwab's epoch-ms datetime into ISO strings for readability
    candles = raw.get("candles")
    if isinstance(candles, list):
        converted: list[Dict[str, Any]] = []
        for candle in candles:
            if isinstance(candle, dict) and isinstance(candle.get("datetime"), (int, float)):
                # Use compact UTC timestamp without microseconds for readability
                iso = datetime.fromtimestamp(candle["datetime"] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                candle = {**candle, "datetime_iso": iso}
            converted.append(candle)
        raw["candles"] = converted

    return raw
