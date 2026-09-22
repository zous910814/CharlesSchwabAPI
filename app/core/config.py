from functools import lru_cache
from typing import Optional

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings

load_dotenv()


class Settings(BaseSettings):
    schwab_client_id: str = Field(..., env="SCHWAB_CLIENT_ID")
    schwab_client_secret: str = Field(..., env="SCHWAB_CLIENT_SECRET")
    schwab_redirect_uri: str = Field(..., env="SCHWAB_REDIRECT_URI")
    schwab_refresh_token: str = Field(..., env="SCHWAB_REFRESH_TOKEN")
    schwab_account_id: str = Field(..., env="SCHWAB_ACCOUNT_ID")
    schwab_base_url: str = Field("https://api.schwabapi.com", env="SCHWAB_BASE_URL")
    schwab_dry_run: bool = Field(True, env="SCHWAB_DRY_RUN")

    # Live-monitor loop (app/core/monitor.py) — generic, strategy-agnostic
    # risk controls that apply no matter what bought the position.
    monitor_poll_seconds: int = Field(60, env="MONITOR_POLL_SECONDS")
    # Stop-loss: force-sell a held position once it's down this fraction from
    # average cost, regardless of the K/D signal.
    schwab_stop_loss_pct: float = Field(0.10, env="SCHWAB_STOP_LOSS_PCT")
    # Circuit breaker: pause new BUY/SELL trades (stop-loss still runs) once
    # today's floating P&L drops below this fraction of account value.
    schwab_daily_loss_limit_pct: float = Field(0.03, env="SCHWAB_DAILY_LOSS_LIMIT_PCT")

    # LINE risk notifications (app/core/line_notify.py). All optional — unset
    # means notifications are silently skipped, not an error.
    line_channel_access_token: Optional[str] = Field(None, env="LINE_CHANNEL_ACCESS_TOKEN")
    line_channel_secret: Optional[str] = Field(None, env="LINE_CHANNEL_SECRET")
    line_user_id: Optional[str] = Field(None, env="LINE_USER_ID")

    # Daily price-history pipeline (app/core/data_pipeline.py + price_history_db.py):
    # movers-based candidate universe -> daily OHLCV backfill into Postgres.
    # Optional — pipeline no-ops (logs + skips) if PG_DSN is unset, so a missing
    # Postgres config never crashes the rest of the app. KD's golden/death-cross
    # signal and its daily candidate pool both read straight from this table.
    pg_dsn: Optional[str] = Field(None, env="PG_DSN")
    price_history_movers_indices: str = Field("$DJI,$COMPX,$SPX", env="PRICE_HISTORY_MOVERS_INDICES")
    price_history_movers_sort: str = Field("VOLUME", env="PRICE_HISTORY_MOVERS_SORT")
    price_history_movers_frequency: int = Field(0, env="PRICE_HISTORY_MOVERS_FREQUENCY")
    price_history_backfill_days: int = Field(60, env="PRICE_HISTORY_BACKFILL_DAYS")
    # Fixed training-data universe (in addition to the movers-based daily
    # candidates above) — a flat JSON array of symbols, kept up to date by
    # hand. Backfilled every run alongside the movers universe so historical
    # coverage (and KD's candidate pool) stays consistent day to day.
    price_history_universe_path: str = Field("sp500_constituents.json", env="PRICE_HISTORY_UNIVERSE_PATH")
    # "HH:MM" 24h, America/New_York. Default is 5 hours before the 9:30am ET
    # open, so the previous day's (already settled) bar is loaded and KD's
    # recommendation has time to run before trading starts.
    price_history_run_time_et: str = Field("04:30", env="PRICE_HISTORY_RUN_TIME_ET")

    # Daily KD golden/death-cross swing strategy (app/core/kd_watchlist.py) —
    # AUTO-TRADES for real (dry-run or live per SCHWAB_DRY_RUN above), not
    # notification-only; see that module's docstring. Runs in the same
    # 04:30 ET job as the price-history backfill above. Its death-cross/stop
    # exit is also re-checked every monitor poll (~monitor_poll_seconds) off
    # today's still-forming bar, not just once a day off last night's close.
    kd_watchlist_enabled: bool = Field(True, env="KD_WATCHLIST_ENABLED")
    # Pattern Day Trader guard: on a margin account under $25k equity, FINRA
    # flags the account once it racks up more than this many day trades
    # (same-symbol buy+sell same calendar day) in a rolling 5 business-day
    # window - triggering a 90-day closing-only restriction. KD's own
    # same-day intraday exit (death-cross, not a hard stop) is the only path
    # in this app that can create one; see kd_watchlist._exit_kd_position's
    # PDT guard. Schwab's account payload already tracks the rolling count
    # itself (securitiesAccount.roundTrips), so this is just the threshold
    # at which that path defers instead of firing - default leaves zero
    # headroom past the 3 FINRA allows before the 4th trips the flag.
    pdt_round_trip_limit: int = Field(3, env="PDT_ROUND_TRIP_LIMIT")

    class Config:
        case_sensitive = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
