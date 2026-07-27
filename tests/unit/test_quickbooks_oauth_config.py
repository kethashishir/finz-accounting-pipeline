"""Tests for secret-safe QuickBooks OAuth configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.models.quickbooks import (
    INTUIT_AUTHORIZATION_ENDPOINT,
    INTUIT_TOKEN_ENDPOINT,
    QBO_ACCOUNTING_SCOPE,
    QBO_PRODUCTION_API_BASE_URL,
    QBO_SANDBOX_API_BASE_URL,
    QuickBooksEnvironment,
    QuickBooksOAuthConfiguration,
)
from app.services.quickbooks.oauth_config import (
    QuickBooksConfigurationError,
    build_quickbooks_oauth_configuration,
)

CLIENT_SECRET = "qbo-client-secret-value"
TOKEN_KEY = "token-encryption-key-value"
SESSION_SECRET = "session-secret-value"


def configured_settings(
    *,
    environment: str = "sandbox",
    redirect_uri: str = ("http://localhost:8000/api/v1/quickbooks/callback"),
) -> Settings:
    """Create complete test-only QBO settings."""

    return Settings(
        _env_file=None,
        qbo_environment=environment,
        qbo_client_id="test-client-id",
        qbo_client_secret=CLIENT_SECRET,
        qbo_redirect_uri=redirect_uri,
        token_encryption_key=TOKEN_KEY,
        session_secret=SESSION_SECRET,
    )


def test_blank_quickbooks_settings_are_disabled() -> None:
    """Blank environment values do not become fake credentials."""

    settings = Settings(
        _env_file=None,
        qbo_client_id=" ",
        qbo_client_secret="",
        qbo_redirect_uri=" ",
        token_encryption_key="",
        session_secret=" ",
    )

    assert settings.qbo_client_id is None
    assert settings.qbo_client_secret is None
    assert settings.qbo_redirect_uri is None
    assert settings.token_encryption_key is None
    assert settings.session_secret is None


def test_sandbox_configuration_uses_accounting_contract() -> None:
    """Sandbox OAuth selects trusted endpoints and one scope."""

    configuration = build_quickbooks_oauth_configuration(configured_settings())

    assert configuration.environment is QuickBooksEnvironment.SANDBOX
    assert configuration.api_base_url == (QBO_SANDBOX_API_BASE_URL)
    assert configuration.authorization_endpoint == (INTUIT_AUTHORIZATION_ENDPOINT)
    assert configuration.token_endpoint == (INTUIT_TOKEN_ENDPOINT)
    assert configuration.scopes == (QBO_ACCOUNTING_SCOPE,)
    assert configuration.scope_value == (QBO_ACCOUNTING_SCOPE)


def test_missing_configuration_is_reported_together() -> None:
    """OAuth fails explicitly without partially configured secrets."""

    with pytest.raises(
        QuickBooksConfigurationError,
        match=(
            "QBO_CLIENT_ID.*QBO_CLIENT_SECRET.*"
            "QBO_REDIRECT_URI.*TOKEN_ENCRYPTION_KEY.*"
            "SESSION_SECRET"
        ),
    ):
        build_quickbooks_oauth_configuration(Settings(_env_file=None))


def test_sandbox_http_redirect_requires_localhost() -> None:
    """Plain HTTP cannot send sandbox codes to a remote host."""

    with pytest.raises(
        QuickBooksConfigurationError,
        match="must use localhost",
    ):
        build_quickbooks_oauth_configuration(
            configured_settings(redirect_uri=("http://example.com/api/v1/quickbooks/callback"))
        )


def test_production_requires_https_redirect() -> None:
    """Production authorization codes require transport security."""

    with pytest.raises(
        QuickBooksConfigurationError,
        match="must use HTTPS",
    ):
        build_quickbooks_oauth_configuration(
            configured_settings(
                environment="production",
            )
        )


def test_production_configuration_uses_production_api() -> None:
    """A secure production redirect selects the production API."""

    configuration = build_quickbooks_oauth_configuration(
        configured_settings(
            environment="production",
            redirect_uri=("https://finz.example.com/api/v1/quickbooks/callback"),
        )
    )

    assert configuration.environment is QuickBooksEnvironment.PRODUCTION
    assert configuration.api_base_url == (QBO_PRODUCTION_API_BASE_URL)


def test_production_redirect_rejects_ip_address() -> None:
    """Production redirect hosts must not be literal IP addresses."""

    with pytest.raises(
        QuickBooksConfigurationError,
        match="cannot use an IP address",
    ):
        build_quickbooks_oauth_configuration(
            configured_settings(
                environment="production",
                redirect_uri=("https://192.0.2.10/api/v1/quickbooks/callback"),
            )
        )


def test_secret_values_are_redacted() -> None:
    """Pydantic representations never expose QBO secrets."""

    configuration = build_quickbooks_oauth_configuration(configured_settings())

    representation = repr(configuration)
    serialized = configuration.model_dump_json()

    for secret in (
        CLIENT_SECRET,
        TOKEN_KEY,
        SESSION_SECRET,
    ):
        assert secret not in representation
        assert secret not in serialized

    assert configuration.client_secret.get_secret_value() == CLIENT_SECRET


def test_trusted_intuit_endpoints_cannot_be_replaced() -> None:
    """Constructed configuration rejects arbitrary OAuth servers."""

    valid = build_quickbooks_oauth_configuration(configured_settings())
    values = valid.model_dump()
    values["token_endpoint"] = "https://attacker.example/token"

    with pytest.raises(
        ValidationError,
        match="Unexpected Intuit token endpoint",
    ):
        QuickBooksOAuthConfiguration.model_validate(values)
