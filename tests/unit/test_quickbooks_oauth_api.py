"""Tests for QuickBooks connect and callback endpoints."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.routes.quickbooks import router
from app.core.config import Settings
from app.models.quickbooks import (
    EncryptedQuickBooksTokenSet,
    QuickBooksAuthorizationState,
    QuickBooksConnectionRecord,
    QuickBooksEnvironment,
    QuickBooksOAuthStateRecord,
    QuickBooksTokenSet,
)
from app.repositories.quickbooks import (
    QuickBooksOAuthStateAlreadyConsumedError,
    QuickBooksOAuthStateNotRegisteredError,
)
from app.services.quickbooks.oauth_client import (
    QuickBooksOAuthProviderError,
)

NOW = datetime(
    2030,
    1,
    1,
    12,
    0,
    tzinfo=UTC,
)
REALM_ID = "9341456789012345"
ACCESS_TOKEN = "api-access-token"
REFRESH_TOKEN = "api-refresh-token"


class FakeOAuthStateRepository:
    """Store OAuth state in memory for endpoint tests."""

    def __init__(self) -> None:
        self.registered: dict[
            str,
            QuickBooksAuthorizationState,
        ] = {}
        self.consumed: set[str] = set()

    async def register(
        self,
        state: QuickBooksAuthorizationState,
    ) -> bool:
        self.registered[state.nonce] = state
        return True

    async def consume(
        self,
        state: QuickBooksAuthorizationState,
    ) -> QuickBooksOAuthStateRecord:
        if state.nonce in self.consumed:
            raise QuickBooksOAuthStateAlreadyConsumedError("already consumed")

        if state.nonce not in self.registered:
            raise QuickBooksOAuthStateNotRegisteredError("not registered")

        self.consumed.add(state.nonce)

        return QuickBooksOAuthStateRecord(
            **state.model_dump(),
            consumed_at=state.issued_at + timedelta(seconds=1),
        )


class FakeConnectionRepository:
    """Persist one encrypted connection in memory."""

    def __init__(self) -> None:
        self.connection: QuickBooksConnectionRecord | None = None

    async def find(
        self,
        *,
        environment: QuickBooksEnvironment,
        realm_id: str,
    ) -> QuickBooksConnectionRecord | None:
        return self.connection

    async def save_initial(
        self,
        encrypted: EncryptedQuickBooksTokenSet,
        *,
        stored_at: datetime,
    ) -> bool:
        self.connection = QuickBooksConnectionRecord(
            **encrypted.model_dump(),
            revision=1,
            created_at=stored_at,
            updated_at=stored_at,
        )
        return True

    async def rotate_tokens(
        self,
        encrypted: EncryptedQuickBooksTokenSet,
        *,
        expected_revision: int,
        stored_at: datetime,
    ) -> QuickBooksConnectionRecord:
        assert self.connection is not None

        self.connection = QuickBooksConnectionRecord(
            **encrypted.model_dump(),
            revision=expected_revision + 1,
            created_at=self.connection.created_at,
            updated_at=stored_at,
        )

        return self.connection


class FakeOAuthClient:
    """Return test tokens or a stable provider error."""

    def __init__(
        self,
        *,
        error: Exception | None = None,
    ) -> None:
        self.error = error

    async def exchange_authorization_code(
        self,
        *,
        code: str,
        realm_id: str,
        now: datetime | None = None,
    ) -> QuickBooksTokenSet:
        if self.error is not None:
            raise self.error

        return QuickBooksTokenSet(
            environment=QuickBooksEnvironment.SANDBOX,
            realm_id=realm_id,
            access_token=SecretStr(ACCESS_TOKEN),
            refresh_token=SecretStr(REFRESH_TOKEN),
            issued_at=NOW,
            access_token_expires_at=(NOW + timedelta(hours=1)),
            refresh_token_expires_at=(NOW + timedelta(days=100)),
        )

    async def __aenter__(
        self,
    ) -> FakeOAuthClient:
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        return None


def configured_settings() -> Settings:
    """Build complete test-only QuickBooks settings."""

    return Settings(
        _env_file=None,
        app_env="test",
        qbo_environment="sandbox",
        qbo_client_id="test-client-id",
        qbo_client_secret="test-client-secret",
        qbo_redirect_uri=("http://localhost:8000/api/v1/quickbooks/callback"),
        token_encryption_key=("test-token-encryption-secret-0123456789abcdef"),
        session_secret=("test-session-signing-secret-fedcba9876543210"),
    )


def create_client(
    *,
    settings: Settings | None = None,
    oauth_error: Exception | None = None,
) -> tuple[
    TestClient,
    FakeOAuthStateRepository,
    FakeConnectionRepository,
]:
    """Create an isolated FastAPI OAuth application."""

    application = FastAPI()
    application.include_router(
        router,
        prefix="/api/v1",
    )

    state_repository = FakeOAuthStateRepository()
    connection_repository = FakeConnectionRepository()

    application.state.settings = settings or configured_settings()
    application.state.quickbooks_oauth_state_repository = state_repository
    application.state.quickbooks_connection_repository = connection_repository
    application.state.quickbooks_oauth_client_factory = lambda configuration: FakeOAuthClient(
        error=oauth_error
    )

    return (
        TestClient(
            application,
            follow_redirects=False,
        ),
        state_repository,
        connection_repository,
    )


def begin_authorization(
    client: TestClient,
) -> str:
    """Start authorization and return the signed state."""

    response = client.get("/api/v1/quickbooks/connect")

    assert response.status_code == 307

    location = response.headers["location"]
    parsed = urlsplit(location)
    query = parse_qs(
        parsed.query,
        strict_parsing=True,
    )

    assert parsed.netloc == "appcenter.intuit.com"

    return query["state"][0]


def test_connect_registers_state_and_redirects() -> None:
    """Connect persists state before redirecting to Intuit."""

    client, state_repository, _ = create_client()

    state = begin_authorization(client)

    assert len(state_repository.registered) == 1
    assert state not in repr(configured_settings())


def test_connect_requires_complete_configuration() -> None:
    """Unconfigured environments return a stable 503."""

    client, _, _ = create_client(
        settings=Settings(
            _env_file=None,
            app_env="test",
        )
    )

    response = client.get("/api/v1/quickbooks/connect")

    assert response.status_code == 503
    assert response.json() == {"detail": "QuickBooks OAuth is not configured"}


def test_callback_connects_without_exposing_tokens() -> None:
    """Successful callbacks encrypt and persist QBO tokens."""

    client, state_repository, connection_repository = create_client()
    state = begin_authorization(client)

    response = client.get(
        "/api/v1/quickbooks/callback",
        params={
            "state": state,
            "code": "callback-code",
            "realmId": REALM_ID,
        },
    )

    assert response.status_code == 200

    payload = response.json()

    assert payload["status"] == "connected"
    assert payload["environment"] == "sandbox"
    assert payload["realm_id"] == REALM_ID
    assert payload["revision"] == 1
    assert ACCESS_TOKEN not in response.text
    assert REFRESH_TOKEN not in response.text
    assert len(state_repository.consumed) == 1
    assert connection_repository.connection is not None


def test_provider_denial_consumes_state() -> None:
    """Denied authorization cannot leave reusable state."""

    client, state_repository, _ = create_client()
    state = begin_authorization(client)

    response = client.get(
        "/api/v1/quickbooks/callback",
        params={
            "state": state,
            "error": "access_denied",
        },
    )

    assert response.status_code == 400
    assert len(state_repository.consumed) == 1


def test_callback_replay_is_rejected() -> None:
    """A callback state can authorize exactly once."""

    client, _, _ = create_client()
    state = begin_authorization(client)
    params = {
        "state": state,
        "code": "callback-code",
        "realmId": REALM_ID,
    }

    first = client.get(
        "/api/v1/quickbooks/callback",
        params=params,
    )
    replay = client.get(
        "/api/v1/quickbooks/callback",
        params=params,
    )

    assert first.status_code == 200
    assert replay.status_code == 409
    assert replay.json() == {"detail": ("QuickBooks OAuth callback was already processed")}


def test_token_exchange_failure_is_secret_safe() -> None:
    """Provider failure returns 502 without callback secrets."""

    client, state_repository, _ = create_client(
        oauth_error=QuickBooksOAuthProviderError("provider rejected request")
    )
    state = begin_authorization(client)

    response = client.get(
        "/api/v1/quickbooks/callback",
        params={
            "state": state,
            "code": "secret-callback-code",
            "realmId": REALM_ID,
        },
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "QuickBooks token exchange failed"}
    assert "secret-callback-code" not in response.text
    assert len(state_repository.consumed) == 1
