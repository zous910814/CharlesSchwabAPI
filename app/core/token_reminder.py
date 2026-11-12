"""LINE reminder (in Chinese, per the user) to re-login before Schwab's
refresh token expires.

Schwab refresh tokens last ~7 days from the OAuth login; once it lapses every
API call fails and the bot stops trading (2026-11-10: a whole session lost to
monitor_error 400s on /oauth/token). The login time is the `logged_in_at`
field app/routers/auth.py writes into .schwab_tokens.json on each login
(older files without it fall back to the file's mtime - same moment).

Called once a day from data_pipeline.run_daily_forever (04:30 ET = 17:30
Taiwan in winter), every day of the week, so a token running out over a
weekend still gets its warnings.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.core.line_notify import send_line_message

logger = logging.getLogger(__name__)

TOKENS_PATH = Path(".schwab_tokens.json")
REFRESH_TOKEN_LIFETIME = timedelta(days=7)
REMIND_WITHIN = timedelta(days=2)
TAIPEI = ZoneInfo("Asia/Taipei")


def last_login_time(path: Path = TOKENS_PATH) -> Optional[datetime]:
    if not path.exists():
        return None
    try:
        stamp = json.loads(path.read_text()).get("logged_in_at")
        if stamp:
            return datetime.fromisoformat(stamp)
    except Exception:  # noqa: BLE001 - unreadable file: fall back to mtime
        pass
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)


def login_url(settings: Settings) -> str:
    u = urlparse(settings.schwab_redirect_uri)
    return f"{u.scheme}://{u.netloc}/accounts/login"


def reminder_text(settings: Settings, now: Optional[datetime] = None, path: Path = TOKENS_PATH) -> Optional[str]:
    login = last_login_time(path)
    if login is None:
        return None
    now = now or datetime.now(timezone.utc)
    expires = login + REFRESH_TOKEN_LIFETIME
    left = expires - now
    if left > REMIND_WITHIN:
        return None
    when = expires.astimezone(TAIPEI).strftime("%m/%d %H:%M")
    url = login_url(settings)
    if left <= timedelta(0):
        return (f"⚠️ Schwab 登入已經過期（台灣時間 {when}），自動交易目前無法下單、也無法停損停利。\n"
                f"請立即重新登入：{url}")
    hours = int(left.total_seconds() // 3600)
    return (f"⏰ Schwab 登入將在 {hours // 24} 天 {hours % 24} 小時後過期（台灣時間 {when}）。\n"
            f"過期後自動交易會停止，請在那之前重新登入：{url}")


async def remind_if_needed(settings: Settings) -> Optional[str]:
    text = reminder_text(settings)
    if text:
        await send_line_message(settings, text)
    return text
