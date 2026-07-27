"""Tests for signed QuickBooks authorization requests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.config import Settings
from app.models.quickbooks import (
    INTUIT_AUTHORIZATION_ENDPOINT,
    QBO_ACCOUNTING_SCOPE,
    QuickBooksEnvironment,
)
from app.services.quickbooks.authorization import (
    OAUTH_STATE_LIFETIME,
    QuickBooksOAuthStateError,
    create_quickbooks_authorization_request,
    verify_quickbooks_authorization_state,
)
from app.services.quickbooks.oauth_config import (
    build_quickbooks_oauth_configuration,
)

NOW = datetime(
    2026,
    7,
    27,
    5,
    45,
    tzinfo=UTC,
)
SESSION_SECRET = "test-session-secret-that-is-longer-than-32-bytes"


def configuration(
    *,
    environment: str = "sandbox",
    session_secret: str = SESSION_SECRET,
):
    """Create complete test-only OAuth configuration."""

    redirect_uri = (
        "http://localhost:8000/api/v1/quickbooks/callback"
        if environment == "sandbox"
        else ("https://finz.example.com/api/v1/quickbooks/callback")
    )

    settings = Settings(
        _env_file=None,
        qbo_environment=environment,
        qbo_client_id="test-client-id",
        qbo_client_secret="test-client-secret",
        qbo_redirect_uri=redirect_uri,
        token_encryption_key=("test-token-encryption-key"),
        session_secret=session_secret,
    )

    return build_quickbooks_oauth_configuration(settings)


def test_authorization_url_contains_exact_oauth_parameters() -> None:
    """The redirect contains Intuit's required authorization fields."""

    config = configuration()
    request = create_quickbooks_authorization_request(
        config,
        now=NOW,
    )

    parsed = urlsplit(request.authorization_url)
    query = parse_qs(
        parsed.query,
        strict_parsing=True,
    )

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == INTUIT_AUTHORIZATION_ENDPOINT
    assert query == {
        "client_id": ["test-client-id"],
        "response_type": ["code"],
        "scope": [QBO_ACCOUNTING_SCOPE],
        "redirect_uri": [config.redirect_uri],
        "state": [request.state],
    }
    assert SESSION_SECRET not in request.authorization_url
    assert request.expires_at == (NOW + OAUTH_STATE_LIFETIME)


def test_created_state_round_trips_valid_claims() -> None:
    """A freshly created signed state verifies successfully."""

    config = configuration()
    request = create_quickbooks_authorization_request(
        config,
        now=NOW,
    )

    claims = verify_quickbooks_authorization_state(
        request.state,
        configuration=config,
        now=NOW + timedelta(minutes=1),
    )

    assert claims.version == 1
    assert claims.environment is QuickBooksEnvironment.SANDBOX
    assert claims.issued_at == NOW
    assert claims.expires_at == request.expires_at
    assert len(claims.nonce) >= 32


def test_tampered_state_signature_is_rejected() -> None:
    """Changing a signed state invalidates the request."""

    config = configuration()
    request = create_quickbooks_authorization_request(
        config,
        now=NOW,
    )
    payload, signature = request.state.split(".")
    first_character = "A" if signature[0] != "A" else "B"
    tampered = f"{payload}.{first_character}{signature[1:]}"

    with pytest.raises(
        QuickBooksOAuthStateError,
        match="signature is invalid",
    ):
        verify_quickbooks_authorization_state(
            tampered,
            configuration=config,
            now=NOW,
        )


def test_state_signed_with_another_secret_is_rejected() -> None:
    """State from another application secret is untrusted."""

    original = configuration()
    request = create_quickbooks_authorization_request(
        original,
        now=NOW,
    )
    different = configuration(session_secret=("different-session-secret-that-is-also-long-enough"))

    with pytest.raises(
        QuickBooksOAuthStateError,
        match="signature is invalid",
    ):
        verify_quickbooks_authorization_state(
            request.state,
            configuration=different,
            now=NOW,
        )


def test_expired_state_is_rejected() -> None:
    """A callback cannot reuse state after its short lifetime."""

    config = configuration()
    request = create_quickbooks_authorization_request(
        config,
        now=NOW,
    )

    with pytest.raises(
        QuickBooksOAuthStateError,
        match="has expired",
    ):
        verify_quickbooks_authorization_state(
            request.state,
            configuration=config,
            now=NOW + OAUTH_STATE_LIFETIME,
        )


def test_future_state_is_rejected() -> None:
    """State created too far in the future is invalid."""

    config = configuration()
    request = create_quickbooks_authorization_request(
        config,
        now=NOW + timedelta(minutes=1),
    )

    with pytest.raises(
        QuickBooksOAuthStateError,
        match="issued in the future",
    ):
        verify_quickbooks_authorization_state(
            request.state,
            configuration=config,
            now=NOW,
        )


def test_state_cannot_cross_environments() -> None:
    """Sandbox state cannot authorize a production callback."""

    sandbox = configuration()
    request = create_quickbooks_authorization_request(
        sandbox,
        now=NOW,
    )
    production = configuration(environment="production")

    with pytest.raises(
        QuickBooksOAuthStateError,
        match="environment does not match",
    ):
        verify_quickbooks_authorization_state(
            request.state,
            configuration=production,
            now=NOW,
        )


@pytest.mark.parametrize(
    "state",
    [
        "",
        "missing-signature",
        "one.two.three",
        ".signature",
        "payload.",
    ],
)
def test_malformed_state_is_rejected(
    state: str,
) -> None:
    """Malformed callback state fails before payload parsing."""

    with pytest.raises(
        QuickBooksOAuthStateError,
        match="invalid",
    ):
        verify_quickbooks_authorization_state(
            state,
            configuration=configuration(),
            now=NOW,
        )


def test_short_session_secret_is_rejected() -> None:
    """Weak application secrets cannot sign OAuth state."""

    config = configuration(
        session_secret="too-short",
    )

    with pytest.raises(
        QuickBooksOAuthStateError,
        match="at least 32",
    ):
        create_quickbooks_authorization_request(
            config,
            now=NOW,
        )


def test_authorization_timestamps_use_bson_precision() -> None:
    """Signed timestamps survive a MongoDB round trip exactly."""

    precise_now = NOW.replace(
        microsecond=123456,
    )
    config = configuration()

    request = create_quickbooks_authorization_request(
        config,
        now=precise_now,
    )
    claims = verify_quickbooks_authorization_state(
        request.state,
        configuration=config,
        now=precise_now,
    )

    assert claims.issued_at.microsecond == 123000
    assert claims.expires_at.microsecond == 123000
