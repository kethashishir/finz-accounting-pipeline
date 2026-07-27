"""Integration tests for approved P&L evidence retrieval."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.core.config import get_settings
from app.db.client import MongoDatabase
from app.db.serialization import classification_to_document
from app.models.classification import (
    ClassificationDecision,
    ClassificationSource,
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
from app.repositories.classification import ClassificationRepository
from app.repositories.ingestion import IngestionRepository
from app.repositories.reporting import (
    ProfitAndLossQueryError,
    ProfitAndLossRepository,
)

REVIEWED_AT = datetime(
    2026,
    7,
    27,
    1,
    0,
    tzinfo=UTC,
)


@pytest.fixture
async def repositories() -> AsyncIterator[
    tuple[
        ProfitAndLossRepository,
        ClassificationRepository,
        IngestionRepository,
        MongoDatabase,
    ]
]:
    """Create isolated reporting, classification, and ingestion stores."""

    settings = get_settings()
    database_name = f"{settings.mongodb_database[:30]}_pnl_{uuid4().hex[:16]}"
    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=database_name,
    )

    ingestion_repository = IngestionRepository(mongodb.database)
    classification_repository = ClassificationRepository(mongodb.database)
    reporting_repository = ProfitAndLossRepository(mongodb.database)

    await ingestion_repository.ensure_indexes()
    await classification_repository.ensure_indexes()

    try:
        yield (
            reporting_repository,
            classification_repository,
            ingestion_repository,
            mongodb,
        )
    finally:
        await mongodb.client.drop_database(database_name)
        await mongodb.close()


def account_for_type(
    transaction_type: TransactionType,
) -> tuple[str, str]:
    """Return one valid chart-of-accounts mapping."""

    return {
        TransactionType.REVENUE: (
            "4000",
            "Repair Service Revenue",
        ),
        TransactionType.REFUND: (
            "4100",
            "Customer Refunds",
        ),
        TransactionType.COST_OF_GOODS_SOLD: (
            "5000",
            "Materials & Supplies",
        ),
        TransactionType.OPERATING_EXPENSE: (
            "6020",
            "Vehicle & Fuel",
        ),
        TransactionType.TRANSFER: (
            "1010",
            "Tax Reserve",
        ),
    }[transaction_type]


async def persist_transaction(
    repository: IngestionRepository,
    *,
    transaction_date: date,
    amount: str,
    currency: str = "USD",
    status: RecordStatus = RecordStatus.VALID,
    duplicate_of: UUID | None = None,
) -> NormalizedTransaction:
    """Persist one normalized transaction and its source evidence."""

    identifier = uuid4()
    upload = UploadBatch(
        source_file_name=f"{identifier}.csv",
        file_type=FileType.CSV,
        file_sha256=identifier.hex * 2,
        physical_record_count=1,
    )
    raw_record = RawRecord(
        upload_id=upload.id,
        source_file_name=upload.source_file_name,
        source_row_number=2,
        raw_values={
            "Date": transaction_date.isoformat(),
            "Description": "Reporting repository test",
            "Amount": amount,
        },
        raw_hash=uuid4().hex * 2,
    )

    decimal_amount = Decimal(amount)
    direction = (
        TransactionDirection.INFLOW
        if decimal_amount > Decimal("0.00")
        else TransactionDirection.OUTFLOW
    )

    transaction = NormalizedTransaction(
        upload_id=upload.id,
        raw_record_id=raw_record.id,
        source_transaction_id=(f"BF-PNL-{identifier.hex[:12]}"),
        transaction_date=transaction_date,
        description_original="Reporting repository test",
        description_normalized=("reporting repository test"),
        amount=decimal_amount,
        currency=currency,
        bank_account="Operating Checking",
        direction=direction,
        fingerprint=uuid4().hex * 2,
        status=status,
        duplicate_of=duplicate_of,
    )

    await repository.save_batch(
        upload=upload,
        raw_records=[raw_record],
        transactions=[transaction],
    )

    return transaction


def create_classification(
    transaction: NormalizedTransaction,
    *,
    transaction_type: TransactionType,
    review_status: ReviewStatus,
) -> TransactionClassification:
    """Create a classification in the requested review state."""

    account_number, account_name = account_for_type(transaction_type)

    reviewer = (
        ReviewerMetadata(
            reviewer_id="integration-reviewer",
            reviewed_at=REVIEWED_AT,
            notes="Approved for reporting.",
        )
        if review_status is ReviewStatus.APPROVED
        else None
    )

    return TransactionClassification(
        normalized_transaction_id=transaction.id,
        decision=ClassificationDecision(
            transaction_type=transaction_type,
            qbo_account=QuickBooksAccountMapping(
                account_number=account_number,
                account_name=account_name,
            ),
            confidence_score=Decimal("1.000"),
            explanation=("Validated reporting repository classification."),
            source=ClassificationSource.DETERMINISTIC_RULE,
            review_required=False,
        ),
        review_status=review_status,
        reviewer=reviewer,
    )


async def persist_classification(
    repository: ClassificationRepository,
    transaction: NormalizedTransaction,
    *,
    transaction_type: TransactionType,
    review_status: ReviewStatus,
) -> TransactionClassification:
    """Persist an initial decision and optional final approval."""

    target = create_classification(
        transaction,
        transaction_type=transaction_type,
        review_status=review_status,
    )

    initial = TransactionClassification(
        normalized_transaction_id=(target.normalized_transaction_id),
        version=target.version,
        decision=target.decision,
    )

    await repository.save_initial(initial)

    if review_status is ReviewStatus.APPROVED:
        await repository.save_review(
            target,
            expected_version=1,
        )

    return target


@pytest.mark.asyncio
async def test_returns_only_approved_canonical_pnl_sources(
    repositories: tuple[
        ProfitAndLossRepository,
        ClassificationRepository,
        IngestionRepository,
        MongoDatabase,
    ],
) -> None:
    """The query excludes unsafe, unapproved, and balance-sheet rows."""

    (
        reporting_repository,
        classification_repository,
        ingestion_repository,
        mongodb,
    ) = repositories

    revenue = await persist_transaction(
        ingestion_repository,
        transaction_date=date(2026, 4, 5),
        amount="1000.25",
    )
    expense = await persist_transaction(
        ingestion_repository,
        transaction_date=date(2026, 5, 10),
        amount="-200.10",
    )
    pending = await persist_transaction(
        ingestion_repository,
        transaction_date=date(2026, 4, 8),
        amount="300.00",
    )
    transfer = await persist_transaction(
        ingestion_repository,
        transaction_date=date(2026, 4, 9),
        amount="-500.00",
    )
    outside_period = await persist_transaction(
        ingestion_repository,
        transaction_date=date(2026, 7, 1),
        amount="700.00",
    )

    await persist_classification(
        classification_repository,
        revenue,
        transaction_type=TransactionType.REVENUE,
        review_status=ReviewStatus.APPROVED,
    )
    await persist_classification(
        classification_repository,
        expense,
        transaction_type=(TransactionType.OPERATING_EXPENSE),
        review_status=ReviewStatus.APPROVED,
    )
    await persist_classification(
        classification_repository,
        pending,
        transaction_type=TransactionType.REVENUE,
        review_status=ReviewStatus.PENDING,
    )
    await persist_classification(
        classification_repository,
        transfer,
        transaction_type=TransactionType.TRANSFER,
        review_status=ReviewStatus.APPROVED,
    )
    await persist_classification(
        classification_repository,
        outside_period,
        transaction_type=TransactionType.REVENUE,
        review_status=ReviewStatus.APPROVED,
    )

    duplicate = await persist_transaction(
        ingestion_repository,
        transaction_date=date(2026, 4, 5),
        amount="1000.25",
        status=RecordStatus.DUPLICATE,
        duplicate_of=revenue.id,
    )
    duplicate_classification = create_classification(
        duplicate,
        transaction_type=TransactionType.REVENUE,
        review_status=ReviewStatus.APPROVED,
    )

    await mongodb.database[ClassificationRepository.CLASSIFICATION_COLLECTION].insert_one(
        classification_to_document(duplicate_classification)
    )

    sources = await reporting_repository.find_approved_sources(
        start_date=date(2026, 4, 1),
        end_date=date(2026, 6, 30),
        currency="USD",
    )

    assert [item.transaction.id for item in sources] == [
        revenue.id,
        expense.id,
    ]

    assert sources[0].transaction.amount == Decimal("1000.25")
    assert sources[1].transaction.amount == Decimal("-200.10")
    assert all(item.classification.review_status is ReviewStatus.APPROVED for item in sources)


@pytest.mark.asyncio
async def test_currency_filter_is_normalized(
    repositories: tuple[
        ProfitAndLossRepository,
        ClassificationRepository,
        IngestionRepository,
        MongoDatabase,
    ],
) -> None:
    """Lowercase input still selects the requested stored currency."""

    (
        reporting_repository,
        classification_repository,
        ingestion_repository,
        _,
    ) = repositories

    transaction = await persist_transaction(
        ingestion_repository,
        transaction_date=date(2026, 4, 5),
        amount="100.00",
        currency="CAD",
    )
    await persist_classification(
        classification_repository,
        transaction,
        transaction_type=TransactionType.REVENUE,
        review_status=ReviewStatus.APPROVED,
    )

    cad_sources = await reporting_repository.find_approved_sources(
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        currency="cad",
    )
    usd_sources = await reporting_repository.find_approved_sources(
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        currency="USD",
    )

    assert len(cad_sources) == 1
    assert cad_sources[0].transaction.id == transaction.id
    assert usd_sources == ()


@pytest.mark.asyncio
async def test_empty_reporting_period_returns_empty_tuple(
    repositories: tuple[
        ProfitAndLossRepository,
        ClassificationRepository,
        IngestionRepository,
        MongoDatabase,
    ],
) -> None:
    """A period with no approved evidence is a valid empty result."""

    reporting_repository, _, _, _ = repositories

    sources = await reporting_repository.find_approved_sources(
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        currency="USD",
    )

    assert sources == ()


@pytest.mark.asyncio
async def test_invalid_reporting_query_is_rejected(
    repositories: tuple[
        ProfitAndLossRepository,
        ClassificationRepository,
        IngestionRepository,
        MongoDatabase,
    ],
) -> None:
    """Invalid periods and currency codes fail before database access."""

    reporting_repository, _, _, _ = repositories

    with pytest.raises(
        ProfitAndLossQueryError,
        match="cannot be after",
    ):
        await reporting_repository.find_approved_sources(
            start_date=date(2026, 5, 1),
            end_date=date(2026, 4, 30),
            currency="USD",
        )

    with pytest.raises(
        ProfitAndLossQueryError,
        match="three-letter",
    ):
        await reporting_repository.find_approved_sources(
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 30),
            currency="US",
        )
