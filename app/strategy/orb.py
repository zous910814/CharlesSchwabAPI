from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")

OR_START = dt_time(9, 30)
OR_END = dt_time(9, 34, 59, 999999)
ENTRY_START = dt_time(9, 35)
ENTRY_END = dt_time(11, 0)
FLATTEN_TIME = dt_time(15, 55)
SESSION_START = dt_time(9, 30)

# Static grouping used both for the market-direction filter (long entries in
# Nasdaq names confirm against QQQ's VWAP, others against SPY's) and as a
# simplified stand-in for "highly correlated same-direction positions count as
# one risk group" (spec doesn't define a precise correlation methodology).
NASDAQ_LINKED = {"QQQ", "AAPL", "NVDA", "MSFT", "AMD", "TSLA"}


def market_reference_symbol(symbol: str) -> str:
    return "QQQ" if symbol in NASDAQ_LINKED else "SPY"


def correlation_group(symbol: str) -> str:
    return market_reference_symbol(symbol)


def candle_et_time(candle: dict) -> datetime:
    ts_ms = candle.get("datetime")
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(MARKET_TZ)


def compute_opening_range(candles: list[dict]) -> Optional[tuple[float, float]]:
    """OR_High/OR_Low from bars within 09:30:00-09:34:59 ET. None if no bar in
    that window is present yet (or.g. before/without the opening range)."""
    highs: list[float] = []
    lows: list[float] = []
    for c in candles:
        t = candle_et_time(c).time()
        if OR_START <= t <= OR_END:
            if c.get("high") is not None:
                highs.append(c["high"])
            if c.get("low") is not None:
                lows.append(c["low"])
    if not highs or not lows:
        return None
    return max(highs), min(lows)


def compute_vwap(candles: list[dict]) -> Optional[float]:
    """Cumulative VWAP from session open (09:30 ET), bar-based typical price."""
    num = 0.0
    den = 0.0
    for c in candles:
        if candle_et_time(c).time() < SESSION_START:
            continue
        high, low, close, vol = c.get("high"), c.get("low"), c.get("close"), c.get("volume")
        if high is None or low is None or close is None or not vol:
            continue
        typical = (high + low + close) / 3
        num += typical * vol
        den += vol
    if den <= 0:
        return None
    return num / den


def compute_atr(candles: list[dict], period: int = 14) -> Optional[float]:
    """Simple (non-Wilder) average true range over the last `period` bars."""
    trs: list[float] = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i].get("high"), candles[i].get("low"), candles[i - 1].get("close")
        if high is None or low is None or prev_close is None:
            continue
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def cumulative_volume_by_minute(candles: list[dict]) -> dict[dt_time, float]:
    """{minute-of-day (ET, seconds zeroed): cumulative session volume through
    that minute}, for one trading day's candles."""
    result: dict[dt_time, float] = {}
    running = 0.0
    for c in sorted(candles, key=lambda c: c["datetime"]):
        t = candle_et_time(c)
        if t.time() < SESSION_START:
            continue
        running += c.get("volume") or 0
        result[t.time().replace(second=0, microsecond=0)] = running
    return result


def average_baseline_curve(daily_curves: list[dict[dt_time, float]]) -> dict[dt_time, float]:
    """Average cumulative-volume-by-minute across multiple historical days,
    used as the RVOL denominator."""
    totals: dict[dt_time, list[float]] = {}
    for curve in daily_curves:
        for t, v in curve.items():
            totals.setdefault(t, []).append(v)
    return {t: sum(vs) / len(vs) for t, vs in totals.items()}


def compute_rvol(today_curve: dict[dt_time, float], baseline_curve: dict[dt_time, float], at: dt_time) -> Optional[float]:
    at = at.replace(second=0, microsecond=0)
    today_val = today_curve.get(at)
    baseline_val = baseline_curve.get(at)
    if not today_val or not baseline_val:
        return None
    return today_val / baseline_val


