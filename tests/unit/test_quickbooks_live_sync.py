"""Tests for guarded serial QuickBooks synchronization."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import SecretStr

from app.models.quickbooks import (
    QuickBooksEnvironment,
)
from app.models.quickbooks_sync import (
    QuickBooksJournalEntryPlan,
    QuickBooksJournalLine,
    QuickBooksPostingType,
    QuickBooksSourceReference,
    QuickBooksSyncError,
    QuickBooksSyncRecord,
    QuickBooksSyncStatus,
    build_quickbooks_request_id,
)
from app.services.quickbooks.live_sync import (
    LIVE_SYNC_CONFIRMATION,
    QuickBooksLiveSyncError,
    require_live_sync_confirmation,
    synchronize_quickbooks_inventory,
)

ENVIRONMENT = QuickBooksEnvironment.SANDBOX
REALM_ID = "9341456789012345"
NOW = datetime(
    2026,
    7,
    27,
    12,
    0,
    tzinfo=UTC,
)


def plan(
    source_id: UUID,
) -> QuickBooksJournalEntryPlan:
    """Create one balanced plan fixture."""

    return QuickBooksJournalEntryPlan(
        request_id=build_quickbooks_request_id((source_id,)),
        sources=(
            QuickBooksSourceReference(
                normalized_transaction_id=source_id,
                classification_version=1,
            ),
        ),
        transaction_date=date(2026, 4, 1),
        currency="USD",
        private_note="Live synchronization fixture",
        lines=(
            QuickBooksJournalLine(
                account_number="1000",
                account_name="Operating Checking",
                qbo_account_id="qbo-bank",
                posting_type=(QuickBooksPostingType.DEBIT),
                amount=Decimal("100.00"),
            ),
            QuickBooksJournalLine(
                account_number="4000",
                account_name="Repair Service Revenue",
                qbo_account_id="qbo-income",
                posting_type=(QuickBooksPostingType.CREDIT),
                amount=Decimal("100.00"),
            ),
        ),
    )


def sync_record(
    posting_plan: QuickBooksJournalEntryPlan,
    *,
    status: QuickBooksSyncStatus,
    retryable: bool | None = None,
    code: str = "test_error",
) -> QuickBooksSyncRecord:
    """Create one legal synchronization record."""

    values = {
        "environment": ENVIRONMENT,
        "realm_id": REALM_ID,
        "plan": posting_plan,
        "status": status,
        "attempt_count": 1,
        "created_at": NOW,
        "updated_at": NOW,
    }

    if status is QuickBooksSyncStatus.SUCCEEDED:
        values.update(
            {
                "qbo_transaction_id": (f"qbo-{posting_plan.request_id}"),
                "qbo_sync_token": "0",
            }
        )
    elif status in {
        QuickBooksSyncStatus.RETRYABLE_ERROR,
        QuickBooksSyncStatus.PERMANENT_ERROR,
    }:
        assert retryable is not None
        values["last_error"] = QuickBooksSyncError(
            code=code,
            message="Safe test synchronization failure.",
            retryable=retryable,
            occurred_at=NOW,
        )

    return QuickBooksSyncRecord.model_validate(values)


def test_confirmation_requires_exact_phrase() -> None:
    """A vague acknowledgement cannot authorize writes."""

    require_live_sync_confirmation(LIVE_SYNC_CONFIRMATION)

    with pytest.raises(
        QuickBooksLiveSyncError,
        match="not confirmed",
    ):
        require_live_sync_confirmation("yes")


async def test_serial_sync_counts_new_and_reused_successes() -> None:
    """Existing successes are reused and new plans are counted."""

    first = plan(UUID("11111111-1111-4111-8111-111111111111"))
    second = plan(UUID("22222222-2222-4222-8222-222222222222"))
    calls: list[str] = []
    progress: list[tuple[int, int]] = []

    async def sync_one(**kwargs):
        posting_plan = kwargs["plan"]
        calls.append(posting_plan.request_id)

        return sync_record(
            posting_plan,
            status=QuickBooksSyncStatus.SUCCEEDED,
        )

    async def already_succeeded(
        request_id: str,
    ) -> bool:
        return request_id == first.request_id

    async def no_sleep(_: float) -> None:
        return None

    result = await synchronize_quickbooks_inventory(
        repository=object(),
        client=object(),
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        access_token=SecretStr("token"),
        plans=(first, second),
        is_already_succeeded=already_succeeded,
        sleep=no_sleep,
        progress=lambda current, total: progress.append((current, total)),
        sync_one=sync_one,
    )

    assert calls == [
        first.request_id,
        second.request_id,
    ]
    assert progress == [
        (1, 2),
        (2, 2),
    ]
    assert result.plan_count == 2
    assert result.newly_succeeded == 1
    assert result.reused_succeeded == 1
    assert result.retry_attempts == 0


async def test_retryable_failure_reuses_same_request() -> None:
    """A retry uses the unchanged deterministic request ID."""

    posting_plan = plan(UUID("33333333-3333-4333-8333-333333333333"))
    outcomes = [
        sync_record(
            posting_plan,
            status=(QuickBooksSyncStatus.RETRYABLE_ERROR),
            retryable=True,
            code="qbo_transport_error",
        ),
        sync_record(
            posting_plan,
            status=QuickBooksSyncStatus.SUCCEEDED,
        ),
    ]
    request_ids: list[str] = []
    sleeps: list[float] = []

    async def sync_one(**kwargs):
        request_ids.append(kwargs["plan"].request_id)
        return outcomes.pop(0)

    async def not_succeeded(_: str) -> bool:
        return False

    async def record_sleep(
        seconds: float,
    ) -> None:
        sleeps.append(seconds)

    result = await synchronize_quickbooks_inventory(
        repository=object(),
        client=object(),
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        access_token=SecretStr("token"),
        plans=(posting_plan,),
        is_already_succeeded=not_succeeded,
        sleep=record_sleep,
        sync_one=sync_one,
    )

    assert request_ids == [
        posting_plan.request_id,
        posting_plan.request_id,
    ]
    assert sleeps == [2.0]
    assert result.retry_attempts == 1
    assert result.newly_succeeded == 1


async def test_authentication_failure_refreshes_token() -> None:
    """An expired access token is refreshed before retry."""

    posting_plan = plan(UUID("44444444-4444-4444-8444-444444444444"))
    outcomes = [
        sync_record(
            posting_plan,
            status=(QuickBooksSyncStatus.RETRYABLE_ERROR),
            retryable=True,
            code="3200",
        ),
        sync_record(
            posting_plan,
            status=QuickBooksSyncStatus.SUCCEEDED,
        ),
    ]
    access_tokens: list[str] = []

    async def sync_one(**kwargs):
        access_tokens.append(kwargs["access_token"].get_secret_value())
        return outcomes.pop(0)

    async def not_succeeded(_: str) -> bool:
        return False

    async def refresh() -> SecretStr:
        return SecretStr("refreshed-token")

    async def no_sleep(_: float) -> None:
        return None

    result = await synchronize_quickbooks_inventory(
        repository=object(),
        client=object(),
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        access_token=SecretStr("expired-token"),
        plans=(posting_plan,),
        is_already_succeeded=not_succeeded,
        refresh_access_token=refresh,
        sleep=no_sleep,
        sync_one=sync_one,
    )

    assert access_tokens == [
        "expired-token",
        "refreshed-token",
    ]
    assert result.token_refreshes == 1
    assert result.retry_attempts == 1


async def test_permanent_failure_stops_immediately() -> None:
    """An accounting rejection blocks later writes."""

    posting_plan = plan(UUID("55555555-5555-4555-8555-555555555555"))

    async def sync_one(**kwargs):
        return sync_record(
            kwargs["plan"],
            status=(QuickBooksSyncStatus.PERMANENT_ERROR),
            retryable=False,
            code="2170",
        )

    async def not_succeeded(_: str) -> bool:
        return False

    async def no_sleep(_: float) -> None:
        return None

    with pytest.raises(
        QuickBooksLiveSyncError,
        match="Permanent",
    ):
        await synchronize_quickbooks_inventory(
            repository=object(),
            client=object(),
            environment=ENVIRONMENT,
            realm_id=REALM_ID,
            access_token=SecretStr("token"),
            plans=(posting_plan,),
            is_already_succeeded=not_succeeded,
            sleep=no_sleep,
            sync_one=sync_one,
        )
