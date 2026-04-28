from datetime import time
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field
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
    poll_interval_sec: float = Field(default=5.0, alias="POLL_INTERVAL_SEC")
    symbol_display: str = Field(default="MXF", alias="SYMBOL_DISPLAY")
    symbol_source: str = Field(default="TAIEX", alias="SYMBOL_SOURCE")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
