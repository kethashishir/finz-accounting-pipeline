"""FastAPI application entry point."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.client import MongoDatabase
from app.repositories.classification import ClassificationRepository
from app.repositories.classification_pattern import (
    ClassificationPatternRepository,
)
from app.repositories.ingestion import IngestionRepository
from app.repositories.quickbooks import (
    QuickBooksOAuthStateRepository,
)
from app.repositories.quickbooks_connection import (
    QuickBooksConnectionRepository,
)
from app.repositories.reporting import ProfitAndLossRepository
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)
from app.services.classification.gemini_adapter import (
    GoogleGeminiClassifier,
    create_google_gemini_classifier,
)
from app.services.classification.rule_config import (
    load_deterministic_rule_set,
)
from app.ui.router import STATIC_DIR
from app.ui.router import router as ui_router

logger = structlog.get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHART_OF_ACCOUNTS_PATH = PROJECT_ROOT / "sample_config" / "chart_of_accounts.json"
CLASSIFICATION_RULES_PATH = PROJECT_ROOT / "sample_config" / "classification_rules.json"


def _build_optional_gemini_classifier(
    settings: Settings,
) -> GoogleGeminiClassifier | None:
    """Create Gemini only when it is configured."""

    if settings.gemini_api_key is None and settings.gemini_model is None:
        return None

    return create_google_gemini_classifier(settings)


def build_lifespan(settings: Settings):
    """Build an application lifespan bound to the supplied settings."""

    @asynccontextmanager
    async def lifespan(
        app: FastAPI,
    ) -> AsyncIterator[None]:
        mongodb = MongoDatabase(
            uri=settings.mongodb_uri,
            database_name=settings.mongodb_database,
        )
        gemini_classifier: GoogleGeminiClassifier | None = None

        try:
            chart_of_accounts = load_chart_of_accounts(CHART_OF_ACCOUNTS_PATH)
            classification_rule_set = load_deterministic_rule_set(
                CLASSIFICATION_RULES_PATH,
                chart_of_accounts=chart_of_accounts,
            )
            gemini_classifier = _build_optional_gemini_classifier(settings)

            ingestion_repository = IngestionRepository(mongodb.database)
            classification_repository = ClassificationRepository(mongodb.database)
            classification_pattern_repository = ClassificationPatternRepository(mongodb.database)
            profit_and_loss_repository = ProfitAndLossRepository(mongodb.database)
            quickbooks_oauth_state_repository = QuickBooksOAuthStateRepository(mongodb.database)
            quickbooks_connection_repository = QuickBooksConnectionRepository(mongodb.database)

            if settings.app_env != "test":
                await ingestion_repository.ensure_indexes()
                await classification_repository.ensure_indexes()
                await classification_pattern_repository.ensure_indexes()
                await quickbooks_oauth_state_repository.ensure_indexes()
                await quickbooks_connection_repository.ensure_indexes()

            app.state.mongodb = mongodb
            app.state.ingestion_repository = ingestion_repository
            app.state.classification_repository = classification_repository
            app.state.classification_pattern_repository = classification_pattern_repository
            app.state.profit_and_loss_repository = profit_and_loss_repository
            app.state.quickbooks_oauth_state_repository = quickbooks_oauth_state_repository
            app.state.quickbooks_connection_repository = quickbooks_connection_repository
            app.state.chart_of_accounts = chart_of_accounts
            app.state.classification_rule_set = classification_rule_set
            app.state.gemini_classifier = gemini_classifier

            logger.info(
                "application_started",
                environment=settings.app_env,
                version=__version__,
                gemini_enabled=(gemini_classifier is not None),
            )

            yield
        finally:
            try:
                if gemini_classifier is not None:
                    await gemini_classifier.close()
            finally:
                await mongodb.close()
                logger.info("application_stopped")

    return lifespan


def create_app(
    settings: Settings | None = None,
) -> FastAPI:
    """Create and configure a FastAPI application instance."""

    app_settings = settings or get_settings()
    configure_logging(app_settings.log_level)

    application = FastAPI(
        title=app_settings.app_name,
        version=__version__,
        debug=app_settings.app_debug,
        description=(
            "Accounting data ingestion, classification, "
            "QuickBooks sync, and cash-basis P&L "
            "reconciliation."
        ),
        lifespan=build_lifespan(app_settings),
    )

    application.state.settings = app_settings
    application.include_router(api_router)
    application.include_router(ui_router)
    application.mount(
        "/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="static",
    )

    return application


app = create_app()
