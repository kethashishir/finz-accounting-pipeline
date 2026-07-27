"""Tests for approved classification-correction pattern models."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

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
from app.models.ingestion import TransactionDirection

APPROVED_AT = datetime(
    2026,
    7,
    25,
    18,
    0,
    tzinfo=UTC,
)


def create_reviewer() -> ReviewerMetadata:
    return ReviewerMetadata(
        reviewer_id="shishir",
        reviewed_at=APPROVED_AT,
        notes="Approved after checking the bank description.",
    )


def create_previous_decision() -> ClassificationDecision:
    return ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=Counterparty(
            raw_name="BrightFix Fuel Stop",
            normalized_name="BrightFix Fuel Stop",
        ),
        qbo_account=QuickBooksAccountMapping(
            account_number="6090",
            account_name="Office & General",
        ),
        confidence_score=Decimal("0.700"),
        explanation="The original automated mapping was uncertain.",
        source=ClassificationSource.GEMINI,
        review_required=True,
    )


def create_corrected_decision(
    *,
    source: ClassificationSource = ClassificationSource.MANUAL_REVIEW,
    qbo_account_id: str | None = None,
) -> ClassificationDecision:
    return ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=Counterparty(
            raw_name="BrightFix Fuel Stop",
            normalized_name="BrightFix Fuel Stop",
        ),
        qbo_account=QuickBooksAccountMapping(
            account_number="6020",
            account_name="Vehicle & Fuel",
            qbo_account_id=qbo_account_id,
        ),
        confidence_score=Decimal("1.000"),
        explanation="A reviewer confirmed this was vehicle fuel.",
        source=source,
        review_required=False,
    )


def create_source_correction(
    *,
    corrected_source: ClassificationSource = (ClassificationSource.MANUAL_REVIEW),
) -> ClassificationCorrection:
    return ClassificationCorrection(
        from_version=1,
        to_version=2,
        previous_decision=create_previous_decision(),
        corrected_decision=create_corrected_decision(source=corrected_source),
        corrected_by=create_reviewer(),
        reason="Correct the expense account using the bank description.",
    )


def create_pattern_decision(
    *,
    account_number: str = "6020",
    account_name: str = "Vehicle & Fuel",
    source: ClassificationSource = (ClassificationSource.STORED_CORRECTION),
    qbo_account_id: str | None = None,
) -> ClassificationDecision:
    return ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=Counterparty(
            raw_name="BrightFix Fuel Stop",
            normalized_name="BrightFix Fuel Stop",
        ),
        qbo_account=QuickBooksAccountMapping(
            account_number=account_number,
            account_name=account_name,
            qbo_account_id=qbo_account_id,
        ),
        confidence_score=Decimal("1.000"),
        explanation=("Matched an exact pattern learned from an approved correction."),
        source=source,
        review_required=False,
    )


def create_pattern(
    **overrides: object,
) -> LearnedClassificationPattern:
    values: dict[str, object] = {
        "key": ClassificationPatternKey(
            description_normalized="brightfix fuel stop",
            bank_account="operating checking",
            direction=TransactionDirection.OUTFLOW,
            currency="USD",
        ),
        "decision": create_pattern_decision(),
        "source_transaction_id": uuid4(),
        "source_classification_version": 2,
        "source_correction": create_source_correction(),
        "source_review_status": ReviewStatus.APPROVED,
        "approved_by": create_reviewer(),
        "learned_at": APPROVED_AT + timedelta(seconds=1),
    }
    values.update(overrides)

    return LearnedClassificationPattern.model_validate(values)


def test_pattern_key_normalizes_exact_match_fields() -> None:
    key = ClassificationPatternKey(
        description_normalized="  BrightFix FUEL Stop ",
        bank_account=" Operating Checking ",
        direction=TransactionDirection.OUTFLOW,
        currency=" usd ",
    )

    assert key.description_normalized == "brightfix fuel stop"
    assert key.bank_account == "operating checking"
    assert key.currency == "USD"


def test_valid_pattern_preserves_auditable_provenance() -> None:
    pattern = create_pattern()

    assert pattern.active is True
    assert pattern.source_classification_version == 2
    assert pattern.source_correction.to_version == 2
    assert pattern.source_review_status is ReviewStatus.APPROVED
    assert pattern.decision.source is ClassificationSource.STORED_CORRECTION
    assert pattern.decision.qbo_account.account_number == "6020"


def test_pattern_requires_approved_source_review() -> None:
    with pytest.raises(
        ValidationError,
        match="approved source classification",
    ):
        create_pattern(
            source_review_status=ReviewStatus.REJECTED,
        )


def test_pattern_requires_manual_review_correction() -> None:
    with pytest.raises(
        ValidationError,
        match="manual-review correction",
    ):
        create_pattern(
            source_correction=create_source_correction(
                corrected_source=ClassificationSource.DETERMINISTIC_RULE
            )
        )


def test_pattern_decision_uses_stored_correction_source() -> None:
    with pytest.raises(
        ValidationError,
        match="stored_correction source",
    ):
        create_pattern(decision=create_pattern_decision(source=ClassificationSource.MANUAL_REVIEW))


def test_source_version_matches_correction_version() -> None:
    with pytest.raises(
        ValidationError,
        match="correction target version",
    ):
        create_pattern(source_classification_version=3)


def test_pattern_account_matches_approved_correction() -> None:
    with pytest.raises(
        ValidationError,
        match="pattern account must match",
    ):
        create_pattern(
            decision=create_pattern_decision(
                account_number="6030",
                account_name="Software & Subscriptions",
            )
        )


def test_learned_at_must_be_timezone_aware() -> None:
    with pytest.raises(
        ValidationError,
        match="learned_at must be timezone-aware",
    ):
        create_pattern(
            learned_at=datetime(2026, 7, 25, 18, 1),
        )


def test_pattern_cannot_be_learned_before_approval() -> None:
    with pytest.raises(
        ValidationError,
        match="earlier than the approval timestamp",
    ):
        create_pattern(
            learned_at=APPROVED_AT - timedelta(seconds=1),
        )


def test_pattern_excludes_environment_specific_qbo_id() -> None:
    with pytest.raises(
        ValidationError,
        match="environment-specific QuickBooks account IDs",
    ):
        create_pattern(decision=create_pattern_decision(qbo_account_id="sandbox-account-123"))
