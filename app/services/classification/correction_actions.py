"""Application service for validated classification corrections."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.models.accounting import ChartOfAccountsConfig
from app.models.classification import (
    ClassificationCorrection,
    ClassificationDecision,
    ClassificationSource,
    Counterparty,
    ImmutableAccountingModel,
    NonEmptyString,
    QuickBooksAccountMapping,
    ReviewerMetadata,
    ReviewStatus,
    TransactionClassification,
    TransactionType,
)
from app.models.ingestion import (
    NormalizedTransaction,
    RecordStatus,
)
from app.repositories.classification import (
    ClassificationNotFoundError,
    StaleClassificationVersionError,
    UnsafeClassificationTransactionError,
)
from app.services.classification.account_mapping import (
    InvalidClassificationAccountMappingError,
    validate_classification_account_target,
)
from app.services.classification.gemini import (
    ALLOWED_TRANSACTION_TYPES_BY_DIRECTION,
)


class InvalidManualCorrectionError(ValueError):
    """A requested manual correction is not accounting-safe."""


class ClassificationCorrectionRepository(Protocol):
    """Classification persistence required by the correction service."""

    async def find_by_transaction_id(
        self,
        normalized_transaction_id: UUID,
    ) -> TransactionClassification | None:
        """Return the current stored classification."""

    async def save_correction(
        self,
        classification: TransactionClassification,
        *,
        expected_version: int,
    ) -> bool:
        """Atomically persist or recognize an exact correction retry."""


class CorrectionTransactionReader(Protocol):
    """Load normalized source evidence for a correction."""

    async def find_transaction_by_id(
        self,
        normalized_transaction_id: UUID,
    ) -> NormalizedTransaction | None:
        """Return one normalized transaction by UUID."""


class ClassificationCorrectionResult(ImmutableAccountingModel):
    """Result of one validated manual correction."""

    updated: bool
    classification: TransactionClassification


async def correct_classification(
    *,
    normalized_transaction_id: UUID,
    expected_version: int,
    corrected_transaction_type: TransactionType,
    corrected_account_number: str,
    corrected_counterparty_name: NonEmptyString | None,
    reviewer_id: NonEmptyString,
    reviewed_at: datetime,
    reason: NonEmptyString,
    notes: NonEmptyString | None,
    chart_of_accounts: ChartOfAccountsConfig,
    classification_repository: ClassificationCorrectionRepository,
    transaction_reader: CorrectionTransactionReader,
) -> ClassificationCorrectionResult:
    """Validate and append one immutable classification correction."""

    if isinstance(expected_version, bool) or expected_version < 1:
        raise ValueError("expected_version must be at least 1")

    current = await classification_repository.find_by_transaction_id(normalized_transaction_id)

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

    transaction = await transaction_reader.find_transaction_by_id(normalized_transaction_id)

    if transaction is None:
        raise UnsafeClassificationTransactionError(
            "The classification has no normalized transaction evidence"
        )

    _require_valid_canonical_transaction(
        transaction=transaction,
        normalized_transaction_id=normalized_transaction_id,
    )

    if transaction.direction is None:
        raise UnsafeClassificationTransactionError("The normalized transaction has no direction")

    if transaction.bank_account is None:
        raise UnsafeClassificationTransactionError(
            "The normalized transaction has no source bank account"
        )

    allowed_types = ALLOWED_TRANSACTION_TYPES_BY_DIRECTION[transaction.direction]

    if corrected_transaction_type not in allowed_types:
        raise InvalidManualCorrectionError(
            "Manual correction transaction type "
            f"{corrected_transaction_type.value!r} is incompatible with "
            f"transaction direction {transaction.direction.value!r}"
        )

    try:
        account = chart_of_accounts.require(corrected_account_number)
    except (KeyError, ValueError) as exc:
        raise InvalidManualCorrectionError(
            "Manual correction referenced an unknown or inactive "
            f"account: {corrected_account_number.strip()}"
        ) from exc

    try:
        validate_classification_account_target(
            transaction_type=corrected_transaction_type,
            account=account,
            source_bank_account=transaction.bank_account,
            subject="Manual correction",
        )
    except InvalidClassificationAccountMappingError as exc:
        raise InvalidManualCorrectionError(str(exc)) from exc

    reviewer = ReviewerMetadata(
        reviewer_id=reviewer_id,
        reviewed_at=reviewed_at,
        notes=notes,
    )

    counterparty = (
        Counterparty(
            raw_name=transaction.description_original,
            normalized_name=corrected_counterparty_name,
        )
        if corrected_counterparty_name is not None
        else current.decision.counterparty
    )

    corrected_decision = ClassificationDecision(
        transaction_type=corrected_transaction_type,
        counterparty=counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number=account.number,
            account_name=account.name,
        ),
        confidence_score="1.000",
        explanation=(f"Manual reviewer corrected the classification: {reason}"),
        source=ClassificationSource.MANUAL_REVIEW,
        review_required=False,
    )

    correction = ClassificationCorrection(
        from_version=current.version,
        to_version=current.version + 1,
        previous_decision=current.decision,
        corrected_decision=corrected_decision,
        corrected_by=reviewer,
        reason=reason,
    )

    corrected = TransactionClassification(
        normalized_transaction_id=(current.normalized_transaction_id),
        version=current.version + 1,
        decision=corrected_decision,
        review_status=ReviewStatus.PENDING,
        reviewer=None,
        corrections=(
            *current.corrections,
            correction,
        ),
    )

    updated = await classification_repository.save_correction(
        corrected,
        expected_version=expected_version,
    )

    return ClassificationCorrectionResult(
        updated=updated,
        classification=corrected,
    )


def _require_valid_canonical_transaction(
    *,
    transaction: NormalizedTransaction,
    normalized_transaction_id: UUID,
) -> None:
    """Require source evidence to be the intended canonical record."""

    if transaction.id != normalized_transaction_id:
        raise UnsafeClassificationTransactionError(
            "Transaction evidence does not match the classification"
        )

    if transaction.status is not RecordStatus.VALID or transaction.duplicate_of is not None:
        raise UnsafeClassificationTransactionError(
            "Only valid canonical normalized transactions may be corrected"
        )
