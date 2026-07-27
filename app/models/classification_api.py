"""HTTP request models for classification review workflows."""

from __future__ import annotations

from pydantic import Field

from app.models.classification import (
    AccountNumber,
    ImmutableAccountingModel,
    NonEmptyString,
    ReviewStatus,
    TransactionType,
)


class ClassificationReviewCommand(ImmutableAccountingModel):
    """Request to approve or reject one pending classification."""

    expected_version: int = Field(ge=1, strict=True)
    outcome: ReviewStatus
    reviewer_id: NonEmptyString
    notes: NonEmptyString | None = None


class ClassificationCorrectionCommand(ImmutableAccountingModel):
    """Request to append one validated manual correction."""

    expected_version: int = Field(ge=1, strict=True)
    corrected_transaction_type: TransactionType
    corrected_account_number: AccountNumber
    corrected_counterparty_name: NonEmptyString | None = None
    reviewer_id: NonEmptyString
    reason: NonEmptyString
    notes: NonEmptyString | None = None
