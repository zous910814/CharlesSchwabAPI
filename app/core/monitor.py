from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from app.clients.schwab import SchwabClient
from app.core import activity_log
from app.core.account import (
    extract_buying_power,
    extract_day_pl,
    extract_equity,
    extract_position_market_value,
    extract_position_qty,
    parse_positions,
)
from app.core.config import Settings, get_settings
from app.core.deps import get_schwab_client
from app.core.order_executor import submit_order
from app.strategy.kd import compute_kd, kd_signal

BASE_DIR = Path(__file__).resolve().parents[2]
MARKET_TZ = ZoneInfo("America/New_York")
K_PERIOD = 14
D_PERIOD = 5
OVERSOLD = 20.0
OVERBOUGHT = 80.0
MIN_CANDLES = K_PERIOD + D_PERIOD

# In-memory snapshot the dashboard reads via GET /api/monitor/status. Resets
# whenever the process restarts (e.g. --reload picks up a file change), which
# also clears per-symbol "already acted on this signal" dedup state, the
# manual pause, and the circuit breaker trip.
STATE: dict = {
    "running": False,
    "market_open": False,
    "last_poll": None,
    "last_error": None,
    "symbols": {},  # symbol -> {close, k, d, signal, checked_at, last_acted_signal, stop_loss_triggered_on}
    "manual_pause": False,
    "circuit_breaker": {"tripped": False, "reason": None, "tripped_on": None, "day_pl": None, "account_value": None},
}


def _today_str() -> str:
    return datetime.now(MARKET_TZ).date().isoformat()


def _trading_allowed(action: str) -> tuple[bool, Optional[str]]:
    """action: 'buy' | 'sell' | 'stop_loss'. Manual pause blocks everything;
    the circuit breaker only blocks new speculative BUY/SELL, not stop-loss
    exits (those should still be able to cut losses)."""
    if STATE.get("manual_pause"):
        return False, "manual_pause"
    if action in ("buy", "sell") and STATE["circuit_breaker"].get("tripped"):
        return False, "circuit_breaker"
    return True, None


def load_watchlist(settings: Optional[Settings] = None) -> list[str]:
    settings = settings or get_settings()
    path = Path(settings.watchlist_path)
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [s.strip().upper() for s in data if isinstance(s, str) and s.strip()]


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


async def _evaluate_symbol(client: SchwabClient, symbol: str) -> Optional[dict]:
    history = await client.get_price_history(
        symbol,
        periodType="day",
        period=1,
        frequencyType="minute",
        frequency=1,
        needExtendedHoursData=False,
    )
    candles = history.get("candles")
    if not isinstance(candles, list) or len(candles) < MIN_CANDLES:
        return None
    compute_kd(candles, k_period=K_PERIOD, d_period=D_PERIOD)
    signal = kd_signal(candles, oversold=OVERSOLD, overbought=OVERBOUGHT)
    last = candles[-1]
    return {"close": last.get("close"), "k": last.get("k"), "d": last.get("d"), "signal": signal}


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
    allowed, reason = _trading_allowed("stop_loss")
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
            continue  # already force-sold today; don't refire every poll

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
            symbol_state["last_acted_signal"] = "SELL"
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


