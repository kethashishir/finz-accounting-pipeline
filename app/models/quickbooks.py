"""QuickBooks Online integration domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    model_validator,
)

INTUIT_AUTHORIZATION_ENDPOINT = "https://appcenter.intuit.com/connect/oauth2"
INTUIT_TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
QBO_ACCOUNTING_SCOPE = "com.intuit.quickbooks.accounting"
QBO_SANDBOX_API_BASE_URL = "https://sandbox-quickbooks.api.intuit.com"
QBO_PRODUCTION_API_BASE_URL = "https://quickbooks.api.intuit.com"


class QuickBooksEnvironment(StrEnum):
    """Supported QuickBooks Online company environments."""

    SANDBOX = "sandbox"
    PRODUCTION = "production"


class QuickBooksOAuthConfiguration(BaseModel):
    """Complete secret-safe configuration for QBO OAuth."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    environment: QuickBooksEnvironment
    client_id: str = Field(min_length=1)
    client_secret: SecretStr
    redirect_uri: str = Field(min_length=1)
    token_encryption_key: SecretStr
    session_secret: SecretStr

    authorization_endpoint: str = INTUIT_AUTHORIZATION_ENDPOINT
    token_endpoint: str = INTUIT_TOKEN_ENDPOINT
    api_base_url: str
    scopes: tuple[str, ...] = (QBO_ACCOUNTING_SCOPE,)

    @property
    def scope_value(self) -> str:
        """Return the space-separated OAuth scope value."""

        return " ".join(self.scopes)

    @model_validator(mode="after")
    def validate_fixed_intuit_contract(
        self,
    ) -> QuickBooksOAuthConfiguration:
        """Prevent callers from replacing trusted Intuit endpoints."""

        if self.authorization_endpoint != INTUIT_AUTHORIZATION_ENDPOINT:
            raise ValueError("Unexpected Intuit authorization endpoint")

        if self.token_endpoint != INTUIT_TOKEN_ENDPOINT:
            raise ValueError("Unexpected Intuit token endpoint")

        if self.scopes != (QBO_ACCOUNTING_SCOPE,):
            raise ValueError("Only the QuickBooks Accounting scope is supported")

        expected_base_url = (
            QBO_SANDBOX_API_BASE_URL
            if self.environment is QuickBooksEnvironment.SANDBOX
            else QBO_PRODUCTION_API_BASE_URL
        )

        if self.api_base_url != expected_base_url:
            raise ValueError("QuickBooks API base URL does not match the configured environment")

        return self


class QuickBooksAuthorizationState(BaseModel):
    """Signed claims carried through the Intuit authorization flow."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    version: Literal[1] = 1
    nonce: str = Field(
        min_length=32,
        max_length=128,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    environment: QuickBooksEnvironment
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_time_window(
        self,
    ) -> QuickBooksAuthorizationState:
        """Require timezone-aware claims with a positive lifetime."""

        if self.issued_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("QuickBooks OAuth state timestamps must be timezone-aware")

        if self.expires_at <= self.issued_at:
            raise ValueError("QuickBooks OAuth state must expire after it is issued")

        return self


class QuickBooksAuthorizationRequest(BaseModel):
    """Intuit authorization redirect and its signed state metadata."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    authorization_url: str = Field(
        min_length=1,
        max_length=8192,
    )
    state: str = Field(
        min_length=1,
        max_length=4096,
    )
    expires_at: datetime


class QuickBooksOAuthStateRecord(QuickBooksAuthorizationState):
    """Persisted single-use QuickBooks OAuth state."""

    consumed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_consumption_time(
        self,
    ) -> QuickBooksOAuthStateRecord:
        """Require a valid callback-consumption timestamp."""

        if self.consumed_at is None:
            return self

        if self.consumed_at.utcoffset() is None:
            raise ValueError("QuickBooks OAuth state consumption timestamp must be timezone-aware")

        if self.consumed_at < self.issued_at:
            raise ValueError("QuickBooks OAuth state cannot be consumed before it is issued")

        if self.consumed_at >= self.expires_at:
            raise ValueError("QuickBooks OAuth state cannot be consumed after it expires")

        return self


class QuickBooksTokenSet(BaseModel):
    """Plaintext QBO tokens held only at trusted service boundaries."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    environment: QuickBooksEnvironment
    realm_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[0-9]+$",
    )
    token_type: Literal["bearer"] = "bearer"
    access_token: SecretStr
    refresh_token: SecretStr
    issued_at: datetime
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime

    @model_validator(mode="after")
    def validate_token_lifetimes(
        self,
    ) -> QuickBooksTokenSet:
        """Require timezone-aware, ordered token lifetimes."""

        timestamps = (
            self.issued_at,
            self.access_token_expires_at,
            self.refresh_token_expires_at,
        )

        if any(value.utcoffset() is None for value in timestamps):
            raise ValueError("QuickBooks token timestamps must be timezone-aware")

        if self.access_token_expires_at <= self.issued_at:
            raise ValueError("QuickBooks access token must expire after it is issued")

        if self.refresh_token_expires_at <= self.access_token_expires_at:
            raise ValueError("QuickBooks refresh token must expire after the access token")

        return self


class EncryptedQuickBooksTokenSet(BaseModel):
    """Encrypted QBO token material safe for persistence."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )

    version: Literal[1] = 1
    environment: QuickBooksEnvironment
    realm_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[0-9]+$",
    )
    token_type: Literal["bearer"] = "bearer"
    access_token_ciphertext: str = Field(
        min_length=1,
        max_length=16384,
    )
    refresh_token_ciphertext: str = Field(
        min_length=1,
        max_length=16384,
    )
    key_fingerprint: str = Field(
        min_length=16,
        max_length=16,
        pattern=r"^[0-9a-f]{16}$",
    )
    issued_at: datetime
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime

    @model_validator(mode="after")
    def validate_encrypted_token_lifetimes(
        self,
    ) -> EncryptedQuickBooksTokenSet:
        """Validate safe token metadata without decrypting tokens."""

        timestamps = (
            self.issued_at,
            self.access_token_expires_at,
            self.refresh_token_expires_at,
        )

        if any(value.utcoffset() is None for value in timestamps):
            raise ValueError("Encrypted QuickBooks token timestamps must be timezone-aware")

        if self.access_token_expires_at <= self.issued_at:
            raise ValueError("Encrypted QuickBooks access token must expire after it is issued")

        if self.refresh_token_expires_at <= self.access_token_expires_at:
            raise ValueError(
                "Encrypted QuickBooks refresh token must expire after the access token"
            )

        return self


class QuickBooksConnectionRecord(EncryptedQuickBooksTokenSet):
    """Persisted encrypted connection to one QBO company."""

    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_connection_timestamps(
        self,
    ) -> QuickBooksConnectionRecord:
        """Validate persistence and token chronology."""

        if self.created_at.utcoffset() is None or self.updated_at.utcoffset() is None:
            raise ValueError("QuickBooks connection timestamps must be timezone-aware")

        if self.updated_at < self.created_at:
            raise ValueError("QuickBooks connection cannot be updated before it is created")

        if self.updated_at < self.issued_at:
            raise ValueError("QuickBooks connection cannot store tokens before they are issued")

        return self
