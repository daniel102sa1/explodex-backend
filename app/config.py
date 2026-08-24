from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "ExplodeX Backend"
    app_env: str = "development"
    database_url: str
    binance_futures_base_url: str = "https://fapi.binance.com"

    scanner_min_quote_volume_usdt: float = 10_000_000
    scanner_max_symbols: int = 80
    scanner_min_setup_score: float = 80
    scanner_max_risk_score: float = 35
    scanner_deep_limit: int = 20

    paper_trading_only: bool = True

    # Optional contextual enrichment. News is deliberately secondary and capped.
    news_enabled: bool = True
    news_max_candidates: int = 8
    news_max_headlines: int = 10
    news_cache_ttl_seconds: int = 900

    # Automatic runtime loops. These values are deliberately conservative so
    # Railway usage and Binance API traffic remain controlled in v1.
    scheduler_enabled: bool = True
    scanner_interval_seconds: int = 300
    paper_manage_interval_seconds: int = 60
    paper_sync_interval_seconds: int = 300

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
