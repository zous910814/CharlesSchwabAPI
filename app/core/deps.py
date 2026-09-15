from __future__ import annotations

from fastapi import Depends

from app.clients.schwab import SchwabClient
from app.core.config import Settings, get_settings


def get_schwab_client(settings: Settings = Depends(get_settings)) -> SchwabClient:
    return SchwabClient(
        client_id=settings.schwab_client_id,
        client_secret=settings.schwab_client_secret,
        redirect_uri=settings.schwab_redirect_uri,
        refresh_token=settings.schwab_refresh_token,
        base_url=settings.schwab_base_url,
    )
