import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
import httpx

from app.clients.schwab import SchwabClient
from app.core.config import Settings, get_settings

router = APIRouter(tags=["auth"])


def get_client(settings: Settings = Depends(get_settings)) -> SchwabClient:
    return SchwabClient(
        client_id=settings.schwab_client_id,
        client_secret=settings.schwab_client_secret,
        redirect_uri=settings.schwab_redirect_uri,
        refresh_token=settings.schwab_refresh_token,
        base_url=settings.schwab_base_url,
    )


@router.get("/accounts/callback")
async def oauth_callback(
    code: str,
    state: Optional[str] = None,
    client: SchwabClient = Depends(get_client),
):
    try:
        tokens = await client.exchange_code_for_tokens(code)
        saved = False
        save_error = None
        try:
            payload = {"code": code, "state": state, "tokens": tokens}
            Path(".schwab_tokens.json").write_text(json.dumps(payload, indent=2))
            saved = True
        except Exception as write_exc:  # pragma: no cover - best-effort persistence
            save_error = str(write_exc)

        # auto update SCHWAB_REFRESH_TOKEN in .env if present
        try:
            refresh_token = tokens.get("refresh_token") if isinstance(tokens, dict) else None
            if refresh_token:
                env_path = Path(".env")
                if env_path.exists():
                    lines = env_path.read_text(encoding="utf-8").splitlines()
                    new_lines = []
                    replaced = False
                    for line in lines:
                        if line.startswith("SCHWAB_REFRESH_TOKEN="):
                            new_lines.append(f"SCHWAB_REFRESH_TOKEN={refresh_token}")
                            replaced = True
                        else:
                            new_lines.append(line)
                    if not replaced:
                        new_lines.append(f"SCHWAB_REFRESH_TOKEN={refresh_token}")
                    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                    print(".env 已自動更新 SCHWAB_REFRESH_TOKEN")
        except Exception as env_exc:  # pragma: no cover - best-effort persistence
            print("更新 .env 失敗：", env_exc)

        return {"state": state, "tokens": tokens, "saved": saved, "save_error": save_error}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)
    except Exception as exc:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(exc))
