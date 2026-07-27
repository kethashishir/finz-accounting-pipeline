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
