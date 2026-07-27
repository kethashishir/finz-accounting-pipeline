"""Tests for final classification review actions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.models.classification import (
    ClassificationDecision,
    ClassificationSource,
    Counterparty,
    QuickBooksAccountMapping,
    ReviewerMetadata,
    ReviewStatus,
    TransactionClassification,
    TransactionType,
)
from app.repositories.classification import (
    ClassificationNotFoundError,
    ClassificationReviewConflictError,
    StaleClassificationVersionError,
)
from app.services.classification.review_actions import (
    finalize_classification_review,
)

REVIEWED_AT = datetime(
    2026,
    7,
    26,
    20,
    0,
    tzinfo=UTC,
)


class FakeReviewRepository:
    """Record review lookup and persistence activity."""

    def __init__(
        self,
        current: TransactionClassification | None,
        *,
        updated: bool = True,
    ) -> None:
        self.current = current
        self.updated = updated
        self.lookups: list[UUID] = []
        self.saved: list[tuple[TransactionClassification, int]] = []

    async def find_by_transaction_id(
        self,
        normalized_transaction_id: UUID,
    ) -> TransactionClassification | None:
        self.lookups.append(normalized_transaction_id)
        return self.current

    async def save_review(
        self,
        classification: TransactionClassification,
        *,
        expected_version: int,
    ) -> bool:
        self.saved.append(
            (
                classification,
                expected_version,
            )
        )
        return self.updated


def create_classification(
    *,
    review_status: ReviewStatus = ReviewStatus.PENDING,
) -> TransactionClassification:
    """Create one classification eligible for review."""

    reviewer = (
        ReviewerMetadata(
            reviewer_id="prior-reviewer",
            reviewed_at=REVIEWED_AT,
            notes="Previously reviewed.",
        )
        if review_status is not ReviewStatus.PENDING
        else None
    )

    return TransactionClassification(
        normalized_transaction_id=uuid4(),
        decision=ClassificationDecision(
            transaction_type=(TransactionType.OPERATING_EXPENSE),
            counterparty=Counterparty(
                raw_name="Unrecognized Merchant",
                normalized_name="Unrecognized Merchant",
            ),
            qbo_account=QuickBooksAccountMapping(
                account_number="6090",
                account_name="Office & General",
            ),
            confidence_score=Decimal("0.700"),
            explanation=("The transaction appears to be a general business expense."),
            source=ClassificationSource.GEMINI,
            review_required=True,
        ),
        review_status=review_status,
        reviewer=reviewer,
    )


@pytest.mark.asyncio
async def test_approval_preserves_accounting_decision() -> None:
    """Approval changes only review metadata and status."""

    current = create_classification()
    repository = FakeReviewRepository(current)

    result = await finalize_classification_review(
        normalized_transaction_id=(current.normalized_transaction_id),
        expected_version=current.version,
        outcome=ReviewStatus.APPROVED,
        reviewer_id="shishir",
        reviewed_at=REVIEWED_AT,
        notes="Confirmed against the bank description.",
        repository=repository,
    )

    reviewed = result.classification

    assert result.updated is True
    assert reviewed.review_status is ReviewStatus.APPROVED
    assert reviewed.reviewer is not None
    assert reviewed.reviewer.reviewer_id == "shishir"
    assert reviewed.reviewer.reviewed_at == REVIEWED_AT
    assert reviewed.reviewer.notes == ("Confirmed against the bank description.")
    assert reviewed.normalized_transaction_id == (current.normalized_transaction_id)
    assert reviewed.version == current.version
    assert reviewed.decision == current.decision
    assert reviewed.corrections == current.corrections
    assert repository.saved == [
        (
            reviewed,
            current.version,
        )
    ]


@pytest.mark.asyncio
async def test_rejection_preserves_accounting_history() -> None:
    """Rejection records the outcome without deleting the decision."""

    current = create_classification()
    repository = FakeReviewRepository(current)

    result = await finalize_classification_review(
        normalized_transaction_id=(current.normalized_transaction_id),
        expected_version=1,
        outcome=ReviewStatus.REJECTED,
        reviewer_id="shishir",
        reviewed_at=REVIEWED_AT,
        notes="The selected account is not supported.",
        repository=repository,
    )

    assert result.classification.review_status is (ReviewStatus.REJECTED)
    assert result.classification.decision == current.decision
    assert result.classification.version == current.version
    assert len(repository.saved) == 1


@pytest.mark.asyncio
async def test_pending_is_not_a_final_review_outcome() -> None:
    """A caller cannot use the final-review service to reopen work."""

    current = create_classification()
    repository = FakeReviewRepository(current)

    with pytest.raises(
        ValueError,
        match="approved or rejected",
    ):
        await finalize_classification_review(
            normalized_transaction_id=(current.normalized_transaction_id),
            expected_version=1,
            outcome=ReviewStatus.PENDING,
            reviewer_id="shishir",
            reviewed_at=REVIEWED_AT,
            notes=None,
            repository=repository,
        )

    assert repository.lookups == []
    assert repository.saved == []


@pytest.mark.asyncio
async def test_missing_classification_is_rejected() -> None:
    """A review cannot target a nonexistent classification."""

    transaction_id = uuid4()
    repository = FakeReviewRepository(None)

    with pytest.raises(
        ClassificationNotFoundError,
        match="has no classification",
    ):
        await finalize_classification_review(
            normalized_transaction_id=transaction_id,
            expected_version=1,
            outcome=ReviewStatus.APPROVED,
            reviewer_id="shishir",
            reviewed_at=REVIEWED_AT,
            notes=None,
            repository=repository,
        )

    assert repository.lookups == [transaction_id]
    assert repository.saved == []


@pytest.mark.asyncio
async def test_stale_expected_version_is_rejected_before_write() -> None:
    """An outdated review page cannot finalize newer data."""

    current = create_classification()
    repository = FakeReviewRepository(current)

    with pytest.raises(
        StaleClassificationVersionError,
        match="stored version is 1",
    ):
        await finalize_classification_review(
            normalized_transaction_id=(current.normalized_transaction_id),
            expected_version=2,
            outcome=ReviewStatus.APPROVED,
            reviewer_id="shishir",
            reviewed_at=REVIEWED_AT,
            notes=None,
            repository=repository,
        )

    assert repository.saved == []


@pytest.mark.asyncio
async def test_already_reviewed_classification_is_rejected() -> None:
    """A competing final review cannot replace the first outcome."""

    current = create_classification(
        review_status=ReviewStatus.APPROVED,
    )
    repository = FakeReviewRepository(current)

    with pytest.raises(
        ClassificationReviewConflictError,
        match="pending classification",
    ):
        await finalize_classification_review(
            normalized_transaction_id=(current.normalized_transaction_id),
            expected_version=1,
            outcome=ReviewStatus.REJECTED,
            reviewer_id="second-reviewer",
            reviewed_at=REVIEWED_AT,
            notes=None,
            repository=repository,
        )

    assert repository.saved == []


@pytest.mark.asyncio
async def test_exact_review_retry_is_reported() -> None:
    """Repository idempotency remains visible to the caller."""

    current = create_classification()
    repository = FakeReviewRepository(
        current,
        updated=False,
    )

    result = await finalize_classification_review(
        normalized_transaction_id=(current.normalized_transaction_id),
        expected_version=1,
        outcome=ReviewStatus.APPROVED,
        reviewer_id="shishir",
        reviewed_at=REVIEWED_AT,
        notes=None,
        repository=repository,
    )

    assert result.updated is False
    assert result.classification.review_status is (ReviewStatus.APPROVED)
    assert len(repository.saved) == 1
