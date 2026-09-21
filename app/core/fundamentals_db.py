"""Daily fundamentals snapshot: persists the *entire* payload from Schwab's
per-symbol fundamental projection (app.clients.schwab.SchwabClient.
get_fundamentals) into Postgres, one row per (symbol, day), so a future model
can join fundamentals onto the price_history features already built for
app.core.kd_watchlist without re-fetching fields we skipped the first time.
Shares the connection pool started by price_history_db.init_pool — this
module only owns its own table.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from app.core import price_history_db

logger = logging.getLogger(__name__)

# Instrument-level metadata that sits alongside (not inside) the `fundamental`
# block in Schwab's /instruments response.
TOP_LEVEL_FIELD_MAP = [
    ("cusip", "cusip", "TEXT"),
    ("description", "description", "TEXT"),
    ("exchange", "exchange", "TEXT"),
    ("assetType", "asset_type", "TEXT"),
]

# Every field Schwab's fundamental projection returns (instruments[0].fundamental),
# mapped from its camelCase JSON key to a snake_case column. Date/string fields
# are kept as TEXT rather than DATE — some come back as empty strings (e.g. no
# upcoming dividend), which a DATE column would reject outright.
FUNDAMENTAL_FIELD_MAP = [
    ("high52", "high_52", "DOUBLE PRECISION"),
    ("low52", "low_52", "DOUBLE PRECISION"),
    ("dividendAmount", "dividend_amount", "DOUBLE PRECISION"),
    ("dividendYield", "dividend_yield", "DOUBLE PRECISION"),
    ("dividendDate", "dividend_date", "TEXT"),
    ("peRatio", "pe_ratio", "DOUBLE PRECISION"),
    ("pegRatio", "peg_ratio", "DOUBLE PRECISION"),
    ("pbRatio", "pb_ratio", "DOUBLE PRECISION"),
    ("prRatio", "pr_ratio", "DOUBLE PRECISION"),
    ("pcfRatio", "pcf_ratio", "DOUBLE PRECISION"),
    ("grossMarginTTM", "gross_margin_ttm", "DOUBLE PRECISION"),
    ("grossMarginMRQ", "gross_margin_mrq", "DOUBLE PRECISION"),
    ("netProfitMarginTTM", "net_profit_margin_ttm", "DOUBLE PRECISION"),
    ("netProfitMarginMRQ", "net_profit_margin_mrq", "DOUBLE PRECISION"),
    ("operatingMarginTTM", "operating_margin_ttm", "DOUBLE PRECISION"),
    ("operatingMarginMRQ", "operating_margin_mrq", "DOUBLE PRECISION"),
    ("returnOnEquity", "return_on_equity", "DOUBLE PRECISION"),
    ("returnOnAssets", "return_on_assets", "DOUBLE PRECISION"),
    ("returnOnInvestment", "return_on_investment", "DOUBLE PRECISION"),
    ("quickRatio", "quick_ratio", "DOUBLE PRECISION"),
    ("currentRatio", "current_ratio", "DOUBLE PRECISION"),
    ("interestCoverage", "interest_coverage", "DOUBLE PRECISION"),
    ("totalDebtToCapital", "total_debt_to_capital", "DOUBLE PRECISION"),
    ("ltDebtToEquity", "lt_debt_to_equity", "DOUBLE PRECISION"),
    ("totalDebtToEquity", "total_debt_to_equity", "DOUBLE PRECISION"),
    ("epsTTM", "eps_ttm", "DOUBLE PRECISION"),
    ("epsChangePercentTTM", "eps_change_percent_ttm", "DOUBLE PRECISION"),
    ("epsChangeYear", "eps_change_year", "DOUBLE PRECISION"),
    ("epsChange", "eps_change", "DOUBLE PRECISION"),
    ("eps", "eps", "DOUBLE PRECISION"),
    ("revChangeYear", "rev_change_year", "DOUBLE PRECISION"),
    ("revChangeTTM", "rev_change_ttm", "DOUBLE PRECISION"),
    ("revChangeIn", "rev_change_in", "DOUBLE PRECISION"),
    ("sharesOutstanding", "shares_outstanding", "DOUBLE PRECISION"),
    ("marketCapFloat", "market_cap_float", "DOUBLE PRECISION"),
    ("marketCap", "market_cap", "DOUBLE PRECISION"),
    ("bookValuePerShare", "book_value_per_share", "DOUBLE PRECISION"),
    ("shortIntToFloat", "short_int_to_float", "DOUBLE PRECISION"),
    ("shortIntDayToCover", "short_int_day_to_cover", "DOUBLE PRECISION"),
    ("divGrowthRate3Year", "div_growth_rate_3_year", "DOUBLE PRECISION"),
    ("dividendPayAmount", "dividend_pay_amount", "DOUBLE PRECISION"),
    ("dividendPayDate", "dividend_pay_date", "TEXT"),
    ("beta", "beta", "DOUBLE PRECISION"),
    ("vol1DayAvg", "vol_1_day_avg", "DOUBLE PRECISION"),
    ("vol10DayAvg", "vol_10_day_avg", "DOUBLE PRECISION"),
    ("vol3MonthAvg", "vol_3_month_avg", "DOUBLE PRECISION"),
    ("avg1DayVolume", "avg_1_day_volume", "DOUBLE PRECISION"),
    ("avg10DaysVolume", "avg_10_days_volume", "DOUBLE PRECISION"),
    ("avg3MonthVolume", "avg_3_month_volume", "DOUBLE PRECISION"),
    ("avg1YearVolume", "avg_1_year_volume", "DOUBLE PRECISION"),
    ("declarationDate", "declaration_date", "TEXT"),
    ("dividendFreq", "dividend_freq", "DOUBLE PRECISION"),
    ("dtnVolume", "dtn_volume", "DOUBLE PRECISION"),
    ("nextDividendPayDate", "next_dividend_pay_date", "TEXT"),
    ("nextDividendDate", "next_dividend_date", "TEXT"),
    # Fund-only fields (ETFs/mutual funds) — NULL for ordinary equities, kept
    # so the same table covers both without a second schema.
    ("fundLeverageFactor", "fund_leverage_factor", "DOUBLE PRECISION"),
    ("fundStrategy", "fund_strategy", "TEXT"),
    ("corpactionDate", "corpaction_date", "TEXT"),
]

ALL_FIELD_MAP = TOP_LEVEL_FIELD_MAP + FUNDAMENTAL_FIELD_MAP

_columns_sql = ",\n    ".join(f"{col} {sql_type}" for _, col, sql_type in ALL_FIELD_MAP)
CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol      TEXT NOT NULL,
    as_of_date  DATE NOT NULL,
    {_columns_sql},
    source      TEXT NOT NULL DEFAULT 'schwab',
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, as_of_date)
)
"""

