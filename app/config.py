"""Settings, read from the environment (or a local `.env`)."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://hotel:hotel@localhost:5432/hotel"
    booking_hold_ttl_seconds: int = Field(default=300, ge=1)
    # 0 disables the background sweeper (tests drive it by hand).
    housekeeping_interval_seconds: int = Field(default=60, ge=0)
    seed_demo_data: bool = False
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        raw = value.strip()
        if raw.startswith("["):
            return json.loads(raw)
        return [origin.strip() for origin in raw.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
