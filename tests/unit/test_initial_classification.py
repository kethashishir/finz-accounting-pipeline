"""Tests for persisting initial learned-pattern classifications."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.classification import (
    ClassificationCorrection,
    ClassificationDecision,
    ClassificationSource,
    Counterparty,
    QuickBooksAccountMapping,
    ReviewerMetadata,
    ReviewStatus,
    TransactionClassification,
    TransactionType,
)
from app.models.classification_pattern import (
    ClassificationPatternKey,
    LearnedClassificationPattern,
)
from app.models.ingestion import (
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
)
from app.services.accounting.chart_of_accounts import load_chart_of_accounts
from app.services.classification.initial_classification import (
    classify_from_learned_pattern,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
REVIEWED_AT = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)


class FakePatternLookup:
    """Return one configured active pattern."""

    def __init__(
        self,
        pattern: LearnedClassificationPattern | None,
    ) -> None:
        self.pattern = pattern
        self.requested_keys: list[ClassificationPatternKey] = []

    async def find_active(
        self,
        key: ClassificationPatternKey,
    ) -> LearnedClassificationPattern | None:
        self.requested_keys.append(key)
        return self.pattern


class FakeClassificationWriter:
    """Record classifications without using MongoDB."""

    def __init__(
        self,
        *,
        inserted: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.inserted = inserted
        self.error = error
        self.saved: list[TransactionClassification] = []

    async def save_initial(
        self,
        classification: TransactionClassification,
    ) -> bool:
        self.saved.append(classification)

        if self.error is not None:
            raise self.error

        return self.inserted


def create_transaction() -> NormalizedTransaction:
    """Create a new canonical transaction matching the learned key."""

    return NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-202605-0042",
        transaction_date=date(2026, 5, 15),
        description_original="BRIGHTFIX FUEL STOP #204",
        description_normalized="BrightFix Fuel Stop",
        amount=Decimal("-147.82"),
        currency="usd",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint="a" * 64,
        status=RecordStatus.VALID,
    )


def create_pattern() -> LearnedClassificationPattern:
    """Create one active pattern backed by an approved manual correction."""

    reviewer = ReviewerMetadata(
        reviewer_id="shishir",
        reviewed_at=REVIEWED_AT,
        notes="Approved after reviewing the original transaction.",
    )
    counterparty = Counterparty(
        raw_name="BrightFix Fuel Stop",
        normalized_name="BrightFix Fuel Stop",
    )
    previous_decision = ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number="6090",
            account_name="Office & General",
        ),
        confidence_score=Decimal("0.700"),
        explanation="The original automated mapping was uncertain.",
        source=ClassificationSource.GEMINI,
        review_required=True,
    )
    corrected_decision = ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number="6020",
            account_name="Vehicle & Fuel",
        ),
        confidence_score=Decimal("1.000"),
        explanation="A reviewer confirmed that the payment was fuel.",
        source=ClassificationSource.MANUAL_REVIEW,
        review_required=False,
    )
    correction = ClassificationCorrection(
        from_version=1,
        to_version=2,
        previous_decision=previous_decision,
        corrected_decision=corrected_decision,
        corrected_by=reviewer,
        reason="Correct the expense account using the merchant description.",
    )
    reusable_decision = ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number="6020",
            account_name="Vehicle & Fuel",
        ),
        confidence_score=Decimal("1.000"),
        explanation=(
            "Matched an exact transaction pattern learned from an approved manual correction."
        ),
        source=ClassificationSource.STORED_CORRECTION,
        review_required=False,
    )

    return LearnedClassificationPattern(
        key=ClassificationPatternKey(
            description_normalized="brightfix fuel stop",
            bank_account="operating checking",
            direction=TransactionDirection.OUTFLOW,
            currency="USD",
        ),
        decision=reusable_decision,
        source_transaction_id=uuid4(),
        source_classification_version=2,
        source_correction=correction,
        source_review_status=ReviewStatus.APPROVED,
        approved_by=reviewer,
        learned_at=REVIEWED_AT + timedelta(seconds=1),
    )


@pytest.mark.asyncio
async def test_matching_pattern_is_persisted_as_initial_classification() -> None:
    """A learned decision becomes a version-one current classification."""

    transaction = create_transaction()
    pattern = create_pattern()
    lookup = FakePatternLookup(pattern)
    writer = FakeClassificationWriter()

    result = await classify_from_learned_pattern(
        transaction=transaction,
        pattern_lookup=lookup,
        classification_writer=writer,
        chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
    )

    assert result is not None
    assert result.inserted is True
    assert result.pattern_id == pattern.id
    assert result.source_transaction_id == pattern.source_transaction_id

    classification = result.classification

    assert writer.saved == [classification]
    assert classification.normalized_transaction_id == transaction.id
    assert classification.version == 1
    assert classification.review_status is ReviewStatus.PENDING
    assert classification.reviewer is None
    assert classification.corrections == ()
    assert classification.decision.source is (ClassificationSource.STORED_CORRECTION)
    assert classification.decision.review_required is False
    assert classification.decision.qbo_account.account_number == "6020"
    assert classification.decision.counterparty is not None
    assert classification.decision.counterparty.raw_name == (transaction.description_original)


@pytest.mark.asyncio
async def test_missing_pattern_does_not_write_classification() -> None:
    """No match allows deterministic and AI classifiers to run later."""

    lookup = FakePatternLookup(None)
    writer = FakeClassificationWriter()

    result = await classify_from_learned_pattern(
        transaction=create_transaction(),
        pattern_lookup=lookup,
        classification_writer=writer,
        chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
    )

    assert result is None
    assert len(lookup.requested_keys) == 1
    assert writer.saved == []


@pytest.mark.asyncio
async def test_exact_retry_reports_existing_classification() -> None:
    """Repository idempotency is preserved by the orchestration service."""

    writer = FakeClassificationWriter(inserted=False)

    result = await classify_from_learned_pattern(
        transaction=create_transaction(),
        pattern_lookup=FakePatternLookup(create_pattern()),
        classification_writer=writer,
        chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
    )

    assert result is not None
    assert result.inserted is False
    assert len(writer.saved) == 1


@pytest.mark.asyncio
async def test_current_transaction_uuid_replaces_pattern_source_uuid() -> None:
    """The new classification belongs to the current transaction."""

    transaction = create_transaction()
    pattern = create_pattern()

    assert transaction.id != pattern.source_transaction_id

    result = await classify_from_learned_pattern(
        transaction=transaction,
        pattern_lookup=FakePatternLookup(pattern),
        classification_writer=FakeClassificationWriter(),
        chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
    )

    assert result is not None
    assert result.classification.normalized_transaction_id == transaction.id
    assert result.source_transaction_id == pattern.source_transaction_id


@pytest.mark.asyncio
async def test_persistence_conflict_is_not_hidden() -> None:
    """A conflicting existing classification remains a visible failure."""

    expected_error = RuntimeError("conflicting stored classification")
    writer = FakeClassificationWriter(error=expected_error)

    with pytest.raises(
        RuntimeError,
        match="conflicting stored classification",
    ):
        await classify_from_learned_pattern(
            transaction=create_transaction(),
            pattern_lookup=FakePatternLookup(create_pattern()),
            classification_writer=writer,
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )

    assert len(writer.saved) == 1
