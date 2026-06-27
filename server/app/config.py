"""Application configuration via environment variables.

Uses pydantic-settings for type-safe config with .env support.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    # App
    app_name: str = "OpenInterest Lens"
    app_version: str = "0.1.0"
    debug: bool = False

    # Database
    database_url: str = "sqlite+aiosqlite:///./openinterest_lens.db"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # API
    api_prefix: str = "/v1"
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:8000"]
    cors_allow_methods: list[str] = ["GET", "POST", "OPTIONS"]
    cors_allow_headers: list[str] = ["X-API-Key", "Authorization", "Content-Type", "Accept"]

    # Auth
    master_api_key: str = "oil_sk_live_master_key_change_me"

    # Rate limits (requests per hour per tier)
    rate_limit_free: int = 60
    rate_limit_pro: int = 600
    rate_limit_enterprise: int = 6000

    # Tier definitions
    tier_free_contracts: list[str] = ["ES", "NQ", "CL"]
    tier_free_history_weeks: int = 4
    tier_pro_max_contracts: int = 50
    tier_pro_history_weeks: int = 104
    tier_enterprise_history_weeks: int = 260

    # Logging
    log_level: str = "info"

    model_config = {"env_prefix": "OIL_", "env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()