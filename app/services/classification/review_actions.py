"""Application service for final classification review actions."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.models.classification import (
    ImmutableAccountingModel,
    NonEmptyString,
    ReviewerMetadata,
    ReviewStatus,
    TransactionClassification,
)
from app.repositories.classification import (
    ClassificationNotFoundError,
    ClassificationReviewConflictError,
    StaleClassificationVersionError,
)


class ClassificationReviewRepository(Protocol):
    """Persistence operations required by the review service."""

    async def find_by_transaction_id(
        self,
        normalized_transaction_id: UUID,
    ) -> TransactionClassification | None:
        """Return the current stored classification."""

    async def save_review(
        self,
        classification: TransactionClassification,
        *,
        expected_version: int,
    ) -> bool:
        """Atomically save or recognize an exact review retry."""


class ClassificationReviewResult(ImmutableAccountingModel):
    """Result of one final approval or rejection attempt."""

    updated: bool
    classification: TransactionClassification


async def finalize_classification_review(
    *,
    normalized_transaction_id: UUID,
    expected_version: int,
    outcome: ReviewStatus,
    reviewer_id: NonEmptyString,
    reviewed_at: datetime,
    notes: NonEmptyString | None,
    repository: ClassificationReviewRepository,
) -> ClassificationReviewResult:
    """Approve or reject a pending classification without changing it."""

    if isinstance(expected_version, bool) or expected_version < 1:
        raise ValueError("expected_version must be at least 1")

    if outcome not in {
        ReviewStatus.APPROVED,
        ReviewStatus.REJECTED,
    }:
        raise ValueError("Review outcome must be approved or rejected")

    current = await repository.find_by_transaction_id(normalized_transaction_id)

    if current is None:
        raise ClassificationNotFoundError(
            f"Normalized transaction {normalized_transaction_id} has no classification"
        )

    if current.version != expected_version:
        raise StaleClassificationVersionError(
            "Expected classification version "
            f"{expected_version}, but stored version is "
            f"{current.version}"
        )

    if current.review_status is not ReviewStatus.PENDING:
        raise ClassificationReviewConflictError(
            "Only a pending classification may receive a final review outcome"
        )

    reviewer = ReviewerMetadata(
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
        notes=notes,
    )

    reviewed = TransactionClassification(
        normalized_transaction_id=(current.normalized_transaction_id),
        version=current.version,
        decision=current.decision,
        review_status=outcome,
        reviewer=reviewer,
        corrections=current.corrections,
    )

    updated = await repository.save_review(
        reviewed,
        expected_version=expected_version,
    )

    return ClassificationReviewResult(
        updated=updated,
        classification=reviewed,
    )
