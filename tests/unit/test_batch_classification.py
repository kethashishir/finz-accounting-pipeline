"""Tests for safe batch initial-classification orchestration."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.models.classification import (
    ClassificationDecision,
    ClassificationSource,
    Counterparty,
    QuickBooksAccountMapping,
    TransactionClassification,
    TransactionType,
)
from app.models.classification_pattern import ClassificationPatternKey
from app.models.ingestion import (
    IssueSeverity,
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
    ValidationIssue,
)
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)
from app.services.classification.batch_classification import (
    BatchClassificationOutcome,
    classify_upload,
)
from app.services.classification.initial_classification import (
    DeterministicRuleClassificationResult,
    GeminiClassificationResult,
    InitialClassificationResult,
    LearnedPatternClassificationResult,
    ManualReviewReason,
    ManualReviewRequiredResult,
)
from app.services.classification.rule_config import (
    load_deterministic_rule_set,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
RULES_PATH = Path("sample_config/classification_rules.json")


class FakeTransactionReader:
    """Return a configured upload transaction sequence."""

    def __init__(
        self,
        transactions: tuple[NormalizedTransaction, ...],
    ) -> None:
        self.transactions = transactions
        self.upload_ids: list[UUID] = []

    async def transactions_for_upload(
        self,
        upload_id: UUID,
    ) -> tuple[NormalizedTransaction, ...]:
        self.upload_ids.append(upload_id)
        return self.transactions


class FakeClassificationRepository:
    """Record batch lookups and reject unexpected direct writes."""

    def __init__(
        self,
        existing: dict[UUID, TransactionClassification] | None = None,
    ) -> None:
        self.existing = existing or {}
        self.lookup_calls: list[tuple[UUID, ...]] = []
        self.saved: list[TransactionClassification] = []

    async def find_by_transaction_ids(
        self,
        normalized_transaction_ids,
    ) -> dict[UUID, TransactionClassification]:
        requested = tuple(normalized_transaction_ids)
        self.lookup_calls.append(requested)

        return {
            transaction_id: classification
            for transaction_id, classification in self.existing.items()
            if transaction_id in requested
        }

    async def save_initial(
        self,
        classification: TransactionClassification,
    ) -> bool:
        self.saved.append(classification)
        return True


class MissingPatternLookup:
    """Represent a learned-pattern miss for dependency wiring."""

    async def find_active(
        self,
        key: ClassificationPatternKey,
    ):
        return None


class FakeInitialClassifier:
    """Return configured per-transaction results or failures."""

    def __init__(
        self,
        responses: dict[
            UUID,
            InitialClassificationResult | Exception,
        ],
    ) -> None:
        self.responses = responses
        self.requests: list[UUID] = []

    async def __call__(
        self,
        *,
        transaction: NormalizedTransaction,
        **_: object,
    ) -> InitialClassificationResult:
        self.requests.append(transaction.id)

        response = self.responses[transaction.id]

        if isinstance(response, Exception):
            raise response

        return response


def create_transaction(
    *,
    upload_id: UUID,
    index: int,
    status: RecordStatus = RecordStatus.VALID,
    duplicate_of: UUID | None = None,
) -> NormalizedTransaction:
    """Create one normalized transaction for batch tests."""

    validation_issues = (
        [
            ValidationIssue(
                code="batch_test_invalid",
                field="_record",
                message="Test transaction is intentionally invalid.",
                severity=IssueSeverity.ERROR,
            )
        ]
        if status is RecordStatus.INVALID
        else []
    )

    return NormalizedTransaction(
        upload_id=upload_id,
        raw_record_id=uuid4(),
        source_transaction_id=f"BF-BATCH-{index:04d}",
        transaction_date=date(2026, 6, index),
        description_original=f"Batch Merchant {index}",
        description_normalized=f"batch merchant {index}",
        amount=Decimal("-100.00"),
        currency="USD",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint=f"{index:x}" * 64,
        status=status,
        duplicate_of=duplicate_of,
        validation_issues=validation_issues,
    )


def create_classification(
    transaction: NormalizedTransaction,
    *,
    source: ClassificationSource,
) -> TransactionClassification:
    """Create a valid stored classification fixture."""

    return TransactionClassification(
        normalized_transaction_id=transaction.id,
        decision=ClassificationDecision(
            transaction_type=TransactionType.OPERATING_EXPENSE,
            counterparty=Counterparty(
                raw_name=transaction.description_original,
                normalized_name=transaction.description_original,
            ),
            qbo_account=QuickBooksAccountMapping(
                account_number="6090",
                account_name="Office & General",
            ),
            confidence_score=Decimal("0.950"),
            explanation="A valid batch classification fixture.",
            source=source,
            review_required=False,
        ),
    )


def deterministic_result(
    transaction: NormalizedTransaction,
    *,
    inserted: bool = True,
) -> DeterministicRuleClassificationResult:
    """Create one deterministic pipeline result."""

    return DeterministicRuleClassificationResult(
        inserted=inserted,
        rule_id="office_general",
        priority=100,
        classification=create_classification(
            transaction,
            source=ClassificationSource.DETERMINISTIC_RULE,
        ),
    )


def supplied_dependencies():
    """Load the approved account and deterministic-rule configuration."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = load_deterministic_rule_set(
        RULES_PATH,
        chart_of_accounts=catalog,
    )
    return catalog, rule_set


