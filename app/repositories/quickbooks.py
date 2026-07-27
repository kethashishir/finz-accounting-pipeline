"""MongoDB persistence for QuickBooks OAuth state."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError
from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.db.client import MongoDocument
from app.models.quickbooks import (
    QuickBooksAuthorizationState,
    QuickBooksOAuthStateRecord,
)


class QuickBooksOAuthStatePersistenceError(RuntimeError):
    """Base error for persisted QuickBooks OAuth state."""


class QuickBooksOAuthStateConflictError(QuickBooksOAuthStatePersistenceError):
    """A nonce already stores different immutable claims."""


class QuickBooksOAuthStateNotRegisteredError(QuickBooksOAuthStatePersistenceError):
    """A callback references an unregistered OAuth state."""


class QuickBooksOAuthStateAlreadyConsumedError(QuickBooksOAuthStatePersistenceError):
    """A callback attempted to replay consumed OAuth state."""


class QuickBooksOAuthStateExpiredError(QuickBooksOAuthStatePersistenceError):
    """Persisted OAuth state expired before consumption."""


class CorruptQuickBooksOAuthStateError(QuickBooksOAuthStatePersistenceError):
    """A stored OAuth state document violates its schema."""


class QuickBooksOAuthStateRepository:
    """Register and atomically consume OAuth state nonces."""

    COLLECTION = "quickbooks_oauth_states"

    def __init__(
        self,
        database: AsyncDatabase[MongoDocument],
    ) -> None:
        self.states: AsyncCollection[MongoDocument] = database[self.COLLECTION]

    async def ensure_indexes(self) -> None:
        """Create automatic expiration for OAuth state."""

        await self.states.create_index(
            [("expires_at", ASCENDING)],
            name="ttl_qbo_oauth_state_expiration",
            expireAfterSeconds=0,
        )

    async def register(
        self,
        state: QuickBooksAuthorizationState,
    ) -> bool:
        """Insert state once or recognize an exact retry."""

        document = _state_to_document(state)
        identity_filter = {
            "_id": state.nonce,
            "version": state.version,
            "environment": state.environment.value,
            "issued_at": _mongodb_datetime(state.issued_at),
            "expires_at": _mongodb_datetime(state.expires_at),
            "consumed_at": None,
        }

        try:
            result = await self.states.update_one(
                identity_filter,
                {
                    "$setOnInsert": document,
                },
                upsert=True,
            )
        except DuplicateKeyError as exc:
            existing = await self.find_by_nonce(state.nonce)

            if existing is None:
                raise QuickBooksOAuthStateConflictError(
                    "QuickBooks OAuth state registration conflicted without a stored nonce"
                ) from exc

            if not _same_claims(existing, state):
                raise QuickBooksOAuthStateConflictError(
                    "QuickBooks OAuth nonce already stores different signed claims"
                ) from exc

            if existing.consumed_at is not None:
                raise (
                    QuickBooksOAuthStateAlreadyConsumedError(
                        "QuickBooks OAuth state was already consumed"
                    )
                ) from exc

            raise QuickBooksOAuthStateConflictError(
                "QuickBooks OAuth state could not be registered atomically"
            ) from exc

        return result.upserted_id is not None

    async def consume(
        self,
        state: QuickBooksAuthorizationState,
        *,
        consumed_at: datetime | None = None,
    ) -> QuickBooksOAuthStateRecord:
        """Atomically mark registered OAuth state as consumed."""

        consumption_time = _mongodb_datetime(consumed_at or datetime.now(UTC))
        issued_at = _mongodb_datetime(state.issued_at)
        expires_at = _mongodb_datetime(state.expires_at)

        document = await self.states.find_one_and_update(
            {
                "_id": state.nonce,
                "version": state.version,
                "environment": state.environment.value,
                "issued_at": issued_at,
                "expires_at": {
                    "$eq": expires_at,
                    "$gt": consumption_time,
                },
                "consumed_at": None,
            },
            {
                "$set": {
                    "consumed_at": consumption_time,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

        if document is not None:
            return _state_from_document(document)

        existing = await self.find_by_nonce(state.nonce)

        if existing is None:
            raise QuickBooksOAuthStateNotRegisteredError(
                "QuickBooks OAuth state was not registered"
            )

        if not _same_claims(existing, state):
            raise QuickBooksOAuthStateConflictError(
                "QuickBooks OAuth callback claims do not match the registered state"
            )

        if existing.consumed_at is not None:
            raise QuickBooksOAuthStateAlreadyConsumedError(
                "QuickBooks OAuth state was already consumed"
            )

        if existing.expires_at <= consumption_time:
            raise QuickBooksOAuthStateExpiredError(
                "QuickBooks OAuth state expired before consumption"
            )

        raise QuickBooksOAuthStateConflictError(
            "QuickBooks OAuth state could not be consumed atomically"
        )

    async def find_by_nonce(
        self,
        nonce: str,
    ) -> QuickBooksOAuthStateRecord | None:
        """Return one stored OAuth state by nonce."""

        document = await self.states.find_one(
            {
                "_id": nonce,
            }
        )

        if document is None:
            return None

        return _state_from_document(document)


def _state_to_document(
    state: QuickBooksAuthorizationState,
) -> MongoDocument:
    """Serialize signed OAuth claims for MongoDB."""

    return {
        "_id": state.nonce,
        "version": state.version,
        "environment": state.environment.value,
        "issued_at": _mongodb_datetime(state.issued_at),
        "expires_at": _mongodb_datetime(state.expires_at),
        "consumed_at": None,
    }


def _state_from_document(
    document: MongoDocument,
) -> QuickBooksOAuthStateRecord:
    """Validate and deserialize stored OAuth state."""

    try:
        return QuickBooksOAuthStateRecord(
            version=document["version"],
            nonce=document["_id"],
            environment=document["environment"],
            issued_at=document["issued_at"],
            expires_at=document["expires_at"],
            consumed_at=document.get("consumed_at"),
        )
    except (
        KeyError,
        TypeError,
        ValidationError,
    ) as exc:
        raise CorruptQuickBooksOAuthStateError("Stored QuickBooks OAuth state is invalid") from exc


def _same_claims(
    stored: QuickBooksOAuthStateRecord,
    supplied: QuickBooksAuthorizationState,
) -> bool:
    """Compare immutable signed claims at BSON precision."""

    return (
        stored.version == supplied.version
        and stored.nonce == supplied.nonce
        and stored.environment is supplied.environment
        and stored.issued_at == _mongodb_datetime(supplied.issued_at)
        and stored.expires_at == _mongodb_datetime(supplied.expires_at)
    )


def _mongodb_datetime(
    value: datetime,
) -> datetime:
    """Normalize a timestamp to BSON millisecond precision."""

    if value.utcoffset() is None:
        raise ValueError("QuickBooks OAuth timestamps must be timezone-aware")

    utc_value = value.astimezone(UTC)

    return utc_value.replace(microsecond=(utc_value.microsecond // 1000) * 1000)
