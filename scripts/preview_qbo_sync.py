"""Preview challenge synchronization without creating QBO transactions."""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.db.client import MongoDatabase
from app.models.quickbooks import QuickBooksEnvironment
from app.repositories.quickbooks_connection import (
    QuickBooksConnectionRepository,
)
from app.repositories.quickbooks_sync_source import (
    QuickBooksSyncSourceRepository,
)
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
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
from app.services.quickbooks.sync_inventory import (
    build_quickbooks_sync_inventory,
)
from app.services.quickbooks.sync_preview import (
    summarize_quickbooks_posting_plans,
)
from app.services.quickbooks.token_crypto import (
    QuickBooksTokenCipher,
)

CATALOG_PATH = PROJECT_ROOT / "sample_config" / "chart_of_accounts.json"
TOKEN_REFRESH_BUFFER = timedelta(minutes=5)

EXPECTED_TOTAL_TRANSACTIONS = 200
EXPECTED_CANONICAL_TRANSACTIONS = 195
EXPECTED_DUPLICATES = 5
EXPECTED_INVALID = 0
EXPECTED_CLASSIFICATIONS = 195

EXPECTED_REVENUE = Decimal("300275.00")
EXPECTED_COGS = Decimal("93850.00")
EXPECTED_GROSS_PROFIT = Decimal("206425.00")
EXPECTED_OPERATING_EXPENSES = Decimal("138245.00")
EXPECTED_NET_INCOME = Decimal("68180.00")


async def main() -> None:
    """Run the real-data synchronization preview."""

    settings = Settings()
    configuration = build_quickbooks_oauth_configuration(settings)
    environment = QuickBooksEnvironment.SANDBOX

    if configuration.environment is not environment:
        raise RuntimeError(
            "The synchronization preview requires the QuickBooks sandbox environment"
        )

    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=settings.mongodb_database,
    )

    try:
        connection_repository = QuickBooksConnectionRepository(mongodb.database)
        source_repository = QuickBooksSyncSourceRepository(mongodb.database)

        connection = await _single_connection(
            connection_repository,
            environment=environment,
        )
        cipher = QuickBooksTokenCipher.from_secret(configuration.token_encryption_key)
        tokens = cipher.decrypt_token_set(connection)
        token_refreshed = False
        now = datetime.now(UTC)

        if tokens.access_token_expires_at <= now + TOKEN_REFRESH_BUFFER:
            async with create_quickbooks_oauth_client(configuration) as oauth_client:
                tokens = await oauth_client.refresh_tokens(
                    refresh_token=tokens.refresh_token,
                    realm_id=tokens.realm_id,
                    now=now,
                )

            encrypted = cipher.encrypt_token_set(tokens)
            connection = await connection_repository.rotate_tokens(
                encrypted,
                expected_revision=connection.revision,
                stored_at=tokens.issued_at,
            )
            token_refreshed = True

        snapshot = await source_repository.read_snapshot()
        catalog = load_chart_of_accounts(CATALOG_PATH)

        async with create_quickbooks_api_client(configuration) as api_client:
            company = await api_client.get_company_info(
                access_token=tokens.access_token,
                realm_id=tokens.realm_id,
            )
            qbo_accounts = await api_client.query_accounts(
                access_token=tokens.access_token,
                realm_id=tokens.realm_id,
            )

        if company.company_name != catalog.company_name:
            raise RuntimeError("Connected QuickBooks company does not match the challenge company")

        inventory = build_quickbooks_sync_inventory(
            transactions=snapshot.transactions,
            classifications=snapshot.classifications,
            qbo_accounts=tuple(qbo_accounts),
        )
        accounting = summarize_quickbooks_posting_plans(inventory.plans)

        print("QuickBooks synchronization preview")
        print(f"Connected company: {company.company_name}")
        print(f"Access token refreshed: {token_refreshed}")
        print(f"Live QBO accounts returned: {len(qbo_accounts)}")
        print(f"Source transactions: {inventory.total_transactions}")
        print(f"Canonical transactions: {inventory.canonical_transactions}")
        print(f"Duplicate transactions excluded: {inventory.duplicate_transactions}")
        print(f"Invalid transactions excluded: {inventory.invalid_transactions}")
        print(f"Current classifications: {inventory.classifications}")
        print(f"Single-transaction plans: {inventory.single_plans}")
        print(f"Paired-transfer plans: {inventory.transfer_plans}")
        print(f"Syncable source transactions: {inventory.syncable_transactions}")
        print(f"Planned QBO JournalEntries: {inventory.plan_count}")
        print(f"Blocked canonical transactions: {inventory.blocked_transactions}")

        if inventory.issues:
            issue_counts = Counter(issue.code for issue in inventory.issues)

            print("Blocking issue counts:")

            for code, count in sorted(issue_counts.items()):
                print(f"  {code}: {count}")

        print(f"Total planned debits: {_money(accounting.total_debits)}")
        print(f"Total planned credits: {_money(accounting.total_credits)}")
        print(f"Preview revenue: {_money(accounting.revenue)}")
        print(f"Preview COGS: {_money(accounting.cost_of_goods_sold)}")
        print(f"Preview gross profit: {_money(accounting.gross_profit)}")
        print(f"Preview operating expenses: {_money(accounting.operating_expenses)}")
        print(f"Preview net income: {_money(accounting.net_income)}")

        print("Planned account movements:")

        for account in accounting.account_totals:
            print(
                f"  {account.account_number} "
                f"{account.account_name}: "
                f"debits={_money(account.debits)}, "
                f"credits={_money(account.credits)}, "
                f"net_debit={_money(account.net_debit)}"
            )

        _assert_challenge_acceptance(
            inventory=inventory,
            accounting=accounting,
        )

        print("QuickBooks synchronization preview: PASS")
        print("No QuickBooks transaction was created.")
    finally:
        await mongodb.close()


