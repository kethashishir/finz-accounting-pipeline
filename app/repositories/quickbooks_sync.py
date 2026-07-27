"""MongoDB persistence for idempotent QuickBooks synchronization."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.models.quickbooks import QuickBooksEnvironment
from app.models.quickbooks_sync import (
    QuickBooksJournalEntryPlan,
    QuickBooksSyncError,
    QuickBooksSyncRecord,
    QuickBooksSyncStatus,
)


class QuickBooksSyncRepositoryError(RuntimeError):
    """Base error for QuickBooks synchronization persistence."""


class QuickBooksSyncConflictError(QuickBooksSyncRepositoryError):
    """A request, source, or QBO transaction is already owned."""


class QuickBooksSyncTransitionError(QuickBooksSyncRepositoryError):
    """The requested synchronization transition is not legal."""


class QuickBooksSyncRepository:
    """Persist and atomically transition QBO sync records."""

    def __init__(
        self,
        database: Any,
        *,
        collection_name: str = ("quickbooks_sync_records"),
    ) -> None:
        self.records = database[collection_name]

    async def ensure_indexes(self) -> None:
        """Create indexes that enforce posting idempotence."""

        await self.records.create_index(
            [
                ("environment", ASCENDING),
                ("realm_id", ASCENDING),
                ("plan.request_id", ASCENDING),
            ],
            name="uq_qbo_sync_company_request",
            unique=True,
        )
        await self.records.create_index(
            [
                ("environment", ASCENDING),
                ("realm_id", ASCENDING),
                (
                    "plan.sources.normalized_transaction_id",
                    ASCENDING,
                ),
            ],
            name="uq_qbo_sync_company_source",
            unique=True,
        )
        await self.records.create_index(
            [
                ("environment", ASCENDING),
                ("realm_id", ASCENDING),
                ("qbo_transaction_id", ASCENDING),
            ],
            name="uq_qbo_sync_company_transaction",
            unique=True,
            partialFilterExpression={
                "qbo_transaction_id": {
                    "$type": "string",
                },
            },
        )
        await self.records.create_index(
            [
                ("environment", ASCENDING),
                ("realm_id", ASCENDING),
                ("status", ASCENDING),
                ("updated_at", ASCENDING),
            ],
            name="ix_qbo_sync_status_updated",
        )

    async def create_pending(
        self,
        *,
        environment: QuickBooksEnvironment,
        realm_id: str,
        plan: QuickBooksJournalEntryPlan,
        created_at: datetime,
    ) -> QuickBooksSyncRecord:
        """Insert a pending plan or return its exact prior record."""

        record = QuickBooksSyncRecord(
            environment=environment,
            realm_id=realm_id,
            plan=plan,
            created_at=created_at,
            updated_at=created_at,
        )

        try:
            await self.records.insert_one(_record_to_document(record))
        except DuplicateKeyError as exc:
            existing = await self.find_by_request_id(
                environment=environment,
                realm_id=realm_id,
                request_id=plan.request_id,
            )

            if existing is not None:
                if existing.plan == plan:
                    return existing

                raise QuickBooksSyncConflictError(
                    "The QuickBooks request ID already belongs to a different immutable plan"
                ) from exc

            for source in plan.sources:
                conflicting = await self.find_by_source_id(
                    environment=environment,
                    realm_id=realm_id,
                    normalized_transaction_id=(source.normalized_transaction_id),
                )

                if conflicting is not None:
                    raise QuickBooksSyncConflictError(
                        "A normalized transaction is already owned by another QuickBooks posting"
                    ) from exc

            raise QuickBooksSyncConflictError(
                "The QuickBooks sync record conflicts with an existing unique value"
            ) from exc

        return record

    async def find_by_request_id(
        self,
        *,
        environment: QuickBooksEnvironment,
        realm_id: str,
        request_id: str,
    ) -> QuickBooksSyncRecord | None:
        """Find one posting by its deterministic request ID."""

        document = await self.records.find_one(
            {
                **_company_filter(
                    environment=environment,
                    realm_id=realm_id,
                ),
                "plan.request_id": request_id,
            }
        )

        return _optional_record(document)

    async def find_by_source_id(
        self,
        *,
        environment: QuickBooksEnvironment,
        realm_id: str,
        normalized_transaction_id: UUID,
    ) -> QuickBooksSyncRecord | None:
        """Find the posting that owns one normalized source."""

        document = await self.records.find_one(
            {
                **_company_filter(
                    environment=environment,
                    realm_id=realm_id,
                ),
                ("plan.sources.normalized_transaction_id"): str(normalized_transaction_id),
            }
        )

        return _optional_record(document)

    async def claim_for_attempt(
        self,
        *,
        environment: QuickBooksEnvironment,
        realm_id: str,
        request_id: str,
        claimed_at: datetime,
    ) -> QuickBooksSyncRecord:
        """Atomically claim a pending or retryable posting."""

        document = await self.records.find_one_and_update(
            {
                **_company_filter(
                    environment=environment,
                    realm_id=realm_id,
                ),
                "plan.request_id": request_id,
                "status": {
                    "$in": [
                        QuickBooksSyncStatus.PENDING.value,
                        (QuickBooksSyncStatus.RETRYABLE_ERROR.value),
                    ],
                },
                "created_at": {
                    "$lte": _utc_datetime(claimed_at),
                },
                "updated_at": {
                    "$lte": _utc_datetime(claimed_at),
                },
            },
            {
                "$set": {
                    "status": (QuickBooksSyncStatus.IN_PROGRESS.value),
                    "last_error": None,
                    "qbo_transaction_id": None,
                    "qbo_sync_token": None,
                    "updated_at": _utc_datetime(claimed_at),
                },
                "$inc": {
                    "attempt_count": 1,
                },
            },
            return_document=ReturnDocument.AFTER,
        )

        if document is None:
            raise QuickBooksSyncTransitionError("QuickBooks posting is not claimable")

        return _record_from_document(document)

    async def mark_succeeded(
        self,
        *,
        environment: QuickBooksEnvironment,
        realm_id: str,
        request_id: str,
        expected_attempt_count: int,
        qbo_transaction_id: str,
        qbo_sync_token: str,
        completed_at: datetime,
    ) -> QuickBooksSyncRecord:
        """Atomically retain successful QBO posting evidence."""

        if expected_attempt_count < 1:
            raise ValueError("Expected attempt count must be positive")

        try:
            document = await self.records.find_one_and_update(
                {
                    **_company_filter(
                        environment=environment,
                        realm_id=realm_id,
                    ),
                    "plan.request_id": request_id,
                    "status": (QuickBooksSyncStatus.IN_PROGRESS.value),
                    "attempt_count": (expected_attempt_count),
                    "updated_at": {
                        "$lte": _utc_datetime(completed_at),
                    },
                },
                {
                    "$set": {
                        "status": (QuickBooksSyncStatus.SUCCEEDED.value),
                        "qbo_transaction_id": (qbo_transaction_id),
                        "qbo_sync_token": (qbo_sync_token),
                        "last_error": None,
                        "updated_at": _utc_datetime(completed_at),
                    },
                },
                return_document=(ReturnDocument.AFTER),
            )
        except DuplicateKeyError as exc:
            raise QuickBooksSyncConflictError(
                "The QBO transaction ID is already linked to another sync record"
            ) from exc

        if document is None:
            raise QuickBooksSyncTransitionError("QuickBooks posting cannot transition to succeeded")

        return _record_from_document(document)

    async def mark_retryable_error(
        self,
        *,
        environment: QuickBooksEnvironment,
        realm_id: str,
        request_id: str,
        expected_attempt_count: int,
        error: QuickBooksSyncError,
    ) -> QuickBooksSyncRecord:
        """Atomically record a failure that may be retried."""

        if not error.retryable:
            raise ValueError("Retryable transition requires a retryable error")

        return await self._mark_error(
            environment=environment,
            realm_id=realm_id,
            request_id=request_id,
            expected_attempt_count=(expected_attempt_count),
            status=(QuickBooksSyncStatus.RETRYABLE_ERROR),
            error=error,
        )

    async def mark_permanent_error(
        self,
        *,
        environment: QuickBooksEnvironment,
        realm_id: str,
        request_id: str,
        expected_attempt_count: int,
        error: QuickBooksSyncError,
    ) -> QuickBooksSyncRecord:
        """Atomically record a non-retryable posting failure."""

        if error.retryable:
            raise ValueError("Permanent transition requires a non-retryable error")

        return await self._mark_error(
            environment=environment,
            realm_id=realm_id,
            request_id=request_id,
            expected_attempt_count=(expected_attempt_count),
            status=(QuickBooksSyncStatus.PERMANENT_ERROR),
            error=error,
        )

    async def _mark_error(
        self,
        *,
        environment: QuickBooksEnvironment,
        realm_id: str,
        request_id: str,
        expected_attempt_count: int,
        status: QuickBooksSyncStatus,
        error: QuickBooksSyncError,
    ) -> QuickBooksSyncRecord:
        """Transition one claimed posting to a failure state."""

        if expected_attempt_count < 1:
            raise ValueError("Expected attempt count must be positive")

        document = await self.records.find_one_and_update(
            {
                **_company_filter(
                    environment=environment,
                    realm_id=realm_id,
                ),
                "plan.request_id": request_id,
                "status": (QuickBooksSyncStatus.IN_PROGRESS.value),
                "attempt_count": expected_attempt_count,
                "updated_at": {
                    "$lte": _utc_datetime(error.occurred_at),
                },
            },
            {
                "$set": {
                    "status": status.value,
                    "qbo_transaction_id": None,
                    "qbo_sync_token": None,
                    "last_error": (_error_to_document(error)),
                    "updated_at": _utc_datetime(error.occurred_at),
                },
            },
            return_document=ReturnDocument.AFTER,
        )

        if document is None:
            raise QuickBooksSyncTransitionError(
                f"QuickBooks posting cannot transition to {status.value}"
            )

        return _record_from_document(document)


def _company_filter(
    *,
    environment: QuickBooksEnvironment,
    realm_id: str,
) -> dict[str, str]:
    """Build a normalized QBO company filter."""

    normalized_realm_id = realm_id.strip()

    if (
        not normalized_realm_id
        or not normalized_realm_id.isascii()
        or not normalized_realm_id.isdigit()
        or len(normalized_realm_id) > 64
    ):
        raise ValueError("QuickBooks realm ID must contain only digits")

    return {
        "environment": environment.value,
        "realm_id": normalized_realm_id,
    }


def _record_to_document(
    record: QuickBooksSyncRecord,
) -> dict[str, Any]:
    """Convert a validated record to Mongo-compatible data."""

    document = record.model_dump(mode="json")
    document["created_at"] = _utc_datetime(record.created_at)
    document["updated_at"] = _utc_datetime(record.updated_at)

    return document


def _error_to_document(
    error: QuickBooksSyncError,
) -> dict[str, Any]:
    """Convert a secret-free sync error for MongoDB."""

    document = error.model_dump(mode="json")
    document["occurred_at"] = _utc_datetime(error.occurred_at)

    return document


def _optional_record(
    document: dict[str, Any] | None,
) -> QuickBooksSyncRecord | None:
    """Validate an optional MongoDB sync document."""

    if document is None:
        return None

    return _record_from_document(document)


def _record_from_document(
    document: dict[str, Any],
) -> QuickBooksSyncRecord:
    """Validate one MongoDB sync document."""

    payload = dict(document)
    payload.pop("_id", None)

    for field_name in (
        "created_at",
        "updated_at",
    ):
        value = payload.get(field_name)

        if isinstance(value, datetime):
            payload[field_name] = _utc_datetime(value)

    error = payload.get("last_error")

    if isinstance(error, dict):
        occurred_at = error.get("occurred_at")

        if isinstance(occurred_at, datetime):
            error["occurred_at"] = _utc_datetime(occurred_at)

    return QuickBooksSyncRecord.model_validate(payload)


def _utc_datetime(value: datetime) -> datetime:
    """Normalize an aware timestamp to UTC."""

    if value.tzinfo is None:
        raise ValueError("QuickBooks sync timestamps must be timezone-aware")

    return value.astimezone(UTC)
