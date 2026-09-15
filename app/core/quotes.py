from __future__ import annotations

from typing import Optional


def extract_bid_ask(quotes: dict, symbol: str) -> tuple[Optional[float], Optional[float]]:
    entry = quotes.get(symbol) or quotes.get(symbol.upper()) or {}
    quote = entry.get("quote") or {}
    bid = quote.get("bidPrice")
    ask = quote.get("askPrice")
    if not bid or not ask:
        return None, None
    return bid, ask


def is_halted(quotes: dict, symbol: str) -> bool:
    """Best-effort halt/anomaly check from Schwab's quote securityStatus field
    (e.g. "Normal" when trading normally). Treat anything else, or a missing
    quote, as not-safe-to-trade."""
    entry = quotes.get(symbol) or quotes.get(symbol.upper())
    if not entry:
        return True
    status = (entry.get("quote") or {}).get("securityStatus")
    return status is not None and status != "Normal"
