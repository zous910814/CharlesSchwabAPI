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

    class Config:
        case_sensitive = False


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
