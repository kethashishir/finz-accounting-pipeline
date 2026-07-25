"""Health endpoint response models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class LivenessResponse(BaseModel):
    """Response returned when the FastAPI process is alive."""

    status: Literal["ok"]
    service: str
    environment: str
    version: str
    timestamp: datetime


class DependencyHealth(BaseModel):
    """Status of one required application dependency."""

    status: Literal["ok", "error"]
    detail: str


class ReadinessResponse(BaseModel):
    """Response describing whether the application can accept work."""

    status: Literal["ok", "not_ready"]
    service: str
    environment: str
    version: str
    timestamp: datetime
    dependencies: dict[str, DependencyHealth]
