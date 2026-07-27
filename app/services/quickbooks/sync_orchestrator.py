"""Idempotent orchestration for QuickBooks JournalEntry sync."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import SecretStr

from app.models.quickbooks import QuickBooksEnvironment
from app.models.quickbooks_sync import (
    QuickBooksJournalEntryPlan,
    QuickBooksSyncError,
    QuickBooksSyncRecord,
    QuickBooksSyncStatus,
)
from app.repositories.quickbooks_sync import (
    QuickBooksSyncRepository,
)
from app.services.quickbooks.api_client import (
    QuickBooksApiClient,
    QuickBooksApiProviderError,
    QuickBooksApiRequestError,
    QuickBooksApiResponseError,
)
from app.services.quickbooks.journal_entries import (
    QuickBooksJournalEntryPayloadError,
    build_quickbooks_journal_entry_payload,
)

Clock = Callable[[], datetime]

RETRYABLE_HTTP_STATUS_CODES = frozenset(
    {
        401,
        408,
        425,
        429,
        500,
        502,
        503,
        504,
    }
)

TERMINAL_SYNC_STATUSES = frozenset(
    {
        QuickBooksSyncStatus.SUCCEEDED,
        QuickBooksSyncStatus.PERMANENT_ERROR,
    }
)


async def sync_quickbooks_journal_entry(
    *,
    repository: QuickBooksSyncRepository,
    client: QuickBooksApiClient,
    environment: QuickBooksEnvironment,
    realm_id: str,
    access_token: SecretStr,
    plan: QuickBooksJournalEntryPlan,
    clock: Clock = lambda: datetime.now(UTC),
) -> QuickBooksSyncRecord:
    """Persist, claim, submit, and finalize one posting plan."""

    pending = await repository.create_pending(
        environment=environment,
        realm_id=realm_id,
        plan=plan,
        created_at=_timestamp(clock),
    )

    if pending.status in TERMINAL_SYNC_STATUSES:
        return pending

    claimed = await repository.claim_for_attempt(
        environment=environment,
        realm_id=realm_id,
        request_id=plan.request_id,
        claimed_at=_timestamp(clock),
    )

    try:
        payload = build_quickbooks_journal_entry_payload(plan)
    except QuickBooksJournalEntryPayloadError as exc:
        return await repository.mark_permanent_error(
            environment=environment,
            realm_id=realm_id,
            request_id=plan.request_id,
            expected_attempt_count=(claimed.attempt_count),
            error=QuickBooksSyncError(
                code="invalid_journal_entry_payload",
                message=_safe_message(str(exc)),
                retryable=False,
                occurred_at=_timestamp(clock),
            ),
        )

    try:
        result = await client.create_journal_entry(
            access_token=access_token,
            realm_id=realm_id,
            request_id=plan.request_id,
            payload=payload,
        )
    except QuickBooksApiProviderError as exc:
        retryable = exc.status_code in RETRYABLE_HTTP_STATUS_CODES
        error = QuickBooksSyncError(
            code=(
                exc.provider_code
                or (
                    f"http_{exc.status_code}"
                    if exc.status_code is not None
                    else "qbo_provider_error"
                )
            ),
            message=_safe_message(str(exc)),
            retryable=retryable,
            occurred_at=_timestamp(clock),
        )

        if retryable:
            return await repository.mark_retryable_error(
                environment=environment,
                realm_id=realm_id,
                request_id=plan.request_id,
                expected_attempt_count=(claimed.attempt_count),
                error=error,
            )

        return await repository.mark_permanent_error(
            environment=environment,
            realm_id=realm_id,
            request_id=plan.request_id,
            expected_attempt_count=(claimed.attempt_count),
            error=error,
        )
    except QuickBooksApiRequestError:
        return await repository.mark_retryable_error(
            environment=environment,
            realm_id=realm_id,
            request_id=plan.request_id,
            expected_attempt_count=(claimed.attempt_count),
            error=QuickBooksSyncError(
                code="qbo_transport_error",
                message=("QuickBooks could not be reached before a valid response was received."),
                retryable=True,
                occurred_at=_timestamp(clock),
            ),
        )
    except QuickBooksApiResponseError:
        return await repository.mark_retryable_error(
            environment=environment,
            realm_id=realm_id,
            request_id=plan.request_id,
            expected_attempt_count=(claimed.attempt_count),
            error=QuickBooksSyncError(
                code="qbo_invalid_response",
                message=(
                    "QuickBooks returned an invalid response "
                    "to the idempotent JournalEntry request."
                ),
                retryable=True,
                occurred_at=_timestamp(clock),
            ),
        )

    return await repository.mark_succeeded(
        environment=environment,
        realm_id=realm_id,
        request_id=plan.request_id,
        expected_attempt_count=claimed.attempt_count,
        qbo_transaction_id=result.id,
        qbo_sync_token=result.sync_token,
        completed_at=_timestamp(clock),
    )


def _timestamp(clock: Clock) -> datetime:
    """Require a timezone-aware orchestration timestamp."""

    value = clock()

    if value.tzinfo is None:
        raise ValueError("QuickBooks sync clock must return timezone-aware timestamps")

    return value.astimezone(UTC)


def _safe_message(value: str) -> str:
    """Normalize a secret-free persisted error message."""

    normalized = " ".join(value.split())

    if not normalized:
        return "QuickBooks synchronization failed."

    return normalized[:500]
