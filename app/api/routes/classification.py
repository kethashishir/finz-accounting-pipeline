"""API endpoints for classification review and correction."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    status,
)
from pymongo.errors import PyMongoError

from app.models.accounting import ChartOfAccountsConfig
from app.models.classification_api import (
    ClassificationCorrectionCommand,
    ClassificationReviewCommand,
)
from app.models.review import ReviewQueueItem
from app.repositories.classification import (
    ClassificationNotFoundError,
    ClassificationRepository,
    ClassificationReviewConflictError,
    ClassificationTransactionNotFoundError,
    InvalidClassificationTransitionError,
    StaleClassificationVersionError,
    UnsafeClassificationTransactionError,
)
from app.repositories.ingestion import IngestionRepository
from app.services.classification.correction_actions import (
    ClassificationCorrectionResult,
    InvalidManualCorrectionError,
    correct_classification,
)
from app.services.classification.review_actions import (
    ClassificationReviewResult,
    finalize_classification_review,
)

router = APIRouter(
    prefix="/classification",
    tags=["classification"],
)
logger = structlog.get_logger(__name__)


def _classification_error(
    error: Exception,
) -> HTTPException:
    """Map domain errors to stable HTTP responses."""

    if isinstance(error, ClassificationNotFoundError):
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "classification_not_found",
                "message": str(error),
            },
        )

    if isinstance(
        error,
        (
            StaleClassificationVersionError,
            ClassificationReviewConflictError,
            ClassificationTransactionNotFoundError,
            InvalidClassificationTransitionError,
        ),
    ):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "classification_conflict",
                "message": str(error),
            },
        )

    if isinstance(
        error,
        (
            InvalidManualCorrectionError,
            UnsafeClassificationTransactionError,
            ValueError,
        ),
    ):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "invalid_classification_action",
                "message": str(error),
            },
        )

    raise TypeError(f"Unsupported classification API error: {type(error).__name__}")


def _database_unavailable() -> HTTPException:
    """Return the stable classification database failure response."""

    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "mongodb_unavailable",
            "message": ("The classification database is unavailable"),
        },
    )


@router.get(
    "/review-queue",
    response_model=list[ReviewQueueItem],
    responses={
        status.HTTP_409_CONFLICT: {"description": "Classification source evidence is inconsistent"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "MongoDB is unavailable"},
    },
)
async def get_review_queue(
    request: Request,
    limit: Annotated[
        int,
        Query(ge=1, le=200),
    ] = 100,
) -> list[ReviewQueueItem]:
    """Return pending classifications in accounting-risk order."""

    repository: ClassificationRepository = request.app.state.classification_repository

    try:
        items = await repository.find_review_queue(limit=limit)
    except ClassificationTransactionNotFoundError as exc:
        raise _classification_error(exc) from exc
    except PyMongoError as exc:
        logger.exception("classification_review_queue_database_failure")
        raise _database_unavailable() from exc

    return list(items)


@router.post(
    "/{normalized_transaction_id}/review",
    response_model=ClassificationReviewResult,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Classification was not found"},
        status.HTTP_409_CONFLICT: {"description": "Classification version or review conflict"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Review command is invalid"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "MongoDB is unavailable"},
    },
)
async def review_classification(
    normalized_transaction_id: UUID,
    command: ClassificationReviewCommand,
    request: Request,
) -> ClassificationReviewResult:
    """Approve or reject one unchanged pending classification."""

    repository: ClassificationRepository = request.app.state.classification_repository

    try:
        result = await finalize_classification_review(
            normalized_transaction_id=(normalized_transaction_id),
            expected_version=command.expected_version,
            outcome=command.outcome,
            reviewer_id=command.reviewer_id,
            reviewed_at=datetime.now(UTC),
            notes=command.notes,
            repository=repository,
        )
    except (
        ClassificationNotFoundError,
        StaleClassificationVersionError,
        ClassificationReviewConflictError,
        InvalidClassificationTransitionError,
        ValueError,
    ) as exc:
        raise _classification_error(exc) from exc
    except PyMongoError as exc:
        logger.exception(
            "classification_review_database_failure",
            normalized_transaction_id=str(normalized_transaction_id),
        )
        raise _database_unavailable() from exc

    logger.info(
        "classification_review_finalized",
        normalized_transaction_id=str(normalized_transaction_id),
        outcome=command.outcome.value,
        updated=result.updated,
    )

    return result


@router.post(
    "/{normalized_transaction_id}/correction",
    response_model=ClassificationCorrectionResult,
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Classification was not found"},
        status.HTTP_409_CONFLICT: {"description": "Classification version conflict"},
        status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "Correction is not accounting-safe"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "MongoDB is unavailable"},
    },
)
async def correct_classification_endpoint(
    normalized_transaction_id: UUID,
    command: ClassificationCorrectionCommand,
    request: Request,
) -> ClassificationCorrectionResult:
    """Append one validated manual classification correction."""

    classification_repository: ClassificationRepository = (
        request.app.state.classification_repository
    )
    ingestion_repository: IngestionRepository = request.app.state.ingestion_repository
    chart_of_accounts: ChartOfAccountsConfig = request.app.state.chart_of_accounts

    try:
        result = await correct_classification(
            normalized_transaction_id=(normalized_transaction_id),
            expected_version=command.expected_version,
            corrected_transaction_type=(command.corrected_transaction_type),
            corrected_account_number=(command.corrected_account_number),
            corrected_counterparty_name=(command.corrected_counterparty_name),
            reviewer_id=command.reviewer_id,
            reviewed_at=datetime.now(UTC),
            reason=command.reason,
            notes=command.notes,
            chart_of_accounts=chart_of_accounts,
            classification_repository=(classification_repository),
            transaction_reader=ingestion_repository,
        )
    except (
        ClassificationNotFoundError,
        StaleClassificationVersionError,
        InvalidClassificationTransitionError,
        InvalidManualCorrectionError,
        UnsafeClassificationTransactionError,
        ValueError,
    ) as exc:
        raise _classification_error(exc) from exc
    except PyMongoError as exc:
        logger.exception(
            "classification_correction_database_failure",
            normalized_transaction_id=str(normalized_transaction_id),
        )
        raise _database_unavailable() from exc

    logger.info(
        "classification_corrected",
        normalized_transaction_id=str(normalized_transaction_id),
        version=result.classification.version,
        updated=result.updated,
    )

    return result
