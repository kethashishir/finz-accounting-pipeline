from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

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
    TransactionClassification,
    TransactionType,
)

NORMALIZED_TRANSACTION_ID = UUID("00000000-0000-0000-0000-000000000001")


def _decision() -> ClassificationDecision:
    return ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=Counterparty(
            raw_name="  BrightFix Payroll Provider  ",
            normalized_name="BrightFix Payroll Provider",
        ),
        qbo_account=QuickBooksAccountMapping(
            account_number="6000",
            account_name="Payroll Expense",
        ),
        confidence_score=Decimal("0.950"),
        explanation="The description contains the known payroll provider pattern.",
        source=ClassificationSource.DETERMINISTIC_RULE,
        review_required=False,
    )


def _corrected_decision() -> ClassificationDecision:
    return ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=Counterparty(
            raw_name="BrightFix Fuel Stop",
            normalized_name="BrightFix Fuel Stop",
        ),
        qbo_account=QuickBooksAccountMapping(
            account_number="6020",
            account_name="Vehicle & Fuel",
        ),
        confidence_score=Decimal("1.000"),
        explanation="A reviewer confirmed this payment was vehicle fuel.",
        source=ClassificationSource.MANUAL_REVIEW,
        review_required=False,
    )


def _reviewer() -> ReviewerMetadata:
    return ReviewerMetadata(
        reviewer_id="shishir",
        reviewed_at=datetime(2026, 7, 25, 18, 0, tzinfo=UTC),
        notes="Reviewed against the bank description.",
    )


def test_transaction_type_values_cover_required_accounting_categories() -> None:
    assert {member.value for member in TransactionType} == {
        "revenue",
        "cost_of_goods_sold",
        "operating_expense",
        "refund",
        "transfer",
        "owner_contribution",
        "owner_distribution",
        "fixed_asset_purchase",
    }


def test_classification_requires_normalized_transaction_uuid() -> None:
    with pytest.raises(ValidationError):
        TransactionClassification(
            normalized_transaction_id="not-a-valid-uuid",
            decision=_decision(),
        )


def test_decision_uses_decimal_confidence_and_preserves_mapping() -> None:
    decision = _decision()

    assert isinstance(decision.confidence_score, Decimal)
    assert decision.confidence_score == Decimal("0.950")
    assert decision.counterparty is not None
    assert decision.counterparty.raw_name == "BrightFix Payroll Provider"
    assert decision.qbo_account.account_number == "6000"


@pytest.mark.parametrize(
    "confidence",
    [
        Decimal("-0.001"),
        Decimal("1.001"),
    ],
)
def test_confidence_must_be_between_zero_and_one(
    confidence: Decimal,
) -> None:
    with pytest.raises(ValidationError):
        ClassificationDecision(
            transaction_type=TransactionType.REVENUE,
            qbo_account=QuickBooksAccountMapping(
                account_number="4000",
                account_name="Repair Service Revenue",
            ),
            confidence_score=confidence,
            explanation="Test classification.",
            source=ClassificationSource.DETERMINISTIC_RULE,
            review_required=False,
        )


def test_pending_classification_cannot_include_reviewer() -> None:
    with pytest.raises(
        ValidationError,
        match="pending classifications cannot include reviewer metadata",
    ):
        TransactionClassification(
            normalized_transaction_id=NORMALIZED_TRANSACTION_ID,
            decision=_decision(),
            review_status=ReviewStatus.PENDING,
            reviewer=_reviewer(),
        )


@pytest.mark.parametrize(
    "review_status",
    [
        ReviewStatus.APPROVED,
        ReviewStatus.REJECTED,
    ],
)
def test_final_review_status_requires_reviewer(
    review_status: ReviewStatus,
) -> None:
    with pytest.raises(
        ValidationError,
        match="approved or rejected classifications require reviewer metadata",
    ):
        TransactionClassification(
            normalized_transaction_id=NORMALIZED_TRANSACTION_ID,
            decision=_decision(),
            review_status=review_status,
        )


def test_reviewer_timestamp_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        ReviewerMetadata(
            reviewer_id="shishir",
            reviewed_at=datetime(2026, 7, 25, 18, 0),
        )


def test_valid_correction_chain_updates_current_version() -> None:
    previous_decision = _decision()
    corrected_decision = _corrected_decision()
    reviewer = _reviewer()

    correction = ClassificationCorrection(
        from_version=1,
        to_version=2,
        previous_decision=previous_decision,
        corrected_decision=corrected_decision,
        corrected_by=reviewer,
        reason="The original payroll classification was incorrect.",
    )

    classification = TransactionClassification(
        normalized_transaction_id=NORMALIZED_TRANSACTION_ID,
        version=2,
        decision=corrected_decision,
        review_status=ReviewStatus.APPROVED,
        reviewer=reviewer,
        corrections=(correction,),
    )

    assert classification.version == 2
    assert classification.decision == corrected_decision
    assert classification.corrections[0].previous_decision == previous_decision


def test_correction_history_must_begin_at_version_one() -> None:
    reviewer = _reviewer()
    corrected_decision = _corrected_decision()

    correction = ClassificationCorrection(
        from_version=2,
        to_version=3,
        previous_decision=_decision(),
        corrected_decision=corrected_decision,
        corrected_by=reviewer,
        reason="Invalid test history.",
    )

    with pytest.raises(
        ValidationError,
        match="correction history must be contiguous",
    ):
        TransactionClassification(
            normalized_transaction_id=NORMALIZED_TRANSACTION_ID,
            version=3,
            decision=corrected_decision,
            review_status=ReviewStatus.APPROVED,
            reviewer=reviewer,
            corrections=(correction,),
        )


def test_current_decision_must_match_latest_correction() -> None:
    previous_decision = _decision()
    corrected_decision = _corrected_decision()
    reviewer = _reviewer()

    correction = ClassificationCorrection(
        from_version=1,
        to_version=2,
        previous_decision=previous_decision,
        corrected_decision=corrected_decision,
        corrected_by=reviewer,
        reason="Correct the expense account.",
    )

    with pytest.raises(
        ValidationError,
        match="current classification decision must match",
    ):
        TransactionClassification(
            normalized_transaction_id=NORMALIZED_TRANSACTION_ID,
            version=2,
            decision=previous_decision,
            review_status=ReviewStatus.APPROVED,
            reviewer=reviewer,
            corrections=(correction,),
        )
