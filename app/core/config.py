from functools import lru_cache
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

    class Config:
        case_sensitive = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
