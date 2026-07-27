"""Configure and validate the connected QBO sandbox company."""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.db.client import MongoDatabase
from app.models.quickbooks import QuickBooksEnvironment
from app.repositories.quickbooks_connection import (
    QuickBooksConnectionRepository,
)
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)
from app.services.quickbooks.account_setup import (
    setup_quickbooks_chart_of_accounts,
)
from app.services.quickbooks.api_client import (
    create_quickbooks_api_client,
)
from app.services.quickbooks.oauth_client import (
    create_quickbooks_oauth_client,
)
from app.services.quickbooks.oauth_config import (
    build_quickbooks_oauth_configuration,
)
from app.services.quickbooks.token_crypto import (
    QuickBooksTokenCipher,
)

CATALOG_PATH = PROJECT_ROOT / "sample_config" / "chart_of_accounts.json"
TOKEN_REFRESH_BUFFER = timedelta(minutes=5)


async def main() -> None:
    """Validate company access and configure required accounts."""

    settings = Settings()
    configuration = build_quickbooks_oauth_configuration(settings)
    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=settings.mongodb_database,
    )
    repository = QuickBooksConnectionRepository(mongodb.database)

    try:
        connection = await _single_connection(
            repository,
            environment=configuration.environment,
        )
        cipher = QuickBooksTokenCipher.from_secret(configuration.token_encryption_key)
        tokens = cipher.decrypt_token_set(connection)
        refreshed = False
        now = datetime.now(UTC)

        if tokens.access_token_expires_at <= now + TOKEN_REFRESH_BUFFER:
            async with create_quickbooks_oauth_client(configuration) as oauth_client:
                tokens = await oauth_client.refresh_tokens(
                    refresh_token=tokens.refresh_token,
                    realm_id=tokens.realm_id,
                    now=now,
                )

            encrypted = cipher.encrypt_token_set(tokens)
            connection = await repository.rotate_tokens(
                encrypted,
                expected_revision=connection.revision,
                stored_at=tokens.issued_at,
            )
            refreshed = True

        catalog = load_chart_of_accounts(CATALOG_PATH)

        async with create_quickbooks_api_client(configuration) as api_client:
            company = await api_client.get_company_info(
                access_token=tokens.access_token,
                realm_id=tokens.realm_id,
            )
            result = await setup_quickbooks_chart_of_accounts(
                client=api_client,
                access_token=tokens.access_token,
                realm_id=tokens.realm_id,
                catalog=catalog,
            )

        print("QuickBooks sandbox account setup: PASS")
        print(f"Connected company: {company.company_name}")
        print(f"Expected company: {catalog.company_name}")
        print(f"Company-name match: {company.company_name == catalog.company_name}")
        print(f"Access token refreshed: {refreshed}")
        print(f"Accounts created: {len(result.created)}")
        print(f"Accounts updated: {len(result.updated)}")
        print(f"Accounts reused: {len(result.reused)}")
        print(f"Required accounts validated: {result.configured_count}")
        print(f"Detail-type differences: {len(result.detail_type_differences)}")

        for difference in result.detail_type_differences:
            print(f"  - {difference}")

        print("OAuth tokens printed: no")
        print("Realm ID printed: no")
    finally:
        await mongodb.close()


async def _single_connection(
    repository: QuickBooksConnectionRepository,
    *,
    environment: QuickBooksEnvironment,
):
    """Load the only connected company for an environment."""

    cursor = repository.connections.find(
        {
            "environment": environment.value,
        },
        projection={
            "realm_id": 1,
        },
    )
    realm_ids = [document["realm_id"] async for document in cursor]

    if not realm_ids:
        raise RuntimeError("No QuickBooks sandbox connection is stored")

    if len(realm_ids) > 1:
        raise RuntimeError("Multiple QuickBooks sandbox connections exist")

    connection = await repository.find(
        environment=environment,
        realm_id=realm_ids[0],
    )

    if connection is None:
        raise RuntimeError("Stored QuickBooks connection disappeared")

    return connection


if __name__ == "__main__":
    asyncio.run(main())