@pytest.mark.asyncio
async def test_batch_filters_noncanonical_and_bulk_skips_existing() -> None:
    """Only unclassified valid canonical transactions are classified."""

    upload_id = uuid4()
    existing_transaction = create_transaction(
        upload_id=upload_id,
        index=1,
    )
    new_transaction = create_transaction(
        upload_id=upload_id,
        index=2,
    )
    invalid_transaction = create_transaction(
        upload_id=upload_id,
        index=3,
        status=RecordStatus.INVALID,
    )
    duplicate_transaction = create_transaction(
        upload_id=upload_id,
        index=4,
        status=RecordStatus.DUPLICATE,
        duplicate_of=existing_transaction.id,
    )

    existing_classification = create_classification(
        existing_transaction,
        source=ClassificationSource.DETERMINISTIC_RULE,
    )
    repository = FakeClassificationRepository(
        {
            existing_transaction.id: existing_classification,
        }
    )
    classifier = FakeInitialClassifier(
        {
            new_transaction.id: deterministic_result(new_transaction),
        }
    )
    catalog, rule_set = supplied_dependencies()

    summary = await classify_upload(
        upload_id=upload_id,
        transaction_reader=FakeTransactionReader(
            (
                existing_transaction,
                new_transaction,
                invalid_transaction,
                duplicate_transaction,
            )
        ),
        classification_repository=repository,
        pattern_lookup=MissingPatternLookup(),
        rule_set=rule_set,
        chart_of_accounts=catalog,
        initial_classifier=classifier,
    )

    assert repository.lookup_calls == [
        (
            existing_transaction.id,
            new_transaction.id,
        )
    ]
    assert classifier.requests == [new_transaction.id]
    assert summary.total_records == 4
    assert summary.canonical_transactions == 2
    assert summary.ignored_noncanonical == 2
    assert summary.already_classified == 1
    assert summary.classified_by_deterministic_rule == 1
    assert summary.failed == 0
    assert [outcome.outcome for outcome in summary.outcomes] == [
        BatchClassificationOutcome.ALREADY_CLASSIFIED,
        BatchClassificationOutcome.DETERMINISTIC_RULE,
    ]


