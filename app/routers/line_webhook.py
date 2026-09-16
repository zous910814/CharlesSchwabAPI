from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.core import activity_log
from app.core.config import Settings, get_settings
from app.core.line_notify import verify_webhook_signature

router = APIRouter(tags=["line"])


@router.post("/line/webhook")
async def line_webhook(
    request: Request,
    x_line_signature: str = Header(None, alias="X-Line-Signature"),
    settings: Settings = Depends(get_settings),
) -> dict:
    """LINE calls this when someone messages the bot. Only needed to log the
    sender's userId once so you can copy it into LINE_USER_ID — the bot never
    needs an active webhook for outbound risk notifications (those use the
    Push Message API instead). See README's LINE setup section.
    """
    body = await request.body()

    if settings.line_channel_secret:
        if not x_line_signature or not verify_webhook_signature(settings.line_channel_secret, body, x_line_signature):
            raise HTTPException(status_code=403, detail="Invalid LINE signature")

    payload = await request.json()
    for event in payload.get("events", []):
        source = event.get("source", {})
        await activity_log.log_event(
            event_type="line_webhook",
            status=event.get("type"),
            detail={"source": source, "message": event.get("message")},
        )

    return {"status": "ok"}
