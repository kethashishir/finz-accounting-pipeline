"""Reconcile internal and QBO cash-basis Profit and Loss reports."""

# ruff: noqa: E402

from __future__ import annotations

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
from app.repositories.quickbooks_connection import (
    QuickBooksConnectionRepository,
)
from app.repositories.reporting import (
    ProfitAndLossRepository,
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
from app.services.quickbooks.profit_and_loss import (
    parse_quickbooks_profit_and_loss,
)
from app.services.quickbooks.reconciliation import (
    reconcile_profit_and_loss,
)
from app.services.quickbooks.token_crypto import (
    QuickBooksTokenCipher,
)
from app.services.reporting.profit_and_loss import (
    generate_profit_and_loss_report_set,
)

CATALOG_PATH = PROJECT_ROOT / "sample_config" / "chart_of_accounts.json"
TOKEN_REFRESH_BUFFER = timedelta(minutes=5)


async def main() -> None:
    """Run all four required live reconciliations."""

    settings = Settings()
    configuration = build_quickbooks_oauth_configuration(settings)
    environment = QuickBooksEnvironment.SANDBOX

    if configuration.environment is not environment:
        raise RuntimeError("Reconciliation requires the QBO sandbox")

    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=settings.mongodb_database,
    )

    try:
        connection_repository = QuickBooksConnectionRepository(mongodb.database)
        reporting_repository = ProfitAndLossRepository(mongodb.database)
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

        chart = load_chart_of_accounts(CATALOG_PATH)
        internal_reports = await generate_profit_and_loss_report_set(
            source_reader=reporting_repository,
            start_date=datetime(
                2026,
                4,
                1,
                tzinfo=UTC,
            ).date(),
            end_date=datetime(
                2026,
                6,
                30,
                tzinfo=UTC,
            ).date(),
            currency="USD",
            chart_of_accounts=chart,
        )
        statements = internal_reports.monthly + (internal_reports.consolidated,)

        async with create_quickbooks_api_client(configuration) as api_client:
            company = await api_client.get_company_info(
                access_token=tokens.access_token,
                realm_id=tokens.realm_id,
            )
            qbo_accounts = tuple(
                await api_client.query_accounts(
                    access_token=tokens.access_token,
                    realm_id=tokens.realm_id,
                )
            )

            if company.company_name != chart.company_name:
                raise RuntimeError("Connected company does not match the challenge company")

            controlled_lines = (
                internal_reports.consolidated.revenue_accounts
                + internal_reports.consolidated.cost_of_goods_sold_accounts
                + internal_reports.consolidated.operating_expense_accounts
            )
            controlled_account_numbers = {line.account_number for line in controlled_lines}
            qbo_account_ids_by_number = {
                account.account_number: account.id
                for account in qbo_accounts
                if account.account_number is not None
            }
            missing_account_numbers = sorted(
                controlled_account_numbers - set(qbo_account_ids_by_number)
            )

            if missing_account_numbers:
                raise RuntimeError(
                    "Required numbered QBO P&L accounts "
                    "were not found: " + ", ".join(missing_account_numbers)
                )

            controlled_account_ids = tuple(
                qbo_account_ids_by_number[account_number]
                for account_number in sorted(controlled_account_numbers)
            )

            results = []

            for internal in statements:
                payload = await api_client.get_profit_and_loss_report(
                    access_token=tokens.access_token,
                    realm_id=tokens.realm_id,
                    start_date=internal.start_date,
                    end_date=internal.end_date,
                    account_ids=controlled_account_ids,
                )
                qbo_statement = parse_quickbooks_profit_and_loss(
                    payload=payload,
                    qbo_accounts=qbo_accounts,
                    expected_company_name=(company.company_name),
                    expected_start_date=(internal.start_date),
                    expected_end_date=(internal.end_date),
                )
                results.append(
                    reconcile_profit_and_loss(
                        internal=internal,
                        quickbooks=qbo_statement,
                    )
                )

        print("QuickBooks P&L reconciliation")
        print(f"Connected company: {chart.company_name}")
        print(f"Access token refreshed: {token_refreshed}")
        print(f"Controlled P&L accounts: {len(controlled_account_ids)}")
        print(f"Periods compared: {len(results)}")

        for result in results:
            print(f"\nPeriod: {result.period_label}")

            for line in result.lines:
                status = "PASS" if line.reconciled else "FAIL"
                print(
                    f"  {line.key} {line.label}: "
                    f"internal={_money(line.internal_amount)}, "
                    f"qbo={_money(line.quickbooks_amount)}, "
                    f"difference={_money(line.difference)}, "
                    f"status={status}"
                )

            print("  Period reconciliation: " + ("PASS" if result.reconciled else "FAIL"))

        failed = [result for result in results if not result.reconciled]

        if failed:
            raise RuntimeError(f"{len(failed)} QBO P&L periods did not reconcile")

        consolidated = statements[-1]

        expected = {
            "revenue": (
                consolidated.total_revenue,
                Decimal("300275.00"),
            ),
            "COGS": (
                consolidated.total_cost_of_goods_sold,
                Decimal("93850.00"),
            ),
            "gross profit": (
                consolidated.gross_profit,
                Decimal("206425.00"),
            ),
            "operating expenses": (
                consolidated.total_operating_expenses,
                Decimal("138245.00"),
            ),
            "net profit": (
                consolidated.net_profit,
                Decimal("68180.00"),
            ),
        }

        for label, (actual, required) in expected.items():
            if actual != required:
                raise RuntimeError(f"Unexpected consolidated {label}")

        print("\nQuickBooks cash-basis P&L reconciliation: PASS")
        print("All monthly and consolidated accounts and totals reconcile exactly.")
    finally:
        await mongodb.close()


async def _single_connection(
    repository: QuickBooksConnectionRepository,
    *,
    environment: QuickBooksEnvironment,
):
    """Return the only sandbox connection."""

    cursor = repository.connections.find(
        {"environment": environment.value},
        projection={"realm_id": 1},
    )
    realm_ids = [document["realm_id"] async for document in cursor]

    if len(realm_ids) != 1:
        raise RuntimeError("Expected exactly one QBO sandbox connection")

    connection = await repository.find(
        environment=environment,
        realm_id=realm_ids[0],
    )

    if connection is None:
        raise RuntimeError("Stored QBO connection disappeared")

    return connection


def _money(value: Decimal) -> str:
    """Format exact currency without floats."""

    return f"${value:,.2f}"


if __name__ == "__main__":
    asyncio.run(main())
