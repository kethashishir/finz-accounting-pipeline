from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmptyString = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]

AccountNumber = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^\d{4}$"),
]

ConfidenceScore = Annotated[
    Decimal,
    Field(ge=Decimal("0"), le=Decimal("1")),
]


class ImmutableAccountingModel(BaseModel):
    """Base model for immutable, auditable accounting-domain records."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )


class TransactionType(StrEnum):
    """Economic classification used by reporting and synchronization logic."""

    REVENUE = "revenue"
    COST_OF_GOODS_SOLD = "cost_of_goods_sold"
    OPERATING_EXPENSE = "operating_expense"
    REFUND = "refund"
    TRANSFER = "transfer"
    OWNER_CONTRIBUTION = "owner_contribution"
    OWNER_DISTRIBUTION = "owner_distribution"
    FIXED_ASSET_PURCHASE = "fixed_asset_purchase"


class ClassificationSource(StrEnum):
    """Origin of a classification decision."""

    DETERMINISTIC_RULE = "deterministic_rule"
    STORED_CORRECTION = "stored_correction"
    GEMINI = "gemini"
    MANUAL_REVIEW = "manual_review"


class ReviewStatus(StrEnum):
    """Human-review workflow state."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Counterparty(ImmutableAccountingModel):
    """Raw and normalized identity of the transaction counterparty."""

    raw_name: NonEmptyString | None = None
    normalized_name: NonEmptyString


class QuickBooksAccountMapping(ImmutableAccountingModel):
    """Reference to the approved chart-of-accounts entry and eventual QBO ID."""

    account_number: AccountNumber
    account_name: NonEmptyString
    qbo_account_id: NonEmptyString | None = None


class ClassificationDecision(ImmutableAccountingModel):
    """Current accounting interpretation of one canonical transaction."""

    transaction_type: TransactionType
    counterparty: Counterparty | None = None
    qbo_account: QuickBooksAccountMapping
    confidence_score: ConfidenceScore
    explanation: NonEmptyString
    source: ClassificationSource
    review_required: bool


class ReviewerMetadata(ImmutableAccountingModel):
    """Identity and timestamp for an explicit human review action."""

    reviewer_id: NonEmptyString
    reviewed_at: datetime
    notes: NonEmptyString | None = None

    @field_validator("reviewed_at")
    @classmethod
    def require_timezone_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("reviewed_at must be timezone-aware")
        return value


class ClassificationCorrection(ImmutableAccountingModel):
    """Auditable transition from one classification version to the next."""

    from_version: int = Field(ge=1)
    to_version: int = Field(ge=2)
    previous_decision: ClassificationDecision
    corrected_decision: ClassificationDecision
    corrected_by: ReviewerMetadata
    reason: NonEmptyString

    @model_validator(mode="after")
    def validate_version_transition(self) -> ClassificationCorrection:
        if self.to_version != self.from_version + 1:
            raise ValueError("classification corrections must increment the version by exactly one")

        if self.previous_decision == self.corrected_decision:
            raise ValueError("classification corrections must change at least one decision field")

        return self


class TransactionClassification(ImmutableAccountingModel):
    """Current classification plus complete correction and review history."""

    normalized_transaction_id: UUID
    version: int = Field(default=1, ge=1)
    decision: ClassificationDecision
    review_status: ReviewStatus = ReviewStatus.PENDING
    reviewer: ReviewerMetadata | None = None
    corrections: tuple[ClassificationCorrection, ...] = ()

    @model_validator(mode="after")
    def validate_review_and_version_history(self) -> TransactionClassification:
        if self.review_status is ReviewStatus.PENDING and self.reviewer is not None:
            raise ValueError("pending classifications cannot include reviewer metadata")

        if (
            self.review_status in {ReviewStatus.APPROVED, ReviewStatus.REJECTED}
            and self.reviewer is None
        ):
            raise ValueError("approved or rejected classifications require reviewer metadata")

        if not self.corrections:
            if self.version != 1:
                raise ValueError("a classification without corrections must remain at version 1")
            return self

        expected_from_version = 1
        previous_corrected_decision: ClassificationDecision | None = None

        for correction in self.corrections:
            if correction.from_version != expected_from_version:
                raise ValueError(
                    "classification correction history must be contiguous and begin at version 1"
                )

            if (
                previous_corrected_decision is not None
                and correction.previous_decision != previous_corrected_decision
            ):
                raise ValueError("each correction must begin with the prior corrected decision")

            expected_from_version = correction.to_version
            previous_corrected_decision = correction.corrected_decision

        latest_correction = self.corrections[-1]

        if self.version != latest_correction.to_version:
            raise ValueError("current classification version must match the latest correction")

        if self.decision != latest_correction.corrected_decision:
            raise ValueError("current classification decision must match the latest correction")

        return self
