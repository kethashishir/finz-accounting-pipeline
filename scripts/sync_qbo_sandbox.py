"""Guarded live synchronization to the BrightFix QBO sandbox."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.db.client import MongoDatabase
from app.models.quickbooks import QuickBooksEnvironment
from app.models.quickbooks_sync import (
    QuickBooksSyncError,
    QuickBooksSyncStatus,
)
from app.repositories.quickbooks_connection import (
    QuickBooksConnectionRepository,
)
from app.repositories.quickbooks_sync import (
    QuickBooksSyncRepository,
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
from app.services.quickbooks.live_sync import (
    LIVE_SYNC_CONFIRMATION,
    require_live_sync_confirmation,
    synchronize_quickbooks_inventory,
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
STALE_IN_PROGRESS_AFTER = timedelta(minutes=15)

EXPECTED_TOTAL_TRANSACTIONS = 200
EXPECTED_CANONICAL_TRANSACTIONS = 195
EXPECTED_DUPLICATES = 5
EXPECTED_CLASSIFICATIONS = 195
EXPECTED_SINGLE_PLANS = 183
EXPECTED_TRANSFER_PLANS = 6
EXPECTED_PLAN_COUNT = 189
EXPECTED_TOTAL_MOVEMENT = Decimal("621220.00")
EXPECTED_REVENUE = Decimal("300275.00")
EXPECTED_COGS = Decimal("93850.00")
EXPECTED_OPERATING_EXPENSES = Decimal("138245.00")
EXPECTED_NET_INCOME = Decimal("68180.00")


def parse_args() -> argparse.Namespace:
    """Parse the deliberate live-write confirmation."""

    parser = argparse.ArgumentParser(
        description=(
            "Synchronize validated challenge transactions to the connected QuickBooks sandbox."
        )
    )
    parser.add_argument(
        "--confirm",
        required=True,
        help=(f"Exact required value: {LIVE_SYNC_CONFIRMATION}"),
    )
    return parser.parse_args()


async def main(
    confirmation: str,
) -> None:
    """Execute the guarded serial sandbox synchronization."""

    require_live_sync_confirmation(confirmation)

    settings = Settings()
    configuration = build_quickbooks_oauth_configuration(settings)
    environment = QuickBooksEnvironment.SANDBOX

    if configuration.environment is not environment:
        raise RuntimeError(
            "Live challenge synchronization requires the QuickBooks sandbox environment"
        )

    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=settings.mongodb_database,
    )

    try:
        connection_repository = QuickBooksConnectionRepository(mongodb.database)
        source_repository = QuickBooksSyncSourceRepository(mongodb.database)
        sync_repository = QuickBooksSyncRepository(mongodb.database)
        sync_collection = mongodb.database["quickbooks_sync_records"]

        await sync_repository.ensure_indexes()

        connection = await _single_connection(
            connection_repository,
            environment=environment,
        )
        cipher = QuickBooksTokenCipher.from_secret(configuration.token_encryption_key)
        tokens = cipher.decrypt_token_set(connection)
        initial_token_refreshed = False
        now = datetime.now(UTC)

        if tokens.access_token_expires_at <= now + TOKEN_REFRESH_BUFFER:
            connection, tokens = await _refresh_tokens(
                repository=connection_repository,
                connection=connection,
                tokens=tokens,
                cipher=cipher,
                configuration=configuration,
            )
            initial_token_refreshed = True

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
                raise RuntimeError(
                    "Connected QuickBooks company does not match the challenge company"
                )

            inventory = build_quickbooks_sync_inventory(
                transactions=(snapshot.transactions),
                classifications=(snapshot.classifications),
                qbo_accounts=tuple(qbo_accounts),
            )
            accounting = summarize_quickbooks_posting_plans(inventory.plans)

            _assert_preflight(
                inventory=inventory,
                accounting=accounting,
            )

            recovered = await _recover_stale_in_progress(
                repository=sync_repository,
                collection=sync_collection,
                environment=environment,
                realm_id=tokens.realm_id,
            )

            print("QuickBooks live synchronization preflight: PASS")
            print(f"Connected company: {company.company_name}")
            print(f"Initial access token refreshed: {initial_token_refreshed}")
            print(f"Canonical source transactions: {inventory.canonical_transactions}")
            print(f"Planned JournalEntries: {inventory.plan_count}")
            print(f"Blocked transactions: {inventory.blocked_transactions}")
            print(f"Stale attempts recovered: {recovered}")

            async def is_already_succeeded(
                request_id: str,
            ) -> bool:
                document = await sync_collection.find_one(
                    {
                        "environment": (environment.value),
                        "realm_id": tokens.realm_id,
                        "plan.request_id": request_id,
                        "status": (QuickBooksSyncStatus.SUCCEEDED.value),
                    },
                    projection={
                        "_id": 1,
                    },
                )
                return document is not None

            async def refresh_access_token():
                nonlocal connection, tokens

                connection, tokens = await _refresh_tokens(
                    repository=(connection_repository),
                    connection=connection,
                    tokens=tokens,
                    cipher=cipher,
                    configuration=configuration,
                )

                return tokens.access_token

            def report_progress(
                current: int,
                total: int,
            ) -> None:
                if current == total or current % 10 == 0:
                    print(f"Synchronized plans: {current}/{total}")

            result = await synchronize_quickbooks_inventory(
                repository=sync_repository,
                client=api_client,
                environment=environment,
                realm_id=tokens.realm_id,
                access_token=tokens.access_token,
                plans=inventory.plans,
                is_already_succeeded=(is_already_succeeded),
                refresh_access_token=(refresh_access_token),
                sleep=asyncio.sleep,
                progress=report_progress,
                max_attempts=3,
                success_delay_seconds=0.1,
            )

        status_counts = {}

        for status in QuickBooksSyncStatus:
            status_counts[status] = await sync_collection.count_documents(
                {
                    "environment": (environment.value),
                    "realm_id": tokens.realm_id,
                    "status": status.value,
                }
            )

        if status_counts[QuickBooksSyncStatus.SUCCEEDED] != EXPECTED_PLAN_COUNT:
            raise RuntimeError("Unexpected succeeded sync-record count")

        for status in (
            QuickBooksSyncStatus.PENDING,
            QuickBooksSyncStatus.IN_PROGRESS,
            QuickBooksSyncStatus.RETRYABLE_ERROR,
            QuickBooksSyncStatus.PERMANENT_ERROR,
        ):
            if status_counts[status] != 0:
                raise RuntimeError(
                    "Nonterminal QuickBooks sync records "
                    f"remain in state {status.value}: "
                    f"{status_counts[status]}"
                )

        print("QuickBooks live synchronization: PASS")
        print(f"Planned JournalEntries: {result.plan_count}")
        print(f"Newly succeeded: {result.newly_succeeded}")
        print(f"Previously succeeded and reused: {result.reused_succeeded}")
        print(f"Retry attempts: {result.retry_attempts}")
        print(f"Mid-run token refreshes: {result.token_refreshes}")
        print(f"Persisted succeeded records: {status_counts[QuickBooksSyncStatus.SUCCEEDED]}")
        print("QuickBooks transaction identifiers were persisted but not displayed.")
    finally:
        await mongodb.close()


async def _single_connection(
    repository: QuickBooksConnectionRepository,
    *,
    environment: QuickBooksEnvironment,
):
    """Return the only stored sandbox connection."""

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


async def _refresh_tokens(
    *,
    repository,
    connection,
    tokens,
    cipher,
    configuration,
):
    """Refresh and atomically persist encrypted OAuth tokens."""

    now = datetime.now(UTC)

    async with create_quickbooks_oauth_client(configuration) as oauth_client:
        refreshed_tokens = await oauth_client.refresh_tokens(
            refresh_token=tokens.refresh_token,
            realm_id=tokens.realm_id,
            now=now,
        )

    encrypted = cipher.encrypt_token_set(refreshed_tokens)
    refreshed_connection = await repository.rotate_tokens(
        encrypted,
        expected_revision=connection.revision,
        stored_at=refreshed_tokens.issued_at,
    )

    return (
        refreshed_connection,
        refreshed_tokens,
    )


async def _recover_stale_in_progress(
    *,
    repository: QuickBooksSyncRepository,
    collection,
    environment: QuickBooksEnvironment,
    realm_id: str,
) -> int:
    """Recover abandoned attempts while rejecting active work."""

    now = datetime.now(UTC)
    stale_before = now - STALE_IN_PROGRESS_AFTER
    cursor = collection.find(
        {
            "environment": environment.value,
            "realm_id": realm_id,
            "status": (QuickBooksSyncStatus.IN_PROGRESS.value),
        }
    )
    recovered = 0

    async for document in cursor:
        updated_at = document["updated_at"]

        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        else:
            updated_at = updated_at.astimezone(UTC)

        if updated_at > stale_before:
            raise RuntimeError("A recent QuickBooks synchronization attempt is still in progress")

        await repository.mark_retryable_error(
            environment=environment,
            realm_id=realm_id,
            request_id=document["plan"]["request_id"],
            expected_attempt_count=document["attempt_count"],
            error=QuickBooksSyncError(
                code="stale_in_progress_recovery",
                message=("Recovered an abandoned in-progress attempt for an idempotent retry."),
                retryable=True,
                occurred_at=now,
            ),
        )
        recovered += 1

    return recovered


def _assert_preflight(
    *,
    inventory,
    accounting,
) -> None:
    """Require the accepted challenge counts and totals."""

    expected_values = {
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
        "classifications": (
            inventory.classifications,
            EXPECTED_CLASSIFICATIONS,
        ),
        "single plans": (
            inventory.single_plans,
            EXPECTED_SINGLE_PLANS,
        ),
        "transfer plans": (
            inventory.transfer_plans,
            EXPECTED_TRANSFER_PLANS,
        ),
        "plan count": (
            inventory.plan_count,
            EXPECTED_PLAN_COUNT,
        ),
        "syncable transactions": (
            inventory.syncable_transactions,
            EXPECTED_CANONICAL_TRANSACTIONS,
        ),
        "blocked transactions": (
            inventory.blocked_transactions,
            0,
        ),
        "total debits": (
            accounting.total_debits,
            EXPECTED_TOTAL_MOVEMENT,
        ),
        "total credits": (
            accounting.total_credits,
            EXPECTED_TOTAL_MOVEMENT,
        ),
        "revenue": (
            accounting.revenue,
            EXPECTED_REVENUE,
        ),
        "COGS": (
            accounting.cost_of_goods_sold,
            EXPECTED_COGS,
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
    ) in expected_values.items():
        if actual != expected:
            raise RuntimeError(
                f"Unexpected preflight {label}: expected {expected}, received {actual}"
            )


if __name__ == "__main__":
    arguments = parse_args()
    asyncio.run(main(arguments.confirm))