async def _maybe_trade(client: SchwabClient, settings: Settings, account_id: str, symbol: str, evaluation: dict) -> None:
    signal = evaluation["signal"]
    symbol_state = STATE["symbols"].setdefault(symbol, {})

    if signal == "HOLD":
        return
    if symbol_state.get("last_acted_signal") == signal:
        return  # already acted on this signal; wait for it to flip before trading again

    allowed, block_reason = _trading_allowed("buy" if signal == "BUY" else "sell")
    if not allowed:
        await activity_log.log_event(
            event_type="order_skipped",
            account_id=account_id,
            symbol=symbol,
            status="skipped",
            detail={"reason": block_reason},
        )
        return

    try:
        account = await client.get_account(account_id)
    except Exception as exc:  # noqa: BLE001
        await activity_log.log_event(
            event_type="order_skipped",
            account_id=account_id,
            symbol=symbol,
            status="error",
            detail={"reason": "account_lookup_failed"},
            error=str(exc),
        )
        return

    close = evaluation.get("close")

    if signal == "BUY":
        buying_power = extract_buying_power(account)
        account_value = extract_equity(account)
        if not buying_power or not close or not account_value:
            await activity_log.log_event(
                event_type="order_skipped",
                account_id=account_id,
                symbol=symbol,
                status="skipped",
                detail={"reason": "no_buying_power_data"},
            )
            return
        max_spend = buying_power * settings.schwab_max_position_pct
        quantity = math.floor(max_spend / close)
        if quantity < 1:
            await activity_log.log_event(
                event_type="order_skipped",
                account_id=account_id,
                symbol=symbol,
                status="skipped",
                detail={
                    "reason": "insufficient_funds",
                    "buying_power": buying_power,
                    "close": close,
                    "max_spend": max_spend,
                    "max_position_pct": settings.schwab_max_position_pct,
                },
            )
            return

        # Total-exposure cap: shrink (or skip) the buy so this symbol's total
        # position never exceeds max_symbol_exposure_pct of account value,
        # even after repeated BUY signals on separate crossovers.
        existing_value = extract_position_market_value(account, symbol)
        max_exposure = account_value * settings.schwab_max_symbol_exposure_pct
        headroom = max_exposure - existing_value
        if headroom < close:
            await activity_log.log_event(
                event_type="order_skipped",
                account_id=account_id,
                symbol=symbol,
                status="skipped",
                detail={
                    "reason": "symbol_exposure_cap_reached",
                    "existing_value": existing_value,
                    "max_exposure": max_exposure,
                    "max_symbol_exposure_pct": settings.schwab_max_symbol_exposure_pct,
                },
            )
            return
        quantity = min(quantity, math.floor(headroom / close))

        order_body = _market_order(symbol, "BUY", quantity)
    else:  # SELL
        held_qty = extract_position_qty(account, symbol)
        if held_qty <= 0:
            await activity_log.log_event(
                event_type="order_skipped",
                account_id=account_id,
                symbol=symbol,
                status="skipped",
                detail={"reason": "no_position_to_sell"},
            )
            return
        order_body = _market_order(symbol, "SELL", held_qty)

    try:
        await submit_order(
            client=client,
            settings=settings,
            account_id=account_id,
            order_body=order_body,
            source="monitor",
        )
        symbol_state["last_acted_signal"] = signal
    except Exception:  # noqa: BLE001
        pass  # already logged inside submit_order


async def _poll_once(settings: Settings) -> None:
    client = get_schwab_client(settings)
    try:
        watchlist = load_watchlist(settings)
        market_open = is_market_open()
        STATE["market_open"] = market_open
        STATE["last_poll"] = datetime.now(MARKET_TZ).isoformat()

        if not market_open:
            return

        # One account snapshot per cycle drives the circuit breaker and
        # stop-loss checks across *all* held positions (not just watchlist
        # symbols), independent of per-symbol K/D signals below.
        try:
            account_snapshot = await client.get_account(settings.schwab_account_id)
        except Exception as exc:  # noqa: BLE001
            account_snapshot = None
            await activity_log.log_event(
                event_type="monitor_error",
                account_id=settings.schwab_account_id,
                status="error",
                detail={"phase": "account_snapshot"},
                error=str(exc),
            )

        if account_snapshot is not None:
            await _update_circuit_breaker(settings, account_snapshot)
            await _run_stop_losses(client, settings, account_snapshot)

        if not watchlist:
            return

        for symbol in watchlist:
            try:
                evaluation = await _evaluate_symbol(client, symbol)
            except Exception as exc:  # noqa: BLE001
                await activity_log.log_event(
                    event_type="signal_check",
                    account_id=settings.schwab_account_id,
                    symbol=symbol,
                    status="error",
                    error=str(exc),
                )
                continue

            if evaluation is None:
                continue

            symbol_state = STATE["symbols"].setdefault(symbol, {})
            symbol_state.update(evaluation)
            symbol_state["checked_at"] = STATE["last_poll"]

            await activity_log.log_event(
                event_type="signal_check",
                account_id=settings.schwab_account_id,
                symbol=symbol,
                status=evaluation["signal"],
                detail=evaluation,
            )

            await _maybe_trade(client, settings, settings.schwab_account_id, symbol, evaluation)
    finally:
        await client.aclose()


async def run_forever() -> None:
    settings = get_settings()
    STATE["running"] = True
    try:
        while True:
            try:
                await _poll_once(settings)
                STATE["last_error"] = None
            except Exception as exc:  # noqa: BLE001
                STATE["last_error"] = str(exc)
                await activity_log.log_event(event_type="monitor_error", status="error", error=str(exc))
            await asyncio.sleep(settings.monitor_poll_seconds)
    finally:
        STATE["running"] = False