async def _single_connection(
    repository: QuickBooksConnectionRepository,
    *,
    environment: QuickBooksEnvironment,
):
    """Return the only stored connection for an environment."""

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


def _assert_challenge_acceptance(
    *,
    inventory,
    accounting,
) -> None:
    """Stop before synchronization when evidence differs."""

    expected_counts = {
        "source transactions": (
            inventory.total_transactions,
            EXPECTED_TOTAL_TRANSACTIONS,
        ),
        "canonical transactions": (
            inventory.canonical_transactions,
            EXPECTED_CANONICAL_TRANSACTIONS,
        ),
        "duplicates": (
            inventory.duplicate_transactions,
            EXPECTED_DUPLICATES,
        ),
        "invalid transactions": (
            inventory.invalid_transactions,
            EXPECTED_INVALID,
        ),
        "classifications": (
            inventory.classifications,
            EXPECTED_CLASSIFICATIONS,
        ),
        "syncable transactions": (
            inventory.syncable_transactions,
            EXPECTED_CANONICAL_TRANSACTIONS,
        ),
        "blocked transactions": (
            inventory.blocked_transactions,
            0,
        ),
    }

    for label, (
        actual,
        expected,
    ) in expected_counts.items():
        if actual != expected:
            raise RuntimeError(f"Unexpected {label}: expected {expected}, received {actual}")

    expected_totals = {
        "revenue": (
            accounting.revenue,
            EXPECTED_REVENUE,
        ),
        "COGS": (
            accounting.cost_of_goods_sold,
            EXPECTED_COGS,
        ),
        "gross profit": (
            accounting.gross_profit,
            EXPECTED_GROSS_PROFIT,
        ),
        "operating expenses": (
            accounting.operating_expenses,
            EXPECTED_OPERATING_EXPENSES,
        ),
        "net income": (
            accounting.net_income,
            EXPECTED_NET_INCOME,
        ),
    }

    for label, (
        actual,
        expected,
    ) in expected_totals.items():
        if actual != expected:
            raise RuntimeError(
                f"Unexpected preview {label}: "
                f"expected {_money(expected)}, "
                f"received {_money(actual)}"
            )


def _money(value: Decimal) -> str:
    """Render exact money without floating-point conversion."""

    return f"${value:,.2f}"


if __name__ == "__main__":
    asyncio.run(main())
