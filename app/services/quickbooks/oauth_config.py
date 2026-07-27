"""Build validated QuickBooks Online OAuth configuration."""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlsplit

from app.core.config import Settings
from app.models.quickbooks import (
    QBO_PRODUCTION_API_BASE_URL,
    QBO_SANDBOX_API_BASE_URL,
    QuickBooksEnvironment,
    QuickBooksOAuthConfiguration,
)


class QuickBooksConfigurationError(ValueError):
    """QuickBooks OAuth configuration is missing or unsafe."""


def build_quickbooks_oauth_configuration(
    settings: Settings,
) -> QuickBooksOAuthConfiguration:
    """Require and validate all settings needed by QBO OAuth."""

    required = {
        "QBO_CLIENT_ID": settings.qbo_client_id,
        "QBO_CLIENT_SECRET": settings.qbo_client_secret,
        "QBO_REDIRECT_URI": settings.qbo_redirect_uri,
        "TOKEN_ENCRYPTION_KEY": (settings.token_encryption_key),
        "SESSION_SECRET": settings.session_secret,
    }
    missing = tuple(name for name, value in required.items() if value is None)

    if missing:
        raise QuickBooksConfigurationError(
            "QuickBooks OAuth is not configured; missing: " + ", ".join(missing)
        )

    client_id = settings.qbo_client_id
    client_secret = settings.qbo_client_secret
    redirect_uri = settings.qbo_redirect_uri
    token_encryption_key = settings.token_encryption_key
    session_secret = settings.session_secret

    assert client_id is not None
    assert client_secret is not None
    assert redirect_uri is not None
    assert token_encryption_key is not None
    assert session_secret is not None

    environment = QuickBooksEnvironment(settings.qbo_environment)
    validated_redirect_uri = _validate_redirect_uri(
        redirect_uri,
        environment=environment,
    )
    api_base_url = (
        QBO_SANDBOX_API_BASE_URL
        if environment is QuickBooksEnvironment.SANDBOX
        else QBO_PRODUCTION_API_BASE_URL
    )

    return QuickBooksOAuthConfiguration(
        environment=environment,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=validated_redirect_uri,
        token_encryption_key=token_encryption_key,
        session_secret=session_secret,
        api_base_url=api_base_url,
    )


def _validate_redirect_uri(
    value: str,
    *,
    environment: QuickBooksEnvironment,
) -> str:
    """Validate the redirect URI against Intuit environment rules."""

    normalized = value.strip()
    parsed = urlsplit(normalized)

    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None:
        raise QuickBooksConfigurationError("QBO_REDIRECT_URI must be an absolute HTTP or HTTPS URL")

    if parsed.username is not None or parsed.password is not None:
        raise QuickBooksConfigurationError("QBO_REDIRECT_URI cannot contain credentials")

    if parsed.fragment:
        raise QuickBooksConfigurationError("QBO_REDIRECT_URI cannot contain a fragment")

    hostname = parsed.hostname.casefold()

    if environment is QuickBooksEnvironment.SANDBOX:
        if parsed.scheme == "http" and hostname != "localhost":
            raise QuickBooksConfigurationError("Sandbox HTTP redirect URIs must use localhost")
    else:
        if parsed.scheme != "https":
            raise QuickBooksConfigurationError("Production QBO redirect URIs must use HTTPS")

        try:
            ip_address(hostname)
        except ValueError:
            pass
        else:
            raise QuickBooksConfigurationError(
                "Production QBO redirect URIs cannot use an IP address"
            )

    return normalized
