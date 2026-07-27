"""Integration tests for repository-backed batch classification."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.core.config import get_settings
from app.db.client import MongoDatabase
from app.models.classification_pattern import ClassificationPatternKey
from app.models.ingestion import (
    FileType,
    IssueSeverity,
    NormalizedTransaction,
    RawRecord,
    RecordStatus,
    TransactionDirection,
    UploadBatch,
    ValidationIssue,
)
from app.repositories.classification import ClassificationRepository
from app.repositories.ingestion import IngestionRepository
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)
from app.services.classification.batch_classification import (
    classify_upload,
)
from app.services.classification.rule_config import (
    load_deterministic_rule_set,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
RULES_PATH = Path("sample_config/classification_rules.json")


class MissingPatternLookup:
    """Represent a learned-pattern miss for integration tests."""

    async def find_active(
        self,
        key: ClassificationPatternKey,
    ):
        return None


@pytest.fixture
async def repositories() -> AsyncIterator[
    tuple[
        ClassificationRepository,
        IngestionRepository,
    ]
]:
    """Create isolated real MongoDB repositories."""

    settings = get_settings()
    database_name = f"{settings.mongodb_database[:28]}_batch_{uuid4().hex[:16]}"
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


def create_raw_record(
    *,
    upload: UploadBatch,
    row_number: int,
    description: str,
    amount: str,
) -> RawRecord:
    """Create immutable source evidence for one normalized record."""

    return RawRecord(
        upload_id=upload.id,
        source_file_name=upload.source_file_name,
        source_row_number=row_number,
        raw_values={
            "Date": f"2026-06-{row_number - 1:02d}",
            "Description": description,
            "Amount": amount,
        },
        raw_hash=f"{row_number:064x}",
    )


def create_transaction(
    *,
    upload_id: UUID,
    raw_record_id: UUID,
    index: int,
    description: str,
    status: RecordStatus = RecordStatus.VALID,
    duplicate_of: UUID | None = None,
) -> NormalizedTransaction:
    """Create one internally valid normalized transaction."""

    validation_issues = (
        (
            ValidationIssue(
                code="integration_invalid_record",
                field="_record",
                message=("This integration fixture is intentionally invalid."),
                severity=IssueSeverity.ERROR,
            ),
        )
        if status is RecordStatus.INVALID
        else ()
    )

    return NormalizedTransaction(
        upload_id=upload_id,
        raw_record_id=raw_record_id,
        source_transaction_id=f"BF-BATCH-INT-{index:04d}",
        transaction_date=date(2026, 6, index),
        description_original=description,
        description_normalized=description.casefold(),
        amount=Decimal("-35.00"),
        currency="USD",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint=f"{index:064x}",
        status=status,
        duplicate_of=duplicate_of,
        validation_issues=validation_issues,
    )


def create_mixed_batch() -> tuple[
    UploadBatch,
    tuple[RawRecord, ...],
    tuple[NormalizedTransaction, ...],
]:
    """Create canonical, duplicate, and invalid records in one upload."""

    upload = UploadBatch(
        source_file_name="batch-classification.csv",
        file_type=FileType.CSV,
        file_sha256="a" * 64,
        physical_record_count=4,
    )

    raw_records = tuple(
        create_raw_record(
            upload=upload,
            row_number=index + 1,
            description=("MONTHLY SERVICE FEE" if index <= 2 else f"NONCANONICAL RECORD {index}"),
            amount="-35.00",
        )
        for index in range(1, 5)
    )

    first = create_transaction(
        upload_id=upload.id,
        raw_record_id=raw_records[0].id,
        index=1,
        description="MONTHLY SERVICE FEE",
    )
    second = create_transaction(
        upload_id=upload.id,
        raw_record_id=raw_records[1].id,
        index=2,
        description="MONTHLY SERVICE FEE",
    )
    duplicate = create_transaction(
        upload_id=upload.id,
        raw_record_id=raw_records[2].id,
        index=3,
        description="MONTHLY SERVICE FEE",
        status=RecordStatus.DUPLICATE,
        duplicate_of=first.id,
    )
    invalid = create_transaction(
        upload_id=upload.id,
        raw_record_id=raw_records[3].id,
        index=4,
        description="INVALID SOURCE RECORD",
        status=RecordStatus.INVALID,
    )

    return (
        upload,
        raw_records,
        (
            first,
            second,
            duplicate,
            invalid,
        ),
    )


def create_unmatched_batch() -> tuple[
    UploadBatch,
    RawRecord,
    NormalizedTransaction,
]:
    """Create one valid transaction with no trusted rule match."""

    upload = UploadBatch(
        source_file_name="unmatched-classification.csv",
        file_type=FileType.CSV,
        file_sha256="b" * 64,
        physical_record_count=1,
    )
    raw_record = create_raw_record(
        upload=upload,
        row_number=2,
        description="ZXQ UNMAPPED 918273",
        amount="-123.45",
    )
    transaction = NormalizedTransaction(
        upload_id=upload.id,
        raw_record_id=raw_record.id,
        source_transaction_id="BF-BATCH-UNMATCHED-0001",
        transaction_date=date(2026, 6, 20),
        description_original="ZXQ UNMAPPED 918273",
        description_normalized="zxq unmapped 918273",
        amount=Decimal("-123.45"),
        currency="USD",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint="f" * 64,
        status=RecordStatus.VALID,
        duplicate_of=None,
    )

    return upload, raw_record, transaction


def supplied_classification_config():
    """Load the approved accounts and deterministic rules."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = load_deterministic_rule_set(
        RULES_PATH,
        chart_of_accounts=catalog,
    )
    return catalog, rule_set


