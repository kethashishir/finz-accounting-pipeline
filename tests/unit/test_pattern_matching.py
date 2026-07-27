"""Tests for applying active learned classification patterns."""

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
from app.services.classification.pattern_matching import (
    InvalidMatchedPatternAccountError,
    InvalidMatchedPatternError,
    UnsafePatternMatchTransactionError,
    match_learned_pattern,
    pattern_key_for_transaction,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
REVIEWED_AT = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)


class FakePatternLookup:
    """Controllable in-memory implementation of the lookup protocol."""

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


def create_transaction(
    *,
    status: RecordStatus = RecordStatus.VALID,
    duplicate_of=None,
    description_original: str = "BRIGHTFIX FUEL STOP #204",
    description_normalized: str = "BrightFix Fuel Stop",
    amount: Decimal = Decimal("-147.82"),
) -> NormalizedTransaction:
    """Create a new transaction eligible for learned-pattern matching."""

    return NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-202605-0042",
        transaction_date=date(2026, 5, 15),
        description_original=description_original,
        description_normalized=description_normalized,
        amount=amount,
        currency="usd",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint="a" * 64,
        status=status,
        duplicate_of=duplicate_of,
    )


def create_pattern(
    *,
    active: bool = True,
    account_number: str = "6020",
    account_name: str = "Vehicle & Fuel",
    key: ClassificationPatternKey | None = None,
) -> LearnedClassificationPattern:
    """Create one approved learned fuel pattern."""

    reviewer = ReviewerMetadata(
        reviewer_id="shishir",
        reviewed_at=REVIEWED_AT,
        notes="Approved after reviewing the source transaction.",
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
            account_number=account_number,
            account_name=account_name,
        ),
        confidence_score=Decimal("1.000"),
        explanation="A reviewer confirmed the transaction was fuel.",
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
            account_number=account_number,
            account_name=account_name,
        ),
        confidence_score=Decimal("1.000"),
        explanation=("Matched an exact pattern learned from an approved correction."),
        source=ClassificationSource.STORED_CORRECTION,
        review_required=False,
    )

    return LearnedClassificationPattern(
        key=key
        or ClassificationPatternKey(
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
        active=active,
    )


def test_pattern_key_normalizes_current_transaction() -> None:
    """Current transaction values become a stable exact-match key."""

    key = pattern_key_for_transaction(create_transaction())

    assert key.description_normalized == "brightfix fuel stop"
    assert key.bank_account == "operating checking"
    assert key.direction is TransactionDirection.OUTFLOW
    assert key.currency == "USD"


@pytest.mark.asyncio
async def test_active_pattern_produces_auditable_current_decision() -> None:
    """The match reuses accounting logic but preserves current raw evidence."""

    transaction = create_transaction()
    pattern = create_pattern()
    lookup = FakePatternLookup(pattern)

    result = await match_learned_pattern(
        transaction=transaction,
        pattern_lookup=lookup,
        chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
    )

    assert result is not None
    assert result.pattern_id == pattern.id
    assert result.source_transaction_id == pattern.source_transaction_id
    assert result.key == pattern.key
    assert lookup.requested_keys == [pattern.key]

    assert result.decision.source is ClassificationSource.STORED_CORRECTION
    assert result.decision.transaction_type is (TransactionType.OPERATING_EXPENSE)
    assert result.decision.qbo_account.account_number == "6020"
    assert result.decision.qbo_account.account_name == "Vehicle & Fuel"
    assert result.decision.qbo_account.qbo_account_id is None
    assert result.decision.counterparty is not None
    assert result.decision.counterparty.raw_name == (transaction.description_original)
    assert result.decision.counterparty.normalized_name == ("BrightFix Fuel Stop")
    assert str(pattern.id) in result.decision.explanation


@pytest.mark.asyncio
async def test_amount_and_date_do_not_prevent_exact_pattern_match() -> None:
    """Recurring transactions may vary in value while sharing the same key."""

    transaction = create_transaction(amount=Decimal("-263.41"))
    pattern = create_pattern()

    result = await match_learned_pattern(
        transaction=transaction,
        pattern_lookup=FakePatternLookup(pattern),
        chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
    )

    assert result is not None
    assert result.key == pattern.key


@pytest.mark.asyncio
async def test_missing_active_pattern_returns_none() -> None:
    """No stored correction match allows later classifiers to continue."""

    lookup = FakePatternLookup(None)

    result = await match_learned_pattern(
        transaction=create_transaction(),
        pattern_lookup=lookup,
        chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
    )

    assert result is None
    assert len(lookup.requested_keys) == 1


@pytest.mark.asyncio
async def test_duplicate_transaction_is_rejected_before_lookup() -> None:
    """Duplicate rows cannot independently consume learned patterns."""

    transaction = create_transaction(
        status=RecordStatus.DUPLICATE,
        duplicate_of=uuid4(),
    )
    lookup = FakePatternLookup(create_pattern())

    with pytest.raises(
        UnsafePatternMatchTransactionError,
        match="valid canonical transactions",
    ):
        await match_learned_pattern(
            transaction=transaction,
            pattern_lookup=lookup,
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )

    assert lookup.requested_keys == []


@pytest.mark.asyncio
async def test_inactive_pattern_returned_by_repository_is_rejected() -> None:
    """Defense in depth prevents reuse of historical inactive patterns."""

    with pytest.raises(
        InvalidMatchedPatternError,
        match="inactive learned pattern",
    ):
        await match_learned_pattern(
            transaction=create_transaction(),
            pattern_lookup=FakePatternLookup(create_pattern(active=False)),
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )


@pytest.mark.asyncio
async def test_repository_pattern_key_must_match_requested_key() -> None:
    """A repository bug cannot apply an unrelated merchant pattern."""

    unrelated_key = ClassificationPatternKey(
        description_normalized="unrelated merchant",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        currency="USD",
    )

    with pytest.raises(
        InvalidMatchedPatternError,
        match="does not match the transaction key",
    ):
        await match_learned_pattern(
            transaction=create_transaction(),
            pattern_lookup=FakePatternLookup(create_pattern(key=unrelated_key)),
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )


@pytest.mark.asyncio
async def test_pattern_account_must_still_match_catalog() -> None:
    """Catalog changes or forged names invalidate a stored mapping."""

    with pytest.raises(
        InvalidMatchedPatternAccountError,
        match="name does not match",
    ):
        await match_learned_pattern(
            transaction=create_transaction(),
            pattern_lookup=FakePatternLookup(
                create_pattern(
                    account_number="6020",
                    account_name="Forged Fuel Account",
                )
            ),
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )
