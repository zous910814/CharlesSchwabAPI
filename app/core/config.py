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

    # Live-monitor loop (app/core/monitor.py)
    watchlist_path: str = Field("watchlist.json", env="WATCHLIST_PATH")
    monitor_poll_seconds: int = Field(60, env="MONITOR_POLL_SECONDS")
    # Risk cap: max fraction of buying power a single BUY signal may spend.
    schwab_max_position_pct: float = Field(0.05, env="SCHWAB_MAX_POSITION_PCT")
    # Risk cap: max fraction of total account value one symbol's position may
    # reach in total (across repeated BUYs), independent of the per-trade cap above.
    schwab_max_symbol_exposure_pct: float = Field(0.15, env="SCHWAB_MAX_SYMBOL_EXPOSURE_PCT")
    # Stop-loss: force-sell a held position once it's down this fraction from
    # average cost, regardless of the K/D signal.
    schwab_stop_loss_pct: float = Field(0.10, env="SCHWAB_STOP_LOSS_PCT")
    # Circuit breaker: pause new BUY/SELL trades (stop-loss still runs) once
    # today's floating P&L drops below this fraction of account value.
    schwab_daily_loss_limit_pct: float = Field(0.03, env="SCHWAB_DAILY_LOSS_LIMIT_PCT")
    # Risk cap: max total notional value of BUY orders submitted today, as a
    # fraction of account value. Tracked by the monitor itself (not read back
    # from Schwab) so it still holds even if the account's reported buying
    # power hasn't yet caught up with an order submitted moments earlier.
    schwab_max_daily_order_value_pct: float = Field(0.20, env="SCHWAB_MAX_DAILY_ORDER_VALUE_PCT")

    # ORB (Opening Range Breakout) strategy — app/strategy/orb.py + monitor.py.
    # Position sizing: shares = floor(account_equity * risk_pct / |entry - stop|).
    schwab_orb_risk_pct: float = Field(0.0015, env="SCHWAB_ORB_RISK_PCT")
    # Initial stop = entry - max(stop_atr_mult * ATR(14), entry - OR_High).
    schwab_orb_stop_atr_mult: float = Field(0.5, env="SCHWAB_ORB_STOP_ATR_MULT")
    schwab_orb_rvol_min: float = Field(1.5, env="SCHWAB_ORB_RVOL_MIN")
    schwab_orb_spread_max_pct: float = Field(0.0005, env="SCHWAB_ORB_SPREAD_MAX_PCT")
    schwab_orb_or_width_min_atr: float = Field(0.3, env="SCHWAB_ORB_OR_WIDTH_MIN_ATR")
    schwab_orb_or_width_max_atr: float = Field(1.5, env="SCHWAB_ORB_OR_WIDTH_MAX_ATR")
    schwab_orb_max_trades_per_symbol: int = Field(2, env="SCHWAB_ORB_MAX_TRADES_PER_SYMBOL")
    schwab_orb_max_consecutive_losses: int = Field(3, env="SCHWAB_ORB_MAX_CONSECUTIVE_LOSSES")
    schwab_orb_time_stop_minutes: int = Field(15, env="SCHWAB_ORB_TIME_STOP_MINUTES")
    schwab_orb_time_stop_r: float = Field(0.5, env="SCHWAB_ORB_TIME_STOP_R")
    schwab_orb_target1_r: float = Field(1.0, env="SCHWAB_ORB_TARGET1_R")
    schwab_orb_target2_r: float = Field(1.5, env="SCHWAB_ORB_TARGET2_R")

    # LINE risk notifications (app/core/line_notify.py). All optional — unset
    # means notifications are silently skipped, not an error.
    line_channel_access_token: Optional[str] = Field(None, env="LINE_CHANNEL_ACCESS_TOKEN")
    line_channel_secret: Optional[str] = Field(None, env="LINE_CHANNEL_SECRET")
    line_user_id: Optional[str] = Field(None, env="LINE_USER_ID")

    class Config:
        case_sensitive = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
