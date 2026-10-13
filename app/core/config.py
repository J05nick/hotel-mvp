"""Application settings.

12-Factor App (III. Config): every tunable comes from the process environment.
A `.env` file is honoured only as a developer convenience; in containers the
orchestrator injects real environment variables.
"""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    app_name: str = "hotel-mvp"
    environment: Environment = Environment.DEVELOPMENT
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")

    # --- PostgreSQL ---------------------------------------------------------
    # Driver: psycopg 3 serves both the async engine (API) and the sync engine
    # (Alembic, Celery) with a single dependency.
    database_url: PostgresDsn
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=20, ge=0)
    db_pool_timeout_seconds: int = Field(default=10, ge=1)
    # Upper bound for waiting on a row lock (SELECT ... FOR UPDATE). Prevents
    # request pile-ups when a hot room is contended; surfaces as HTTP 409/503.
    db_lock_timeout_ms: int = Field(default=5_000, ge=0)
    db_statement_timeout_ms: int = Field(default=15_000, ge=0)

    # --- Redis / Celery -----------------------------------------------------
    redis_url: RedisDsn
    celery_broker_url: RedisDsn
    celery_result_backend: RedisDsn

    # --- Booking domain -----------------------------------------------------
    booking_hold_ttl_seconds: int = Field(default=300, ge=30, le=3_600)

    # --- HTTP ---------------------------------------------------------------
    # NoDecode: accept both JSON (`["a","b"]`) and CSV (`a,b`) from the env.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if raw.startswith("["):
            return json.loads(raw)
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide singleton. Cached so env parsing happens once per process."""
    # Required fields (DATABASE_URL, REDIS_URL, ...) are read from the environment.
    return Settings()
