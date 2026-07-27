"""Top-level API router."""

from fastapi import APIRouter

from app.api.routes import (
    classification,
    health,
    ingestion,
    reporting,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(ingestion.router)
api_router.include_router(classification.router)
api_router.include_router(reporting.router)