_INSERT_COLUMNS = ["symbol", "as_of_date"] + [col for _, col, _ in ALL_FIELD_MAP] + ["source"]
UPSERT_SQL = (
    f"INSERT INTO fundamentals ({', '.join(_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join(f'${i + 1}' for i in range(len(_INSERT_COLUMNS)))}) "
    "ON CONFLICT (symbol, as_of_date) DO UPDATE SET "
    + ", ".join(f"{col} = EXCLUDED.{col}" for col in _INSERT_COLUMNS if col not in ("symbol", "as_of_date"))
    + ", fetched_at = now()"
)


async def ensure_table() -> None:
    """Idempotent; a no-op if Postgres isn't configured (mirrors
    price_history_db.is_ready's fail-open behavior). Called once at app
    startup, after price_history_db.init_pool."""
    pool = price_history_db.get_pool()
    if pool is None:
        return
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLE_SQL)


def is_ready() -> bool:
    return price_history_db.get_pool() is not None


def parse_instrument(instrument_response: dict) -> Optional[dict]:
    """Flatten a get_fundamentals() response into one dict keyed by the JSON
    field names in ALL_FIELD_MAP (top-level instrument metadata plus the
    nested `fundamental` block). None only if Schwab returned no match for
    the symbol at all — a match with a mostly-empty fundamental block (e.g.
    some ETFs) still comes back as a dict, so its metadata still gets stored."""
    instruments = instrument_response.get("instruments") or []
    if not instruments:
        return None
    instrument = instruments[0]
    fundamental = instrument.get("fundamental") or {}
    merged = {key: instrument.get(key) for key, _, _ in TOP_LEVEL_FIELD_MAP}
    merged.update({key: fundamental.get(key) for key, _, _ in FUNDAMENTAL_FIELD_MAP})
    return merged


async def upsert_fundamental(symbol: str, as_of_date: date, parsed: dict, *, source: str = "schwab") -> int:
    pool = price_history_db.get_pool()
    if pool is None or parsed is None:
        return 0
    values = [symbol, as_of_date] + [parsed.get(key) for key, _, _ in ALL_FIELD_MAP] + [source]
    async with pool.acquire() as conn:
        await conn.execute(UPSERT_SQL, *values)
    return 1


async def fetch_all_fundamentals() -> list[dict]:
    """Every symbol's full fundamentals history, oldest first per symbol —
    for joining onto price_history features when training a model."""
    pool = price_history_db.get_pool()
    if pool is None:
        return []
    columns = ", ".join(col for _, col, _ in ALL_FIELD_MAP)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT symbol, as_of_date, {columns} FROM fundamentals ORDER BY symbol, as_of_date"
        )
    return [dict(r) for r in rows]
