"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app import __version__
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.client import MongoDatabase
from app.repositories.ingestion import IngestionRepository

logger = structlog.get_logger(__name__)


def build_lifespan(settings: Settings):
    """Build an application lifespan bound to the supplied settings."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mongodb = MongoDatabase(
            uri=settings.mongodb_uri,
            database_name=settings.mongodb_database,
        )
        app.state.mongodb = mongodb
        app.state.ingestion_repository = IngestionRepository(mongodb.database)

        logger.info(
            "application_started",
            environment=settings.app_env,
            version=__version__,
        )

        try:
            yield
        finally:
            await mongodb.close()
            logger.info("application_stopped")

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure a FastAPI application instance."""

    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level)

    application = FastAPI(
        title=app_settings.app_name,
        version=__version__,
        debug=app_settings.app_debug,
        description=(
            "Accounting data ingestion, classification, QuickBooks sync, "
            "and cash-basis P&L reconciliation."
        ),
        lifespan=build_lifespan(app_settings),
    )

    application.state.settings = app_settings
    application.include_router(api_router)

    return application


app = create_app()
