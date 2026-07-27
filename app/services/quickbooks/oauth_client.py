"""HTTP client for QuickBooks Online OAuth token operations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Self

import httpx2
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)

from app.models.quickbooks import (
    QuickBooksOAuthConfiguration,
    QuickBooksTokenSet,
)

OAUTH_REQUEST_TIMEOUT_SECONDS = 10.0
MAXIMUM_AUTHORIZATION_CODE_LENGTH = 512
MAXIMUM_REALM_ID_LENGTH = 64


class QuickBooksOAuthClientError(RuntimeError):
    """Base error for Intuit OAuth token operations."""


class QuickBooksOAuthRequestError(QuickBooksOAuthClientError):
    """The Intuit OAuth server could not be reached."""


class QuickBooksOAuthProviderError(QuickBooksOAuthClientError):
    """The Intuit OAuth server rejected a token request."""


class QuickBooksOAuthResponseError(QuickBooksOAuthClientError):
    """The Intuit OAuth server returned an invalid response."""


class _IntuitTokenResponse(BaseModel):
    """Validated token payload returned by Intuit."""

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
    )

    token_type: str
    access_token: SecretStr
    refresh_token: SecretStr
    expires_in: int = Field(gt=0)
    refresh_token_expires_in: int = Field(
        alias="x_refresh_token_expires_in",
        gt=0,
    )

    @field_validator("token_type")
    @classmethod
    def normalize_token_type(
        cls,
        value: str,
    ) -> str:
        """Accept only OAuth bearer tokens."""

        normalized = value.strip().casefold()

        if normalized != "bearer":
            raise ValueError("Intuit token_type must be bearer")

        return normalized


class QuickBooksOAuthClient:
    """Exchange authorization codes and refresh QBO tokens."""

    def __init__(
        self,
        *,
        configuration: QuickBooksOAuthConfiguration,
        client: httpx2.AsyncClient,
        owns_client: bool = False,
    ) -> None:
        self._configuration = configuration
        self._client = client
        self._owns_client = owns_client
        self._closed = False

    async def exchange_authorization_code(
        self,
        *,
        code: str,
        realm_id: str,
        now: datetime | None = None,
    ) -> QuickBooksTokenSet:
        """Exchange one callback authorization code for tokens."""

        self._require_open()
        normalized_code = _validate_authorization_code(code)
        normalized_realm_id = _validate_realm_id(realm_id)

        payload = await self._request_tokens(
            {
                "grant_type": "authorization_code",
                "code": normalized_code,
                "redirect_uri": (self._configuration.redirect_uri),
            }
        )

        return _build_token_set(
            payload,
            configuration=self._configuration,
            realm_id=normalized_realm_id,
            now=now,
        )

    async def refresh_tokens(
        self,
        *,
        refresh_token: SecretStr,
        realm_id: str,
        now: datetime | None = None,
    ) -> QuickBooksTokenSet:
        """Use the latest refresh token to obtain new tokens."""

        self._require_open()
        normalized_refresh_token = refresh_token.get_secret_value().strip()
        normalized_realm_id = _validate_realm_id(realm_id)

        if not normalized_refresh_token:
            raise ValueError("QuickBooks refresh token cannot be empty")

        payload = await self._request_tokens(
            {
                "grant_type": "refresh_token",
                "refresh_token": (normalized_refresh_token),
            }
        )

        return _build_token_set(
            payload,
            configuration=self._configuration,
            realm_id=normalized_realm_id,
            now=now,
        )

    async def _request_tokens(
        self,
        form_data: dict[str, str],
    ) -> _IntuitTokenResponse:
        """Send one form-encoded request to Intuit."""

        try:
            response = await self._client.post(
                self._configuration.token_endpoint,
                data=form_data,
                auth=httpx2.BasicAuth(
                    self._configuration.client_id,
                    self._configuration.client_secret.get_secret_value(),
                ),
                headers={
                    "Accept": "application/json",
                },
                timeout=OAUTH_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx2.RequestError as exc:
            raise QuickBooksOAuthRequestError("QuickBooks OAuth token request failed") from exc

        if response.status_code < 200 or response.status_code >= 300:
            transaction_id = response.headers.get("intuit_tid")
            detail = f"; Intuit transaction ID: {transaction_id}" if transaction_id else ""

            raise QuickBooksOAuthProviderError(
                "QuickBooks OAuth token request was "
                f"rejected with HTTP {response.status_code}"
                f"{detail}"
            )

        try:
            response_payload = response.json()
        except ValueError as exc:
            raise QuickBooksOAuthResponseError("QuickBooks OAuth returned invalid JSON") from exc

        try:
            return _IntuitTokenResponse.model_validate(response_payload)
        except ValidationError as exc:
            raise QuickBooksOAuthResponseError(
                "QuickBooks OAuth returned an invalid token payload"
            ) from exc

    def _require_open(self) -> None:
        """Reject work after the adapter has been closed."""

        if self._closed:
            raise RuntimeError("QuickBooks OAuth client is already closed")

    async def close(self) -> None:
        """Close owned HTTP resources exactly once."""

        if self._closed:
            return

        self._closed = True

        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(
        self,
    ) -> Self:
        """Return this open OAuth client."""

        self._require_open()
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        """Close owned resources on context exit."""

        await self.close()


def create_quickbooks_oauth_client(
    configuration: QuickBooksOAuthConfiguration,
) -> QuickBooksOAuthClient:
    """Create the production Intuit OAuth HTTP adapter."""

    client = httpx2.AsyncClient(
        timeout=httpx2.Timeout(OAUTH_REQUEST_TIMEOUT_SECONDS),
        follow_redirects=False,
    )

    return QuickBooksOAuthClient(
        configuration=configuration,
        client=client,
        owns_client=True,
    )


def _build_token_set(
    payload: _IntuitTokenResponse,
    *,
    configuration: QuickBooksOAuthConfiguration,
    realm_id: str,
    now: datetime | None,
) -> QuickBooksTokenSet:
    """Convert a validated Intuit payload into domain tokens."""

    issued_at = _normalized_now(now)

    return QuickBooksTokenSet(
        environment=configuration.environment,
        realm_id=realm_id,
        token_type="bearer",
        access_token=payload.access_token,
        refresh_token=payload.refresh_token,
        issued_at=issued_at,
        access_token_expires_at=(issued_at + timedelta(seconds=payload.expires_in)),
        refresh_token_expires_at=(
            issued_at + timedelta(seconds=(payload.refresh_token_expires_in))
        ),
    )


def _validate_authorization_code(
    value: str,
) -> str:
    """Validate the short-lived callback code locally."""

    normalized = value.strip()

    if not normalized:
        raise ValueError("QuickBooks authorization code cannot be empty")

    if len(normalized) > MAXIMUM_AUTHORIZATION_CODE_LENGTH:
        raise ValueError("QuickBooks authorization code is too long")

    return normalized


def _validate_realm_id(
    value: str,
) -> str:
    """Validate the numeric QuickBooks company identifier."""

    normalized = value.strip()

    if (
        not normalized
        or len(normalized) > MAXIMUM_REALM_ID_LENGTH
        or not normalized.isascii()
        or not normalized.isdigit()
    ):
        raise ValueError("QuickBooks realm ID must contain only digits")

    return normalized


def _normalized_now(
    value: datetime | None,
) -> datetime:
    """Return UTC time at BSON millisecond precision."""

    timestamp = value or datetime.now(UTC)

    if timestamp.utcoffset() is None:
        raise ValueError("QuickBooks token issuance time must be timezone-aware")

    utc_timestamp = timestamp.astimezone(UTC)

    return utc_timestamp.replace(microsecond=(utc_timestamp.microsecond // 1000) * 1000)
