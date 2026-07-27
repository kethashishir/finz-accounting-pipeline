"""Typed models for approved classification-correction patterns."""

from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from app.models.classification import (
    ClassificationCorrection,
    ClassificationDecision,
    ClassificationSource,
    ImmutableAccountingModel,
    NonEmptyString,
    ReviewerMetadata,
    ReviewStatus,
)
from app.models.ingestion import TransactionDirection


class ClassificationPatternKey(ImmutableAccountingModel):
    """Exact normalized transaction attributes used for pattern matching."""

    description_normalized: NonEmptyString
    bank_account: NonEmptyString
    direction: TransactionDirection
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @field_validator(
        "description_normalized",
        "bank_account",
        mode="before",
    )
    @classmethod
    def normalize_match_text(cls, value: object) -> object:
        """Create stable case-insensitive exact-match text."""

        if isinstance(value, str):
            return value.strip().casefold()

        return value

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(cls, value: object) -> object:
        """Create a stable uppercase currency match value."""

        if isinstance(value, str):
            return value.strip().upper()

        return value


class LearnedClassificationPattern(ImmutableAccountingModel):
    """Reusable decision learned from one approved manual correction."""

    id: UUID = Field(default_factory=uuid4)
    key: ClassificationPatternKey
    decision: ClassificationDecision

    source_transaction_id: UUID
    source_classification_version: int = Field(ge=2)
    source_correction: ClassificationCorrection
    source_review_status: ReviewStatus = ReviewStatus.APPROVED
    approved_by: ReviewerMetadata

    learned_at: datetime
    active: bool = True

    @field_validator("learned_at")
    @classmethod
    def require_timezone_aware_timestamp(
        cls,
        value: datetime,
    ) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("learned_at must be timezone-aware")

        return value

    @model_validator(mode="after")
    def validate_approved_correction_provenance(
        self,
    ) -> Self:
        if self.source_review_status is not ReviewStatus.APPROVED:
            raise ValueError("learned patterns require an approved source classification")

        if (
            self.source_correction.corrected_decision.source
            is not ClassificationSource.MANUAL_REVIEW
        ):
            raise ValueError("learned patterns require a manual-review correction")

        if self.source_classification_version != self.source_correction.to_version:
            raise ValueError(
                "source classification version must match the correction target version"
            )

        if self.decision.source is not ClassificationSource.STORED_CORRECTION:
            raise ValueError("a learned pattern decision must use stored_correction source")

        corrected = self.source_correction.corrected_decision

        if self.decision.transaction_type != corrected.transaction_type:
            raise ValueError("pattern transaction type must match the approved correction")

        if self.decision.counterparty != corrected.counterparty:
            raise ValueError("pattern counterparty must match the approved correction")

        if (
            self.decision.qbo_account.account_number != corrected.qbo_account.account_number
            or self.decision.qbo_account.account_name != corrected.qbo_account.account_name
        ):
            raise ValueError("pattern account must match the approved correction")

        if self.decision.qbo_account.qbo_account_id is not None:
            raise ValueError(
                "learned patterns cannot store environment-specific QuickBooks account IDs"
            )

        if self.learned_at < self.approved_by.reviewed_at:
            raise ValueError("learned_at cannot be earlier than the approval timestamp")

        return self
