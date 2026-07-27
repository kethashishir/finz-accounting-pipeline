"""Tests for the Intuit OAuth token HTTP adapter."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx2
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.quickbooks.oauth_client import (
    QuickBooksOAuthClient,
    QuickBooksOAuthProviderError,
    QuickBooksOAuthRequestError,
    QuickBooksOAuthResponseError,
)
from app.services.quickbooks.oauth_config import (
    build_quickbooks_oauth_configuration,
)

NOW = datetime(
    2030,
    1,
    1,
    12,
    0,
    0,
    123456,
    tzinfo=UTC,
)
REALM_ID = "9341456789012345"
CLIENT_ID = "test-client-id"
CLIENT_SECRET = "test-client-secret"
ACCESS_TOKEN = "returned-access-token"
REFRESH_TOKEN = "returned-refresh-token"


def configuration():
    """Build complete test-only OAuth configuration."""

    return build_quickbooks_oauth_configuration(
        Settings(
            _env_file=None,
            qbo_environment="sandbox",
            qbo_client_id=CLIENT_ID,
            qbo_client_secret=CLIENT_SECRET,
            qbo_redirect_uri=("http://localhost:8000/api/v1/quickbooks/callback"),
            token_encryption_key=("test-token-key-that-is-at-least-32-bytes"),
            session_secret=("test-session-key-that-is-at-least-32-bytes"),
        )
    )


def successful_payload() -> dict[str, object]:
    """Return a realistic Intuit token response."""

    return {
        "token_type": "bearer",
        "access_token": ACCESS_TOKEN,
        "refresh_token": REFRESH_TOKEN,
        "expires_in": 3600,
        "x_refresh_token_expires_in": 8640000,
        "scope": "com.intuit.quickbooks.accounting",
    }


def oauth_client(handler) -> QuickBooksOAuthClient:
    """Create an adapter backed by mock HTTP transport."""

    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler),
    )

    return QuickBooksOAuthClient(
        configuration=configuration(),
        client=client,
        owns_client=True,
    )


async def test_exchange_sends_exact_intuit_request() -> None:
    """Authorization codes use Basic Auth and form encoding."""

    captured: list[httpx2.Request] = []

    async def handler(
        request: httpx2.Request,
    ) -> httpx2.Response:
        captured.append(request)
        return httpx2.Response(
            200,
            json=successful_payload(),
        )

    async with oauth_client(handler) as client:
        tokens = await client.exchange_authorization_code(
            code="callback-code",
            realm_id=REALM_ID,
            now=NOW,
        )

    assert len(captured) == 1

    request = captured[0]
    form = parse_qs(
        request.content.decode("utf-8"),
        strict_parsing=True,
    )
    expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()

    assert request.method == "POST"
    assert str(request.url) == (configuration().token_endpoint)
    assert request.headers["authorization"] == (f"Basic {expected_basic}")
    assert request.headers["accept"] == ("application/json")
    assert form == {
        "grant_type": ["authorization_code"],
        "code": ["callback-code"],
        "redirect_uri": [configuration().redirect_uri],
    }

    assert tokens.realm_id == REALM_ID
    assert tokens.issued_at.microsecond == 123000
    assert tokens.access_token_expires_at == (tokens.issued_at + timedelta(seconds=3600))
    assert tokens.refresh_token_expires_at == (tokens.issued_at + timedelta(seconds=8640000))
    assert tokens.access_token.get_secret_value() == ACCESS_TOKEN
    assert tokens.refresh_token.get_secret_value() == REFRESH_TOKEN


async def test_refresh_uses_latest_refresh_token() -> None:
    """Refresh requests send only the supplied current token."""

    captured_form: dict[str, list[str]] = {}

    async def handler(
        request: httpx2.Request,
    ) -> httpx2.Response:
        captured_form.update(
            parse_qs(
                request.content.decode("utf-8"),
                strict_parsing=True,
            )
        )
        return httpx2.Response(
            200,
            json=successful_payload(),
        )

    async with oauth_client(handler) as client:
        tokens = await client.refresh_tokens(
            refresh_token=SecretStr("current-refresh-token"),
            realm_id=REALM_ID,
            now=NOW,
        )

    assert captured_form == {
        "grant_type": ["refresh_token"],
        "refresh_token": ["current-refresh-token"],
    }
    assert tokens.refresh_token.get_secret_value() == REFRESH_TOKEN


async def test_provider_error_is_secret_safe() -> None:
    """Provider errors expose status but not response secrets."""

    leaked_value = "must-not-appear-in-error"

    async def handler(
        request: httpx2.Request,
    ) -> httpx2.Response:
        return httpx2.Response(
            400,
            headers={
                "intuit_tid": "intuit-test-id",
            },
            json={
                "error": "invalid_grant",
                "access_token": leaked_value,
            },
        )

    async with oauth_client(handler) as client:
        with pytest.raises(
            QuickBooksOAuthProviderError,
            match="HTTP 400",
        ) as error:
            await client.exchange_authorization_code(
                code="bad-code",
                realm_id=REALM_ID,
                now=NOW,
            )

    message = str(error.value)

    assert "intuit-test-id" in message
    assert leaked_value not in message
    assert "bad-code" not in message


async def test_network_failure_is_translated() -> None:
    """Transport errors become stable application errors."""

    async def handler(
        request: httpx2.Request,
    ) -> httpx2.Response:
        raise httpx2.ConnectError(
            "connection failed",
            request=request,
        )

    async with oauth_client(handler) as client:
        with pytest.raises(
            QuickBooksOAuthRequestError,
            match="request failed",
        ):
            await client.exchange_authorization_code(
                code="callback-code",
                realm_id=REALM_ID,
                now=NOW,
            )


async def test_invalid_json_is_rejected() -> None:
    """Successful HTTP responses must contain JSON."""

    async def handler(
        request: httpx2.Request,
    ) -> httpx2.Response:
        return httpx2.Response(
            200,
            text="not-json",
        )

    async with oauth_client(handler) as client:
        with pytest.raises(
            QuickBooksOAuthResponseError,
            match="invalid JSON",
        ):
            await client.exchange_authorization_code(
                code="callback-code",
                realm_id=REALM_ID,
                now=NOW,
            )


async def test_invalid_token_payload_is_rejected() -> None:
    """Missing or malformed token fields cannot cross the boundary."""

    async def handler(
        request: httpx2.Request,
    ) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "token_type": "bearer",
                "access_token": ACCESS_TOKEN,
            },
        )

    async with oauth_client(handler) as client:
        with pytest.raises(
            QuickBooksOAuthResponseError,
            match="invalid token payload",
        ):
            await client.exchange_authorization_code(
                code="callback-code",
                realm_id=REALM_ID,
                now=NOW,
            )


async def test_non_bearer_token_is_rejected() -> None:
    """The application accepts only bearer access tokens."""

    payload = successful_payload()
    payload["token_type"] = "mac"

    async def handler(
        request: httpx2.Request,
    ) -> httpx2.Response:
        return httpx2.Response(
            200,
            json=payload,
        )

    async with oauth_client(handler) as client:
        with pytest.raises(
            QuickBooksOAuthResponseError,
            match="invalid token payload",
        ):
            await client.exchange_authorization_code(
                code="callback-code",
                realm_id=REALM_ID,
                now=NOW,
            )


async def test_invalid_callback_values_fail_before_http() -> None:
    """Invalid code and realm data never reach Intuit."""

    request_count = 0

    async def handler(
        request: httpx2.Request,
    ) -> httpx2.Response:
        nonlocal request_count
        request_count += 1
        return httpx2.Response(
            200,
            json=successful_payload(),
        )

    async with oauth_client(handler) as client:
        with pytest.raises(
            ValueError,
            match="code cannot be empty",
        ):
            await client.exchange_authorization_code(
                code=" ",
                realm_id=REALM_ID,
                now=NOW,
            )

        with pytest.raises(
            ValueError,
            match="realm ID",
        ):
            await client.exchange_authorization_code(
                code="callback-code",
                realm_id="not-a-company",
                now=NOW,
            )

    assert request_count == 0


async def test_closed_client_rejects_requests() -> None:
    """A closed adapter cannot perform token operations."""

    async def handler(
        request: httpx2.Request,
    ) -> httpx2.Response:
        return httpx2.Response(
            200,
            json=successful_payload(),
        )

    client = oauth_client(handler)

    await client.close()
    await client.close()

    with pytest.raises(
        RuntimeError,
        match="already closed",
    ):
        await client.exchange_authorization_code(
            code="callback-code",
            realm_id=REALM_ID,
            now=NOW,
        )
