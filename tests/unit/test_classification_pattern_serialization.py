"""Tests for learned classification pattern BSON serialization."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from bson import BSON
from bson.codec_options import CodecOptions, UuidRepresentation
from bson.decimal128 import Decimal128
from pydantic import ValidationError

from app.db.serialization import (
    learned_pattern_from_document,
    learned_pattern_to_document,
)
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

CODEC_OPTIONS = CodecOptions(
    tz_aware=True,
    uuid_representation=UuidRepresentation.STANDARD,
)

REVIEWED_AT = datetime(
    2026,
    7,
    25,
    18,
    0,
    tzinfo=UTC,
)


def assert_bson_encodable(document: dict[str, object]) -> None:
    """Prove that MongoDB's BSON encoder accepts the document."""

    BSON.encode(document, codec_options=CODEC_OPTIONS)


def create_pattern() -> LearnedClassificationPattern:
    """Create one approved, reusable accounting correction pattern."""

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
        explanation="The original automated decision was uncertain.",
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
        explanation="A reviewer confirmed the payment was vehicle fuel.",
        source=ClassificationSource.MANUAL_REVIEW,
        review_required=False,
    )
    correction = ClassificationCorrection(
        from_version=1,
        to_version=2,
        previous_decision=previous_decision,
        corrected_decision=corrected_decision,
        corrected_by=reviewer,
        reason="Correct the expense account using the bank description.",
    )
    reusable_decision = ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number="6020",
            account_name="Vehicle & Fuel",
        ),
        confidence_score=Decimal("1.000"),
        explanation=("Matched an exact pattern learned from an approved correction."),
        source=ClassificationSource.STORED_CORRECTION,
        review_required=False,
    )

    return LearnedClassificationPattern(
        key=ClassificationPatternKey(
            description_normalized="BrightFix Fuel Stop",
            bank_account="Operating Checking",
            direction=TransactionDirection.OUTFLOW,
            currency="usd",
        ),
        decision=reusable_decision,
        source_transaction_id=uuid4(),
        source_classification_version=2,
        source_correction=correction,
        source_review_status=ReviewStatus.APPROVED,
        approved_by=reviewer,
        learned_at=REVIEWED_AT + timedelta(seconds=1),
    )


def test_learned_pattern_round_trip_preserves_nested_accounting_values() -> None:
    """Pattern identity, decimals, provenance, and timestamps survive BSON."""

    pattern = create_pattern()

    document = learned_pattern_to_document(pattern)

    assert document["_id"] == pattern.id
    assert "id" not in document
    assert document["decision"]["confidence_score"] == Decimal128("1.000")
    assert document["source_correction"]["previous_decision"]["confidence_score"] == Decimal128(
        "0.700"
    )
    assert document["source_transaction_id"] == pattern.source_transaction_id
    assert document["key"]["description_normalized"] == ("brightfix fuel stop")
    assert document["key"]["bank_account"] == "operating checking"
    assert document["key"]["currency"] == "USD"

    assert_bson_encodable(document)
    assert learned_pattern_from_document(document) == pattern


def test_stored_pattern_is_revalidated_when_reconstructed() -> None:
    """Corrupted persisted provenance cannot bypass model validation."""

    pattern = create_pattern()
    document = learned_pattern_to_document(pattern)
    document["source_review_status"] = ReviewStatus.REJECTED.value

    with pytest.raises(
        ValidationError,
        match="approved source classification",
    ):
        learned_pattern_from_document(document)
