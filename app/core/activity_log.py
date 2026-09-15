from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

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
        await db.execute(
            "INSERT INTO activity_log (ts, event_type, account_id, symbol, status, detail, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, event_type, account_id, symbol, status, detail_json, error),
        )
        await db.commit()


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
