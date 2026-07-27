"""Typed application configuration loaded from environment variables."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the Finz accounting application."""

    app_name: str = "Finz Accounting Pipeline"
    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_debug: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "finz_accounting"

    gemini_api_key: SecretStr | None = None
    gemini_model: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    @field_validator(
        "gemini_api_key",
        "gemini_model",
        mode="before",
    )
    @classmethod
    def normalize_optional_gemini_settings(
        cls,
        value: object,
    ) -> object:
        """Convert blank Gemini settings to disabled values."""

        if isinstance(value, str):
            normalized = value.strip()
            return normalized or None

        return value

    @field_validator("mongodb_database")
    @classmethod
    def validate_database_name(cls, value: str) -> str:
        """Reject empty or unsafe MongoDB database names."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("MongoDB database name cannot be empty")

        if len(normalized) > 63:
            raise ValueError("MongoDB database name cannot exceed 63 characters")

        forbidden_characters = set('/\\."$*<>:|? ')
        if any(character in forbidden_characters for character in normalized):
            raise ValueError("MongoDB database name contains an unsafe character")

        return normalized


@lru_cache
def get_settings() -> Settings:
    """Return one immutable settings instance per process."""

    return Settings()
