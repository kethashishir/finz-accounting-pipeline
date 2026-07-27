"""Integration tests for encrypted QBO connection persistence."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from app.db.client import MongoDatabase
from app.models.quickbooks import (
    EncryptedQuickBooksTokenSet,
    QuickBooksConnectionRecord,
    QuickBooksEnvironment,
    QuickBooksTokenSet,
)
from app.repositories.quickbooks_connection import (
    QuickBooksConnectionConflictError,
    QuickBooksConnectionNotFoundError,
    QuickBooksConnectionRepository,
    QuickBooksTokenRollbackError,
    StaleQuickBooksConnectionRevisionError,
)
from app.services.quickbooks.token_crypto import QuickBooksTokenCipher

NOW = datetime(
    2030,
    1,
    1,
    12,
    0,
    0,
    123000,
    tzinfo=UTC,
)
REALM_ID = "9341456789012345"
ACCESS_TOKEN = "repository-access-token"
REFRESH_TOKEN = "repository-refresh-token"
TOKEN_KEY = "repository-token-encryption-key-0123456789abcdef"


@pytest.fixture
async def repository() -> AsyncIterator[QuickBooksConnectionRepository]:
    """Create an isolated encrypted connection collection."""

    settings = get_settings()
    database_name = f"{settings.mongodb_database[:28]}_qbo_conn_{uuid4().hex[:16]}"
    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=database_name,
    )
    connection_repository = QuickBooksConnectionRepository(mongodb.database)

    await connection_repository.ensure_indexes()

    try:
        yield connection_repository
    finally:
        await mongodb.client.drop_database(database_name)
        await mongodb.close()


def token_cipher() -> QuickBooksTokenCipher:
    """Create the deterministic test encryption boundary."""

    return QuickBooksTokenCipher.from_secret(SecretStr(TOKEN_KEY))


def encrypted_tokens(
    *,
    issued_at: datetime = NOW,
    access_token: str = ACCESS_TOKEN,
    refresh_token: str = REFRESH_TOKEN,
) -> EncryptedQuickBooksTokenSet:
    """Create encrypted test-only OAuth tokens."""

    plaintext = QuickBooksTokenSet(
        environment=QuickBooksEnvironment.SANDBOX,
        realm_id=REALM_ID,
        access_token=SecretStr(access_token),
        refresh_token=SecretStr(refresh_token),
        issued_at=issued_at,
        access_token_expires_at=(issued_at + timedelta(hours=1)),
        refresh_token_expires_at=(issued_at + timedelta(days=100)),
    )

    return token_cipher().encrypt_token_set(plaintext)


async def test_repository_creates_connection_indexes(
    repository: QuickBooksConnectionRepository,
) -> None:
    """Connections are unique and queryable by expiration."""

    index_cursor = await repository.connections.list_indexes()
    indexes = {index["name"]: index async for index in index_cursor}

    company_index = indexes["uq_qbo_connection_environment_realm"]
    expiration_index = indexes["ix_qbo_connection_access_expiration"]

    assert company_index["unique"] is True
    assert company_index["key"] == {
        "environment": 1,
        "realm_id": 1,
    }
    assert expiration_index["key"] == {
        "access_token_expires_at": 1,
    }


async def test_initial_connection_round_trips_encrypted(
    repository: QuickBooksConnectionRepository,
) -> None:
    """MongoDB stores ciphertext while decryption recovers tokens."""

    encrypted = encrypted_tokens()
    stored_at = NOW + timedelta(seconds=1)

    inserted = await repository.save_initial(
        encrypted,
        stored_at=stored_at,
    )

    assert inserted is True

    stored = await repository.find(
        environment=QuickBooksEnvironment.SANDBOX,
        realm_id=REALM_ID,
    )

    assert stored is not None
    assert stored.revision == 1
    assert stored.created_at == stored_at
    assert stored.updated_at == stored_at

    decrypted = token_cipher().decrypt_token_set(stored)

    assert decrypted.access_token.get_secret_value() == ACCESS_TOKEN
    assert decrypted.refresh_token.get_secret_value() == REFRESH_TOKEN

    raw_document = await repository.connections.find_one(
        {
            "_id": f"sandbox:{REALM_ID}",
        }
    )

    assert raw_document is not None

    raw_text = repr(raw_document)

    assert ACCESS_TOKEN not in raw_text
    assert REFRESH_TOKEN not in raw_text


async def test_initial_save_is_idempotent(
    repository: QuickBooksConnectionRepository,
) -> None:
    """An exact persistence retry does not duplicate a company."""

    encrypted = encrypted_tokens()

    first = await repository.save_initial(
        encrypted,
        stored_at=NOW + timedelta(seconds=1),
    )
    retry = await repository.save_initial(
        encrypted,
        stored_at=NOW + timedelta(seconds=2),
    )

    assert first is True
    assert retry is False


async def test_different_initial_tokens_conflict(
    repository: QuickBooksConnectionRepository,
) -> None:
    """Existing company tokens cannot be silently overwritten."""

    original = encrypted_tokens()
    conflicting = encrypted_tokens(
        access_token="different-access-token",
    )

    await repository.save_initial(
        original,
        stored_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(
        QuickBooksConnectionConflictError,
        match="different initial",
    ):
        await repository.save_initial(
            conflicting,
            stored_at=NOW + timedelta(seconds=2),
        )


async def test_rotation_increments_revision(
    repository: QuickBooksConnectionRepository,
) -> None:
    """A newer token set replaces ciphertext atomically."""

    initial = encrypted_tokens()
    rotated = encrypted_tokens(
        issued_at=NOW + timedelta(hours=1),
        access_token="rotated-access-token",
        refresh_token="rotated-refresh-token",
    )
    created_at = NOW + timedelta(seconds=1)
    updated_at = NOW + timedelta(
        hours=1,
        seconds=1,
    )

    await repository.save_initial(
        initial,
        stored_at=created_at,
    )

    result = await repository.rotate_tokens(
        rotated,
        expected_revision=1,
        stored_at=updated_at,
    )

    assert result.revision == 2
    assert result.created_at == created_at
    assert result.updated_at == updated_at
    assert result.issued_at == (NOW + timedelta(hours=1))

    decrypted = token_cipher().decrypt_token_set(result)

    assert decrypted.access_token.get_secret_value() == "rotated-access-token"
    assert decrypted.refresh_token.get_secret_value() == "rotated-refresh-token"


async def test_rotation_retry_is_idempotent(
    repository: QuickBooksConnectionRepository,
) -> None:
    """Retrying an exact completed rotation returns stored evidence."""

    initial = encrypted_tokens()
    rotated = encrypted_tokens(
        issued_at=NOW + timedelta(hours=1),
        access_token="rotated-access-token",
        refresh_token="rotated-refresh-token",
    )

    await repository.save_initial(
        initial,
        stored_at=NOW + timedelta(seconds=1),
    )

    first = await repository.rotate_tokens(
        rotated,
        expected_revision=1,
        stored_at=NOW + timedelta(hours=1, seconds=1),
    )
    retry = await repository.rotate_tokens(
        rotated,
        expected_revision=1,
        stored_at=NOW + timedelta(hours=1, seconds=2),
    )

    assert retry == first


async def test_missing_connection_cannot_rotate(
    repository: QuickBooksConnectionRepository,
) -> None:
    """Token rotation requires existing company evidence."""

    with pytest.raises(
        QuickBooksConnectionNotFoundError,
        match="does not exist",
    ):
        await repository.rotate_tokens(
            encrypted_tokens(issued_at=NOW + timedelta(hours=1)),
            expected_revision=1,
            stored_at=NOW + timedelta(hours=1, seconds=1),
        )


async def test_stale_revision_cannot_overwrite_tokens(
    repository: QuickBooksConnectionRepository,
) -> None:
    """An outdated writer cannot replace a completed rotation."""

    initial = encrypted_tokens()
    first_rotation = encrypted_tokens(
        issued_at=NOW + timedelta(hours=1),
        access_token="first-rotation-access",
        refresh_token="first-rotation-refresh",
    )
    stale_rotation = encrypted_tokens(
        issued_at=NOW + timedelta(hours=2),
        access_token="stale-rotation-access",
        refresh_token="stale-rotation-refresh",
    )

    await repository.save_initial(
        initial,
        stored_at=NOW + timedelta(seconds=1),
    )
    await repository.rotate_tokens(
        first_rotation,
        expected_revision=1,
        stored_at=NOW + timedelta(hours=1, seconds=1),
    )

    with pytest.raises(
        StaleQuickBooksConnectionRevisionError,
        match="changed before",
    ):
        await repository.rotate_tokens(
            stale_rotation,
            expected_revision=1,
            stored_at=NOW + timedelta(hours=2, seconds=1),
        )


async def test_older_tokens_cannot_replace_current_tokens(
    repository: QuickBooksConnectionRepository,
) -> None:
    """A rotation cannot roll the connection backward."""

    initial = encrypted_tokens()

    await repository.save_initial(
        initial,
        stored_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(
        QuickBooksTokenRollbackError,
        match="newer issuance timestamp",
    ):
        await repository.rotate_tokens(
            encrypted_tokens(
                issued_at=NOW,
                access_token="rollback-access",
                refresh_token="rollback-refresh",
            ),
            expected_revision=1,
            stored_at=NOW + timedelta(seconds=2),
        )


async def test_concurrent_rotations_have_one_winner(
    repository: QuickBooksConnectionRepository,
) -> None:
    """Optimistic concurrency permits one token replacement."""

    initial = encrypted_tokens()
    first = encrypted_tokens(
        issued_at=NOW + timedelta(hours=1),
        access_token="concurrent-access-one",
        refresh_token="concurrent-refresh-one",
    )
    second = encrypted_tokens(
        issued_at=NOW + timedelta(hours=1),
        access_token="concurrent-access-two",
        refresh_token="concurrent-refresh-two",
    )

    await repository.save_initial(
        initial,
        stored_at=NOW + timedelta(seconds=1),
    )

    results = await asyncio.gather(
        repository.rotate_tokens(
            first,
            expected_revision=1,
            stored_at=NOW + timedelta(hours=1, seconds=1),
        ),
        repository.rotate_tokens(
            second,
            expected_revision=1,
            stored_at=NOW + timedelta(hours=1, seconds=1),
        ),
        return_exceptions=True,
    )

    successful = [
        result
        for result in results
        if isinstance(
            result,
            QuickBooksConnectionRecord,
        )
    ]
    stale_errors = [
        result
        for result in results
        if isinstance(
            result,
            StaleQuickBooksConnectionRevisionError,
        )
    ]

    assert len(successful) == 1
    assert len(stale_errors) == 1


async def test_connection_timestamps_use_bson_precision(
    repository: QuickBooksConnectionRepository,
) -> None:
    """Stored connection timestamps round-trip by milliseconds."""

    precise_issue = NOW.replace(microsecond=123456)
    precise_store = (precise_issue + timedelta(seconds=1)).replace(microsecond=987654)
    encrypted = encrypted_tokens(issued_at=precise_issue)

    await repository.save_initial(
        encrypted,
        stored_at=precise_store,
    )

    stored = await repository.find(
        environment=QuickBooksEnvironment.SANDBOX,
        realm_id=REALM_ID,
    )

    assert stored is not None
    assert stored.issued_at.microsecond == 123000
    assert stored.created_at.microsecond == 987000
    assert stored.updated_at.microsecond == 987000
