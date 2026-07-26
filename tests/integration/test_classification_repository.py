"""Integration tests for safe classification persistence."""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.core.config import get_settings
from app.db.client import MongoDatabase
from app.models.classification import (
    ClassificationDecision,
    ClassificationSource,
    Counterparty,
    QuickBooksAccountMapping,
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
    ClassificationTransactionNotFoundError,
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
