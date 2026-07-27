"""Guarded serial execution of QuickBooks synchronization plans."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr

from app.models.quickbooks import QuickBooksEnvironment
from app.models.quickbooks_sync import (
    QuickBooksJournalEntryPlan,
    QuickBooksSyncRecord,
    QuickBooksSyncStatus,
)
from app.services.quickbooks.sync_orchestrator import (
    sync_quickbooks_journal_entry,
)

LIVE_SYNC_CONFIRMATION = "BRIGHTFIX-SANDBOX-LIVE-SYNC"
AUTHENTICATION_ERROR_CODES = frozenset(
    {
        "3200",
        "http_401",
    }
)
RETRY_BACKOFF_SECONDS = (
    2.0,
    4.0,
)

SyncOne = Callable[..., Awaitable[QuickBooksSyncRecord]]
SuccessLookup = Callable[[str], Awaitable[bool]]
TokenRefresh = Callable[[], Awaitable[SecretStr]]
Sleeper = Callable[[float], Awaitable[None]]
ProgressCallback = Callable[[int, int], None]


class QuickBooksLiveSyncError(RuntimeError):
    """A guarded live synchronization run could not finish."""


@dataclass(frozen=True, slots=True)
class QuickBooksLiveSyncResult:
    """Safe aggregate outcome for one complete live run."""

    plan_count: int
    newly_succeeded: int
    reused_succeeded: int
    retry_attempts: int
    token_refreshes: int


def require_live_sync_confirmation(
    supplied: str,
) -> None:
    """Require the exact deliberate sandbox-write phrase."""

    if supplied.strip() != LIVE_SYNC_CONFIRMATION:
        raise QuickBooksLiveSyncError(
            "Live QuickBooks synchronization was not "
            "confirmed. Supply the exact sandbox "
            "confirmation phrase."
        )


async def synchronize_quickbooks_inventory(
    *,
    repository: Any,
    client: Any,
    environment: QuickBooksEnvironment,
    realm_id: str,
    access_token: SecretStr,
    plans: tuple[QuickBooksJournalEntryPlan, ...],
    is_already_succeeded: SuccessLookup,
    refresh_access_token: TokenRefresh | None = None,
    sleep: Sleeper,
    progress: ProgressCallback | None = None,
    max_attempts: int = 3,
    success_delay_seconds: float = 0.0,
    sync_one: SyncOne = (sync_quickbooks_journal_entry),
) -> QuickBooksLiveSyncResult:
    """Synchronize plans serially using stable request IDs."""

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one")

    if success_delay_seconds < 0:
        raise ValueError("success_delay_seconds cannot be negative")

    current_access_token = access_token
    newly_succeeded = 0
    reused_succeeded = 0
    retry_attempts = 0
    token_refreshes = 0
    total = len(plans)

    for index, plan in enumerate(
        plans,
        start=1,
    ):
        already_succeeded = await is_already_succeeded(plan.request_id)
        final_record: QuickBooksSyncRecord | None = None
        refreshed_for_plan = False

        for attempt in range(
            1,
            max_attempts + 1,
        ):
            record = await sync_one(
                repository=repository,
                client=client,
                environment=environment,
                realm_id=realm_id,
                access_token=current_access_token,
                plan=plan,
            )

            if record.status is QuickBooksSyncStatus.SUCCEEDED:
                final_record = record
                break

            error = record.last_error
            error_code = error.code if error is not None else "missing_sync_error"
            error_message = (
                error.message
                if error is not None
                else ("QuickBooks synchronization returned a nonterminal state without an error.")
            )

            if record.status is QuickBooksSyncStatus.PERMANENT_ERROR:
                raise QuickBooksLiveSyncError(
                    "Permanent QuickBooks synchronization "
                    f"failure for request {plan.request_id}: "
                    f"{error_code}: {error_message}"
                )

            if record.status is not QuickBooksSyncStatus.RETRYABLE_ERROR:
                raise QuickBooksLiveSyncError(
                    "Unexpected QuickBooks synchronization "
                    f"state for request {plan.request_id}: "
                    f"{record.status.value}"
                )

            if attempt == max_attempts:
                raise QuickBooksLiveSyncError(
                    "Retry limit reached for QuickBooks "
                    f"request {plan.request_id}: "
                    f"{error_code}: {error_message}"
                )

            retry_attempts += 1

            if (
                error_code in AUTHENTICATION_ERROR_CODES
                and refresh_access_token is not None
                and not refreshed_for_plan
            ):
                current_access_token = await refresh_access_token()
                token_refreshes += 1
                refreshed_for_plan = True
                continue

            backoff_index = min(
                attempt - 1,
                len(RETRY_BACKOFF_SECONDS) - 1,
            )
            await sleep(RETRY_BACKOFF_SECONDS[backoff_index])

        if final_record is None:
            raise QuickBooksLiveSyncError(
                "QuickBooks synchronization ended without terminal evidence"
            )

        if already_succeeded:
            reused_succeeded += 1
        else:
            newly_succeeded += 1

        if progress is not None:
            progress(index, total)

        if success_delay_seconds > 0 and index < total:
            await sleep(success_delay_seconds)

    return QuickBooksLiveSyncResult(
        plan_count=total,
        newly_succeeded=newly_succeeded,
        reused_succeeded=reused_succeeded,
        retry_attempts=retry_attempts,
        token_refreshes=token_refreshes,
    )
