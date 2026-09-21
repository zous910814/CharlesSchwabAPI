from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from app.clients.schwab import SchwabClient
from app.core import activity_log, kd_watchlist, price_history_db
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
from app.core.quotes import extract_bid_ask, is_halted
from app.strategy import orb

BASE_DIR = Path(__file__).resolve().parents[2]
MARKET_TZ = ZoneInfo("America/New_York")
RVOL_BASELINE_DAYS = 20

# In-memory snapshot the dashboard reads via GET /api/monitor/status. Resets
# whenever the process restarts (e.g. --reload picks up a file change), which
# also clears per-symbol day-state (OR/position/entries-today), the manual
# pause, and the circuit breaker trip.
STATE: dict = {
    "running": False,
    "market_open": False,
    "last_poll": None,
    "last_error": None,
    "symbols": {},
    "manual_pause": False,
    "circuit_breaker": {"tripped": False, "reason": None, "tripped_on": None, "day_pl": None, "account_value": None},
    "daily_order_value": {
        "date": None,
        "pending": [],
        "pending_total": 0.0,
        "last_seen_buying_power": None,
        "cap": None,
    },
    "orb_day": {"date": None, "consecutive_losses": 0},
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


def _reset_orb_day_if_new(now: datetime) -> None:
    today = now.date().isoformat()
    orb_day = STATE["orb_day"]
    if orb_day.get("date") != today:
        orb_day["date"] = today
        orb_day["consecutive_losses"] = 0
        for symbol_state in STATE["symbols"].values():
            symbol_state["entries_today"] = 0
            symbol_state["position"] = None  # safety net; should already be flat from EOD


# ---------------------------------------------------------------------------
# Generic account-level risk controls (strategy-agnostic; unchanged from the
# earlier K/D-based monitor).
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
    """Blanket account-wide safety net (independent of ORB's own tighter,
    ATR-based per-trade stop): force-sell anything down more than
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
            symbol_state["position"] = None
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


async def _reconcile_untracked_positions(settings: Settings, account: dict) -> None:
    """After a process restart, STATE (and every symbol's position tracking)
    starts empty even though real shares may still be held at Schwab. Broker
    account data can recover quantity and average cost, but never the
    ATR-based stop, entry_time, or half-closed flag — those are ORB-internal
    decisions made once at entry time and only ever kept in this process's
    memory. Rather than leaving such a position invisible to _manage_position
    until it hits the blanket 10% stop-loss, rebuild a conservative tracking
    entry for it: real qty/entry_price from the account, a fresh ATR-based
    stop anchored on entry_price (not current price — anchoring on current
    price can push stop_price above entry_price if the trade ran up while the
    process was down, which makes r_multiple's entry_price - stop_price
    denominator <= 0 and permanently disables the target1/target2 checks;
    anchoring on entry_price also means a position that already fell past its
    real risk budget while untracked gets stopped out on the very next poll
    instead of being handed a new, looser stop from wherever it now sits).
    entry_time reset to now (only delays time_stop, never skips the tighter
    stop/target checks); half_closed defaulted to False (worst case target1
    fires again on the already-remaining shares, which is harmless)."""
    for pos in parse_positions(account):
        symbol = pos["symbol"]
        quantity = pos["quantity"]
        if quantity <= 0:
            continue

        symbol_state = STATE["symbols"].setdefault(symbol, {})
        if symbol_state.get("position"):
            continue  # already tracked; normal case, no restart happened

        entry_price = pos.get("average_price") or pos.get("current_price")
        current_price = pos.get("current_price")
        if entry_price is None or current_price is None:
            continue

        daily_atr = symbol_state.get("daily_atr")
        if daily_atr is None:
            try:
                daily_bars = await price_history_db.get_recent_daily_bars(symbol)
                daily_atr = orb.compute_atr(daily_bars, period=14) if daily_bars else None
            except Exception:  # noqa: BLE001
                daily_atr = None

        if daily_atr and daily_atr > 0:
            stop_price = orb.compute_stop_price(entry_price, entry_price, daily_atr, settings.schwab_orb_stop_atr_mult)
        else:
            stop_price = entry_price * (1 - settings.schwab_stop_loss_pct)

        symbol_state["position"] = {
            "entry_price": entry_price,
            "stop_price": stop_price,
            "initial_stop_price": stop_price,
            "shares": quantity,
            "initial_shares": quantity,
            "half_closed": False,
            "entry_time": datetime.now(MARKET_TZ).isoformat(),
            "r_multiple": 0.0,
            "reconstructed": True,
        }
        await activity_log.log_event(
            event_type="position_reconciled",
            account_id=settings.schwab_account_id,
            symbol=symbol,
            status="reconstructed",
            detail={"quantity": quantity, "entry_price": entry_price, "stop_price": stop_price},
        )


def _daily_order_value_remaining(settings: Settings, buying_power: float, account_value: float) -> float:
    """Remaining BUY budget for today: never more than the account's actual
    buying power, minus BUY orders submitted today whose effect on that
    buying power we can't yet confirm. See _reserve_daily_order_value.
    """
    tracker = STATE["daily_order_value"]
    today = _today_str()
    if tracker.get("date") != today:
        tracker["date"] = today
        tracker["pending"] = []
        tracker["last_seen_buying_power"] = None

    pending = tracker["pending"]
    prev_bp = tracker["last_seen_buying_power"]
    if prev_bp is not None and pending:
        drop = prev_bp - buying_power
        while pending and drop >= pending[0] - 1e-6:
            drop -= pending.pop(0)
    tracker["last_seen_buying_power"] = buying_power

    pending_total = sum(pending)
    tracker["pending_total"] = pending_total

    cap = min(buying_power, account_value * settings.schwab_max_daily_order_value_pct)
    tracker["cap"] = cap
    return cap - pending_total


def _reserve_daily_order_value(amount: float) -> None:
    tracker = STATE["daily_order_value"]
    today = _today_str()
    if tracker.get("date") != today:
        tracker["date"] = today
        tracker["pending"] = []
        tracker["last_seen_buying_power"] = None
    tracker["pending"].append(amount)
    tracker["pending_total"] = sum(tracker["pending"])


# ---------------------------------------------------------------------------
# ORB indicators
# ---------------------------------------------------------------------------


async def _refresh_rvol_baseline(client: SchwabClient, symbol: str) -> dict:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    history = await client.get_price_history(
        symbol,
        periodType="day",
        frequencyType="minute",
        frequency=1,
        startDate=int(start.timestamp() * 1000),
        endDate=int(end.timestamp() * 1000),
        needExtendedHoursData=False,
    )
    candles = history.get("candles") or []
    today = _today_str()
    by_date: dict[str, list[dict]] = {}
    for c in candles:
        d = orb.candle_et_time(c).date().isoformat()
        if d == today:
            continue
        by_date.setdefault(d, []).append(c)
    recent_dates = sorted(by_date.keys())[-RVOL_BASELINE_DAYS:]
    if not recent_dates:
        return {}
    daily_curves = [orb.cumulative_volume_by_minute(by_date[d]) for d in recent_dates]
    return orb.average_baseline_curve(daily_curves)


async def _compute_symbol_indicators(client: SchwabClient, symbol: str, now: datetime) -> None:
    symbol_state = STATE["symbols"].setdefault(symbol, {})
    today = _today_str()

    if symbol_state.get("rvol_baseline_date") != today:
        try:
            symbol_state["rvol_baseline"] = await _refresh_rvol_baseline(client, symbol)
        except Exception as exc:  # noqa: BLE001
            symbol_state.setdefault("rvol_baseline", {})
            await activity_log.log_event(
                event_type="monitor_error", symbol=symbol, status="error",
                detail={"phase": "rvol_baseline"}, error=str(exc),
            )
        symbol_state["rvol_baseline_date"] = today

    # Daily ATR(14) from price_history's daily bars, not a same-day 1-minute
    # ATR: the opening-range-width check (or_width_ok) compares a 5-minute
    # price range against this ATR, so it needs to be on the same "normal
    # daily volatility" scale as that range, not a tiny few-minutes-of-noise
    # figure. Cached per symbol per day (a daily-scale number doesn't change
    # intraday); None if price_history isn't configured/reachable or the
    # symbol has no history yet — evaluate_long_entry already fails closed
    # (no trade) on a missing ATR, it never falls back to the old minute ATR.
    if symbol_state.get("daily_atr_date") != today:
        try:
            daily_bars = await price_history_db.get_recent_daily_bars(symbol)
            symbol_state["daily_atr"] = orb.compute_atr(daily_bars, period=14) if daily_bars else None
        except Exception as exc:  # noqa: BLE001
            symbol_state["daily_atr"] = None
            await activity_log.log_event(
                event_type="monitor_error", symbol=symbol, status="error",
                detail={"phase": "daily_atr"}, error=str(exc),
            )
        symbol_state["daily_atr_date"] = today

    history = await client.get_price_history(
        symbol,
        periodType="day",
        period=1,
        frequencyType="minute",
        frequency=1,
        needExtendedHoursData=False,
    )
    candles = history.get("candles") or []
    if not candles:
        return

    last = candles[-1]
    or_range = orb.compute_opening_range(candles)
    baseline = symbol_state.get("rvol_baseline") or {}
    today_curve = orb.cumulative_volume_by_minute(candles)
    rvol = orb.compute_rvol(today_curve, baseline, orb.candle_et_time(last).time()) if baseline else None

    symbol_state["close"] = last.get("close")
    symbol_state["or_high"] = or_range[0] if or_range else None
    symbol_state["or_low"] = or_range[1] if or_range else None
    symbol_state["vwap"] = orb.compute_vwap(candles)
    symbol_state["atr"] = symbol_state.get("daily_atr")
    symbol_state["rvol"] = rvol
    symbol_state["checked_at"] = now.isoformat()


# ---------------------------------------------------------------------------
# ORB position management (exits) and entries
# ---------------------------------------------------------------------------


async def _manage_position(client: SchwabClient, settings: Settings, symbol: str, now: datetime, account: dict) -> None:
    symbol_state = STATE["symbols"].get(symbol) or {}
    position = symbol_state.get("position")
    if not position:
        return

    close = symbol_state.get("close")
    or_low = symbol_state.get("or_low")
    vwap = symbol_state.get("vwap")
    if close is None:
        return

    entry_price = position["entry_price"]
    stop_price = position["stop_price"]
    # R-multiple is always measured against the *original* risk distance, not
    # the current stop (which moves to breakeven after target1) — otherwise
    # entry_price - stop_price hits 0 post-breakeven and r_multiple() would
    # permanently return None, silently disabling the target2 exit.
    r = orb.r_multiple(entry_price, position["initial_stop_price"], close)
    position["r_multiple"] = r

    exit_reason: Optional[str] = None
    exit_half = False
    gate_action = "sell"

    if orb.is_past_flatten_time(now):
        exit_reason, gate_action = "eod_flatten", "eod_flatten"
    elif close <= stop_price:
        exit_reason, gate_action = "stop_hit", "stop_loss"
    elif or_low is not None and close < or_low:
        exit_reason = "back_in_range"
    elif vwap is not None and close < vwap:
        exit_reason = "vwap_cross_back"
    elif r is not None and r >= settings.schwab_orb_target2_r:
        exit_reason = "target2"
    elif r is not None and r >= settings.schwab_orb_target1_r and not position.get("half_closed") and position["shares"] > 1:
        exit_reason, exit_half = "target1", True
    else:
        entry_time = datetime.fromisoformat(position["entry_time"])
        minutes_open = (now - entry_time).total_seconds() / 60
        if minutes_open >= settings.schwab_orb_time_stop_minutes and (r is None or r < settings.schwab_orb_time_stop_r):
            exit_reason = "time_stop"

    if exit_reason is None:
        return

    allowed, _ = _trading_allowed(gate_action)
    if not allowed:
        return  # paused; leave the position as-is and re-check next poll

    held_qty = extract_position_qty(account, symbol)
    if held_qty <= 0:
        # Our tracked position is stale (closed some other way) — clear it.
        symbol_state["position"] = None
        return

    shares = min(max(1, position["shares"] // 2) if exit_half else position["shares"], held_qty)

    order_body = _market_order(symbol, "SELL", shares)
    try:
        await submit_order(
            client=client,
            settings=settings,
            account_id=settings.schwab_account_id,
            order_body=order_body,
            source=f"orb_{exit_reason}",
        )
    except Exception:  # noqa: BLE001
        return  # already logged inside submit_order; retry next poll

    await activity_log.log_event(
        event_type="orb_exit",
        account_id=settings.schwab_account_id,
        symbol=symbol,
        status=exit_reason,
        detail={"r_multiple": r, "shares": shares, "half_close": exit_half},
    )

    if exit_half:
        position["shares"] -= shares
        position["half_closed"] = True
        position["stop_price"] = entry_price  # move stop to breakeven
    else:
        if (r if r is not None else -1) < 0:
            STATE["orb_day"]["consecutive_losses"] += 1
            if STATE["orb_day"]["consecutive_losses"] == settings.schwab_orb_max_consecutive_losses:
                await activity_log.log_event(
                    event_type="daily_trading_halted",
                    account_id=settings.schwab_account_id,
                    symbol=symbol,
                    status="consecutive_losses",
                    detail={
                        "consecutive_losses": STATE["orb_day"]["consecutive_losses"],
                        "max_consecutive_losses": settings.schwab_orb_max_consecutive_losses,
                    },
                )
        else:
            STATE["orb_day"]["consecutive_losses"] = 0
        symbol_state["position"] = None


async def _maybe_enter_orb(
    client: SchwabClient,
    settings: Settings,
    symbol: str,
    now: datetime,
    quotes: dict,
    account: dict,
) -> None:
    symbol_state = STATE["symbols"].setdefault(symbol, {})

    if symbol_state.get("position"):
        return
    if symbol_state.get("entries_today", 0) >= settings.schwab_orb_max_trades_per_symbol:
        return
    if STATE["orb_day"]["consecutive_losses"] >= settings.schwab_orb_max_consecutive_losses:
        return
    if not orb.is_entry_window(now):
        return

    allowed, block_reason = _trading_allowed("buy")
    if not allowed:
        return

    group = orb.correlation_group(symbol)
    for other_symbol, other_state in STATE["symbols"].items():
        if other_symbol != symbol and orb.correlation_group(other_symbol) == group and other_state.get("position"):
            await activity_log.log_event(
                event_type="order_skipped", account_id=settings.schwab_account_id, symbol=symbol, status="skipped",
                detail={"reason": "correlated_group_position_open", "group": group, "blocking_symbol": other_symbol},
            )
            return

    existing_qty = extract_position_qty(account, symbol)
    if existing_qty > 0:
        await activity_log.log_event(
            event_type="order_skipped", account_id=settings.schwab_account_id, symbol=symbol, status="skipped",
            detail={"reason": "unexpected_existing_position", "existing_qty": existing_qty},
        )
        return

    close = symbol_state.get("close")
    or_high = symbol_state.get("or_high")
    or_low = symbol_state.get("or_low")
    vwap = symbol_state.get("vwap")
    atr = symbol_state.get("atr")
    rvol = symbol_state.get("rvol")
    if None in (close, or_high, or_low, vwap):
        return

    if is_halted(quotes, symbol):
        await activity_log.log_event(
            event_type="order_skipped", account_id=settings.schwab_account_id, symbol=symbol, status="skipped",
            detail={"reason": "halted_or_no_quote"},
        )
        return

    bid, ask = extract_bid_ask(quotes, symbol)
    spread_pct = orb.compute_spread_pct(bid, ask)
    symbol_state["spread_pct"] = spread_pct

    ref_symbol = orb.market_reference_symbol(symbol)
    ref_state = STATE["symbols"].get(ref_symbol) or {}

    ctx = orb.EntryContext(
        price=close,
        or_high=or_high,
        or_low=or_low,
        vwap=vwap,
        atr=atr,
        rvol=rvol,
        spread_pct=spread_pct,
        market_price=ref_state.get("close"),
        market_vwap=ref_state.get("vwap"),
        rvol_min=settings.schwab_orb_rvol_min,
        spread_max_pct=settings.schwab_orb_spread_max_pct,
        or_width_min_atr=settings.schwab_orb_or_width_min_atr,
        or_width_max_atr=settings.schwab_orb_or_width_max_atr,
    )
    passed, checks = orb.evaluate_long_entry(ctx)
    symbol_state["entry_checks"] = checks

    await activity_log.log_event(
        event_type="signal_check",
        account_id=settings.schwab_account_id,
        symbol=symbol,
        status="BUY" if passed else "HOLD",
        detail={
            "checks": checks, "close": close, "or_high": or_high, "or_low": or_low,
            "vwap": vwap, "atr": atr, "rvol": rvol, "spread_pct": spread_pct,
        },
    )

    if not passed or atr is None or atr <= 0:
        return

    stop_price = orb.compute_stop_price(close, or_high, atr, settings.schwab_orb_stop_atr_mult)
    risk_per_share = close - stop_price
    if risk_per_share <= 0:
        return

    account_value = extract_equity(account)
    buying_power = extract_buying_power(account)
    if not account_value or not buying_power:
        await activity_log.log_event(
            event_type="order_skipped", account_id=settings.schwab_account_id, symbol=symbol, status="skipped",
            detail={"reason": "no_account_data"},
        )
        return

    risk_amount = account_value * settings.schwab_orb_risk_pct
    risk_based_shares = math.floor(risk_amount / risk_per_share)

    pct_cap_shares = math.floor((buying_power * settings.schwab_max_position_pct) / close)
    existing_value = extract_position_market_value(account, symbol)
    exposure_headroom = (account_value * settings.schwab_max_symbol_exposure_pct) - existing_value
    exposure_cap_shares = math.floor(exposure_headroom / close) if exposure_headroom > 0 else 0
    daily_remaining = _daily_order_value_remaining(settings, buying_power, account_value)
    daily_cap_shares = math.floor(daily_remaining / close) if daily_remaining > 0 else 0

    quantity = min(risk_based_shares, pct_cap_shares, exposure_cap_shares, daily_cap_shares)
    if quantity < 1:
        await activity_log.log_event(
            event_type="order_skipped", account_id=settings.schwab_account_id, symbol=symbol, status="skipped",
            detail={
                "reason": "position_size_zero",
                "risk_based_shares": risk_based_shares,
                "pct_cap_shares": pct_cap_shares,
                "exposure_cap_shares": exposure_cap_shares,
                "daily_cap_shares": daily_cap_shares,
            },
        )
        return

    order_body = _market_order(symbol, "BUY", quantity)
    try:
        await submit_order(
            client=client,
            settings=settings,
            account_id=settings.schwab_account_id,
            order_body=order_body,
            source="orb_entry",
        )
    except Exception:  # noqa: BLE001
        return

    _reserve_daily_order_value(quantity * close)
    symbol_state["entries_today"] = symbol_state.get("entries_today", 0) + 1
    symbol_state["position"] = {
        "entry_price": close,
        "stop_price": stop_price,
        "initial_stop_price": stop_price,
        "shares": quantity,
        "initial_shares": quantity,
        "half_closed": False,
        "entry_time": now.isoformat(),
        "r_multiple": 0.0,
    }


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


async def _poll_once(settings: Settings) -> None:
    client = get_schwab_client(settings)
    try:
        watchlist = load_watchlist(settings)
        now = datetime.now(MARKET_TZ)
        market_open = is_market_open(now)
        STATE["market_open"] = market_open
        STATE["last_poll"] = now.isoformat()

        _reset_orb_day_if_new(now)

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

        if not settings.schwab_orb_enabled:
            return

        if account_snapshot is not None:
            await _reconcile_untracked_positions(settings, account_snapshot)

        # A symbol can hold an open ORB position from a previous day's
        # watchlist even after today's (e.g. auto-regenerated) watchlist drops
        # it — it must keep being tracked/managed (stops, targets, EOD
        # flatten) until that position is closed, regardless of whether it's
        # still on the current watchlist. Only NEW entries are gated by the
        # watchlist itself.
        held_symbols = [sym for sym, s in STATE["symbols"].items() if s.get("position")]
        managed_symbols = list(dict.fromkeys(watchlist + held_symbols))

        if not managed_symbols:
            return

        # SPY/QQQ are always evaluated too (even if not on the watchlist) since
        # every other symbol's market-direction filter references one of them.
        symbols_to_process = list(dict.fromkeys(managed_symbols + ["SPY", "QQQ"]))
        for symbol in symbols_to_process:
            try:
                await _compute_symbol_indicators(client, symbol, now)
            except Exception as exc:  # noqa: BLE001
                await activity_log.log_event(event_type="signal_check", symbol=symbol, status="error", error=str(exc))

        if account_snapshot is not None:
            for symbol in managed_symbols:
                try:
                    await _manage_position(client, settings, symbol, now, account_snapshot)
                except Exception as exc:  # noqa: BLE001
                    await activity_log.log_event(
                        event_type="monitor_error", symbol=symbol, status="error",
                        detail={"phase": "manage_position"}, error=str(exc),
                    )

            if settings.schwab_orb_entries_enabled and watchlist and orb.is_entry_window(now):
                try:
                    quotes = await client.get_quotes(watchlist)
                except Exception as exc:  # noqa: BLE001
                    quotes = {}
                    await activity_log.log_event(
                        event_type="monitor_error", status="error", detail={"phase": "quotes"}, error=str(exc)
                    )

                for symbol in watchlist:
                    try:
                        await _maybe_enter_orb(client, settings, symbol, now, quotes, account_snapshot)
                    except Exception as exc:  # noqa: BLE001
                        await activity_log.log_event(
                            event_type="monitor_error", symbol=symbol, status="error",
                            detail={"phase": "orb_entry"}, error=str(exc),
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