@pytest.mark.asyncio
async def test_batch_counts_every_success_review_and_retry_outcome() -> None:
    """Every result type contributes to exactly one summary count."""

    upload_id = uuid4()
    transactions = tuple(
        create_transaction(
            upload_id=upload_id,
            index=index,
        )
        for index in range(1, 6)
    )

    learned = LearnedPatternClassificationResult(
        inserted=True,
        pattern_id=uuid4(),
        source_transaction_id=uuid4(),
        classification=create_classification(
            transactions[0],
            source=ClassificationSource.STORED_CORRECTION,
        ),
    )
    deterministic = deterministic_result(transactions[1])
    gemini = GeminiClassificationResult(
        inserted=True,
        classification=create_classification(
            transactions[2],
            source=ClassificationSource.GEMINI,
        ),
    )
    manual = ManualReviewRequiredResult(
        normalized_transaction_id=transactions[3].id,
        reason=ManualReviewReason.GEMINI_DISABLED,
        explanation="No safe automated classification was available.",
    )
    exact_retry = deterministic_result(
        transactions[4],
        inserted=False,
    )

    classifier = FakeInitialClassifier(
        {
            transactions[0].id: learned,
            transactions[1].id: deterministic,
            transactions[2].id: gemini,
            transactions[3].id: manual,
            transactions[4].id: exact_retry,
        }
    )
    repository = FakeClassificationRepository()
    catalog, rule_set = supplied_dependencies()

    summary = await classify_upload(
        upload_id=upload_id,
        transaction_reader=FakeTransactionReader(transactions),
        classification_repository=repository,
        pattern_lookup=MissingPatternLookup(),
        rule_set=rule_set,
        chart_of_accounts=catalog,
        initial_classifier=classifier,
    )

    assert summary.total_records == 5
    assert summary.canonical_transactions == 5
    assert summary.ignored_noncanonical == 0
    assert summary.already_classified == 1
    assert summary.classified_by_learned_pattern == 1
    assert summary.classified_by_deterministic_rule == 1
    assert summary.classified_by_gemini == 1
    assert summary.manual_review_required == 1
    assert summary.failed == 0


@pytest.mark.asyncio
async def test_batch_records_failure_and_continues() -> None:
    """One failed transaction cannot hide a later successful result."""

    upload_id = uuid4()
    failed_transaction = create_transaction(
        upload_id=upload_id,
        index=1,
    )
    successful_transaction = create_transaction(
        upload_id=upload_id,
        index=2,
    )

    classifier = FakeInitialClassifier(
        {
            failed_transaction.id: RuntimeError("classification persistence conflict"),
            successful_transaction.id: deterministic_result(successful_transaction),
        }
    )
    catalog, rule_set = supplied_dependencies()

    summary = await classify_upload(
        upload_id=upload_id,
        transaction_reader=FakeTransactionReader(
            (
                failed_transaction,
                successful_transaction,
            )
        ),
        classification_repository=FakeClassificationRepository(),
        pattern_lookup=MissingPatternLookup(),
        rule_set=rule_set,
        chart_of_accounts=catalog,
        initial_classifier=classifier,
    )

    assert classifier.requests == [
        failed_transaction.id,
        successful_transaction.id,
    ]
    assert summary.failed == 1
    assert summary.classified_by_deterministic_rule == 1

    failed_outcome = summary.outcomes[0]

    assert failed_outcome.outcome is (BatchClassificationOutcome.FAILED)
    assert failed_outcome.error_type == "RuntimeError"
    assert "persistence conflict" in failed_outcome.explanation
    assert summary.outcomes[1].outcome is (BatchClassificationOutcome.DETERMINISTIC_RULE)


@pytest.mark.asyncio
async def test_batch_with_no_canonical_transactions_avoids_lookup() -> None:
    """An upload containing only invalid records performs no lookup."""

    upload_id = uuid4()
    invalid_transaction = create_transaction(
        upload_id=upload_id,
        index=1,
        status=RecordStatus.INVALID,
    )
    repository = FakeClassificationRepository()
    classifier = FakeInitialClassifier({})
    catalog, rule_set = supplied_dependencies()

    summary = await classify_upload(
        upload_id=upload_id,
        transaction_reader=FakeTransactionReader((invalid_transaction,)),
        classification_repository=repository,
        pattern_lookup=MissingPatternLookup(),
        rule_set=rule_set,
        chart_of_accounts=catalog,
        initial_classifier=classifier,
    )

    assert repository.lookup_calls == []
    assert classifier.requests == []
    assert summary.total_records == 1
    assert summary.canonical_transactions == 0
    assert summary.ignored_noncanonical == 1
    assert summary.outcomes == ()
