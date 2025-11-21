from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.clients.schwab import SchwabClient
from app.core.config import get_settings, Settings

router = APIRouter(prefix="/accounts", tags=["accounts"])


def get_client(settings: Settings = Depends(get_settings)) -> SchwabClient:
    return SchwabClient(
        client_id=settings.schwab_client_id,
        client_secret=settings.schwab_client_secret,
        redirect_uri=settings.schwab_redirect_uri,
        refresh_token=settings.schwab_refresh_token,
        base_url=settings.schwab_base_url,
    )


@router.get("/login")
async def login(settings: Settings = Depends(get_settings), format: str = "html"):
    
    # ⭐ 完全不用 PKCE，只用 client_id + redirect_uri
    params = {
        "response_type": "code",
        "client_id": settings.schwab_client_id,
        "redirect_uri": settings.schwab_redirect_uri,
    }

    authorize_url = f"{settings.schwab_base_url}/oauth/authorize?" + urlencode(params)

    # JSON 模式
    if format.lower() == "json":
        return {"authorize_url": authorize_url}

    # HTML 模式
    html = f"""
    <html>
      <body>
        <h3>Schwab OAuth Login</h3>
        <p><a href="{authorize_url}">Click here to login and get your code</a></p>
      </body>
    </html>
    """
    return HTMLResponse(content=html)
