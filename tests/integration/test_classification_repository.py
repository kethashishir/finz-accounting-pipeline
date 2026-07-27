"""Integration tests for safe classification persistence."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.core.config import get_settings
from app.db.client import MongoDatabase
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
    FileType,
    NormalizedTransaction,
    RawRecord,
    RecordStatus,
    TransactionDirection,
    UploadBatch,
)
from app.repositories.classification import (
    ClassificationPersistenceConflictError,
    ClassificationRepository,
    ClassificationReviewConflictError,
    ClassificationTransactionNotFoundError,
    InvalidClassificationTransitionError,
    StaleClassificationVersionError,
    UnsafeClassificationTransactionError,
)
from app.repositories.ingestion import IngestionRepository


@pytest.fixture
async def repositories() -> AsyncIterator[tuple[ClassificationRepository, IngestionRepository]]:
    """Create isolated classification and ingestion repositories."""

    settings = get_settings()
    database_name = f"{settings.mongodb_database[:32]}_cls_{uuid4().hex[:16]}"
    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=database_name,
    )
    ingestion_repository = IngestionRepository(mongodb.database)
    classification_repository = ClassificationRepository(mongodb.database)

    await ingestion_repository.ensure_indexes()
    await classification_repository.ensure_indexes()

    try:
        yield classification_repository, ingestion_repository
    finally:
        await mongodb.client.drop_database(database_name)
        await mongodb.close()


async def persist_valid_transaction(
    repository: IngestionRepository,
) -> NormalizedTransaction:
    """Persist one valid canonical transaction."""

    upload = UploadBatch(
        source_file_name="bank.csv",
        file_type=FileType.CSV,
        file_sha256="a" * 64,
        physical_record_count=1,
    )
    raw_record = RawRecord(
        upload_id=upload.id,
        source_file_name=upload.source_file_name,
        source_row_number=2,
        raw_values={
            "Date": "2026-04-01",
            "Description": "BrightFix Fuel Stop",
            "Amount": "-100.00",
        },
        raw_hash="b" * 64,
    )
    transaction = NormalizedTransaction(
        upload_id=upload.id,
        raw_record_id=raw_record.id,
        source_transaction_id="BF-202604-0001",
        transaction_date=date(2026, 4, 1),
        description_original="BrightFix Fuel Stop",
        description_normalized="brightfix fuel stop",
        amount=Decimal("-100.00"),
        currency="USD",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint="c" * 64,
        status=RecordStatus.VALID,
    )

    await repository.save_batch(
        upload=upload,
        raw_records=[raw_record],
        transactions=[transaction],
    )

    return transaction


def create_classification(
    normalized_transaction_id: UUID,
    *,
    account_number: str = "6020",
    account_name: str = "Vehicle & Fuel",
) -> TransactionClassification:
    """Create an initial deterministic classification."""

    return TransactionClassification(
        normalized_transaction_id=normalized_transaction_id,
        decision=ClassificationDecision(
            transaction_type=TransactionType.OPERATING_EXPENSE,
            counterparty=Counterparty(
                raw_name="BrightFix Fuel Stop",
                normalized_name="BrightFix Fuel Stop",
            ),
            qbo_account=QuickBooksAccountMapping(
                account_number=account_number,
                account_name=account_name,
            ),
            confidence_score=Decimal("0.950"),
            explanation=("The normalized description matches a known fuel pattern."),
            source=ClassificationSource.DETERMINISTIC_RULE,
            review_required=False,
        ),
    )


REVIEWED_AT = datetime(
    2026,
    7,
    25,
    18,
    0,
    tzinfo=UTC,
)


def create_reviewer() -> ReviewerMetadata:
    """Create stable reviewer metadata for persistence tests."""

    return ReviewerMetadata(
        reviewer_id="shishir",
        reviewed_at=REVIEWED_AT,
        notes="Reviewed against the source bank transaction.",
    )


def create_corrected_classification(
    current: TransactionClassification,
    *,
    account_number: str,
    account_name: str,
) -> TransactionClassification:
    """Append one manual correction and reopen the review state."""

    reviewer = create_reviewer()
    corrected_decision = ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=current.decision.counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number=account_number,
            account_name=account_name,
        ),
        confidence_score=Decimal("1.000"),
        explanation="A reviewer confirmed the corrected expense account.",
        source=ClassificationSource.MANUAL_REVIEW,
        review_required=False,
    )
    correction = ClassificationCorrection(
        from_version=current.version,
        to_version=current.version + 1,
        previous_decision=current.decision,
        corrected_decision=corrected_decision,
        corrected_by=reviewer,
        reason="Correct the account using the reviewed bank description.",
    )

    return TransactionClassification(
        normalized_transaction_id=current.normalized_transaction_id,
        version=current.version + 1,
        decision=corrected_decision,
        review_status=ReviewStatus.PENDING,
        corrections=(*current.corrections, correction),
    )


def create_reviewed_classification(
    current: TransactionClassification,
    *,
    review_status: ReviewStatus,
) -> TransactionClassification:
    """Apply a final review without changing accounting history."""

    return TransactionClassification(
        normalized_transaction_id=current.normalized_transaction_id,
        version=current.version,
        decision=current.decision,
        review_status=review_status,
        reviewer=create_reviewer(),
        corrections=current.corrections,
    )


@pytest.mark.asyncio
async def test_index_creation_is_idempotent(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """Indexes can be safely ensured during every startup."""

    classification_repository, _ = repositories

    await classification_repository.ensure_indexes()
    await classification_repository.ensure_indexes()

    indexes = await classification_repository.classifications.index_information()

    assert "ix_classification_review_queue" in indexes
    assert "ix_classification_account_type" in indexes
    assert "ix_classification_source" in indexes


@pytest.mark.asyncio
async def test_initial_save_is_idempotent(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """An exact retry does not create another classification."""

    classification_repository, ingestion_repository = repositories
    transaction = await persist_valid_transaction(ingestion_repository)
    classification = create_classification(transaction.id)

    first = await classification_repository.save_initial(classification)
    second = await classification_repository.save_initial(classification)
    stored = await classification_repository.find_by_transaction_id(transaction.id)

    assert first is True
    assert second is False
    assert stored == classification
    assert await classification_repository.classifications.count_documents({}) == 1


@pytest.mark.asyncio
async def test_missing_transaction_is_rejected(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """A classification cannot refer to nonexistent source evidence."""

    classification_repository, _ = repositories
    classification = create_classification(uuid4())

    with pytest.raises(
        ClassificationTransactionNotFoundError,
        match="does not exist",
    ):
        await classification_repository.save_initial(classification)


@pytest.mark.asyncio
async def test_duplicate_transaction_is_rejected(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """A duplicate row cannot receive an independent classification."""

    classification_repository, ingestion_repository = repositories
    canonical = await persist_valid_transaction(ingestion_repository)

    duplicate_upload = UploadBatch(
        source_file_name="overlap.csv",
        file_type=FileType.CSV,
        file_sha256="d" * 64,
        physical_record_count=1,
    )
    duplicate_raw = RawRecord(
        upload_id=duplicate_upload.id,
        source_file_name=duplicate_upload.source_file_name,
        source_row_number=2,
        raw_values={
            "Date": "2026-04-01",
            "Description": "BrightFix Fuel Stop",
            "Amount": "-100.00",
        },
        raw_hash="e" * 64,
    )
    duplicate = NormalizedTransaction(
        upload_id=duplicate_upload.id,
        raw_record_id=duplicate_raw.id,
        source_transaction_id="BF-202604-0001",
        transaction_date=canonical.transaction_date,
        description_original=canonical.description_original,
        description_normalized=canonical.description_normalized,
        amount=canonical.amount,
        currency=canonical.currency,
        bank_account=canonical.bank_account,
        direction=canonical.direction,
        fingerprint=canonical.fingerprint,
        status=RecordStatus.DUPLICATE,
        duplicate_of=canonical.id,
    )

    await ingestion_repository.save_batch(
        upload=duplicate_upload,
        raw_records=[duplicate_raw],
        transactions=[duplicate],
    )

    with pytest.raises(
        UnsafeClassificationTransactionError,
        match="valid canonical",
    ):
        await classification_repository.save_initial(create_classification(duplicate.id))


@pytest.mark.asyncio
async def test_conflicting_initial_classification_is_rejected(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """The same transaction cannot silently acquire a second decision."""

    classification_repository, ingestion_repository = repositories
    transaction = await persist_valid_transaction(ingestion_repository)
    original = create_classification(transaction.id)
    conflicting = create_classification(
        transaction.id,
        account_number="6090",
        account_name="Office & General",
    )

    await classification_repository.save_initial(original)

    with pytest.raises(
        ClassificationPersistenceConflictError,
        match="different classification",
    ):
        await classification_repository.save_initial(conflicting)

    stored = await classification_repository.find_by_transaction_id(transaction.id)
    assert stored == original


@pytest.mark.asyncio
async def test_corrections_are_atomic_idempotent_and_preserve_history(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """Sequential corrections preserve every prior accounting decision."""

    classification_repository, ingestion_repository = repositories
    transaction = await persist_valid_transaction(ingestion_repository)
    initial = create_classification(transaction.id)

    await classification_repository.save_initial(initial)

    first_correction = create_corrected_classification(
        initial,
        account_number="6090",
        account_name="Office & General",
    )

    first_saved = await classification_repository.save_correction(
        first_correction,
        expected_version=1,
    )
    first_retry = await classification_repository.save_correction(
        first_correction,
        expected_version=1,
    )

    second_correction = create_corrected_classification(
        first_correction,
        account_number="6030",
        account_name="Software & Subscriptions",
    )

    second_saved = await classification_repository.save_correction(
        second_correction,
        expected_version=2,
    )

    stored = await classification_repository.find_by_transaction_id(transaction.id)

    assert first_saved is True
    assert first_retry is False
    assert second_saved is True
    assert stored == second_correction
    assert stored is not None
    assert stored.version == 3
    assert len(stored.corrections) == 2
    assert stored.corrections[0] == first_correction.corrections[0]
    assert stored.review_status is ReviewStatus.PENDING


@pytest.mark.asyncio
async def test_stale_correction_version_is_rejected(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """An outdated reviewer cannot overwrite a newer classification."""

    classification_repository, ingestion_repository = repositories
    transaction = await persist_valid_transaction(ingestion_repository)
    initial = create_classification(transaction.id)

    await classification_repository.save_initial(initial)

    winning_correction = create_corrected_classification(
        initial,
        account_number="6090",
        account_name="Office & General",
    )
    stale_correction = create_corrected_classification(
        initial,
        account_number="6030",
        account_name="Software & Subscriptions",
    )

    await classification_repository.save_correction(
        winning_correction,
        expected_version=1,
    )

    with pytest.raises(
        StaleClassificationVersionError,
        match="stored version is 2",
    ):
        await classification_repository.save_correction(
            stale_correction,
            expected_version=1,
        )

    stored = await classification_repository.find_by_transaction_id(transaction.id)
    assert stored == winning_correction


@pytest.mark.asyncio
async def test_forged_correction_history_is_rejected(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """A client cannot replace the stored decision with invented history."""

    classification_repository, ingestion_repository = repositories
    transaction = await persist_valid_transaction(ingestion_repository)
    stored_initial = create_classification(transaction.id)

    await classification_repository.save_initial(stored_initial)

    invented_initial = create_classification(
        transaction.id,
        account_number="6090",
        account_name="Office & General",
    )
    forged_update = create_corrected_classification(
        invented_initial,
        account_number="6030",
        account_name="Software & Subscriptions",
    )

    with pytest.raises(
        InvalidClassificationTransitionError,
        match="stored decision",
    ):
        await classification_repository.save_correction(
            forged_update,
            expected_version=1,
        )

    stored = await classification_repository.find_by_transaction_id(transaction.id)
    assert stored == stored_initial


@pytest.mark.asyncio
async def test_approval_is_persisted_idempotently(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """Approval records reviewer metadata without changing the decision."""

    classification_repository, ingestion_repository = repositories
    transaction = await persist_valid_transaction(ingestion_repository)
    initial = create_classification(transaction.id)
    approved = create_reviewed_classification(
        initial,
        review_status=ReviewStatus.APPROVED,
    )

    await classification_repository.save_initial(initial)

    first = await classification_repository.save_review(
        approved,
        expected_version=1,
    )
    retry = await classification_repository.save_review(
        approved,
        expected_version=1,
    )
    stored = await classification_repository.find_by_transaction_id(transaction.id)

    assert first is True
    assert retry is False
    assert stored == approved
    assert stored is not None
    assert stored.version == 1
    assert stored.decision == initial.decision


@pytest.mark.asyncio
async def test_rejection_is_persisted_idempotently(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """Rejection preserves the accounting decision for auditability."""

    classification_repository, ingestion_repository = repositories
    transaction = await persist_valid_transaction(ingestion_repository)
    initial = create_classification(transaction.id)
    rejected = create_reviewed_classification(
        initial,
        review_status=ReviewStatus.REJECTED,
    )

    await classification_repository.save_initial(initial)

    first = await classification_repository.save_review(
        rejected,
        expected_version=1,
    )
    retry = await classification_repository.save_review(
        rejected,
        expected_version=1,
    )
    stored = await classification_repository.find_by_transaction_id(transaction.id)

    assert first is True
    assert retry is False
    assert stored == rejected
    assert stored is not None
    assert stored.decision == initial.decision


@pytest.mark.asyncio
async def test_competing_review_outcome_is_rejected(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """The first final review wins over a competing browser session."""

    classification_repository, ingestion_repository = repositories
    transaction = await persist_valid_transaction(ingestion_repository)
    initial = create_classification(transaction.id)
    approved = create_reviewed_classification(
        initial,
        review_status=ReviewStatus.APPROVED,
    )
    rejected = create_reviewed_classification(
        initial,
        review_status=ReviewStatus.REJECTED,
    )

    await classification_repository.save_initial(initial)
    await classification_repository.save_review(
        approved,
        expected_version=1,
    )

    with pytest.raises(
        ClassificationReviewConflictError,
        match="pending classification",
    ):
        await classification_repository.save_review(
            rejected,
            expected_version=1,
        )

    stored = await classification_repository.find_by_transaction_id(transaction.id)
    assert stored == approved


@pytest.mark.asyncio
async def test_bulk_find_returns_only_existing_classifications(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """Batch lookup returns requested stored classifications by UUID."""

    classification_repository, ingestion_repository = repositories
    transaction = await persist_valid_transaction(ingestion_repository)
    classification = create_classification(transaction.id)

    await classification_repository.save_initial(classification)

    missing_transaction_id = uuid4()

    found = await classification_repository.find_by_transaction_ids(
        (
            transaction.id,
            missing_transaction_id,
            transaction.id,
        )
    )

    assert found == {
        transaction.id: classification,
    }
    assert missing_transaction_id not in found


@pytest.mark.asyncio
async def test_bulk_find_with_no_ids_returns_empty_mapping(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """An empty batch avoids an unnecessary MongoDB query."""

    classification_repository, _ = repositories

    found = await classification_repository.find_by_transaction_ids(())

    assert found == {}
