from datetime import time
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    finmind_token: str = Field(default="", alias="FINMIND_TOKEN")
    database_url: str = Field(
        default="postgresql+asyncpg://taiex:taiex@localhost:5432/taiex",
        alias="DATABASE_URL",
    )
    discord_webhook_url: str = Field(default="", alias="DISCORD_WEBHOOK_URL")
    n8n_webhook_url: str = Field(default="", alias="N8N_WEBHOOK_URL")
    alert_secret: str = Field(default="", alias="ALERT_SECRET")
    timezone: str = Field(default="Asia/Taipei", alias="TIMEZONE")
    market_open: time = Field(default=time(8, 45), alias="MARKET_OPEN")
    market_close: time = Field(default=time(13, 45), alias="MARKET_CLOSE")
    # TAIFEX 夜盤 (after-hours): 15:00 today through 05:00 the next morning.
    # Mon-Fri evening starts a session; the following morning continues it.
    # Saturday morning <=05:00 belongs to Friday's night session; Sat after
    # 05:00 and all of Sunday are closed.
    night_session_open: time = Field(default=time(15, 0), alias="NIGHT_SESSION_OPEN")
    night_session_close: time = Field(default=time(5, 0), alias="NIGHT_SESSION_CLOSE")
    poll_interval_sec: float = Field(default=5.0, alias="POLL_INTERVAL_SEC")
    symbol_display: str = Field(default="MXF", alias="SYMBOL_DISPLAY")
    # FinMind taiwan_futures_snapshot only serves TXF / TMF / CDF on the
    # sponsor tier — MXF (小台) returns 0 rows. Keep source = TXF so the feed
    # stays alive; SYMBOL_DISPLAY decouples the UI label.
    symbol_source: str = Field(default="TXF", alias="SYMBOL_SOURCE")

    # Historical backfill (V2.5) — TaiwanFuturesTick dataset for gap fills
    # and backtesting.
    backfill_data_id: str = Field(default="MTX", alias="BACKFILL_DATA_ID")
    backfill_on_startup_days: int = Field(default=7, alias="BACKFILL_ON_STARTUP_DAYS")
    backfill_min_ticks_per_day: int = Field(default=1000, alias="BACKFILL_MIN_TICKS_PER_DAY")

    # AI insights (Anthropic Claude Sonnet) — V2 strategy analysis page
    anthropic_api_key: SecretStr | None = Field(
        default=None, alias="TAIEX_ANTHROPIC_API_KEY"
    )
    anthropic_model: str = Field(default="claude-sonnet-4-6", alias="ANTHROPIC_MODEL")
    insights_cache_ttl_seconds: int = Field(
        default=1800, alias="INSIGHTS_CACHE_TTL_SECONDS"
    )
    insights_cache_max_entries: int = Field(
        default=256, alias="INSIGHTS_CACHE_MAX_ENTRIES"
    )

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
