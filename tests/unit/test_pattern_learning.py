"""Tests for learning patterns from approved accounting corrections."""

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
from app.models.ingestion import (
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
)
from app.services.accounting.chart_of_accounts import load_chart_of_accounts
from app.services.classification.pattern_learning import (
    InvalidPatternAccountError,
    UnsafePatternLearningSourceError,
    learn_pattern,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
REVIEWED_AT = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)
LEARNED_AT = REVIEWED_AT + timedelta(seconds=1)


def create_transaction(
    *,
    status: RecordStatus = RecordStatus.VALID,
    duplicate_of=None,
) -> NormalizedTransaction:
    """Create a canonical fuel transaction."""

    return NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-202604-0001",
        transaction_date=date(2026, 4, 1),
        description_original="BrightFix Fuel Stop",
        description_normalized="BrightFix Fuel Stop",
        amount=Decimal("-100.00"),
        currency="usd",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint="a" * 64,
        status=status,
        duplicate_of=duplicate_of,
    )


def create_classification(
    transaction: NormalizedTransaction,
    *,
    review_status: ReviewStatus = ReviewStatus.APPROVED,
    account_number: str = "6020",
    account_name: str = "Vehicle & Fuel",
    corrected_source: ClassificationSource = ClassificationSource.MANUAL_REVIEW,
    include_correction: bool = True,
    normalized_transaction_id=None,
) -> TransactionClassification:
    """Create a corrected and optionally approved classification."""

    reviewer = ReviewerMetadata(
        reviewer_id="shishir",
        reviewed_at=REVIEWED_AT,
        notes="Approved after checking the source bank transaction.",
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
        explanation="The initial automated mapping was uncertain.",
        source=ClassificationSource.GEMINI,
        review_required=True,
    )
    corrected_decision = ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number=account_number,
            account_name=account_name,
            qbo_account_id="sandbox-account-6020",
        ),
        confidence_score=Decimal("1.000"),
        explanation="A reviewer confirmed the payment was vehicle fuel.",
        source=corrected_source,
        review_required=False,
    )

    transaction_id = normalized_transaction_id or transaction.id

    if not include_correction:
        return TransactionClassification(
            normalized_transaction_id=transaction_id,
            decision=previous_decision,
            review_status=review_status,
            reviewer=(reviewer if review_status is not ReviewStatus.PENDING else None),
        )

    correction = ClassificationCorrection(
        from_version=1,
        to_version=2,
        previous_decision=previous_decision,
        corrected_decision=corrected_decision,
        corrected_by=reviewer,
        reason="Correct the expense account using the bank description.",
    )

    return TransactionClassification(
        normalized_transaction_id=transaction_id,
        version=2,
        decision=corrected_decision,
        review_status=review_status,
        reviewer=(reviewer if review_status is not ReviewStatus.PENDING else None),
        corrections=(correction,),
    )


def test_approved_manual_correction_becomes_portable_pattern() -> None:
    """The factory creates a normalized, auditable reusable decision."""

    transaction = create_transaction()
    classification = create_classification(transaction)
    catalog = load_chart_of_accounts(CATALOG_PATH)

    pattern = learn_pattern(
        transaction=transaction,
        classification=classification,
        chart_of_accounts=catalog,
        learned_at=LEARNED_AT,
    )

    assert pattern.key.description_normalized == "brightfix fuel stop"
    assert pattern.key.bank_account == "operating checking"
    assert pattern.key.currency == "USD"
    assert pattern.source_transaction_id == transaction.id
    assert pattern.source_classification_version == 2
    assert pattern.approved_by == classification.reviewer
    assert pattern.source_correction == classification.corrections[-1]

    assert pattern.decision.source is ClassificationSource.STORED_CORRECTION
    assert pattern.decision.confidence_score == Decimal("1.000")
    assert pattern.decision.qbo_account.account_number == "6020"
    assert pattern.decision.qbo_account.account_name == "Vehicle & Fuel"
    assert pattern.decision.qbo_account.qbo_account_id is None


def test_pending_classification_cannot_be_learned() -> None:
    """A correction must receive final approval before reuse."""

    transaction = create_transaction()
    classification = create_classification(
        transaction,
        review_status=ReviewStatus.PENDING,
    )

    with pytest.raises(
        UnsafePatternLearningSourceError,
        match="approved classification",
    ):
        learn_pattern(
            transaction=transaction,
            classification=classification,
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
            learned_at=LEARNED_AT,
        )


def test_classification_without_correction_cannot_be_learned() -> None:
    """An ordinary approved decision is not a learned correction."""

    transaction = create_transaction()
    classification = create_classification(
        transaction,
        include_correction=False,
    )

    with pytest.raises(
        UnsafePatternLearningSourceError,
        match="approved correction history",
    ):
        learn_pattern(
            transaction=transaction,
            classification=classification,
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
            learned_at=LEARNED_AT,
        )


def test_transaction_and_classification_ids_must_match() -> None:
    """Provenance cannot be attached to another transaction."""

    transaction = create_transaction()
    classification = create_classification(
        transaction,
        normalized_transaction_id=uuid4(),
    )

    with pytest.raises(
        UnsafePatternLearningSourceError,
        match="identifiers do not match",
    ):
        learn_pattern(
            transaction=transaction,
            classification=classification,
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
            learned_at=LEARNED_AT,
        )


def test_duplicate_transaction_cannot_produce_pattern() -> None:
    """Duplicate evidence must not independently influence learning."""

    canonical_id = uuid4()
    transaction = create_transaction(
        status=RecordStatus.DUPLICATE,
        duplicate_of=canonical_id,
    )
    classification = create_classification(transaction)

    with pytest.raises(
        UnsafePatternLearningSourceError,
        match="valid canonical transactions",
    ):
        learn_pattern(
            transaction=transaction,
            classification=classification,
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
            learned_at=LEARNED_AT,
        )


def test_unknown_corrected_account_is_rejected() -> None:
    """A correction cannot teach an account outside the catalog."""

    transaction = create_transaction()
    classification = create_classification(
        transaction,
        account_number="9999",
        account_name="Invented Expense",
    )

    with pytest.raises(
        InvalidPatternAccountError,
        match="active configured account",
    ):
        learn_pattern(
            transaction=transaction,
            classification=classification,
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
            learned_at=LEARNED_AT,
        )


def test_corrected_account_name_must_match_catalog() -> None:
    """A valid account number cannot be paired with a forged name."""

    transaction = create_transaction()
    classification = create_classification(
        transaction,
        account_number="6020",
        account_name="Incorrect Account Name",
    )

    with pytest.raises(
        InvalidPatternAccountError,
        match="name does not match",
    ):
        learn_pattern(
            transaction=transaction,
            classification=classification,
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
            learned_at=LEARNED_AT,
        )
