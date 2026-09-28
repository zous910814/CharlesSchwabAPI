from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from app.core.config import Settings

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LA_TZ = ZoneInfo("America/Los_Angeles")

# (event_type, status) combos worth waking someone up for. "orb_exit" and
# "order_result" are only risky for specific statuses, so those are handled
# separately below rather than listed wholesale here. "kd_stop_placed"
# covers both statuses it's ever logged with ("placed"/"error") - both are
# worth a push, since it's the only place that reports whether a KD
# position's protective stop actually made it to the broker (see
# kd_watchlist.ensure_stop_order). "kd_exit_deferred_pdt" fires when a
# death-cross exit was skipped to avoid tripping the Pattern Day Trader flag
# (see kd_watchlist._pdt_guard) - the position is still held, unexpectedly,
# so it's worth knowing about even though nothing went wrong.
_ALWAYS_RISKY_EVENT_TYPES = {
    "stop_loss_triggered", "circuit_breaker", "monitor_error", "daily_trading_halted",
    "kd_stop_placed", "kd_exit_deferred_pdt",
}


def is_risk_event(event_type: str, status: Optional[str]) -> bool:
    if event_type in _ALWAYS_RISKY_EVENT_TYPES:
        return True
    if event_type == "orb_exit" and status == "stop_hit":
        return True
    if event_type == "order_result" and status in ("failed", "error"):
        return True
    return False


def format_risk_message(
    *,
    event_type: str,
    symbol: Optional[str],
    status: Optional[str],
    detail: Any,
    error: Optional[str],
) -> str:
    now = datetime.now(LA_TZ).strftime("%Y-%m-%d %H:%M:%S PT")
    lines = [f"[Schwab Bot Alert] {now}", f"Event: {event_type}" + (f" ({status})" if status else "")]
    if symbol:
        lines.append(f"Symbol: {symbol}")

    if isinstance(detail, dict):
        for key in ("reason", "r_multiple", "unrealized_pl_pct", "day_pl", "quantity", "phase",
                    "stop_price", "stop_order_id", "round_trips", "limit"):
            if key in detail and detail[key] is not None:
                value = detail[key]
                if isinstance(value, float):
                    value = round(value, 4)
                lines.append(f"{key}: {value}")

    if error:
        lines.append(f"error: {error[:300]}")

    return "\n".join(lines)


def format_daily_watchlist_message(result: dict) -> str:
    now = datetime.now(LA_TZ).strftime("%Y-%m-%d %H:%M:%S PT")
    lines = [f"[Daily Watchlist] {now}"]

    failed = result.get("symbols_failed") or []
    lines.append(
        f"Backfill: {result.get('symbols_ok', 0)}/{result.get('combined_universe_size', 0)} symbols ok"
        + (f", {len(failed)} failed" if failed else "")
        + f", {result.get('rows_upserted', 0)} rows upserted"
    )

    fundamentals = result.get("fundamentals") or {}
    if fundamentals.get("skipped"):
        lines.append(f"Fundamentals: skipped ({fundamentals['skipped']})")
    elif "symbols_ok" in fundamentals:
        f_failed = fundamentals.get("symbols_failed") or []
        lines.append(
            f"Fundamentals: {fundamentals['symbols_ok']}/{result.get('combined_universe_size', 0)} symbols ok"
            + (f", {len(f_failed)} failed" if f_failed else "")
        )

    watchlist = result.get("watchlist") or {}
    if watchlist.get("symbols"):
        lines.append(f"Watchlist ({watchlist['count']}): " + ", ".join(watchlist["symbols"]))
    elif watchlist.get("skipped"):
        lines.append(f"Watchlist: skipped ({watchlist['skipped']})")
    elif watchlist.get("error"):
        lines.append(f"Watchlist: error ({watchlist['error'][:200]})")

    return "\n".join(lines)


def format_kd_recommendation_message(result: dict) -> str:
    """result: app.core.kd_watchlist.recommend_action's return value. That
    function actually submits the order (dry-run or live per SCHWAB_DRY_RUN)
    before returning - this message reports what happened, it doesn't ask
    the user to act."""
    now = datetime.now(LA_TZ).strftime("%Y-%m-%d %H:%M:%S PT")
    lines = [f"[KD Swing] {now}"]

    order = result.get("order")
    order_line = None
    if order is not None:
        order_line = "Order: PREVIEW only (SCHWAB_DRY_RUN=true)" if order.get("dry_run") else "Order: LIVE - submitted to Schwab"
    elif result.get("order_error"):
        order_line = f"Order FAILED: {result['order_error'][:200]}"

    action = result.get("action")
    if action == "BUY":
        stop_distance = result["entry_price"] - result["stop_price"]
        lines.append(f"BUY {result.get('shares', 0)} {result['symbol']}")
        lines.append(f"Sizing reference (signal-day close): {result['entry_price']:.2f}, "
                      f"stop {stop_distance:.2f} below fill (2x ATR14={result['atr14']:.2f})")
        if order_line:
            lines.append(order_line)
        elif result.get("reason") == "insufficient_buying_power":
            lines.append("No order sent: insufficient buying power for even 1 share")

        if order is not None and not order.get("dry_run"):
            lines.append(f"Stop order: pending - placed once the BUY fills "
                          f"(target {result['stop_price']:.2f}; separate alert when it's actually placed)")

        others = [c for c in result.get("candidates", []) if c != result["symbol"]]
        if others:
            lines.append(f"Other golden crosses today (not picked): {', '.join(others)}")
    elif action == "SELL":
        lines.append(f"SELL {result.get('shares', 0)} {result['symbol']} (reason: {result.get('reason')})")
        if order_line:
            lines.append(order_line)
    elif action == "HOLD":
        lines.append(f"Holding {result['symbol']} - no exit signal today")
    elif action == "NONE":
        reason = result.get("reason")
        lines.append(f"No golden cross today - stay flat ({reason})" if reason else "No golden cross today - stay flat")
    elif result.get("skipped"):
        lines.append(f"Skipped: {result['skipped']}")

    return "\n".join(lines)


async def send_line_message(settings: Settings, text: str) -> None:
    if not settings.line_channel_access_token or not settings.line_user_id:
        return  # not configured yet — silent no-op, never blocks the caller

    headers = {
        "Authorization": f"Bearer {settings.line_channel_access_token}",
        "Content-Type": "application/json",
    }
    payload = {"to": settings.line_user_id, "messages": [{"type": "text", "text": text[:4900]}]}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(LINE_PUSH_URL, headers=headers, json=payload)
            response.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        # Never let a notification failure take down logging/trading; this is
        # intentionally not routed through activity_log.log_event() to avoid
        # a retry loop if LINE itself is what's failing.
        print(f"[line_notify] failed to send message: {exc}")


def verify_webhook_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature)
