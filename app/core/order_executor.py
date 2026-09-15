from __future__ import annotations

from typing import Optional

from app.clients.schwab import SchwabClient
from app.core import activity_log
from app.core.config import Settings


def extract_symbol(order_body: dict) -> Optional[str]:
    legs = order_body.get("orderLegCollection")
    if isinstance(legs, list) and legs and isinstance(legs[0], dict):
        instrument = legs[0].get("instrument") or {}
        return instrument.get("symbol")
    return None


async def submit_order(
    *,
    client: SchwabClient,
    settings: Settings,
    account_id: str,
    order_body: dict,
    source: str = "manual",
) -> dict:
    """Preview or place an order (per SCHWAB_DRY_RUN) and log the attempt/result.

    Shared by the manual /orders endpoint and the automated monitor loop so
    both paths log identically and go through the same safety gate. Raises
    httpx.HTTPStatusError / Exception on failure — the result is always logged
    to the activity log before the exception propagates.
    """
    symbol = extract_symbol(order_body)
    dry_run = settings.schwab_dry_run
    phase = "preview" if dry_run else "live"

    await activity_log.log_event(
        event_type="order_submitted",
        account_id=account_id,
        symbol=symbol,
        status="dry_run" if dry_run else "sent",
        detail={"phase": phase, "source": source, "order": order_body},
    )

    try:
        if dry_run:
            # Sent to Schwab's previewOrder endpoint: validates the order
            # (cost/commission estimate, balance checks) but never executes it.
            result = await client.preview_order(account_id, order_body)
            response_body: dict = {"dry_run": True, "preview": result}
            status = "dry_run"
        else:
            result = await client.place_order(account_id, order_body)
            response_body = result
            status = "success"

        await activity_log.log_event(
            event_type="order_result",
            account_id=account_id,
            symbol=symbol,
            status=status,
            detail={"source": source, "result": result},
        )
        return response_body
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        error_text = getattr(getattr(exc, "response", None), "text", None) or str(exc)
        await activity_log.log_event(
            event_type="order_result",
            account_id=account_id,
            symbol=symbol,
            status="failed" if status_code else "error",
            detail={"phase": phase, "source": source, "status_code": status_code},
            error=error_text,
        )
        raise
