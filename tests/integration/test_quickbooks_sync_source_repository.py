"""Integration tests for QuickBooks synchronization source reads."""

from collections.abc import AsyncIterator
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.db.client import MongoDatabase
from app.models.classification import (
    ClassificationDecision,
    ClassificationSource,
    QuickBooksAccountMapping,
    TransactionClassification,
    TransactionType,
)
from app.models.ingestion import (
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
)
from app.repositories.classification import (
    ClassificationRepository,
)
from app.repositories.ingestion import IngestionRepository
from app.repositories.quickbooks_sync_source import (
    QuickBooksSyncSourceRepository,
)


@pytest.fixture
async def repositories() -> AsyncIterator[
    tuple[
        QuickBooksSyncSourceRepository,
        IngestionRepository,
        ClassificationRepository,
        MongoDatabase,
    ]
]:
    """Create isolated real MongoDB repositories."""

    settings = Settings()
    database_name = f"{settings.mongodb_database[:24]}_qsrc_{uuid4().hex[:16]}"
    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=database_name,
    )
    ingestion = IngestionRepository(mongodb.database)
    classification = ClassificationRepository(mongodb.database)
    source = QuickBooksSyncSourceRepository(mongodb.database)

    await ingestion.ensure_indexes()
    await classification.ensure_indexes()

    try:
        yield (
            source,
            ingestion,
            classification,
            mongodb,
        )
    finally:
        await mongodb.client.drop_database(database_name)
        await mongodb.close()


def make_transaction(
    *,
    source_id: str,
    amount: Decimal,
) -> NormalizedTransaction:
    """Create one valid canonical transaction."""

    return NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id=source_id,
        transaction_date=date(2026, 4, 1),
        description_original=source_id,
        description_normalized=source_id.casefold(),
        amount=amount,
        currency="USD",
        bank_account="Operating Checking",
        direction=(
            TransactionDirection.INFLOW
            if amount > Decimal("0.00")
            else TransactionDirection.OUTFLOW
        ),
        fingerprint=uuid4().hex * 2,
        status=RecordStatus.VALID,
    )


def make_classification(
    transaction: NormalizedTransaction,
) -> TransactionClassification:
    """Create one safe classification."""

    return TransactionClassification(
        normalized_transaction_id=transaction.id,
        decision=ClassificationDecision(
            transaction_type=TransactionType.REVENUE,
            counterparty=None,
            qbo_account=QuickBooksAccountMapping(
                account_number="4000",
                account_name="Repair Service Revenue",
            ),
            confidence_score=Decimal("1.000"),
            explanation="Deterministic revenue classification.",
            source=ClassificationSource.DETERMINISTIC_RULE,
            review_required=False,
        ),
    )


async def test_snapshot_reads_current_mongo_evidence(
    repositories,
) -> None:
    """The source repository validates both existing collections."""

    (
        source_repository,
        ingestion_repository,
        classification_repository,
        _,
    ) = repositories

    second = make_transaction(
        source_id="SECOND",
        amount=Decimal("-10.00"),
    )
    first = make_transaction(
        source_id="FIRST",
        amount=Decimal("100.00"),
    )

    await ingestion_repository.save_transactions(
        [
            second,
            first,
        ]
    )
    classification = make_classification(first)
    await classification_repository.save_initial(classification)

    snapshot = await source_repository.read_snapshot()

    assert {transaction.id for transaction in snapshot.transactions} == {
        first.id,
        second.id,
    }
    assert snapshot.classifications == (classification,)
