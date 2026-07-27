"""Integration tests for idempotent ingestion persistence."""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.db.client import MongoDatabase
from app.models.ingestion import (
    FileType,
    NormalizedTransaction,
    RawRecord,
    RecordStatus,
    TransactionDirection,
    UploadBatch,
)
from app.repositories.ingestion import (
    DuplicateFileUploadError,
    IngestionRepository,
    PersistenceConflictError,
)


@pytest.fixture
async def repository() -> AsyncIterator[IngestionRepository]:
    """Create an isolated temporary MongoDB database."""

    settings = get_settings()
    database_name = f"{settings.mongodb_database}_test_{uuid4().hex}"
    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=database_name,
    )
    ingestion_repository = IngestionRepository(mongodb.database)

    await ingestion_repository.ensure_indexes()

    try:
        yield ingestion_repository
    finally:
        await mongodb.client.drop_database(database_name)
        await mongodb.close()


def create_batch() -> tuple[
    UploadBatch,
    RawRecord,
    NormalizedTransaction,
]:
    """Create a linked ingestion batch fixture."""

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
            "Description": "Fuel payment",
            "Amount": "-100.00",
        },
        raw_hash="b" * 64,
    )
    transaction = NormalizedTransaction(
        upload_id=upload.id,
        raw_record_id=raw_record.id,
        source_transaction_id="BF-202604-0001",
        transaction_date=date(2026, 4, 1),
        description_original="Fuel payment",
        description_normalized="fuel payment",
        amount=Decimal("-100.00"),
        currency="USD",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint="c" * 64,
        status=RecordStatus.VALID,
    )

    return upload, raw_record, transaction


@pytest.mark.asyncio
async def test_index_creation_is_idempotent(
    repository: IngestionRepository,
) -> None:
    """Index setup can run during every application startup."""

    await repository.ensure_indexes()
    await repository.ensure_indexes()

    upload_indexes = await repository.uploads.index_information()
    transaction_indexes = await repository.transactions.index_information()

    assert "uq_upload_file_sha256" in upload_indexes
    assert "ix_transaction_source_identity" in transaction_indexes
    assert "ix_transaction_fingerprint" in transaction_indexes
    assert "ix_transaction_reporting_period" in transaction_indexes


@pytest.mark.asyncio
async def test_batch_save_is_idempotent(
    repository: IngestionRepository,
) -> None:
    """Retrying the same batch does not create additional documents."""

    upload, raw_record, transaction = create_batch()

    first = await repository.save_batch(
        upload=upload,
        raw_records=[raw_record],
        transactions=[transaction],
    )
    second = await repository.save_batch(
        upload=upload,
        raw_records=[raw_record],
        transactions=[transaction],
    )

    assert first.upload_inserted is True
    assert first.raw_inserted == 1
    assert first.transactions_inserted == 1
    assert second.upload_inserted is False
    assert second.raw_inserted == 0
    assert second.raw_existing == 1
    assert second.transactions_inserted == 0
    assert second.transactions_existing == 1

    stored = await repository.transactions_for_upload(upload.id)
    assert stored == (transaction,)


@pytest.mark.asyncio
async def test_identical_file_hash_is_rejected(
    repository: IngestionRepository,
) -> None:
    """The same complete source file cannot create another upload."""

    first, _, _ = create_batch()
    second = UploadBatch(
        source_file_name="renamed-bank.csv",
        file_type=FileType.CSV,
        file_sha256=first.file_sha256,
        physical_record_count=1,
    )

    await repository.save_upload(first)

    with pytest.raises(DuplicateFileUploadError) as error:
        await repository.save_upload(second)

    assert error.value.existing_upload_id == first.id


@pytest.mark.asyncio
async def test_conflicting_raw_record_uuid_is_rejected(
    repository: IngestionRepository,
) -> None:
    """A retry cannot silently replace immutable source evidence."""

    _, raw_record, _ = create_batch()
    conflicting = RawRecord(
        id=raw_record.id,
        upload_id=raw_record.upload_id,
        source_file_name=raw_record.source_file_name,
        source_row_number=raw_record.source_row_number,
        raw_values={"Amount": "999.99"},
        raw_hash="d" * 64,
    )

    await repository.save_raw_records([raw_record])

    with pytest.raises(PersistenceConflictError):
        await repository.save_raw_records([conflicting])


@pytest.mark.asyncio
async def test_finds_prior_duplicate_candidates(
    repository: IngestionRepository,
) -> None:
    """Incoming rows can find canonical records from older uploads."""

    upload, raw_record, existing = create_batch()
    await repository.save_batch(
        upload=upload,
        raw_records=[raw_record],
        transactions=[existing],
    )

    incoming = NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="DIFFERENT-ID",
        transaction_date=existing.transaction_date,
        description_original=existing.description_original,
        description_normalized=existing.description_normalized,
        amount=existing.amount,
        currency=existing.currency,
        bank_account=existing.bank_account,
        direction=existing.direction,
        fingerprint=existing.fingerprint,
        status=RecordStatus.VALID,
    )

    matches = await repository.find_existing_transactions([incoming])

    assert matches == (existing,)


@pytest.mark.asyncio
async def test_find_transaction_by_id_returns_stored_transaction(
    repository: IngestionRepository,
) -> None:
    """A stored normalized transaction can be loaded by UUID."""

    upload, raw_record, transaction = create_batch()

    await repository.save_batch(
        upload=upload,
        raw_records=[raw_record],
        transactions=[transaction],
    )

    found = await repository.find_transaction_by_id(transaction.id)

    assert found == transaction


@pytest.mark.asyncio
async def test_find_transaction_by_id_returns_none_when_missing(
    repository: IngestionRepository,
) -> None:
    """A missing transaction lookup returns no fabricated evidence."""

    found = await repository.find_transaction_by_id(uuid4())

    assert found is None
