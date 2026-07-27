"""Integration tests for single-use QBO OAuth state."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.db.client import MongoDatabase
from app.models.quickbooks import (
    QuickBooksAuthorizationState,
    QuickBooksEnvironment,
    QuickBooksOAuthStateRecord,
)
from app.repositories.quickbooks import (
    QuickBooksOAuthStateAlreadyConsumedError,
    QuickBooksOAuthStateConflictError,
    QuickBooksOAuthStateExpiredError,
    QuickBooksOAuthStateNotRegisteredError,
    QuickBooksOAuthStateRepository,
)

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


@pytest.fixture
async def repository() -> AsyncIterator[QuickBooksOAuthStateRepository]:
    """Create an isolated OAuth-state collection."""

    settings = get_settings()
    database_name = f"{settings.mongodb_database[:28]}_qbo_state_{uuid4().hex[:16]}"
    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=database_name,
    )
    state_repository = QuickBooksOAuthStateRepository(mongodb.database)

    await state_repository.ensure_indexes()

    try:
        yield state_repository
    finally:
        await mongodb.client.drop_database(database_name)
        await mongodb.close()


def create_state(
    *,
    nonce: str = f"oauth-state-{'a' * 40}",
    environment: QuickBooksEnvironment = (QuickBooksEnvironment.SANDBOX),
    issued_at: datetime = NOW,
    expires_at: datetime = NOW + timedelta(minutes=10),
) -> QuickBooksAuthorizationState:
    """Create valid signed-state claims for persistence tests."""

    return QuickBooksAuthorizationState(
        nonce=nonce,
        environment=environment,
        issued_at=issued_at,
        expires_at=expires_at,
    )


async def test_repository_creates_ttl_index(
    repository: QuickBooksOAuthStateRepository,
) -> None:
    """Expired state records are scheduled for automatic removal."""

    index_cursor = await repository.states.list_indexes()
    indexes = {index["name"]: index async for index in index_cursor}

    ttl_index = indexes["ttl_qbo_oauth_state_expiration"]

    assert ttl_index["key"] == {
        "expires_at": 1,
    }
    assert ttl_index["expireAfterSeconds"] == 0


async def test_register_is_idempotent_for_exact_state(
    repository: QuickBooksOAuthStateRepository,
) -> None:
    """An exact registration retry does not create a duplicate."""

    state = create_state()

    assert await repository.register(state) is True
    assert await repository.register(state) is False

    stored = await repository.find_by_nonce(state.nonce)

    assert stored is not None
    assert stored.nonce == state.nonce
    assert stored.environment is state.environment
    assert stored.issued_at == state.issued_at
    assert stored.expires_at == state.expires_at
    assert stored.consumed_at is None


async def test_conflicting_registration_is_rejected(
    repository: QuickBooksOAuthStateRepository,
) -> None:
    """The same nonce cannot represent different signed claims."""

    original = create_state()
    conflicting = create_state(
        expires_at=NOW + timedelta(minutes=9),
    )

    assert await repository.register(original) is True

    with pytest.raises(
        QuickBooksOAuthStateConflictError,
        match="different signed claims",
    ):
        await repository.register(conflicting)


async def test_state_is_consumed_once_atomically(
    repository: QuickBooksOAuthStateRepository,
) -> None:
    """Successful consumption stores its callback timestamp."""

    state = create_state()

    await repository.register(state)
    consumed = await repository.consume(
        state,
        consumed_at=NOW + timedelta(minutes=1),
    )

    assert isinstance(
        consumed,
        QuickBooksOAuthStateRecord,
    )
    assert consumed.consumed_at == (NOW + timedelta(minutes=1))

    stored = await repository.find_by_nonce(state.nonce)

    assert stored == consumed


async def test_consumed_state_cannot_be_replayed(
    repository: QuickBooksOAuthStateRepository,
) -> None:
    """A second callback cannot reuse the same state."""

    state = create_state()

    await repository.register(state)
    await repository.consume(
        state,
        consumed_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(
        QuickBooksOAuthStateAlreadyConsumedError,
        match="already consumed",
    ):
        await repository.consume(
            state,
            consumed_at=NOW + timedelta(minutes=2),
        )


async def test_unknown_state_cannot_be_consumed(
    repository: QuickBooksOAuthStateRepository,
) -> None:
    """A valid signature alone is insufficient without registration."""

    with pytest.raises(
        QuickBooksOAuthStateNotRegisteredError,
        match="not registered",
    ):
        await repository.consume(
            create_state(),
            consumed_at=NOW + timedelta(minutes=1),
        )


async def test_expired_state_remains_unconsumed(
    repository: QuickBooksOAuthStateRepository,
) -> None:
    """An expired state fails without changing stored evidence."""

    state = create_state()

    await repository.register(state)

    with pytest.raises(
        QuickBooksOAuthStateExpiredError,
        match="expired",
    ):
        await repository.consume(
            state,
            consumed_at=state.expires_at,
        )

    stored = await repository.find_by_nonce(state.nonce)

    assert stored is not None
    assert stored.consumed_at is None


async def test_callback_claim_mismatch_is_rejected(
    repository: QuickBooksOAuthStateRepository,
) -> None:
    """Stored and callback claims must match exactly."""

    registered = create_state()
    mismatched = create_state(
        environment=QuickBooksEnvironment.PRODUCTION,
    )

    await repository.register(registered)

    with pytest.raises(
        QuickBooksOAuthStateConflictError,
        match="do not match",
    ):
        await repository.consume(
            mismatched,
            consumed_at=NOW + timedelta(minutes=1),
        )


async def test_concurrent_callbacks_have_one_winner(
    repository: QuickBooksOAuthStateRepository,
) -> None:
    """Atomic consumption permits exactly one callback."""

    state = create_state()

    await repository.register(state)

    results = await asyncio.gather(
        repository.consume(
            state,
            consumed_at=NOW + timedelta(minutes=1),
        ),
        repository.consume(
            state,
            consumed_at=NOW + timedelta(minutes=1),
        ),
        return_exceptions=True,
    )

    successful = [
        result
        for result in results
        if isinstance(
            result,
            QuickBooksOAuthStateRecord,
        )
    ]
    replay_errors = [
        result
        for result in results
        if isinstance(
            result,
            QuickBooksOAuthStateAlreadyConsumedError,
        )
    ]

    assert len(successful) == 1
    assert len(replay_errors) == 1


async def test_consumed_state_cannot_be_registered_again(
    repository: QuickBooksOAuthStateRepository,
) -> None:
    """Registration cannot reset already-consumed state."""

    state = create_state()

    await repository.register(state)
    await repository.consume(
        state,
        consumed_at=NOW + timedelta(minutes=1),
    )

    with pytest.raises(
        QuickBooksOAuthStateAlreadyConsumedError,
        match="already consumed",
    ):
        await repository.register(state)
