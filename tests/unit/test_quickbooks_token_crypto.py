"""Tests for authenticated QuickBooks token encryption."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr, ValidationError

from app.models.quickbooks import (
    QuickBooksEnvironment,
    QuickBooksTokenSet,
)
from app.services.quickbooks.token_crypto import (
    QuickBooksTokenCipher,
    QuickBooksTokenDecryptionError,
    QuickBooksTokenEncryptionError,
)

NOW = datetime(
    2030,
    1,
    1,
    12,
    0,
    tzinfo=UTC,
)
ACCESS_TOKEN = "plain-access-token-value"
REFRESH_TOKEN = "plain-refresh-token-value"
KEY_ONE = "test-token-encryption-key-one-0123456789abcdef"
KEY_TWO = "test-token-encryption-key-two-fedcba9876543210"


def token_set() -> QuickBooksTokenSet:
    """Create a valid plaintext token set."""

    return QuickBooksTokenSet(
        environment=QuickBooksEnvironment.SANDBOX,
        realm_id="9341456789012345",
        access_token=SecretStr(ACCESS_TOKEN),
        refresh_token=SecretStr(REFRESH_TOKEN),
        issued_at=NOW,
        access_token_expires_at=(NOW + timedelta(hours=1)),
        refresh_token_expires_at=(NOW + timedelta(days=100)),
    )


def cipher(
    key: str = KEY_ONE,
) -> QuickBooksTokenCipher:
    """Create a test token cipher."""

    return QuickBooksTokenCipher.from_secret(SecretStr(key))


def test_plaintext_token_model_redacts_secrets() -> None:
    """Representations and JSON never expose plaintext tokens."""

    tokens = token_set()
    representation = repr(tokens)
    serialized = tokens.model_dump_json()

    for secret in (
        ACCESS_TOKEN,
        REFRESH_TOKEN,
    ):
        assert secret not in representation
        assert secret not in serialized

    assert tokens.access_token.get_secret_value() == ACCESS_TOKEN


def test_realm_id_must_be_numeric() -> None:
    """QBO company identifiers cannot contain arbitrary text."""

    values = token_set().model_dump()
    values["realm_id"] = "company-example"

    with pytest.raises(
        ValidationError,
        match="realm_id",
    ):
        QuickBooksTokenSet.model_validate(values)


def test_token_timestamps_must_be_timezone_aware() -> None:
    """Naive expiration metadata is rejected."""

    values = token_set().model_dump()
    values["issued_at"] = NOW.replace(tzinfo=None)

    with pytest.raises(
        ValidationError,
        match="timezone-aware",
    ):
        QuickBooksTokenSet.model_validate(values)


def test_access_token_must_expire_after_issue() -> None:
    """An already-expired access token is invalid evidence."""

    values = token_set().model_dump()
    values["access_token_expires_at"] = NOW

    with pytest.raises(
        ValidationError,
        match="access token must expire after",
    ):
        QuickBooksTokenSet.model_validate(values)


def test_refresh_token_must_outlive_access_token() -> None:
    """Refresh metadata must permit access-token renewal."""

    values = token_set().model_dump()
    values["refresh_token_expires_at"] = NOW + timedelta(minutes=30)

    with pytest.raises(
        ValidationError,
        match="refresh token must expire after",
    ):
        QuickBooksTokenSet.model_validate(values)


def test_token_set_round_trips_through_encryption() -> None:
    """Authenticated encryption recovers the exact token set."""

    tokens = token_set()
    token_cipher = cipher()

    encrypted = token_cipher.encrypt_token_set(tokens)
    decrypted = token_cipher.decrypt_token_set(encrypted)

    assert decrypted == tokens
    assert encrypted.key_fingerprint == (token_cipher.key_fingerprint)


def test_ciphertext_does_not_contain_plaintext() -> None:
    """Stored fields contain no readable OAuth token material."""

    encrypted = cipher().encrypt_token_set(token_set())
    serialized = encrypted.model_dump_json()

    assert ACCESS_TOKEN not in serialized
    assert REFRESH_TOKEN not in serialized
    assert encrypted.access_token_ciphertext != ACCESS_TOKEN
    assert encrypted.refresh_token_ciphertext != REFRESH_TOKEN


def test_encryption_is_randomized() -> None:
    """Repeated encryption does not produce identical ciphertext."""

    token_cipher = cipher()
    tokens = token_set()

    first = token_cipher.encrypt_token_set(tokens)
    second = token_cipher.encrypt_token_set(tokens)

    assert first.access_token_ciphertext != second.access_token_ciphertext
    assert first.refresh_token_ciphertext != second.refresh_token_ciphertext


def test_wrong_key_is_rejected() -> None:
    """Token material cannot be opened with another app key."""

    encrypted = cipher(KEY_ONE).encrypt_token_set(token_set())

    with pytest.raises(
        QuickBooksTokenDecryptionError,
        match="fingerprint",
    ):
        cipher(KEY_TWO).decrypt_token_set(encrypted)


def test_tampered_ciphertext_is_rejected() -> None:
    """Authenticated encryption detects modified token data."""

    token_cipher = cipher()
    encrypted = token_cipher.encrypt_token_set(token_set())
    values = encrypted.model_dump()
    ciphertext = encrypted.access_token_ciphertext
    replacement = "A" if ciphertext[-1] != "A" else "B"
    values["access_token_ciphertext"] = ciphertext[:-1] + replacement
    tampered = type(encrypted).model_validate(values)

    with pytest.raises(
        QuickBooksTokenDecryptionError,
        match="tampered",
    ):
        token_cipher.decrypt_token_set(tampered)


def test_short_encryption_secret_is_rejected() -> None:
    """Weak encryption configuration cannot protect QBO tokens."""

    with pytest.raises(
        QuickBooksTokenEncryptionError,
        match="at least 32",
    ):
        QuickBooksTokenCipher.from_secret(SecretStr("too-short"))