def compute_spread_pct(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    if mid <= 0:
        return None
    return (ask - bid) / mid


@dataclass
class EntryContext:
    price: float
    or_high: float
    or_low: float
    vwap: float
    atr: Optional[float]
    rvol: Optional[float]
    spread_pct: Optional[float]
    market_price: Optional[float]
    market_vwap: Optional[float]
    rvol_min: float = 1.5
    spread_max_pct: float = 0.0005
    or_width_min_atr: float = 0.3
    or_width_max_atr: float = 1.5


def evaluate_long_entry(ctx: EntryContext) -> tuple[bool, dict]:
    """All of spec §6's long-entry conditions. Returns (passed, per-condition
    breakdown) for logging. See evaluate_short_entry for the §7 mirror —
    live trading (app/core/monitor.py) still only calls this one; the short
    side is currently wired into the backtest only (no real-money shorting
    yet — see README's note on short-locate data)."""
    or_width = ctx.or_high - ctx.or_low
    or_width_ok = (
        ctx.atr is not None
        and ctx.atr > 0
        and (ctx.or_width_min_atr * ctx.atr) <= or_width <= (ctx.or_width_max_atr * ctx.atr)
    )
    checks = {
        "breakout_above_or_high": ctx.price > ctx.or_high,
        "above_vwap": ctx.price > ctx.vwap,
        "rvol_ok": ctx.rvol is not None and ctx.rvol >= ctx.rvol_min,
        "spread_ok": ctx.spread_pct is not None and ctx.spread_pct <= ctx.spread_max_pct,
        "market_aligned": ctx.market_price is not None and ctx.market_vwap is not None and ctx.market_price > ctx.market_vwap,
        "or_width_ok": or_width_ok,
    }
    return all(checks.values()), checks


def evaluate_short_entry(ctx: EntryContext) -> tuple[bool, dict]:
    """Spec §7's short-entry conditions — the exact mirror of
    evaluate_long_entry (breakout below OR_Low instead of above OR_High,
    below VWAP/market-VWAP instead of above). RVOL/spread/OR-width are
    magnitude checks, so they're identical to the long side.

    Spec §7.7 (broker confirms the stock is borrowable, at an acceptable
    locate cost) is NOT checked here — no historical or live short-locate
    data source exists (same gap README already documents for the live
    long-only strategy). Backtests using this function are therefore
    optimistic versus what would actually be executable live."""
    or_width = ctx.or_high - ctx.or_low
    or_width_ok = (
        ctx.atr is not None
        and ctx.atr > 0
        and (ctx.or_width_min_atr * ctx.atr) <= or_width <= (ctx.or_width_max_atr * ctx.atr)
    )
    checks = {
        "breakdown_below_or_low": ctx.price < ctx.or_low,
        "below_vwap": ctx.price < ctx.vwap,
        "rvol_ok": ctx.rvol is not None and ctx.rvol >= ctx.rvol_min,
        "spread_ok": ctx.spread_pct is not None and ctx.spread_pct <= ctx.spread_max_pct,
        "market_aligned": ctx.market_price is not None and ctx.market_vwap is not None and ctx.market_price < ctx.market_vwap,
        "or_width_ok": or_width_ok,
    }
    return all(checks.values()), checks


def compute_stop_price(entry_price: float, or_high: float, atr: float, atr_mult: float) -> float:
    stop_distance = max(atr_mult * atr, entry_price - or_high)
    return entry_price - stop_distance


def compute_short_stop_price(entry_price: float, or_low: float, atr: float, atr_mult: float) -> float:
    """Mirror of compute_stop_price for a short position (spec §9.2: "做空方向
    相反" — stop sits above entry instead of below)."""
    stop_distance = max(atr_mult * atr, or_low - entry_price)
    return entry_price + stop_distance


def r_multiple(entry_price: float, stop_price: float, current_price: float, side: str = "long") -> Optional[float]:
    if side == "short":
        risk = stop_price - entry_price
        if risk <= 0:
            return None
        return (entry_price - current_price) / risk
    risk = entry_price - stop_price
    if risk <= 0:
        return None
    return (current_price - entry_price) / risk


def is_opening_range_window(now: datetime) -> bool:
    return OR_START <= now.time() <= OR_END


def is_entry_window(now: datetime) -> bool:
    return ENTRY_START <= now.time() <= ENTRY_END


def is_past_flatten_time(now: datetime) -> bool:
    return now.time() >= FLATTEN_TIME
