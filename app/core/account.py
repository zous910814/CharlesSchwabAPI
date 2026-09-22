from __future__ import annotations

from typing import Optional


def extract_buying_power(account: dict) -> Optional[float]:
    balances = (account.get("securitiesAccount") or {}).get("currentBalances") or {}
    # Field name depends on account type (margin vs cash); try the common ones.
    for key in ("buyingPower", "cashAvailableForTrading", "availableFunds", "cashBalance"):
        value = balances.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def extract_equity(account: dict) -> Optional[float]:
    """Total account value if liquidated right now: cash + market value of all
    positions. Schwab reports this as equity/liquidationValue (same number)."""
    balances = (account.get("securitiesAccount") or {}).get("currentBalances") or {}
    for key in ("liquidationValue", "equity"):
        value = balances.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def extract_position_qty(account: dict, symbol: str) -> float:
    positions = (account.get("securitiesAccount") or {}).get("positions") or []
    for pos in positions:
        instrument = pos.get("instrument") or {}
        if instrument.get("symbol") == symbol:
            return float(pos.get("longQuantity", 0)) - float(pos.get("shortQuantity", 0))
    return 0.0


def extract_position_market_value(account: dict, symbol: str) -> float:
    positions = (account.get("securitiesAccount") or {}).get("positions") or []
    for pos in positions:
        instrument = pos.get("instrument") or {}
        if instrument.get("symbol") == symbol:
            value = pos.get("marketValue")
            return float(value) if isinstance(value, (int, float)) else 0.0
    return 0.0


def extract_round_trips(account: dict) -> Optional[int]:
    """Day trades in the trailing 5 rolling business days - what a margin
    account's Pattern Day Trader flag counts against (FINRA flags the
    account once this exceeds 3, i.e. on the 4th). Only meaningful on a
    margin account under $25k equity; see kd_watchlist's PDT guard."""
    value = (account.get("securitiesAccount") or {}).get("roundTrips")
    return int(value) if isinstance(value, (int, float)) else None


def extract_day_pl(account: dict) -> float:
    """Sum of today's floating P&L across currently open positions. Does not
    capture realized P&L from positions already closed earlier today — Schwab
    doesn't expose an account-level day P&L field, only per-position."""
    positions = (account.get("securitiesAccount") or {}).get("positions") or []
    return sum(float(pos.get("currentDayProfitLoss") or 0.0) for pos in positions)


def parse_positions(account: dict) -> list[dict]:
    """Held symbols with cost basis and unrealized P&L, derived from Schwab's
    account/positions response (fields=positions)."""
    positions = (account.get("securitiesAccount") or {}).get("positions") or []
    parsed: list[dict] = []

    for pos in positions:
        instrument = pos.get("instrument") or {}
        symbol = instrument.get("symbol")
        if not symbol:
            continue

        long_qty = float(pos.get("longQuantity", 0) or 0)
        short_qty = float(pos.get("shortQuantity", 0) or 0)
        quantity = long_qty - short_qty
        if quantity == 0:
            continue

        average_price = pos.get("averagePrice")
        market_value = pos.get("marketValue")
        current_price = (market_value / quantity) if isinstance(market_value, (int, float)) and quantity else None

        unrealized_pl = None
        unrealized_pl_pct = None
        if isinstance(average_price, (int, float)) and isinstance(market_value, (int, float)):
            cost_basis = average_price * quantity
            unrealized_pl = market_value - cost_basis
            if cost_basis:
                unrealized_pl_pct = unrealized_pl / cost_basis

        parsed.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "average_price": average_price,
                "current_price": current_price,
                "market_value": market_value,
                "unrealized_pl": unrealized_pl,
                "unrealized_pl_pct": unrealized_pl_pct,
                "day_pl": pos.get("currentDayProfitLoss"),
                "day_pl_pct": pos.get("currentDayProfitLossPercentage"),
            }
        )

    return parsed
