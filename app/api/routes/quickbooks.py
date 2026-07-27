"""QuickBooks Online OAuth connection endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    status,
)
from fastapi.responses import RedirectResponse

from app.models.quickbooks import (
    QuickBooksConnectionRecord,
    QuickBooksConnectionStatus,
    QuickBooksOAuthConfiguration,
)
from app.repositories.quickbooks import (
    QuickBooksOAuthStateAlreadyConsumedError,
    QuickBooksOAuthStatePersistenceError,
)
from app.repositories.quickbooks_connection import (
    QuickBooksConnectionPersistenceError,
)
from app.services.quickbooks.authorization import (
    QuickBooksOAuthStateError,
    create_quickbooks_authorization_request,
    verify_quickbooks_authorization_state,
)
from app.services.quickbooks.oauth_client import (
    QuickBooksOAuthClient,
    QuickBooksOAuthClientError,
    create_quickbooks_oauth_client,
)
from app.services.quickbooks.oauth_config import (
    QuickBooksConfigurationError,
    build_quickbooks_oauth_configuration,
)
from app.services.quickbooks.token_crypto import (
    QuickBooksTokenCipher,
    QuickBooksTokenEncryptionError,
)

router = APIRouter(
    prefix="/quickbooks",
    tags=["quickbooks"],
)


@router.get(
    "/connect",
    response_class=RedirectResponse,
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
)
async def connect_quickbooks(
    request: Request,
) -> RedirectResponse:
    """Register single-use state and redirect to Intuit OAuth."""

    configuration = _configuration(request)

    try:
        authorization = create_quickbooks_authorization_request(configuration)
        claims = verify_quickbooks_authorization_state(
            authorization.state,
            configuration=configuration,
        )
        await request.app.state.quickbooks_oauth_state_repository.register(claims)
    except QuickBooksOAuthStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("QuickBooks OAuth security configuration is invalid"),
        ) from exc
    except QuickBooksOAuthStatePersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=("QuickBooks authorization could not be started safely"),
        ) from exc

    return RedirectResponse(
        url=authorization.authorization_url,
        status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    )


@router.get(
    "/callback",
    response_model=QuickBooksConnectionStatus,
)
async def quickbooks_callback(
    request: Request,
    state_value: Annotated[
        str,
        Query(
            alias="state",
            min_length=1,
            max_length=4096,
        ),
    ],
    code: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=512,
        ),
    ] = None,
    realm_id: Annotated[
        str | None,
        Query(
            alias="realmId",
            min_length=1,
            max_length=64,
        ),
    ] = None,
    error: Annotated[
        str | None,
        Query(
            min_length=1,
            max_length=256,
        ),
    ] = None,
) -> QuickBooksConnectionStatus:
    """Validate callback, exchange code, and persist encrypted tokens."""

    configuration = _configuration(request)

    try:
        claims = verify_quickbooks_authorization_state(
            state_value,
            configuration=configuration,
        )
    except QuickBooksOAuthStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="QuickBooks OAuth state is invalid",
        ) from exc

    try:
        await request.app.state.quickbooks_oauth_state_repository.consume(claims)
    except QuickBooksOAuthStateAlreadyConsumedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=("QuickBooks OAuth callback was already processed"),
        ) from exc
    except QuickBooksOAuthStatePersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("QuickBooks OAuth state was not registered or is no longer valid"),
        ) from exc

    if error is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("QuickBooks authorization was not completed"),
        )

    if code is None or realm_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=("QuickBooks callback requires code and realmId"),
        )

    oauth_client = _oauth_client(
        request,
        configuration,
    )

    try:
        async with oauth_client:
            token_set = await oauth_client.exchange_authorization_code(
                code=code,
                realm_id=realm_id,
            )
    except QuickBooksOAuthClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=("QuickBooks token exchange failed"),
        ) from exc

    try:
        token_cipher = QuickBooksTokenCipher.from_secret(configuration.token_encryption_key)
        encrypted = token_cipher.encrypt_token_set(token_set)
        repository = request.app.state.quickbooks_connection_repository
        existing = await repository.find(
            environment=configuration.environment,
            realm_id=realm_id,
        )

        if existing is None:
            await repository.save_initial(
                encrypted,
                stored_at=token_set.issued_at,
            )
            stored = await repository.find(
                environment=configuration.environment,
                realm_id=realm_id,
            )

            if stored is None:
                raise RuntimeError("QuickBooks connection was not stored")
        else:
            stored = await repository.rotate_tokens(
                encrypted,
                expected_revision=existing.revision,
                stored_at=token_set.issued_at,
            )
    except QuickBooksTokenEncryptionError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("QuickBooks token encryption is not configured safely"),
        ) from exc
    except QuickBooksConnectionPersistenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=("QuickBooks company connection changed during authorization"),
        ) from exc

    return _connection_status(stored)


def _configuration(
    request: Request,
) -> QuickBooksOAuthConfiguration:
    """Build complete OAuth configuration without exposing secrets."""

    try:
        return build_quickbooks_oauth_configuration(request.app.state.settings)
    except QuickBooksConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="QuickBooks OAuth is not configured",
        ) from exc


def _oauth_client(
    request: Request,
    configuration: QuickBooksOAuthConfiguration,
) -> QuickBooksOAuthClient:
    """Use an injected test factory or the production HTTP client."""

    factory = getattr(
        request.app.state,
        "quickbooks_oauth_client_factory",
        create_quickbooks_oauth_client,
    )

    return factory(configuration)


def _connection_status(
    connection: QuickBooksConnectionRecord,
) -> QuickBooksConnectionStatus:
    """Return only non-secret connection metadata."""

    return QuickBooksConnectionStatus(
        environment=connection.environment,
        realm_id=connection.realm_id,
        revision=connection.revision,
        access_token_expires_at=(connection.access_token_expires_at),
        refresh_token_expires_at=(connection.refresh_token_expires_at),
    )
