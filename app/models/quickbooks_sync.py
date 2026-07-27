"""Immutable contracts for idempotent QuickBooks transaction sync."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

NonEmptyString = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
    ),
]

SafeErrorMessage = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=500,
    ),
]

AccountNumber = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^\d{4}$",
    ),
]

QuickBooksRequestId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=50,
        pattern=r"^[a-z0-9-]+$",
    ),
]


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(UTC)


class ImmutableSyncModel(BaseModel):
    """Base model for immutable synchronization records."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )


class QuickBooksPostingType(StrEnum):
    """Debit or credit direction for one journal-entry line."""

    DEBIT = "Debit"
    CREDIT = "Credit"


class QuickBooksTransactionKind(StrEnum):
    """QuickBooks entity selected for challenge synchronization."""

    JOURNAL_ENTRY = "journal_entry"


class QuickBooksSyncStatus(StrEnum):
    """Persistent lifecycle of one QuickBooks posting."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    RETRYABLE_ERROR = "retryable_error"
    PERMANENT_ERROR = "permanent_error"


class QuickBooksSourceReference(ImmutableSyncModel):
    """Immutable source and classification version used for posting."""

    normalized_transaction_id: UUID
    classification_version: int = Field(ge=1)
    source_transaction_id: NonEmptyString | None = None


class QuickBooksJournalLine(ImmutableSyncModel):
    """One validated debit or credit in a QBO journal entry."""

    account_number: AccountNumber
    account_name: NonEmptyString
    qbo_account_id: NonEmptyString
    posting_type: QuickBooksPostingType
    amount: Decimal
    description: NonEmptyString | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def validate_amount(
        cls,
        value: Any,
    ) -> Decimal:
        """Require a positive, finite, cent-precise Decimal."""

        if isinstance(value, (bool, float)):
            raise ValueError("QuickBooks amounts cannot use floating-point values")

        try:
            amount = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("QuickBooks amount must be a valid decimal") from exc

        if not amount.is_finite():
            raise ValueError("QuickBooks amount must be finite")

        try:
            quantized = amount.quantize(CENT)
        except InvalidOperation as exc:
            raise ValueError("QuickBooks amount cannot be represented in cents") from exc

        if amount != quantized:
            raise ValueError("QuickBooks amount cannot exceed two decimal places")

        if quantized <= ZERO:
            raise ValueError("QuickBooks journal-line amount must be positive")

        return quantized


class QuickBooksJournalEntryPlan(ImmutableSyncModel):
    """Balanced immutable posting plan sent to QuickBooks."""

    transaction_kind: QuickBooksTransactionKind = QuickBooksTransactionKind.JOURNAL_ENTRY
    request_id: QuickBooksRequestId
    sources: tuple[QuickBooksSourceReference, ...] = Field(
        min_length=1,
        max_length=2,
    )
    transaction_date: date
    currency: str
    private_note: NonEmptyString
    lines: tuple[QuickBooksJournalLine, ...] = Field(
        min_length=2,
        max_length=2,
    )

    @field_validator("currency")
    @classmethod
    def validate_currency(
        cls,
        value: str,
    ) -> str:
        """Normalize an ASCII three-letter currency code."""

        normalized = value.strip().upper()

        if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
            raise ValueError("QuickBooks currency must be a three-letter code")

        return normalized

    @model_validator(mode="after")
    def validate_plan(
        self,
    ) -> QuickBooksJournalEntryPlan:
        """Require deterministic identity and balanced unique lines."""

        source_ids = tuple(source.normalized_transaction_id for source in self.sources)

        if len(set(source_ids)) != len(source_ids):
            raise ValueError("QuickBooks posting sources must be unique")

        expected_request_id = build_quickbooks_request_id(source_ids)

        if self.request_id != expected_request_id:
            raise ValueError("QuickBooks request ID must be derived from the posting sources")

        debit_total = sum(
            (
                line.amount
                for line in self.lines
                if line.posting_type is QuickBooksPostingType.DEBIT
            ),
            start=ZERO,
        )
        credit_total = sum(
            (
                line.amount
                for line in self.lines
                if line.posting_type is QuickBooksPostingType.CREDIT
            ),
            start=ZERO,
        )

        if debit_total == ZERO or credit_total == ZERO:
            raise ValueError("QuickBooks journal entry requires both a debit and a credit")

        if debit_total != credit_total:
            raise ValueError("QuickBooks journal entry is not balanced")

        account_ids = {line.qbo_account_id for line in self.lines}
        account_numbers = {line.account_number for line in self.lines}

        if len(account_ids) != len(self.lines):
            raise ValueError("QuickBooks journal lines must use different account IDs")

        if len(account_numbers) != len(self.lines):
            raise ValueError("QuickBooks journal lines must use different account numbers")

        return self


class QuickBooksSyncError(ImmutableSyncModel):
    """Secret-free error retained for retry and review."""

    code: NonEmptyString | None = None
    message: SafeErrorMessage
    retryable: bool
    occurred_at: AwareDatetime = Field(default_factory=utc_now)


class QuickBooksSyncRecord(ImmutableSyncModel):
    """Persistent state for one idempotent QBO posting plan."""

    id: UUID = Field(default_factory=uuid4)
    plan: QuickBooksJournalEntryPlan
    status: QuickBooksSyncStatus = QuickBooksSyncStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    qbo_transaction_id: NonEmptyString | None = None
    qbo_sync_token: NonEmptyString | None = None
    last_error: QuickBooksSyncError | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_state(
        self,
    ) -> QuickBooksSyncRecord:
        """Enforce legal synchronization-state combinations."""

        if self.updated_at < self.created_at:
            raise ValueError("QuickBooks sync update cannot precede creation")

        if self.status is QuickBooksSyncStatus.PENDING:
            if self.attempt_count != 0:
                raise ValueError("Pending sync records cannot have attempts")

            if (
                self.qbo_transaction_id is not None
                or self.qbo_sync_token is not None
                or self.last_error is not None
            ):
                raise ValueError("Pending sync records cannot contain QBO results or errors")

            return self

        if self.attempt_count < 1:
            raise ValueError("Started sync records require at least one attempt")

        if self.status is QuickBooksSyncStatus.IN_PROGRESS:
            if (
                self.qbo_transaction_id is not None
                or self.qbo_sync_token is not None
                or self.last_error is not None
            ):
                raise ValueError(
                    "In-progress sync records cannot contain final QBO results or errors"
                )

            return self

        if self.status is QuickBooksSyncStatus.SUCCEEDED:
            if self.qbo_transaction_id is None or self.qbo_sync_token is None:
                raise ValueError(
                    "Successful sync records require the QBO transaction ID and sync token"
                )

            if self.last_error is not None:
                raise ValueError("Successful sync records cannot retain a final error")

            return self

        if self.qbo_transaction_id is not None or self.qbo_sync_token is not None:
            raise ValueError("Failed sync records cannot claim a completed QBO transaction")

        if self.last_error is None:
            raise ValueError("Failed sync records require a safe error")

        if self.status is QuickBooksSyncStatus.RETRYABLE_ERROR and not self.last_error.retryable:
            raise ValueError("Retryable sync status requires a retryable error")

        if self.status is QuickBooksSyncStatus.PERMANENT_ERROR and self.last_error.retryable:
            raise ValueError("Permanent sync status requires a non-retryable error")

        return self

    @property
    def normalized_transaction_ids(
        self,
    ) -> tuple[UUID, ...]:
        """Return all normalized source records covered by the post."""

        return tuple(source.normalized_transaction_id for source in self.plan.sources)


def build_quickbooks_request_id(
    normalized_transaction_ids: tuple[UUID, ...],
) -> str:
    """Create a stable QBO request ID for one posting group."""

    if not normalized_transaction_ids:
        raise ValueError("At least one source transaction is required")

    if len(set(normalized_transaction_ids)) != len(normalized_transaction_ids):
        raise ValueError("Source transaction IDs must be unique")

    canonical = "|".join(
        sorted(str(transaction_id) for transaction_id in normalized_transaction_ids)
    )
    digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()[:40]

    request_id = f"finz-je-{digest}"

    if len(request_id) > 50:
        raise RuntimeError("Generated QuickBooks request ID is too long")

    return request_id
