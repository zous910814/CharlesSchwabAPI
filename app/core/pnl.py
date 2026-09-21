from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from app.clients.schwab import SchwabClient
from app.core.config import Settings

# Schwab's transactions endpoint doesn't expose a realized-gain/loss field
# directly (confirmed against the transaction schema: netAmount is just the
# net cash impact of that one fill). Realized P&L per round trip is derived
# here by pairing each symbol's OPENING (buy) transactions against its next
# CLOSING (sell) transaction and summing netAmount across the pair - safe
# under this strategy's one-position-at-a-time rule (see kd_watchlist.py).


def _chunk_ranges(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Schwab's transactions endpoint is documented against a per-call date
    range cap; chunk calendar-year-aligned to stay well under it regardless
    of how far back `start` is."""
    chunks: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        year_end = datetime(cursor.year + 1, 1, 1, tzinfo=cursor.tzinfo)
        chunk_end = min(year_end, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end
    return chunks


async def _fetch_trade_transactions(
    client: SchwabClient, settings: Settings, start: datetime, end: datetime
) -> list[dict]:
    transactions: list[dict] = []
    for chunk_start, chunk_end in _chunk_ranges(start, end):
        result = await client.get_transactions(
            settings.schwab_account_id,
            start_date=chunk_start.isoformat(),
            end_date=chunk_end.isoformat(),
            types="TRADE",
        )
        if isinstance(result, list):
            transactions.extend(result)
    transactions.sort(key=lambda t: t.get("tradeDate") or t.get("time") or "")
    return transactions


def _equity_leg(transaction: dict) -> Optional[dict]:
    for item in transaction.get("transferItems") or []:
        instrument = item.get("instrument") or {}
        if instrument.get("symbol") and item.get("amount") not in (None, 0):
            return item
    return None


def _closed_trades(transactions: list[dict]) -> list[dict]:
    """Pair each symbol's accumulated opening cost against its closing
    transaction; returns one entry per closed round trip."""
    open_cost: dict[str, float] = {}
    closed: list[dict] = []

    for txn in transactions:
        if txn.get("type") != "TRADE":
            continue
        leg = _equity_leg(txn)
        if leg is None:
            continue
        symbol = (leg.get("instrument") or {}).get("symbol")
        if not symbol:
            continue
        net_amount = txn.get("netAmount")
        if not isinstance(net_amount, (int, float)):
            continue

        position_effect = leg.get("positionEffect")
        is_opening = position_effect == "OPENING" if position_effect else float(leg["amount"]) > 0

        if is_opening:
            open_cost[symbol] = open_cost.get(symbol, 0.0) + float(net_amount)
        else:
            realized = float(net_amount) + open_cost.pop(symbol, 0.0)
            when = txn.get("tradeDate") or txn.get("time")
            closed.append({"symbol": symbol, "realized_pnl": realized, "closed_at": when})

    return closed


def _month_key(iso_ts: Optional[str]) -> Optional[str]:
    if not iso_ts:
        return None
    try:
        return iso_ts[:7]  # "YYYY-MM" - Schwab's dates are ISO-8601 with this prefix
    except Exception:  # noqa: BLE001
        return None


async def compute_monthly_pnl(
    client: SchwabClient, settings: Settings, *, months_back: int = 12
) -> dict:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=31 * months_back)

    transactions = await _fetch_trade_transactions(client, settings, start, end)
    closed_trades = _closed_trades(transactions)

    months: dict[str, dict] = {}
    for trade in closed_trades:
        key = _month_key(trade["closed_at"])
        if key is None:
            continue
        bucket = months.setdefault(key, {"month": key, "realized_pnl": 0.0, "trade_count": 0, "trades": []})
        bucket["realized_pnl"] += trade["realized_pnl"]
        bucket["trade_count"] += 1
        bucket["trades"].append(trade)

    ordered = sorted(months.values(), key=lambda m: m["month"])
    total = sum(m["realized_pnl"] for m in ordered)
    return {"months": ordered, "total_realized_pnl": total}
