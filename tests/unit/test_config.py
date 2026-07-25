"""Tests for safe application configuration."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_mongodb_database_accepts_63_characters() -> None:
    """MongoDB permits a database name up to 63 characters."""

    database_name = "a" * 63

    settings = Settings(mongodb_database=database_name)

    assert settings.mongodb_database == database_name


def test_mongodb_database_rejects_more_than_63_characters() -> None:
    """An invalid name fails before making a MongoDB request."""

    with pytest.raises(
        ValidationError,
        match="cannot exceed 63 characters",
    ):
        Settings(mongodb_database="a" * 64)
