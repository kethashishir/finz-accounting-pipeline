"""Batch orchestration for safe initial transaction classification."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol, Self
from uuid import UUID

from pydantic import NonNegativeInt, model_validator

from app.models.accounting import ChartOfAccountsConfig
from app.models.classification import (
    ImmutableAccountingModel,
    NonEmptyString,
    TransactionClassification,
)
from app.models.classification_rule import DeterministicRuleSet
from app.models.ingestion import (
    NormalizedTransaction,
    RecordStatus,
)
from app.services.classification.gemini import GeminiClassifier
from app.services.classification.initial_classification import (
    DeterministicRuleClassificationResult,
    GeminiClassificationResult,
    InitialClassificationResult,
    InitialClassificationWriter,
    LearnedPatternClassificationResult,
    ManualReviewRequiredResult,
    classify_initial,
)
from app.services.classification.pattern_matching import PatternLookup


class BatchTransactionReader(Protocol):
    """Read normalized transactions belonging to one upload."""

    async def transactions_for_upload(
        self,
        upload_id: UUID,
    ) -> tuple[NormalizedTransaction, ...]:
        """Return normalized records in stable source order."""


class BatchClassificationRepository(
    InitialClassificationWriter,
    Protocol,
):
    """Lookup and persistence operations required by the batch service."""

    async def find_by_transaction_ids(
        self,
        normalized_transaction_ids: Sequence[UUID],
    ) -> dict[UUID, TransactionClassification]:
        """Return existing classifications keyed by transaction UUID."""


class InitialClassifier(Protocol):
    """Callable boundary for classifying one canonical transaction."""

    async def __call__(
        self,
        *,
        transaction: NormalizedTransaction,
        pattern_lookup: PatternLookup,
        rule_set: DeterministicRuleSet,
        classification_writer: InitialClassificationWriter,
        chart_of_accounts: ChartOfAccountsConfig,
        gemini_classifier: GeminiClassifier | None = None,
    ) -> InitialClassificationResult:
        """Return one persisted or manual-review classification outcome."""


class BatchClassificationOutcome(StrEnum):
    """Outcome assigned to one canonical transaction in a batch."""

    ALREADY_CLASSIFIED = "already_classified"
    LEARNED_PATTERN = "learned_pattern"
    DETERMINISTIC_RULE = "deterministic_rule"
    GEMINI = "gemini"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    FAILED = "failed"


class BatchTransactionOutcome(ImmutableAccountingModel):
    """Auditable batch result for one canonical transaction."""

    normalized_transaction_id: UUID
    outcome: BatchClassificationOutcome
    explanation: NonEmptyString
    error_type: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_error_metadata(self) -> Self:
        """Require error metadata only for failed transactions."""

        if self.outcome is BatchClassificationOutcome.FAILED and self.error_type is None:
            raise ValueError("Failed batch outcomes require an error type")

        if self.outcome is not BatchClassificationOutcome.FAILED and self.error_type is not None:
            raise ValueError("Only failed batch outcomes may include an error type")

        return self


class BatchClassificationSummary(ImmutableAccountingModel):
    """Complete summary for one upload classification run."""

    upload_id: UUID
    total_records: NonNegativeInt
    canonical_transactions: NonNegativeInt
    ignored_noncanonical: NonNegativeInt
    already_classified: NonNegativeInt
    classified_by_learned_pattern: NonNegativeInt
    classified_by_deterministic_rule: NonNegativeInt
    classified_by_gemini: NonNegativeInt
    manual_review_required: NonNegativeInt
    failed: NonNegativeInt
    outcomes: tuple[BatchTransactionOutcome, ...]

    @model_validator(mode="after")
    def validate_summary_counts(self) -> Self:
        """Ensure every source record and canonical outcome is counted."""

        if self.total_records != (self.canonical_transactions + self.ignored_noncanonical):
            raise ValueError("Batch source-record counts are inconsistent")

        classified_or_reviewed = (
            self.already_classified
            + self.classified_by_learned_pattern
            + self.classified_by_deterministic_rule
            + self.classified_by_gemini
            + self.manual_review_required
            + self.failed
        )

        if self.canonical_transactions != classified_or_reviewed:
            raise ValueError("Batch canonical outcome counts are inconsistent")

        if self.canonical_transactions != len(self.outcomes):
            raise ValueError("Every canonical transaction requires one outcome")

        return self


async def classify_upload(
    *,
    upload_id: UUID,
    transaction_reader: BatchTransactionReader,
    classification_repository: BatchClassificationRepository,
    pattern_lookup: PatternLookup,
    rule_set: DeterministicRuleSet,
    chart_of_accounts: ChartOfAccountsConfig,
    gemini_classifier: GeminiClassifier | None = None,
    initial_classifier: InitialClassifier = classify_initial,
) -> BatchClassificationSummary:
    """Classify every unclassified canonical transaction in one upload."""

    transactions = await transaction_reader.transactions_for_upload(upload_id)
    canonical_transactions = tuple(
        transaction for transaction in transactions if _is_valid_canonical(transaction)
    )

    canonical_ids = tuple(transaction.id for transaction in canonical_transactions)

    if canonical_ids:
        existing_classifications = await classification_repository.find_by_transaction_ids(
            canonical_ids
        )
    else:
        existing_classifications = {}

    outcomes: list[BatchTransactionOutcome] = []

    for transaction in canonical_transactions:
        if transaction.id in existing_classifications:
            outcomes.append(
                BatchTransactionOutcome(
                    normalized_transaction_id=transaction.id,
                    outcome=(BatchClassificationOutcome.ALREADY_CLASSIFIED),
                    explanation=("The transaction already has a stored classification."),
                )
            )
            continue

        try:
            result = await initial_classifier(
                transaction=transaction,
                pattern_lookup=pattern_lookup,
                rule_set=rule_set,
                classification_writer=classification_repository,
                chart_of_accounts=chart_of_accounts,
                gemini_classifier=gemini_classifier,
            )
            outcome = _outcome_from_initial_result(
                transaction=transaction,
                result=result,
            )
        except Exception as exc:
            outcome = _failure_outcome(
                transaction=transaction,
                error=exc,
            )

        outcomes.append(outcome)

    return _build_summary(
        upload_id=upload_id,
        total_records=len(transactions),
        canonical_transactions=len(canonical_transactions),
        outcomes=tuple(outcomes),
    )


def _is_valid_canonical(
    transaction: NormalizedTransaction,
) -> bool:
    """Return whether a transaction is safe for classification."""

    return transaction.status is RecordStatus.VALID and transaction.duplicate_of is None


def _outcome_from_initial_result(
    *,
    transaction: NormalizedTransaction,
    result: InitialClassificationResult,
) -> BatchTransactionOutcome:
    """Convert one initial-classification result into a batch outcome."""

    if isinstance(result, ManualReviewRequiredResult):
        return BatchTransactionOutcome(
            normalized_transaction_id=transaction.id,
            outcome=(BatchClassificationOutcome.MANUAL_REVIEW_REQUIRED),
            explanation=result.explanation,
        )

    if not result.inserted:
        return BatchTransactionOutcome(
            normalized_transaction_id=transaction.id,
            outcome=BatchClassificationOutcome.ALREADY_CLASSIFIED,
            explanation=("An exact classification retry was detected during persistence."),
        )

    if isinstance(result, LearnedPatternClassificationResult):
        return BatchTransactionOutcome(
            normalized_transaction_id=transaction.id,
            outcome=BatchClassificationOutcome.LEARNED_PATTERN,
            explanation=(f"Classified from approved learned pattern {result.pattern_id}."),
        )

    if isinstance(result, DeterministicRuleClassificationResult):
        return BatchTransactionOutcome(
            normalized_transaction_id=transaction.id,
            outcome=BatchClassificationOutcome.DETERMINISTIC_RULE,
            explanation=(f"Classified by deterministic rule {result.rule_id}."),
        )

    if isinstance(result, GeminiClassificationResult):
        return BatchTransactionOutcome(
            normalized_transaction_id=transaction.id,
            outcome=BatchClassificationOutcome.GEMINI,
            explanation=("Classified from validated structured Gemini output."),
        )

    raise TypeError(f"Unsupported initial-classification result: {type(result).__name__}")


def _failure_outcome(
    *,
    transaction: NormalizedTransaction,
    error: Exception,
) -> BatchTransactionOutcome:
    """Create a visible per-transaction failure without aborting the batch."""

    error_type = type(error).__name__
    error_message = str(error).strip() or error_type

    return BatchTransactionOutcome(
        normalized_transaction_id=transaction.id,
        outcome=BatchClassificationOutcome.FAILED,
        explanation=f"Classification failed: {error_message}",
        error_type=error_type,
    )


def _build_summary(
    *,
    upload_id: UUID,
    total_records: int,
    canonical_transactions: int,
    outcomes: tuple[BatchTransactionOutcome, ...],
) -> BatchClassificationSummary:
    """Count auditable outcomes without silently dropping a transaction."""

    counts = Counter(outcome.outcome for outcome in outcomes)

    return BatchClassificationSummary(
        upload_id=upload_id,
        total_records=total_records,
        canonical_transactions=canonical_transactions,
        ignored_noncanonical=(total_records - canonical_transactions),
        already_classified=counts[BatchClassificationOutcome.ALREADY_CLASSIFIED],
        classified_by_learned_pattern=counts[BatchClassificationOutcome.LEARNED_PATTERN],
        classified_by_deterministic_rule=counts[BatchClassificationOutcome.DETERMINISTIC_RULE],
        classified_by_gemini=counts[BatchClassificationOutcome.GEMINI],
        manual_review_required=counts[BatchClassificationOutcome.MANUAL_REVIEW_REQUIRED],
        failed=counts[BatchClassificationOutcome.FAILED],
        outcomes=outcomes,
    )
