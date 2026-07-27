"""Tests for idempotent QuickBooks sync orchestration."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx2
from pydantic import SecretStr

from app.core.config import Settings
from app.models.quickbooks import QuickBooksEnvironment
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
from app.services.quickbooks.api_client import (
    QuickBooksApiClient,
    QuickBooksApiJournalEntry,
    QuickBooksApiProviderError,
    QuickBooksApiRequestError,
    QuickBooksApiResponseError,
)
from app.services.quickbooks.oauth_config import (
    build_quickbooks_oauth_configuration,
)
from app.services.quickbooks.sync_orchestrator import (
    sync_quickbooks_journal_entry,
)

ENVIRONMENT = QuickBooksEnvironment.SANDBOX
REALM_ID = "9341456789012345"
SOURCE_ID = UUID("11111111-1111-4111-8111-111111111111")
ACCESS_TOKEN = SecretStr("test-access-token")
STARTED_AT = datetime(
    2026,
    7,
    27,
    10,
    0,
    tzinfo=UTC,
)


def configuration():
    """Create test-only QBO configuration."""

    return build_quickbooks_oauth_configuration(
        Settings(
            _env_file=None,
            qbo_environment="sandbox",
            qbo_client_id="client-id",
            qbo_client_secret="client-secret",
            qbo_redirect_uri=("http://localhost:8000/api/v1/quickbooks/callback"),
            token_encryption_key=("token-encryption-key-0123456789abcdef"),
            session_secret=("session-secret-key-0123456789abcdef"),
        )
    )


def plan(
    *,
    currency: str = "USD",
) -> QuickBooksJournalEntryPlan:
    """Create one balanced revenue JournalEntry plan."""

    return QuickBooksJournalEntryPlan(
        request_id=build_quickbooks_request_id((SOURCE_ID,)),
        sources=(
            QuickBooksSourceReference(
                normalized_transaction_id=SOURCE_ID,
                classification_version=1,
                source_transaction_id="BF-SYNC-0001",
            ),
        ),
        transaction_date=date(2026, 4, 1),
        currency=currency,
        private_note="Finz BF-SYNC-0001",
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


class FakeClock:
    """Return deterministic increasing UTC timestamps."""

    def __init__(self) -> None:
        self.current = STARTED_AT

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


class FakeSyncRepository:
    """Model repository transitions without MongoDB."""

    def __init__(self) -> None:
        self.record: QuickBooksSyncRecord | None = None
        self.create_calls = 0
        self.claim_calls = 0
        self.success_calls = 0
        self.retryable_calls = 0
        self.permanent_calls = 0

    async def create_pending(
        self,
        *,
        environment: QuickBooksEnvironment,
        realm_id: str,
        plan: QuickBooksJournalEntryPlan,
        created_at: datetime,
    ) -> QuickBooksSyncRecord:
        self.create_calls += 1

        if self.record is None:
            self.record = QuickBooksSyncRecord(
                environment=environment,
                realm_id=realm_id,
                plan=plan,
                created_at=created_at,
                updated_at=created_at,
            )

        return self.record

    async def claim_for_attempt(
        self,
        *,
        environment: QuickBooksEnvironment,
        realm_id: str,
        request_id: str,
        claimed_at: datetime,
    ) -> QuickBooksSyncRecord:
        self.claim_calls += 1
        assert self.record is not None

        self.record = _replace_record(
            self.record,
            status=QuickBooksSyncStatus.IN_PROGRESS,
            attempt_count=(self.record.attempt_count + 1),
            last_error=None,
            qbo_transaction_id=None,
            qbo_sync_token=None,
            updated_at=claimed_at,
        )

        return self.record

    async def mark_succeeded(
        self,
        *,
        expected_attempt_count: int,
        qbo_transaction_id: str,
        qbo_sync_token: str,
        completed_at: datetime,
        **_: Any,
    ) -> QuickBooksSyncRecord:
        self.success_calls += 1
        assert self.record is not None
        assert self.record.attempt_count == expected_attempt_count

        self.record = _replace_record(
            self.record,
            status=QuickBooksSyncStatus.SUCCEEDED,
            qbo_transaction_id=qbo_transaction_id,
            qbo_sync_token=qbo_sync_token,
            last_error=None,
            updated_at=completed_at,
        )

        return self.record

    async def mark_retryable_error(
        self,
        *,
        expected_attempt_count: int,
        error: QuickBooksSyncError,
        **_: Any,
    ) -> QuickBooksSyncRecord:
        self.retryable_calls += 1
        assert self.record is not None
        assert self.record.attempt_count == expected_attempt_count

        self.record = _replace_record(
            self.record,
            status=(QuickBooksSyncStatus.RETRYABLE_ERROR),
            qbo_transaction_id=None,
            qbo_sync_token=None,
            last_error=error,
            updated_at=error.occurred_at,
        )

        return self.record

    async def mark_permanent_error(
        self,
        *,
        expected_attempt_count: int,
        error: QuickBooksSyncError,
        **_: Any,
    ) -> QuickBooksSyncRecord:
        self.permanent_calls += 1
        assert self.record is not None
        assert self.record.attempt_count == expected_attempt_count

        self.record = _replace_record(
            self.record,
            status=(QuickBooksSyncStatus.PERMANENT_ERROR),
            qbo_transaction_id=None,
            qbo_sync_token=None,
            last_error=error,
            updated_at=error.occurred_at,
        )

        return self.record


class FakeJournalEntryClient:
    """Return evidence or raise one configured API error."""

    def __init__(
        self,
        outcome: QuickBooksApiJournalEntry | Exception,
    ) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def create_journal_entry(
        self,
        **kwargs: Any,
    ) -> QuickBooksApiJournalEntry:
        self.calls.append(kwargs)

        if isinstance(self.outcome, Exception):
            raise self.outcome

        return self.outcome


def _replace_record(
    record: QuickBooksSyncRecord,
    **updates: Any,
) -> QuickBooksSyncRecord:
    """Revalidate one fake repository transition."""

    payload = record.model_dump()
    payload.update(updates)

    return QuickBooksSyncRecord.model_validate(payload)


async def run_sync(
    *,
    repository: FakeSyncRepository,
    client: Any,
    posting_plan: QuickBooksJournalEntryPlan | None = None,
) -> QuickBooksSyncRecord:
    """Run one deterministic orchestration attempt."""

    return await sync_quickbooks_journal_entry(
        repository=repository,
        client=client,
        environment=ENVIRONMENT,
        realm_id=REALM_ID,
        access_token=ACCESS_TOKEN,
        plan=posting_plan or plan(),
        clock=FakeClock(),
    )


async def test_success_persists_qbo_evidence() -> None:
    """A successful create becomes a terminal sync record."""

    repository = FakeSyncRepository()
    client = FakeJournalEntryClient(
        QuickBooksApiJournalEntry(
            id="qbo-je-123",
            sync_token="0",
            transaction_date=date(2026, 4, 1),
        )
    )

    result = await run_sync(
        repository=repository,
        client=client,
    )

    assert result.status is (QuickBooksSyncStatus.SUCCEEDED)
    assert result.attempt_count == 1
    assert result.qbo_transaction_id == "qbo-je-123"
    assert result.qbo_sync_token == "0"
    assert repository.success_calls == 1
    assert len(client.calls) == 1
    assert client.calls[0]["request_id"] == (plan().request_id)


async def test_repeated_success_does_not_call_qbo_again() -> None:
    """A terminal success short-circuits duplicate writes."""

    repository = FakeSyncRepository()
    client = FakeJournalEntryClient(
        QuickBooksApiJournalEntry(
            id="qbo-je-123",
            sync_token="0",
        )
    )

    first = await run_sync(
        repository=repository,
        client=client,
    )
    repeated = await run_sync(
        repository=repository,
        client=client,
    )

    assert first == repeated
    assert len(client.calls) == 1
    assert repository.claim_calls == 1


async def test_transport_failure_is_retryable() -> None:
    """A network failure can safely retry the same request ID."""

    repository = FakeSyncRepository()
    client = FakeJournalEntryClient(QuickBooksApiRequestError("connection failed"))

    result = await run_sync(
        repository=repository,
        client=client,
    )

    assert result.status is (QuickBooksSyncStatus.RETRYABLE_ERROR)
    assert result.last_error is not None
    assert result.last_error.code == ("qbo_transport_error")
    assert result.last_error.retryable is True


async def test_invalid_success_response_is_retryable() -> None:
    """A safe request-ID retry resolves ambiguous QBO results."""

    repository = FakeSyncRepository()
    client = FakeJournalEntryClient(QuickBooksApiResponseError("invalid response"))

    result = await run_sync(
        repository=repository,
        client=client,
    )

    assert result.status is (QuickBooksSyncStatus.RETRYABLE_ERROR)
    assert result.last_error is not None
    assert result.last_error.code == ("qbo_invalid_response")


async def test_real_api_429_fault_is_retryable() -> None:
    """Structured Intuit fault fields drive retry classification."""

    repository = FakeSyncRepository()

    async def handler(
        request: httpx2.Request,
    ) -> httpx2.Response:
        return httpx2.Response(
            429,
            headers={
                "intuit_tid": "safe-test-tid",
            },
            json={
                "Fault": {
                    "Error": [
                        {
                            "code": "003001",
                            "Message": ("Application has been throttled"),
                            "Detail": ("Retry the request later"),
                        }
                    ]
                }
            },
        )

    client = QuickBooksApiClient(
        configuration=configuration(),
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        owns_client=True,
    )

    async with client:
        result = await run_sync(
            repository=repository,
            client=client,
        )

    assert result.status is (QuickBooksSyncStatus.RETRYABLE_ERROR)
    assert result.last_error is not None
    assert result.last_error.code == "003001"
    assert result.last_error.retryable is True
    assert "test-access-token" not in (result.last_error.message)


async def test_http_400_validation_error_is_permanent() -> None:
    """Invalid accounting enumerations require correction."""

    repository = FakeSyncRepository()
    client = FakeJournalEntryClient(
        QuickBooksApiProviderError(
            "HTTP 400 invalid account mapping",
            status_code=400,
            provider_code="2170",
            transaction_id="safe-tid",
        )
    )

    result = await run_sync(
        repository=repository,
        client=client,
    )

    assert result.status is (QuickBooksSyncStatus.PERMANENT_ERROR)
    assert result.last_error is not None
    assert result.last_error.code == "2170"
    assert result.last_error.retryable is False


async def test_http_401_is_retryable_after_token_refresh() -> None:
    """An expired token may succeed after credential refresh."""

    repository = FakeSyncRepository()
    client = FakeJournalEntryClient(
        QuickBooksApiProviderError(
            "HTTP 401 authentication required",
            status_code=401,
            provider_code="3200",
        )
    )

    result = await run_sync(
        repository=repository,
        client=client,
    )

    assert result.status is (QuickBooksSyncStatus.RETRYABLE_ERROR)
    assert result.last_error is not None
    assert result.last_error.retryable is True


async def test_invalid_currency_becomes_permanent_without_api_call() -> None:
    """Unsupported currency fails before contacting QBO."""

    repository = FakeSyncRepository()
    client = FakeJournalEntryClient(
        QuickBooksApiJournalEntry(
            id="must-not-be-used",
            sync_token="0",
        )
    )

    result = await run_sync(
        repository=repository,
        client=client,
        posting_plan=plan(currency="EUR"),
    )

    assert result.status is (QuickBooksSyncStatus.PERMANENT_ERROR)
    assert result.last_error is not None
    assert result.last_error.code == ("invalid_journal_entry_payload")
    assert client.calls == []
    assert repository.permanent_calls == 1
