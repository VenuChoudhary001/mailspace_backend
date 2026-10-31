import os
from functools import lru_cache

from pydantic_settings import BaseSettings
from pydantic import Field, field_validator

class Settings(BaseSettings):
    app_name: str = "Mailspace API"
    app_version: str = "0.1.0"
    debug: bool = False

    forward_from_address: str  = Field(default="mail@relayhost.site")   # on your verified Resend domain
    webhook_secret: bytes   = Field(..., validation_alias="WEBHOOK_SECRET")      # same secret the Postfix pipe script uses
    forward_brand: str = "Mail Forwarder"
    max_raw_bytes: int = 25 * 1024 * 1024

    resend_api_key: str = Field(..., validation_alias="RESEND_API_KEY")
    email_from: str = "mailspace@mailspace.shop"

    supabase_db_password: str = Field(..., validation_alias="SUPABASE_DB_PASSWORD")
    supabase_url: str = Field(..., validation_alias="SUPABASE_URL")
    supabase_secret: str = Field(..., validation_alias="SUPABASE_SECRET")

    environment: str = "development"  # development | staging | production
    log_level: str = "INFO"

    host: str = "0.0.0.0"
    port: int = 8000
    # Number of worker processes (one event loop each). Standard env var name.
    web_concurrency: int = max(1, min(os.cpu_count() or 1, 8))

    # Max simultaneous in-flight requests PER WORKER before returning 503.
    max_concurrent_requests: int = 1000
    # Max threads for sync endpoints / run_in_threadpool PER WORKER.
    threadpool_size: int = 64

    # Seconds to let in-flight requests finish on shutdown.
    graceful_shutdown_timeout: int = 30
    keep_alive_timeout: int = 5

    cors_origins: list[str] = []
    upstream_timeout_seconds: float = 10.0

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def is_dev(self) -> bool:
        return self.environment == "development"

    @property
    def max_request_bytes(self) -> int:
        return (self.max_raw_bytes * 4)

    @field_validator("webhook_secret", mode="before")
    @classmethod
    def _strip_secret(cls, v):
        return v.strip() if isinstance(v, (str, bytes)) else v


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
