"""Create and validate QuickBooks Online authorization requests."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from pydantic import ValidationError

from app.models.quickbooks import (
    QuickBooksAuthorizationRequest,
    QuickBooksAuthorizationState,
    QuickBooksOAuthConfiguration,
)

OAUTH_STATE_LIFETIME = timedelta(minutes=10)
OAUTH_STATE_CLOCK_SKEW = timedelta(seconds=30)
MINIMUM_SESSION_SECRET_BYTES = 32
MAXIMUM_STATE_TOKEN_LENGTH = 4096


class QuickBooksOAuthStateError(ValueError):
    """QuickBooks OAuth state is invalid, expired, or untrusted."""


def create_quickbooks_authorization_request(
    configuration: QuickBooksOAuthConfiguration,
    *,
    now: datetime | None = None,
) -> QuickBooksAuthorizationRequest:
    """Create an Intuit authorization URL with signed CSRF state."""

    issued_at = _normalized_now(now)
    expires_at = issued_at + OAUTH_STATE_LIFETIME
    secret = _session_secret(configuration)

    claims = QuickBooksAuthorizationState(
        nonce=secrets.token_urlsafe(32),
        environment=configuration.environment,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    state = _encode_state(
        claims,
        secret=secret,
    )

    query = urlencode(
        {
            "client_id": configuration.client_id,
            "response_type": "code",
            "scope": configuration.scope_value,
            "redirect_uri": configuration.redirect_uri,
            "state": state,
        }
    )
    authorization_url = f"{configuration.authorization_endpoint}?{query}"

    return QuickBooksAuthorizationRequest(
        authorization_url=authorization_url,
        state=state,
        expires_at=expires_at,
    )


def verify_quickbooks_authorization_state(
    state: str,
    *,
    configuration: QuickBooksOAuthConfiguration,
    now: datetime | None = None,
) -> QuickBooksAuthorizationState:
    """Verify signature, environment, timestamps, and expiration."""

    if not state or len(state) > MAXIMUM_STATE_TOKEN_LENGTH:
        raise QuickBooksOAuthStateError("QuickBooks OAuth state has an invalid length")

    parts = state.split(".")

    if len(parts) != 2 or not all(parts):
        raise QuickBooksOAuthStateError("QuickBooks OAuth state has an invalid format")

    payload_segment, signature_segment = parts
    secret = _session_secret(configuration)

    expected_signature = hmac.new(
        secret,
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()

    try:
        supplied_signature = _decode_base64url(signature_segment)
    except (
        ValueError,
        binascii.Error,
    ) as exc:
        raise QuickBooksOAuthStateError("QuickBooks OAuth state signature is malformed") from exc

    if not hmac.compare_digest(
        supplied_signature,
        expected_signature,
    ):
        raise QuickBooksOAuthStateError("QuickBooks OAuth state signature is invalid")

    try:
        payload = _decode_base64url(payload_segment)
        claims = QuickBooksAuthorizationState.model_validate_json(payload)
    except (
        ValueError,
        UnicodeDecodeError,
        binascii.Error,
        ValidationError,
    ) as exc:
        raise QuickBooksOAuthStateError("QuickBooks OAuth state payload is invalid") from exc

    current_time = _normalized_now(now)

    if claims.environment is not configuration.environment:
        raise QuickBooksOAuthStateError(
            "QuickBooks OAuth state environment does not match the configured environment"
        )

    if claims.expires_at - claims.issued_at > OAUTH_STATE_LIFETIME:
        raise QuickBooksOAuthStateError("QuickBooks OAuth state lifetime is invalid")

    if claims.issued_at > current_time + OAUTH_STATE_CLOCK_SKEW:
        raise QuickBooksOAuthStateError("QuickBooks OAuth state was issued in the future")

    if claims.expires_at <= current_time:
        raise QuickBooksOAuthStateError("QuickBooks OAuth state has expired")

    return claims


def _encode_state(
    claims: QuickBooksAuthorizationState,
    *,
    secret: bytes,
) -> str:
    """Serialize and sign state claims with HMAC-SHA256."""

    payload = json.dumps(
        claims.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload_segment = _encode_base64url(payload)
    signature = hmac.new(
        secret,
        payload_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()

    return f"{payload_segment}.{_encode_base64url(signature)}"


def _session_secret(
    configuration: QuickBooksOAuthConfiguration,
) -> bytes:
    """Return a sufficiently strong state-signing secret."""

    secret = configuration.session_secret.get_secret_value().encode("utf-8")

    if len(secret) < MINIMUM_SESSION_SECRET_BYTES:
        raise QuickBooksOAuthStateError("SESSION_SECRET must contain at least 32 UTF-8 bytes")

    return secret


def _normalized_now(
    value: datetime | None,
) -> datetime:
    """Return a timezone-aware UTC timestamp."""

    timestamp = value or datetime.now(UTC)

    if timestamp.utcoffset() is None:
        raise QuickBooksOAuthStateError("QuickBooks OAuth timestamps must be timezone-aware")

    return timestamp.astimezone(UTC)


def _encode_base64url(value: bytes) -> str:
    """Encode URL-safe base64 without padding."""

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str) -> bytes:
    """Decode strict URL-safe base64 with restored padding."""

    padding = "=" * (-len(value) % 4)

    return base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )
