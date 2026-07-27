"""Authenticated encryption for QuickBooks OAuth tokens."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr

from app.models.quickbooks import (
    EncryptedQuickBooksTokenSet,
    QuickBooksTokenSet,
)

MINIMUM_TOKEN_ENCRYPTION_SECRET_BYTES = 32
TOKEN_ENCRYPTION_INFO = b"finz-accounting-qbo-token-encryption-v1"


class QuickBooksTokenEncryptionError(ValueError):
    """Token encryption configuration is invalid."""


class QuickBooksTokenDecryptionError(ValueError):
    """Encrypted QBO token material cannot be trusted."""


@dataclass(frozen=True, slots=True)
class QuickBooksTokenCipher:
    """Encrypt and decrypt QBO tokens with authenticated encryption."""

    _fernet: Fernet
    key_fingerprint: str

    @classmethod
    def from_secret(
        cls,
        secret: SecretStr,
    ) -> QuickBooksTokenCipher:
        """Derive a domain-separated Fernet key from an app secret."""

        secret_bytes = secret.get_secret_value().encode("utf-8")

        if len(secret_bytes) < MINIMUM_TOKEN_ENCRYPTION_SECRET_BYTES:
            raise QuickBooksTokenEncryptionError(
                "TOKEN_ENCRYPTION_KEY must contain at least 32 UTF-8 bytes"
            )

        derived_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=TOKEN_ENCRYPTION_INFO,
        ).derive(secret_bytes)

        fernet_key = base64.urlsafe_b64encode(derived_key)
        fingerprint = hashlib.sha256(secret_bytes).hexdigest()[:16]

        return cls(
            _fernet=Fernet(fernet_key),
            key_fingerprint=fingerprint,
        )

    def encrypt_token_set(
        self,
        token_set: QuickBooksTokenSet,
    ) -> EncryptedQuickBooksTokenSet:
        """Encrypt both OAuth tokens and preserve safe metadata."""

        access_token_ciphertext = self._encrypt(token_set.access_token)
        refresh_token_ciphertext = self._encrypt(token_set.refresh_token)

        return EncryptedQuickBooksTokenSet(
            environment=token_set.environment,
            realm_id=token_set.realm_id,
            token_type=token_set.token_type,
            access_token_ciphertext=(access_token_ciphertext),
            refresh_token_ciphertext=(refresh_token_ciphertext),
            key_fingerprint=self.key_fingerprint,
            issued_at=token_set.issued_at,
            access_token_expires_at=(token_set.access_token_expires_at),
            refresh_token_expires_at=(token_set.refresh_token_expires_at),
        )

    def decrypt_token_set(
        self,
        encrypted: EncryptedQuickBooksTokenSet,
    ) -> QuickBooksTokenSet:
        """Authenticate and decrypt a stored QBO token set."""

        if encrypted.key_fingerprint != self.key_fingerprint:
            raise QuickBooksTokenDecryptionError(
                "QuickBooks token key fingerprint does not match the configured encryption key"
            )

        access_token = self._decrypt(encrypted.access_token_ciphertext)
        refresh_token = self._decrypt(encrypted.refresh_token_ciphertext)

        return QuickBooksTokenSet(
            environment=encrypted.environment,
            realm_id=encrypted.realm_id,
            token_type=encrypted.token_type,
            access_token=SecretStr(access_token),
            refresh_token=SecretStr(refresh_token),
            issued_at=encrypted.issued_at,
            access_token_expires_at=(encrypted.access_token_expires_at),
            refresh_token_expires_at=(encrypted.refresh_token_expires_at),
        )

    def _encrypt(
        self,
        value: SecretStr,
    ) -> str:
        """Encrypt one secret as URL-safe authenticated ciphertext."""

        plaintext = value.get_secret_value().encode("utf-8")

        return self._fernet.encrypt(plaintext).decode("ascii")

    def _decrypt(
        self,
        ciphertext: str,
    ) -> str:
        """Authenticate and decrypt one token ciphertext."""

        try:
            plaintext = self._fernet.decrypt(ciphertext.encode("ascii"))
            return plaintext.decode("utf-8")
        except (
            InvalidToken,
            UnicodeDecodeError,
            UnicodeEncodeError,
        ) as exc:
            raise QuickBooksTokenDecryptionError(
                "Encrypted QuickBooks token is invalid or has been tampered with"
            ) from exc
