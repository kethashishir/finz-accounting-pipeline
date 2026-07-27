"""Integration tests for MongoDB QBO sync persistence."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from app.core.config import Settings
from app.db.client import MongoDatabase
from app.models.quickbooks import QuickBooksEnvironment
from app.models.quickbooks_sync import (
    QuickBooksJournalEntryPlan,
    QuickBooksJournalLine,
    QuickBooksPostingType,
    QuickBooksSourceReference,
    QuickBooksSyncError,
    QuickBooksSyncStatus,
    build_quickbooks_request_id,
)
from app.repositories.quickbooks_sync import (
    QuickBooksSyncConflictError,
    QuickBooksSyncRepository,
    QuickBooksSyncTransitionError,
)

ENVIRONMENT = QuickBooksEnvironment.SANDBOX
REALM_ID = "9341456789012345"
SECOND_REALM_ID = "9341456789099999"
SOURCE_ID = UUID("11111111-1111-4111-8111-111111111111")
SECOND_SOURCE_ID = UUID("22222222-2222-4222-8222-222222222222")
THIRD_SOURCE_ID = UUID("33333333-3333-4333-8333-333333333333")
CREATED_AT = datetime(
    2026,
    7,
    27,
    8,
    0,
    tzinfo=UTC,
)
CLAIMED_AT = CREATED_AT + timedelta(minutes=1)
FAILED_AT = CREATED_AT + timedelta(minutes=2)
RECLAIMED_AT = CREATED_AT + timedelta(minutes=3)
COMPLETED_AT = CREATED_AT + timedelta(minutes=4)


@pytest.fixture
async def repository() -> AsyncIterator[QuickBooksSyncRepository]:
    """Create an isolated real MongoDB collection."""

    settings = Settings()
    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=settings.mongodb_database,
    )
    collection_name = f"test_qbo_sync_{uuid4().hex}"
    sync_repository = QuickBooksSyncRepository(
        mongodb.database,
        collection_name=collection_name,
    )
    await sync_repository.ensure_indexes()

    try:
        yield sync_repository
    finally:
        await mongodb.database.drop_collection(collection_name)
        await mongodb.close()


def make_source(
    transaction_id: UUID,
    *,
    version: int = 1,
) -> QuickBooksSourceReference:
    """Create one immutable source reference."""

    return QuickBooksSourceReference(
        normalized_transaction_id=transaction_id,
        classification_version=version,
        source_transaction_id=(f"source-{transaction_id}"),
    )


def make_plan(
    source_ids: tuple[UUID, ...] = (SOURCE_ID,),
    *,
    version: int = 1,
    note: str = "Finz posting plan",
) -> QuickBooksJournalEntryPlan:
    """Create one balanced posting plan."""

    sources = tuple(
        make_source(
            transaction_id,
            version=version,
        )
        for transaction_id in source_ids
    )

    return QuickBooksJournalEntryPlan(
        request_id=build_quickbooks_request_id(source_ids),
        sources=sources,
        transaction_date=date(2026, 4, 1),
        currency="USD",
        private_note=note,
        lines=(
            QuickBooksJournalLine(
                account_number="1000",
                account_name="Operating Checking",
                qbo_account_id="qbo-bank-1000",
                posting_type=(QuickBooksPostingType.DEBIT),
                amount=Decimal("100.00"),
            ),
            QuickBooksJournalLine(
                account_number="4000",
                account_name="Repair Service Revenue",
                qbo_account_id="qbo-income-4000",
                posting_type=(QuickBooksPostingType.CREDIT),
                amount=Decimal("100.00"),
            ),
        ),
    )


async def create_pending(
    repository: QuickBooksSyncRepository,
    plan: QuickBooksJournalEntryPlan | None = None,
    *,
    realm_id: str = REALM_ID,
):
    """Create one pending sandbox record."""

    return await repository.create_pending(
        environment=ENVIRONMENT,
        realm_id=realm_id,
        plan=plan or make_plan(),
        created_at=CREATED_AT,
    )


async def test_repository_creates_idempotence_indexes(
    repository: QuickBooksSyncRepository,
) -> None:
    """MongoDB enforces company-scoped posting identity."""

    cursor = await repository.records.list_indexes()
    indexes = {index["name"]: index async for index in cursor}

    assert indexes["uq_qbo_sync_company_request"]["unique"] is True
    assert indexes["uq_qbo_sync_company_source"]["unique"] is True
    assert indexes["uq_qbo_sync_company_transaction"]["unique"] is True
    assert "partialFilterExpression" in indexes["uq_qbo_sync_company_transaction"]
    assert "ix_qbo_sync_status_updated" in indexes


async def test_create_pending_is_idempotent(
    repository: QuickBooksSyncRepository,
) -> None:
    """Repeating the exact immutable plan returns one record."""

    first = await create_pending(repository)
    repeated = await create_pending(repository)

    assert repeated == first
    assert await repository.records.count_documents({}) == 1


async def test_changed_plan_with_same_request_is_rejected(
    repository: QuickBooksSyncRepository,
) -> None:
    """A classification revision cannot mutate a planned post."""

    await create_pending(repository)

    changed = make_plan(
        version=2,
        note="Changed classification plan",
    )

    with pytest.raises(
        QuickBooksSyncConflictError,
        match="different immutable plan",
    ):
        await create_pending(
            repository,
            changed,
        )


async def test_overlapping_source_is_rejected(
    repository: QuickBooksSyncRepository,
) -> None:
    """One normalized transaction cannot enter two postings."""

    await create_pending(
        repository,
        make_plan(
            (
                SOURCE_ID,
                SECOND_SOURCE_ID,
            )
        ),
    )

    with pytest.raises(
        QuickBooksSyncConflictError,
        match="already owned",
    ):
        await create_pending(
            repository,
            make_plan((SOURCE_ID,)),
        )


async def test_same_source_is_allowed_for_another_company(
    repository: QuickBooksSyncRepository,
) -> None:
    """Idempotence boundaries are scoped to a QBO company."""

    first = await create_pending(repository)
    second = await create_pending(
        repository,
        realm_id=SECOND_REALM_ID,
    )

    assert first.plan.request_id == (second.plan.request_id)
    assert first.realm_id != second.realm_id
    assert await repository.records.count_documents({}) == 2


async def test_claim_is_atomic(
    repository: QuickBooksSyncRepository,
) -> None:
    """Only one worker can claim a posting attempt."""

    pending = await create_pending(repository)
    claimed = await repository.claim_for_attempt(
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        request_id=pending.plan.request_id,
        claimed_at=CLAIMED_AT,
    )

    assert claimed.status is (QuickBooksSyncStatus.IN_PROGRESS)
    assert claimed.attempt_count == 1

    with pytest.raises(
        QuickBooksSyncTransitionError,
        match="not claimable",
    ):
        await repository.claim_for_attempt(
            environment=ENVIRONMENT,
            realm_id=REALM_ID,
            request_id=pending.plan.request_id,
            claimed_at=FAILED_AT,
        )


async def test_retryable_error_can_be_reclaimed(
    repository: QuickBooksSyncRepository,
) -> None:
    """A retry increments the attempt count atomically."""

    pending = await create_pending(repository)
    claimed = await repository.claim_for_attempt(
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        request_id=pending.plan.request_id,
        claimed_at=CLAIMED_AT,
    )
    failed = await repository.mark_retryable_error(
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        request_id=pending.plan.request_id,
        expected_attempt_count=(claimed.attempt_count),
        error=QuickBooksSyncError(
            code="http_429",
            message="QuickBooks throttled the request.",
            retryable=True,
            occurred_at=FAILED_AT,
        ),
    )
    reclaimed = await repository.claim_for_attempt(
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        request_id=pending.plan.request_id,
        claimed_at=RECLAIMED_AT,
    )

    assert failed.status is (QuickBooksSyncStatus.RETRYABLE_ERROR)
    assert reclaimed.status is (QuickBooksSyncStatus.IN_PROGRESS)
    assert reclaimed.attempt_count == 2
    assert reclaimed.last_error is None


async def test_success_persists_qbo_evidence(
    repository: QuickBooksSyncRepository,
) -> None:
    """A claimed post retains its QBO ID and sync token."""

    pending = await create_pending(repository)
    claimed = await repository.claim_for_attempt(
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        request_id=pending.plan.request_id,
        claimed_at=CLAIMED_AT,
    )
    succeeded = await repository.mark_succeeded(
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        request_id=pending.plan.request_id,
        expected_attempt_count=(claimed.attempt_count),
        qbo_transaction_id="qbo-je-123",
        qbo_sync_token="0",
        completed_at=COMPLETED_AT,
    )

    assert succeeded.status is (QuickBooksSyncStatus.SUCCEEDED)
    assert succeeded.qbo_transaction_id == ("qbo-je-123")
    assert succeeded.qbo_sync_token == "0"
    assert succeeded.last_error is None


async def test_permanent_error_cannot_be_reclaimed(
    repository: QuickBooksSyncRepository,
) -> None:
    """A non-retryable accounting error remains stopped."""

    pending = await create_pending(repository)
    claimed = await repository.claim_for_attempt(
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        request_id=pending.plan.request_id,
        claimed_at=CLAIMED_AT,
    )
    failed = await repository.mark_permanent_error(
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        request_id=pending.plan.request_id,
        expected_attempt_count=(claimed.attempt_count),
        error=QuickBooksSyncError(
            code="invalid_account_mapping",
            message=("The classified account does not exist in QuickBooks."),
            retryable=False,
            occurred_at=FAILED_AT,
        ),
    )

    assert failed.status is (QuickBooksSyncStatus.PERMANENT_ERROR)

    with pytest.raises(
        QuickBooksSyncTransitionError,
        match="not claimable",
    ):
        await repository.claim_for_attempt(
            environment=ENVIRONMENT,
            realm_id=REALM_ID,
            request_id=pending.plan.request_id,
            claimed_at=RECLAIMED_AT,
        )


async def test_qbo_transaction_id_cannot_be_reused(
    repository: QuickBooksSyncRepository,
) -> None:
    """Two sync records cannot claim the same QBO entity."""

    first = await create_pending(
        repository,
        make_plan((SOURCE_ID,)),
    )
    second = await create_pending(
        repository,
        make_plan((THIRD_SOURCE_ID,)),
    )

    first_claim = await repository.claim_for_attempt(
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        request_id=first.plan.request_id,
        claimed_at=CLAIMED_AT,
    )
    second_claim = await repository.claim_for_attempt(
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        request_id=second.plan.request_id,
        claimed_at=CLAIMED_AT,
    )

    await repository.mark_succeeded(
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        request_id=first.plan.request_id,
        expected_attempt_count=(first_claim.attempt_count),
        qbo_transaction_id="shared-qbo-je",
        qbo_sync_token="0",
        completed_at=COMPLETED_AT,
    )

    with pytest.raises(
        QuickBooksSyncConflictError,
        match="already linked",
    ):
        await repository.mark_succeeded(
            environment=ENVIRONMENT,
            realm_id=REALM_ID,
            request_id=second.plan.request_id,
            expected_attempt_count=(second_claim.attempt_count),
            qbo_transaction_id="shared-qbo-je",
            qbo_sync_token="0",
            completed_at=COMPLETED_AT,
        )
