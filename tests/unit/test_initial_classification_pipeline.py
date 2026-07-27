"""Tests for trusted initial-classification precedence."""

from __future__ import annotations

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
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)
from app.services.classification.initial_classification import (
    DeterministicRuleClassificationResult,
    LearnedPatternClassificationResult,
    classify_initial,
)
from app.services.classification.rule_config import (
    load_deterministic_rule_set,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
RULES_PATH = Path("sample_config/classification_rules.json")
REVIEWED_AT = datetime(2026, 7, 26, 18, 0, tzinfo=UTC)


class FakePatternLookup:
    """Return one optional approved learned pattern."""

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
    """Record initial persistence calls without MongoDB."""

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


def create_transaction(
    *,
    description: str = "MONTHLY SERVICE FEE",
    amount: Decimal = Decimal("-35.00"),
    direction: TransactionDirection = TransactionDirection.OUTFLOW,
) -> NormalizedTransaction:
    """Create one valid canonical transaction."""

    return NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-PIPELINE-0001",
        transaction_date=date(2026, 4, 30),
        description_original=description,
        description_normalized=description.casefold(),
        amount=amount,
        currency="USD",
        bank_account="Operating Checking",
        direction=direction,
        fingerprint="e" * 64,
        status=RecordStatus.VALID,
        duplicate_of=None,
    )


def create_monthly_fee_correction_pattern() -> LearnedClassificationPattern:
    """Create an approved correction overriding the generic bank-fee rule."""

    reviewer = ReviewerMetadata(
        reviewer_id="shishir",
        reviewed_at=REVIEWED_AT,
        notes="Reviewed supporting documentation.",
    )
    counterparty = Counterparty(
        raw_name="MONTHLY SERVICE FEE",
        normalized_name="Monthly Service Fee",
    )
    previous_decision = ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number="6080",
            account_name="Bank Fees",
        ),
        confidence_score=Decimal("1.000"),
        explanation="The deterministic rule treated this as a bank fee.",
        source=ClassificationSource.DETERMINISTIC_RULE,
        review_required=False,
    )
    corrected_decision = ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number="6090",
            account_name="Office & General",
        ),
        confidence_score=Decimal("1.000"),
        explanation=("The reviewer confirmed this recurring charge belongs to Office & General."),
        source=ClassificationSource.MANUAL_REVIEW,
        review_required=False,
    )
    correction = ClassificationCorrection(
        from_version=1,
        to_version=2,
        previous_decision=previous_decision,
        corrected_decision=corrected_decision,
        corrected_by=reviewer,
        reason=("Supporting documentation identified a general office charge."),
    )
    reusable_decision = ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number="6090",
            account_name="Office & General",
        ),
        confidence_score=Decimal("1.000"),
        explanation=("Matched an exact pattern learned from an approved manual correction."),
        source=ClassificationSource.STORED_CORRECTION,
        review_required=False,
    )

    return LearnedClassificationPattern(
        key=ClassificationPatternKey(
            description_normalized="monthly service fee",
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


def supplied_dependencies():
    """Load the approved account catalog and rule configuration."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = load_deterministic_rule_set(
        RULES_PATH,
        chart_of_accounts=catalog,
    )
    return catalog, rule_set


@pytest.mark.asyncio
async def test_approved_correction_precedes_deterministic_rule() -> None:
    """An exact reviewed correction overrides a general configured rule."""

    catalog, rule_set = supplied_dependencies()
    pattern = create_monthly_fee_correction_pattern()
    lookup = FakePatternLookup(pattern)
    writer = FakeClassificationWriter()

    result = await classify_initial(
        transaction=create_transaction(),
        pattern_lookup=lookup,
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
    )

    assert isinstance(result, LearnedPatternClassificationResult)
    assert result.pattern_id == pattern.id
    assert result.classification.decision.source is (ClassificationSource.STORED_CORRECTION)
    assert result.classification.decision.qbo_account.account_number == "6090"
    assert len(lookup.requested_keys) == 1
    assert writer.saved == [result.classification]


@pytest.mark.asyncio
async def test_deterministic_rule_runs_when_no_pattern_exists() -> None:
    """General rules run only after an exact approved-pattern miss."""

    catalog, rule_set = supplied_dependencies()
    lookup = FakePatternLookup(None)
    writer = FakeClassificationWriter()

    result = await classify_initial(
        transaction=create_transaction(),
        pattern_lookup=lookup,
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
    )

    assert isinstance(result, DeterministicRuleClassificationResult)
    assert result.rule_id == "bank_fees"
    assert result.priority == 380
    assert result.classification.decision.source is (ClassificationSource.DETERMINISTIC_RULE)
    assert result.classification.decision.qbo_account.account_number == "6080"
    assert len(lookup.requested_keys) == 1
    assert writer.saved == [result.classification]


@pytest.mark.asyncio
async def test_unmatched_transaction_is_left_for_gemini() -> None:
    """No trusted match leaves the transaction unpersisted for fallback."""

    catalog, rule_set = supplied_dependencies()
    writer = FakeClassificationWriter()

    result = await classify_initial(
        transaction=create_transaction(
            description="UNRECOGNIZED MERCHANT PAYMENT",
            amount=Decimal("-125.00"),
        ),
        pattern_lookup=FakePatternLookup(None),
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
    )

    assert result is None
    assert writer.saved == []


@pytest.mark.asyncio
async def test_selected_decision_is_persisted_exactly_once() -> None:
    """Precedence selection cannot create two classification writes."""

    catalog, rule_set = supplied_dependencies()
    writer = FakeClassificationWriter()

    result = await classify_initial(
        transaction=create_transaction(),
        pattern_lookup=FakePatternLookup(create_monthly_fee_correction_pattern()),
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
    )

    assert result is not None
    assert len(writer.saved) == 1


@pytest.mark.asyncio
async def test_exact_repository_retry_is_preserved() -> None:
    """The combined pipeline exposes repository idempotency."""

    catalog, rule_set = supplied_dependencies()
    writer = FakeClassificationWriter(inserted=False)

    result = await classify_initial(
        transaction=create_transaction(),
        pattern_lookup=FakePatternLookup(None),
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
    )

    assert isinstance(result, DeterministicRuleClassificationResult)
    assert result.inserted is False
    assert len(writer.saved) == 1


@pytest.mark.asyncio
async def test_persistence_conflict_stops_the_pipeline() -> None:
    """A conflicting stored classification is never hidden or retried."""

    catalog, rule_set = supplied_dependencies()
    writer = FakeClassificationWriter(
        error=RuntimeError("classification conflict"),
    )

    with pytest.raises(
        RuntimeError,
        match="classification conflict",
    ):
        await classify_initial(
            transaction=create_transaction(),
            pattern_lookup=FakePatternLookup(create_monthly_fee_correction_pattern()),
            rule_set=rule_set,
            classification_writer=writer,
            chart_of_accounts=catalog,
        )

    assert len(writer.saved) == 1
