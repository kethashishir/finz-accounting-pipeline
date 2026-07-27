"""MongoDB persistence for encrypted QuickBooks connections."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import ValidationError
from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.db.client import MongoDocument
from app.models.quickbooks import (
    EncryptedQuickBooksTokenSet,
    QuickBooksConnectionRecord,
    QuickBooksEnvironment,
)


class QuickBooksConnectionPersistenceError(RuntimeError):
    """Base error for persisted QBO connections."""


class QuickBooksConnectionConflictError(QuickBooksConnectionPersistenceError):
    """A company already stores different initial token evidence."""


class QuickBooksConnectionNotFoundError(QuickBooksConnectionPersistenceError):
    """The requested QBO company connection does not exist."""


class StaleQuickBooksConnectionRevisionError(QuickBooksConnectionPersistenceError):
    """A token rotation used an outdated connection revision."""


class QuickBooksTokenRollbackError(QuickBooksConnectionPersistenceError):
    """A rotation attempted to restore older token material."""


class CorruptQuickBooksConnectionError(QuickBooksConnectionPersistenceError):
    """A stored QBO connection violates its schema."""


class QuickBooksConnectionRepository:
    """Persist encrypted QBO tokens with optimistic rotation."""

    COLLECTION = "quickbooks_connections"

    def __init__(
        self,
        database: AsyncDatabase[MongoDocument],
    ) -> None:
        self.connections: AsyncCollection[MongoDocument] = database[self.COLLECTION]

    async def ensure_indexes(self) -> None:
        """Create company uniqueness and expiration indexes."""

        await self.connections.create_index(
            [
                ("environment", ASCENDING),
                ("realm_id", ASCENDING),
            ],
            name="uq_qbo_connection_environment_realm",
            unique=True,
        )

        await self.connections.create_index(
            [
                ("access_token_expires_at", ASCENDING),
            ],
            name="ix_qbo_connection_access_expiration",
        )

    async def save_initial(
        self,
        encrypted: EncryptedQuickBooksTokenSet,
        *,
        stored_at: datetime | None = None,
    ) -> bool:
        """Insert a company connection or recognize an exact retry."""

        storage_time = _mongodb_datetime(stored_at or datetime.now(UTC))
        issued_at = _mongodb_datetime(encrypted.issued_at)

        if storage_time < issued_at:
            raise ValueError("QuickBooks connection cannot be stored before its tokens are issued")

        record = _build_record(
            encrypted,
            revision=1,
            created_at=storage_time,
            updated_at=storage_time,
        )
        document = _record_to_document(record)

        try:
            result = await self.connections.update_one(
                {
                    "_id": _connection_id(
                        encrypted.environment,
                        encrypted.realm_id,
                    )
                },
                {
                    "$setOnInsert": document,
                },
                upsert=True,
            )
        except DuplicateKeyError as exc:
            existing = await self.find(
                environment=encrypted.environment,
                realm_id=encrypted.realm_id,
            )

            if existing is not None and _same_encrypted_payload(
                existing,
                encrypted,
            ):
                return False

            raise QuickBooksConnectionConflictError(
                "QuickBooks company already stores different initial encrypted tokens"
            ) from exc

        if result.upserted_id is not None:
            return True

        existing = await self.find(
            environment=encrypted.environment,
            realm_id=encrypted.realm_id,
        )

        if existing is not None and _same_encrypted_payload(
            existing,
            encrypted,
        ):
            return False

        raise QuickBooksConnectionConflictError(
            "QuickBooks company already stores different initial encrypted tokens"
        )

    async def rotate_tokens(
        self,
        encrypted: EncryptedQuickBooksTokenSet,
        *,
        expected_revision: int,
        stored_at: datetime | None = None,
    ) -> QuickBooksConnectionRecord:
        """Atomically replace tokens using optimistic concurrency."""

        if isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValueError("expected_revision must be at least 1")

        update_time = _mongodb_datetime(stored_at or datetime.now(UTC))
        issued_at = _mongodb_datetime(encrypted.issued_at)

        if update_time < issued_at:
            raise ValueError("QuickBooks connection cannot store tokens before they are issued")

        connection_id = _connection_id(
            encrypted.environment,
            encrypted.realm_id,
        )
        updated_fields = _encrypted_fields(encrypted)
        updated_fields["updated_at"] = update_time

        document = await self.connections.find_one_and_update(
            {
                "_id": connection_id,
                "revision": expected_revision,
                "issued_at": {
                    "$lt": issued_at,
                },
            },
            {
                "$set": updated_fields,
                "$inc": {
                    "revision": 1,
                },
            },
            return_document=ReturnDocument.AFTER,
        )

        if document is not None:
            return _record_from_document(document)

        existing = await self.find(
            environment=encrypted.environment,
            realm_id=encrypted.realm_id,
        )

        if existing is None:
            raise QuickBooksConnectionNotFoundError("QuickBooks company connection does not exist")

        if existing.revision == expected_revision + 1 and _same_encrypted_payload(
            existing,
            encrypted,
        ):
            return existing

        if existing.revision == expected_revision and issued_at <= existing.issued_at:
            raise QuickBooksTokenRollbackError(
                "QuickBooks token rotation must use a newer issuance timestamp"
            )

        raise StaleQuickBooksConnectionRevisionError(
            "QuickBooks connection changed before token rotation could be saved"
        )

    async def find(
        self,
        *,
        environment: QuickBooksEnvironment,
        realm_id: str,
    ) -> QuickBooksConnectionRecord | None:
        """Return one encrypted company connection."""

        document = await self.connections.find_one(
            {
                "_id": _connection_id(
                    environment,
                    realm_id,
                )
            }
        )

        if document is None:
            return None

        return _record_from_document(document)


def _build_record(
    encrypted: EncryptedQuickBooksTokenSet,
    *,
    revision: int,
    created_at: datetime,
    updated_at: datetime,
) -> QuickBooksConnectionRecord:
    """Build a persistence record at BSON precision."""

    return QuickBooksConnectionRecord(
        version=encrypted.version,
        environment=encrypted.environment,
        realm_id=encrypted.realm_id,
        token_type=encrypted.token_type,
        access_token_ciphertext=(encrypted.access_token_ciphertext),
        refresh_token_ciphertext=(encrypted.refresh_token_ciphertext),
        key_fingerprint=encrypted.key_fingerprint,
        issued_at=_mongodb_datetime(encrypted.issued_at),
        access_token_expires_at=_mongodb_datetime(encrypted.access_token_expires_at),
        refresh_token_expires_at=_mongodb_datetime(encrypted.refresh_token_expires_at),
        revision=revision,
        created_at=_mongodb_datetime(created_at),
        updated_at=_mongodb_datetime(updated_at),
    )


def _record_to_document(
    record: QuickBooksConnectionRecord,
) -> MongoDocument:
    """Serialize one encrypted QBO connection."""

    return {
        "_id": _connection_id(
            record.environment,
            record.realm_id,
        ),
        **_encrypted_fields(record),
        "revision": record.revision,
        "created_at": _mongodb_datetime(record.created_at),
        "updated_at": _mongodb_datetime(record.updated_at),
    }


def _encrypted_fields(
    encrypted: EncryptedQuickBooksTokenSet,
) -> MongoDocument:
    """Return encrypted token fields safe for MongoDB."""

    return {
        "version": encrypted.version,
        "environment": encrypted.environment.value,
        "realm_id": encrypted.realm_id,
        "token_type": encrypted.token_type,
        "access_token_ciphertext": (encrypted.access_token_ciphertext),
        "refresh_token_ciphertext": (encrypted.refresh_token_ciphertext),
        "key_fingerprint": encrypted.key_fingerprint,
        "issued_at": _mongodb_datetime(encrypted.issued_at),
        "access_token_expires_at": _mongodb_datetime(encrypted.access_token_expires_at),
        "refresh_token_expires_at": _mongodb_datetime(encrypted.refresh_token_expires_at),
    }


def _record_from_document(
    document: MongoDocument,
) -> QuickBooksConnectionRecord:
    """Validate and deserialize a stored connection."""

    try:
        return QuickBooksConnectionRecord(
            version=document["version"],
            environment=document["environment"],
            realm_id=document["realm_id"],
            token_type=document["token_type"],
            access_token_ciphertext=(document["access_token_ciphertext"]),
            refresh_token_ciphertext=(document["refresh_token_ciphertext"]),
            key_fingerprint=document["key_fingerprint"],
            issued_at=document["issued_at"],
            access_token_expires_at=(document["access_token_expires_at"]),
            refresh_token_expires_at=(document["refresh_token_expires_at"]),
            revision=document["revision"],
            created_at=document["created_at"],
            updated_at=document["updated_at"],
        )
    except (
        KeyError,
        TypeError,
        ValidationError,
    ) as exc:
        raise CorruptQuickBooksConnectionError("Stored QuickBooks connection is invalid") from exc


def _same_encrypted_payload(
    stored: QuickBooksConnectionRecord,
    supplied: EncryptedQuickBooksTokenSet,
) -> bool:
    """Compare encrypted token evidence at BSON precision."""

    return (
        stored.version == supplied.version
        and stored.environment is supplied.environment
        and stored.realm_id == supplied.realm_id
        and stored.token_type == supplied.token_type
        and stored.access_token_ciphertext == supplied.access_token_ciphertext
        and stored.refresh_token_ciphertext == supplied.refresh_token_ciphertext
        and stored.key_fingerprint == supplied.key_fingerprint
        and stored.issued_at == _mongodb_datetime(supplied.issued_at)
        and stored.access_token_expires_at == _mongodb_datetime(supplied.access_token_expires_at)
        and stored.refresh_token_expires_at == _mongodb_datetime(supplied.refresh_token_expires_at)
    )


def _connection_id(
    environment: QuickBooksEnvironment,
    realm_id: str,
) -> str:
    """Return a deterministic company connection identifier."""

    return f"{environment.value}:{realm_id}"


def _mongodb_datetime(
    value: datetime,
) -> datetime:
    """Normalize timestamps to BSON millisecond precision."""

    if value.utcoffset() is None:
        raise ValueError("QuickBooks connection timestamps must be timezone-aware")

    utc_value = value.astimezone(UTC)

    return utc_value.replace(microsecond=(utc_value.microsecond // 1000) * 1000)
