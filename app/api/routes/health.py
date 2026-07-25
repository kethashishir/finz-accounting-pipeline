"""Liveness and readiness API endpoints."""

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from app import __version__
from app.core.config import Settings
from app.db.client import MongoDatabase
from app.models.health import (
    DependencyHealth,
    LivenessResponse,
    ReadinessResponse,
)

router = APIRouter(prefix="/health", tags=["health"])
logger = structlog.get_logger(__name__)


@router.get("/live", response_model=LivenessResponse)
async def liveness(request: Request) -> LivenessResponse:
    """Report that the API process is running."""

    settings: Settings = request.app.state.settings

    return LivenessResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.app_env,
        version=__version__,
        timestamp=datetime.now(UTC),
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessResponse}},
)
async def readiness(request: Request) -> ReadinessResponse | JSONResponse:
    """Report whether required infrastructure is available."""

    settings: Settings = request.app.state.settings
    mongodb: MongoDatabase = request.app.state.mongodb
    timestamp = datetime.now(UTC)

    try:
        if not await mongodb.ping():
            raise RuntimeError("MongoDB ping returned a non-success status")
    except (PyMongoError, RuntimeError):
        logger.exception("mongodb_readiness_check_failed")

        response = ReadinessResponse(
            status="not_ready",
            service=settings.app_name,
            environment=settings.app_env,
            version=__version__,
            timestamp=timestamp,
            dependencies={
                "mongodb": DependencyHealth(
                    status="error",
                    detail="MongoDB is unavailable",
                )
            },
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=response.model_dump(mode="json"),
        )

    return ReadinessResponse(
        status="ok",
        service=settings.app_name,
        environment=settings.app_env,
        version=__version__,
        timestamp=timestamp,
        dependencies={
            "mongodb": DependencyHealth(
                status="ok",
                detail="MongoDB connection succeeded",
            )
        },
    )