@pytest.mark.asyncio
async def test_batch_rerun_is_idempotent_with_real_repositories(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """A rerun bulk-skips stored decisions without duplicate writes."""

    classification_repository, ingestion_repository = repositories
    upload, raw_records, transactions = create_mixed_batch()

    await ingestion_repository.save_batch(
        upload=upload,
        raw_records=raw_records,
        transactions=transactions,
    )

    catalog, rule_set = supplied_classification_config()

    first = await classify_upload(
        upload_id=upload.id,
        transaction_reader=ingestion_repository,
        classification_repository=classification_repository,
        pattern_lookup=MissingPatternLookup(),
        rule_set=rule_set,
        chart_of_accounts=catalog,
    )
    second = await classify_upload(
        upload_id=upload.id,
        transaction_reader=ingestion_repository,
        classification_repository=classification_repository,
        pattern_lookup=MissingPatternLookup(),
        rule_set=rule_set,
        chart_of_accounts=catalog,
    )

    assert first.total_records == 4
    assert first.canonical_transactions == 2
    assert first.ignored_noncanonical == 2
    assert first.classified_by_deterministic_rule == 2
    assert first.already_classified == 0
    assert first.manual_review_required == 0
    assert first.failed == 0

    assert second.total_records == 4
    assert second.canonical_transactions == 2
    assert second.ignored_noncanonical == 2
    assert second.classified_by_deterministic_rule == 0
    assert second.already_classified == 2
    assert second.manual_review_required == 0
    assert second.failed == 0

    stored = await classification_repository.find_by_transaction_ids(
        tuple(transaction.id for transaction in transactions)
    )

    assert set(stored) == {
        transactions[0].id,
        transactions[1].id,
    }
    assert await classification_repository.classifications.count_documents({}) == 2


@pytest.mark.asyncio
async def test_manual_review_fallback_remains_unpersisted(
    repositories: tuple[
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """An unresolved transaction remains outside accounting persistence."""

    classification_repository, ingestion_repository = repositories
    upload, raw_record, transaction = create_unmatched_batch()

    await ingestion_repository.save_batch(
        upload=upload,
        raw_records=(raw_record,),
        transactions=(transaction,),
    )

    catalog, rule_set = supplied_classification_config()

    first = await classify_upload(
        upload_id=upload.id,
        transaction_reader=ingestion_repository,
        classification_repository=classification_repository,
        pattern_lookup=MissingPatternLookup(),
        rule_set=rule_set,
        chart_of_accounts=catalog,
    )
    second = await classify_upload(
        upload_id=upload.id,
        transaction_reader=ingestion_repository,
        classification_repository=classification_repository,
        pattern_lookup=MissingPatternLookup(),
        rule_set=rule_set,
        chart_of_accounts=catalog,
    )

    assert first.canonical_transactions == 1
    assert first.manual_review_required == 1
    assert first.already_classified == 0
    assert first.failed == 0

    assert second.canonical_transactions == 1
    assert second.manual_review_required == 1
    assert second.already_classified == 0
    assert second.failed == 0

    assert await classification_repository.find_by_transaction_id(transaction.id) is None
    assert await classification_repository.classifications.count_documents({}) == 0
