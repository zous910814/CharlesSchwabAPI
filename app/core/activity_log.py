from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from app.core.config import get_settings
from app.core.line_notify import claim_risk_alert, format_risk_message, is_risk_event, send_line_message

BASE_DIR = Path(__file__).resolve().parents[2]
DB_PATH = BASE_DIR / "activity_log.db"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event_type TEXT NOT NULL,
    account_id TEXT,
    symbol TEXT,
    status TEXT,
    detail TEXT,
    error TEXT
);
"""


async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # WAL lets the monitor loop's frequent writes and the dashboard's
        # polling reads proceed concurrently instead of blocking each other;
        # it's a database-level setting, persisted in the file, so this only
        # needs to run once. busy_timeout (set per-connection below) covers
        # the remaining brief contention instead of failing immediately.
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute(CREATE_TABLE_SQL)
        await db.commit()


async def log_event(
    *,
    event_type: str,
    account_id: Optional[str] = None,
    symbol: Optional[str] = None,
    status: Optional[str] = None,
    detail: Any = None,
    error: Optional[str] = None,
) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    detail_json = json.dumps(detail, default=str) if detail is not None else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout = 5000")
        await db.execute(
            "INSERT INTO activity_log (ts, event_type, account_id, symbol, status, detail, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, event_type, account_id, symbol, status, detail_json, error),
        )
        await db.commit()

    if is_risk_event(event_type, status):
        suppressed = claim_risk_alert(event_type, status, symbol)
        if suppressed is None:
            return  # same alert already pushed within the cooldown; still logged above
        text = format_risk_message(event_type=event_type, symbol=symbol, status=status, detail=detail,
                                   error=error, suppressed=suppressed)
        # Fire-and-forget: don't let notification delivery (or LINE being
        # slow/down) add latency to the trading loop that just logged this.
        asyncio.create_task(send_line_message(get_settings(), text))


async def fetch_events(
    *,
    limit: int = 100,
    symbol: Optional[str] = None,
    status: Optional[str] = None,
    event_type: Optional[str] = None,
) -> list[dict]:
    query = "SELECT id, ts, event_type, account_id, symbol, status, detail, error FROM activity_log"
    clauses = []
    params: list = []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout = 5000")
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()

    events = []
    for row in rows:
        event = dict(row)
        if event.get("detail"):
            try:
                event["detail"] = json.loads(event["detail"])
            except (TypeError, ValueError):
                pass
        events.append(event)
    return events
