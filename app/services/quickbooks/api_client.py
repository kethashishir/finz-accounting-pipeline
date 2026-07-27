"""Authenticated QuickBooks Online Accounting API client."""

from __future__ import annotations

from typing import Self

import httpx2
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
)

from app.models.quickbooks import (
    QuickBooksOAuthConfiguration,
)

QBO_MINOR_VERSION = "75"
QBO_API_TIMEOUT_SECONDS = 15.0


class QuickBooksApiError(RuntimeError):
    """Base error for QuickBooks Accounting API operations."""


class QuickBooksApiRequestError(QuickBooksApiError):
    """The QuickBooks API could not be reached."""


class QuickBooksApiProviderError(QuickBooksApiError):
    """QuickBooks rejected an Accounting API request."""


class QuickBooksApiResponseError(QuickBooksApiError):
    """QuickBooks returned an invalid response payload."""


class QuickBooksCompanyInfo(BaseModel):
    """Safe company metadata returned by QuickBooks."""

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    id: str = Field(alias="Id")
    sync_token: str = Field(alias="SyncToken")
    company_name: str = Field(alias="CompanyName")
    legal_name: str | None = Field(
        default=None,
        alias="LegalName",
    )
    country: str | None = Field(
        default=None,
        alias="Country",
    )


class QuickBooksApiAccount(BaseModel):
    """Validated QuickBooks chart-of-accounts entity."""

    model_config = ConfigDict(
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    id: str = Field(alias="Id")
    sync_token: str = Field(alias="SyncToken")
    name: str = Field(alias="Name")
    account_number: str | None = Field(
        default=None,
        alias="AcctNum",
    )
    account_type: str = Field(alias="AccountType")
    account_sub_type: str | None = Field(
        default=None,
        alias="AccountSubType",
    )
    description: str | None = Field(
        default=None,
        alias="Description",
    )
    active: bool = Field(
        default=True,
        alias="Active",
    )


class QuickBooksApiClient:
    """Perform secret-safe QBO Accounting API requests."""

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

    async def get_company_info(
        self,
        *,
        access_token: SecretStr,
        realm_id: str,
    ) -> QuickBooksCompanyInfo:
        """Retrieve the connected sandbox company metadata."""

        payload = await self._get(
            path=f"companyinfo/{_realm_id(realm_id)}",
            access_token=access_token,
            realm_id=realm_id,
        )

        return _validate_entity(
            payload,
            key="CompanyInfo",
            model=QuickBooksCompanyInfo,
        )

    async def query_accounts(
        self,
        *,
        access_token: SecretStr,
        realm_id: str,
    ) -> tuple[QuickBooksApiAccount, ...]:
        """Return active and inactive QBO accounts."""

        payload = await self._get(
            path="query",
            access_token=access_token,
            realm_id=realm_id,
            params={
                "query": ("SELECT * FROM Account WHERE Active IN (true, false) MAXRESULTS 1000"),
            },
        )

        query_response = payload.get("QueryResponse")

        if not isinstance(query_response, dict):
            raise QuickBooksApiResponseError("QuickBooks account query omitted QueryResponse")

        raw_accounts = query_response.get("Account", [])

        if not isinstance(raw_accounts, list):
            raise QuickBooksApiResponseError("QuickBooks account query returned an invalid list")

        try:
            return tuple(QuickBooksApiAccount.model_validate(account) for account in raw_accounts)
        except ValidationError as exc:
            raise QuickBooksApiResponseError("QuickBooks returned an invalid account") from exc

    async def create_account(
        self,
        *,
        access_token: SecretStr,
        realm_id: str,
        payload: dict[str, object],
    ) -> QuickBooksApiAccount:
        """Create one QBO account serially."""

        response = await self._post(
            path="account",
            access_token=access_token,
            realm_id=realm_id,
            payload=payload,
        )

        return _validate_entity(
            response,
            key="Account",
            model=QuickBooksApiAccount,
        )

    async def update_account(
        self,
        *,
        access_token: SecretStr,
        realm_id: str,
        payload: dict[str, object],
    ) -> QuickBooksApiAccount:
        """Perform one sparse QBO account update."""

        response = await self._post(
            path="account",
            access_token=access_token,
            realm_id=realm_id,
            payload=payload,
        )

        return _validate_entity(
            response,
            key="Account",
            model=QuickBooksApiAccount,
        )

    async def _get(
        self,
        *,
        path: str,
        access_token: SecretStr,
        realm_id: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, object]:
        """Send one authenticated GET request."""

        self._require_open()
        url = self._url(
            realm_id=realm_id,
            path=path,
        )
        request_params = {
            **(params or {}),
            "minorversion": QBO_MINOR_VERSION,
        }

        try:
            response = await self._client.get(
                url,
                params=request_params,
                headers=_headers(access_token),
                timeout=QBO_API_TIMEOUT_SECONDS,
            )
        except httpx2.RequestError as exc:
            raise QuickBooksApiRequestError("QuickBooks Accounting API request failed") from exc

        return _response_payload(response)

    async def _post(
        self,
        *,
        path: str,
        access_token: SecretStr,
        realm_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """Send one authenticated JSON POST request."""

        self._require_open()
        url = self._url(
            realm_id=realm_id,
            path=path,
        )

        try:
            response = await self._client.post(
                url,
                params={
                    "minorversion": QBO_MINOR_VERSION,
                },
                json=payload,
                headers=_headers(access_token),
                timeout=QBO_API_TIMEOUT_SECONDS,
            )
        except httpx2.RequestError as exc:
            raise QuickBooksApiRequestError("QuickBooks Accounting API request failed") from exc

        return _response_payload(response)

    def _url(
        self,
        *,
        realm_id: str,
        path: str,
    ) -> str:
        """Build a sandbox or production company URL."""

        normalized_realm_id = _realm_id(realm_id)

        return f"{self._configuration.api_base_url}/v3/company/{normalized_realm_id}/{path}"

    def _require_open(self) -> None:
        """Reject requests after client shutdown."""

        if self._closed:
            raise RuntimeError("QuickBooks API client is already closed")

    async def close(self) -> None:
        """Close owned HTTP resources once."""

        if self._closed:
            return

        self._closed = True

        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(
        self,
    ) -> Self:
        """Return this open API client."""

        self._require_open()
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        """Close owned resources."""

        await self.close()


def create_quickbooks_api_client(
    configuration: QuickBooksOAuthConfiguration,
) -> QuickBooksApiClient:
    """Create the production QBO Accounting API adapter."""

    client = httpx2.AsyncClient(
        timeout=httpx2.Timeout(QBO_API_TIMEOUT_SECONDS),
        follow_redirects=False,
    )

    return QuickBooksApiClient(
        configuration=configuration,
        client=client,
        owns_client=True,
    )


def _headers(
    access_token: SecretStr,
) -> dict[str, str]:
    """Build authenticated headers without logging tokens."""

    token = access_token.get_secret_value().strip()

    if not token:
        raise ValueError("QuickBooks access token cannot be empty")

    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def _realm_id(value: str) -> str:
    """Validate a numeric QuickBooks company identifier."""

    normalized = value.strip()

    if (
        not normalized
        or not normalized.isascii()
        or not normalized.isdigit()
        or len(normalized) > 64
    ):
        raise ValueError("QuickBooks realm ID must contain only digits")

    return normalized


def _response_payload(
    response: httpx2.Response,
) -> dict[str, object]:
    """Validate HTTP status and parse a JSON object."""

    if not 200 <= response.status_code < 300:
        transaction_id = response.headers.get("intuit_tid")
        fault_detail = _safe_fault_detail(response)
        parts = [f"QuickBooks Accounting API rejected the request with HTTP {response.status_code}"]

        if fault_detail is not None:
            parts.append(fault_detail)

        if transaction_id:
            parts.append(f"Intuit transaction ID: {transaction_id}")

        raise QuickBooksApiProviderError("; ".join(parts))

    try:
        payload = response.json()
    except ValueError as exc:
        raise QuickBooksApiResponseError("QuickBooks Accounting API returned invalid JSON") from exc

    if not isinstance(payload, dict):
        raise QuickBooksApiResponseError("QuickBooks Accounting API returned a non-object response")

    return payload


def _safe_fault_detail(
    response: httpx2.Response,
) -> str | None:
    """Extract safe QBO validation fields."""

    try:
        payload = response.json()
    except ValueError:
        return None

    if not isinstance(payload, dict):
        return None

    fault = payload.get("Fault")

    if not isinstance(fault, dict):
        return None

    errors = fault.get("Error")

    if not isinstance(errors, list) or not errors:
        return None

    error = errors[0]

    if not isinstance(error, dict):
        return None

    fields: list[str] = []

    for label, key in (
        ("code", "code"),
        ("message", "Message"),
        ("detail", "Detail"),
    ):
        value = error.get(key)

        if not isinstance(value, str):
            continue

        normalized = " ".join(value.split())[:500]

        if normalized:
            fields.append(f"{label}={normalized}")

    return ", ".join(fields) if fields else None


def _validate_entity(
    payload: dict[str, object],
    *,
    key: str,
    model: type[QuickBooksCompanyInfo] | type[QuickBooksApiAccount],
):
    """Validate one named QBO response entity."""

    entity = payload.get(key)

    if not isinstance(entity, dict):
        raise QuickBooksApiResponseError(f"QuickBooks response omitted {key}")

    try:
        return model.model_validate(entity)
    except ValidationError as exc:
        raise QuickBooksApiResponseError(f"QuickBooks returned an invalid {key}") from exc
