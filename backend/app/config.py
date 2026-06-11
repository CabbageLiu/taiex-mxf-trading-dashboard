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
    symbol_display: str = Field(default="MXF", alias="SYMBOL_DISPLAY")

    # Shioaji (SinoPac) credentials — live + historical feed
    shioaji_api_key: SecretStr | None = Field(default=None, alias="SHIOAJI_API_KEY")
    shioaji_secret_key: SecretStr | None = Field(default=None, alias="SHIOAJI_SECRET_KEY")
    shioaji_ca_cert_path: str = Field(default="", alias="SHIOAJI_CA_CERT_PATH")
    shioaji_ca_password: SecretStr | None = Field(default=None, alias="SHIOAJI_CA_PASSWORD")
    shioaji_person_id: str = Field(default="", alias="SHIOAJI_PERSON_ID")
    shioaji_simulation: bool = Field(default=False, alias="SHIOAJI_SIMULATION")
    # Contract code subscribed for live + historical. TXFR1 = TXF rolling
    # near-month alias; SinoPac auto-rolls on expiry. DB rows are still
    # labelled with `symbol_display` (e.g. MXF) since TXF + MXF track the
    # same TAIEX index and strategies are agnostic.
    shioaji_contract: str = Field(default="TXFR1", alias="SHIOAJI_CONTRACT")
    # Bound on the in-process tick queue between the SDK callback thread
    # and the asyncio consumer. Oldest tick is dropped on overflow.
    shioaji_queue_maxsize: int = Field(default=10_000, alias="SHIOAJI_QUEUE_MAXSIZE")
    # Cool-down between login attempts (daily login cap = 1000/day).
    shioaji_login_cooldown_sec: float = Field(
        default=30.0, alias="SHIOAJI_LOGIN_COOLDOWN_SEC"
    )

    # Feed-health watchdog — detects silent tick-starvation (the SDK's own
    # auto-reconnect can fail permanently with the session stuck "down") and
    # forces a full re-login during market hours. See IngestRunner.
    feed_watchdog_enabled: bool = Field(default=True, alias="FEED_WATCHDOG_ENABLED")
    # Tick silence (s) during an open session before forcing a reconnect.
    feed_stale_seconds: float = Field(default=90.0, alias="FEED_STALE_SECONDS")
    # Upper bound on the exponential backoff between forced reconnects.
    feed_reconnect_backoff_max_sec: float = Field(
        default=300.0, alias="FEED_RECONNECT_BACKOFF_MAX_SEC"
    )
    # Hard cap on forced reconnects per market session (protects the
    # 1000-logins/day SinoPac budget when the feed will not recover).
    feed_max_reconnects_per_session: int = Field(
        default=10, alias="FEED_MAX_RECONNECTS_PER_SESSION"
    )

    # Historical backfill — gap fills and backtesting via Shioaji `api.ticks`.
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
