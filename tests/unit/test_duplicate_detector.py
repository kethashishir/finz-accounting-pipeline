"""Tests for deterministic duplicate detection."""

import hashlib
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from app.models.ingestion import (
    IssueSeverity,
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
    ValidationIssue,
)
from app.services.ingestion.duplicate_detector import DuplicateDetector


def fingerprint(identity: str) -> str:
    """Return a deterministic test fingerprint."""

    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def valid_transaction(
    *,
    upload_id: UUID,
    source_transaction_id: str | None,
    identity: str,
) -> NormalizedTransaction:
    """Create a valid normalized transaction fixture."""

    amount = Decimal("-100.00")
    return NormalizedTransaction(
        upload_id=upload_id,
        raw_record_id=uuid4(),
        source_transaction_id=source_transaction_id,
        transaction_date=date(2026, 4, 1),
        posted_date=date(2026, 4, 2),
        description_original=identity,
        description_normalized=identity.casefold(),
        amount=amount,
        currency="USD",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint=fingerprint(identity),
        status=RecordStatus.VALID,
    )


def test_detects_duplicate_within_one_upload() -> None:
    """The later occurrence points to the first occurrence."""

    upload_id = uuid4()
    first = valid_transaction(
        upload_id=upload_id,
        source_transaction_id="BF-1",
        identity="fuel payment",
    )
    second = valid_transaction(
        upload_id=upload_id,
        source_transaction_id="BF-1",
        identity="fuel payment",
    )

    result = DuplicateDetector().detect([first, second])

    assert result.transactions[0].status == RecordStatus.VALID
    assert result.transactions[1].status == RecordStatus.DUPLICATE
    assert result.transactions[1].duplicate_of == first.id
    assert result.within_upload_count == 1
    assert result.cross_upload_count == 0


def test_detects_duplicate_across_uploads() -> None:
    """A fingerprint can match a prior canonical transaction."""

    existing = valid_transaction(
        upload_id=uuid4(),
        source_transaction_id="OLD-ID",
        identity="monthly insurance",
    )
    incoming = valid_transaction(
        upload_id=uuid4(),
        source_transaction_id="NEW-ID",
        identity="monthly insurance",
    )

    result = DuplicateDetector().detect(
        [incoming],
        existing_transactions=[existing],
    )

    detected = result.transactions[0]
    assert detected.status == RecordStatus.DUPLICATE
    assert detected.duplicate_of == existing.id
    assert result.cross_upload_count == 1
    assert result.within_upload_count == 0


def test_conflicting_reused_source_id_is_invalid() -> None:
    """A reused bank ID with different content requires review."""

    upload_id = uuid4()
    first = valid_transaction(
        upload_id=upload_id,
        source_transaction_id="BF-1",
        identity="fuel payment",
    )
    conflicting = valid_transaction(
        upload_id=upload_id,
        source_transaction_id="BF-1",
        identity="equipment payment",
    )

    result = DuplicateDetector().detect([first, conflicting])

    detected = result.transactions[1]
    assert detected.status == RecordStatus.INVALID
    assert detected.duplicate_of is None
    assert result.conflict_count == 1
    assert any(
        issue.code == "source_transaction_id_conflict" for issue in detected.validation_issues
    )


def test_invalid_transaction_is_not_canonical() -> None:
    """Unsafe records cannot cause valid transactions to be discarded."""

    upload_id = uuid4()
    invalid_values = valid_transaction(
        upload_id=upload_id,
        source_transaction_id="BF-1",
        identity="fuel payment",
    ).model_dump()
    invalid_values.update(
        status=RecordStatus.INVALID,
        validation_issues=[
            ValidationIssue(
                code="manual_review",
                field="_record",
                message="Test record requires review",
                severity=IssueSeverity.ERROR,
            )
        ],
    )
    invalid = NormalizedTransaction.model_validate(invalid_values)
    valid = valid_transaction(
        upload_id=upload_id,
        source_transaction_id="BF-1",
        identity="fuel payment",
    )

    result = DuplicateDetector().detect([invalid, valid])

    assert result.transactions[0].status == RecordStatus.INVALID
    assert result.transactions[1].status == RecordStatus.VALID
    assert result.within_upload_count == 0


def test_duplicate_detection_is_idempotent() -> None:
    """Running detection again does not create duplicate chains."""

    upload_id = uuid4()
    first = valid_transaction(
        upload_id=upload_id,
        source_transaction_id="BF-1",
        identity="fuel payment",
    )
    second = valid_transaction(
        upload_id=upload_id,
        source_transaction_id="BF-1",
        identity="fuel payment",
    )
    detector = DuplicateDetector()

    first_result = detector.detect([first, second])
    second_result = detector.detect(first_result.transactions)

    assert second_result.transactions == first_result.transactions
    assert second_result.within_upload_count == 0
    assert second_result.cross_upload_count == 0
    assert second_result.conflict_count == 0


def test_same_persisted_uuid_does_not_self_duplicate() -> None:
    """Retrying the same record UUID keeps it canonical."""

    transaction = valid_transaction(
        upload_id=uuid4(),
        source_transaction_id="BF-1",
        identity="fuel payment",
    )

    result = DuplicateDetector().detect(
        [transaction],
        existing_transactions=[transaction],
    )

    assert result.transactions[0].status == RecordStatus.VALID
    assert result.transactions[0].duplicate_of is None
    assert result.within_upload_count == 0
    assert result.cross_upload_count == 0
