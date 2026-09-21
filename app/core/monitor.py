from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app.clients.schwab import SchwabClient
from app.core import activity_log, kd_watchlist, price_history_db
from app.core.account import extract_day_pl, extract_equity, parse_positions
from app.core.config import Settings, get_settings
from app.core.deps import get_schwab_client
from app.core.order_executor import submit_order

MARKET_TZ = ZoneInfo("America/New_York")

# In-memory snapshot the dashboard reads via GET /api/monitor/status. Resets
# whenever the process restarts (e.g. --reload picks up a file change), which
# also clears the manual pause and the circuit breaker trip.
STATE: dict = {
    "running": False,
    "market_open": False,
    "last_poll": None,
    "last_error": None,
    "symbols": {},
    "manual_pause": False,
    "circuit_breaker": {"tripped": False, "reason": None, "tripped_on": None, "day_pl": None, "account_value": None},
}


def _today_str() -> str:
    return datetime.now(MARKET_TZ).date().isoformat()


def _trading_allowed(action: str) -> tuple[bool, Optional[str]]:
    """action: 'buy' | 'sell' | 'stop_loss' | 'eod_flatten'. Manual pause blocks
    everything except EOD flatten (never leave a position open overnight just
    because the bot was paused); the circuit breaker only blocks new
    speculative BUY/SELL, not protective stop-loss/flatten exits."""
    if action == "eod_flatten":
        return True, None
    if STATE.get("manual_pause"):
        return False, "manual_pause"
    if action in ("buy", "sell") and STATE["circuit_breaker"].get("tripped"):
        return False, "circuit_breaker"
    return True, None


def is_market_open(now: Optional[datetime] = None) -> bool:
    """US equities regular session, Mon-Fri 09:30-16:00 America/New_York.
    Does not account for market holidays.
    """
    now = now or datetime.now(MARKET_TZ)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def _market_order(symbol: str, instruction: str, quantity: float) -> dict:
    return {
        "orderType": "MARKET",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": instruction,
                "quantity": quantity,
                "instrument": {"symbol": symbol, "assetType": "EQUITY"},
            }
        ],
    }


# ---------------------------------------------------------------------------
# Generic account-level risk controls (strategy-agnostic — apply no matter
# what bought the position).
# ---------------------------------------------------------------------------


async def _update_circuit_breaker(settings: Settings, account: dict) -> None:
    day_pl = extract_day_pl(account)
    account_value = extract_equity(account)
    breaker = STATE["circuit_breaker"]
    breaker["day_pl"] = day_pl
    breaker["account_value"] = account_value

    today = _today_str()
    if breaker.get("tripped") and breaker.get("tripped_on") != today:
        breaker["tripped"] = False
        breaker["reason"] = None
        breaker["tripped_on"] = None
        await activity_log.log_event(
            event_type="circuit_breaker",
            account_id=settings.schwab_account_id,
            status="reset",
            detail={"day_pl": day_pl},
        )

    if not account_value or breaker.get("tripped"):
        return

    limit = -abs(account_value * settings.schwab_daily_loss_limit_pct)
    if day_pl <= limit:
        breaker["tripped"] = True
        breaker["reason"] = "daily_loss_limit"
        breaker["tripped_on"] = today
        await activity_log.log_event(
            event_type="circuit_breaker",
            account_id=settings.schwab_account_id,
            status="tripped",
            detail={"day_pl": day_pl, "limit": limit, "account_value": account_value},
        )


async def _run_stop_losses(client: SchwabClient, settings: Settings, account: dict) -> None:
    """Blanket account-wide safety net: force-sell anything down more than
    SCHWAB_STOP_LOSS_PCT from average cost, no matter the strategy."""
    allowed, _ = _trading_allowed("stop_loss")
    if not allowed:
        return

    for pos in parse_positions(account):
        symbol = pos["symbol"]
        quantity = pos["quantity"]
        pl_pct = pos.get("unrealized_pl_pct")
        if quantity <= 0 or pl_pct is None or pl_pct > -abs(settings.schwab_stop_loss_pct):
            continue

        symbol_state = STATE["symbols"].setdefault(symbol, {})
        today = _today_str()
        if symbol_state.get("stop_loss_triggered_on") == today:
            continue

        order_body = _market_order(symbol, "SELL", quantity)
        try:
            await submit_order(
                client=client,
                settings=settings,
                account_id=settings.schwab_account_id,
                order_body=order_body,
                source="stop_loss",
            )
            symbol_state["stop_loss_triggered_on"] = today
            await activity_log.log_event(
                event_type="stop_loss_triggered",
                account_id=settings.schwab_account_id,
                symbol=symbol,
                status="SELL",
                detail={
                    "unrealized_pl_pct": pl_pct,
                    "quantity": quantity,
                    "stop_loss_pct": settings.schwab_stop_loss_pct,
                },
            )
        except Exception:  # noqa: BLE001
            pass  # already logged inside submit_order


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


async def _poll_once(settings: Settings) -> None:
    client = get_schwab_client(settings)
    try:
        now = datetime.now(MARKET_TZ)
        market_open = is_market_open(now)
        STATE["market_open"] = market_open
        STATE["last_poll"] = now.isoformat()

        if not market_open:
            return

        try:
            account_snapshot = await client.get_account(settings.schwab_account_id)
        except Exception as exc:  # noqa: BLE001
            account_snapshot = None
            await activity_log.log_event(
                event_type="monitor_error", account_id=settings.schwab_account_id, status="error",
                detail={"phase": "account_snapshot"}, error=str(exc),
            )

        if account_snapshot is not None:
            await _update_circuit_breaker(settings, account_snapshot)
            await _run_stop_losses(client, settings, account_snapshot)

        if settings.kd_watchlist_enabled and price_history_db.is_ready():
            try:
                kd_exit = await kd_watchlist.check_intraday_exit(settings, client)
                if kd_exit is not None:
                    await activity_log.log_event(
                        event_type="kd_intraday_exit", account_id=settings.schwab_account_id,
                        symbol=kd_exit.get("symbol"), status=kd_exit.get("reason"), detail=kd_exit,
                    )
            except Exception as exc:  # noqa: BLE001
                await activity_log.log_event(
                    event_type="monitor_error", status="error",
                    detail={"phase": "kd_intraday_exit"}, error=str(exc),
                )
    finally:
        await client.aclose()


async def run_forever() -> None:
    STATE["running"] = True
    try:
        while True:
            # Re-fetched every iteration (cheap: get_settings() is cached and
            # only rebuilds after auth.py's callback clears the cache on a
            # fresh login) so a renewed refresh_token takes effect on the next
            # poll instead of requiring a process restart.
            settings = get_settings()
            try:
                await _poll_once(settings)
                STATE["last_error"] = None
            except Exception as exc:  # noqa: BLE001
                STATE["last_error"] = str(exc)
                await activity_log.log_event(event_type="monitor_error", status="error", error=str(exc))
            await asyncio.sleep(settings.monitor_poll_seconds)
    finally:
        STATE["running"] = False
