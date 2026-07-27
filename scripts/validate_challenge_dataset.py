"""Validate the supplied Finz workbook through the real pipeline."""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings
from app.db.client import MongoDatabase
from app.models.accounting import QBOAccountType
from app.models.classification import (
    ClassificationSource,
    ReviewStatus,
    TransactionType,
)
from app.models.ingestion import (
    ColumnMapping,
    FileType,
    IngestionConfig,
    RecordStatus,
    UploadStatus,
)
from app.models.profit_and_loss import ProfitAndLossReportSet
from app.repositories.classification import (
    ClassificationRepository,
)
from app.repositories.classification_pattern import (
    ClassificationPatternRepository,
)
from app.repositories.ingestion import IngestionRepository
from app.repositories.reporting import ProfitAndLossRepository
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)
from app.services.classification.batch_classification import (
    BatchClassificationSummary,
    classify_upload,
)
from app.services.classification.review_actions import (
    finalize_classification_review,
)
from app.services.classification.rule_config import (
    load_deterministic_rule_set,
)
from app.services.ingestion.pipeline import IngestionPipeline
from app.services.reporting.profit_and_loss import (
    generate_profit_and_loss_report_set,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHART_OF_ACCOUNTS_PATH = PROJECT_ROOT / "sample_config" / "chart_of_accounts.json"
CLASSIFICATION_RULES_PATH = PROJECT_ROOT / "sample_config" / "classification_rules.json"

EXPECTED_SHA256 = "0929fafb08003790354e7691172b9926800dc08ff20877ce04233afe9e005484"
EXPECTED_DUPLICATE_SOURCE_IDS = {
    "BF-202604-0001",
    "BF-202605-0071",
    "BF-202605-0096",
    "BF-202606-0136",
    "BF-202606-0171",
}


def parse_args() -> argparse.Namespace:
    """Parse the source workbook path."""

    parser = argparse.ArgumentParser(
        description=(
            "Run the supplied Finz workbook through an isolated "
            "ingestion and classification validation."
        )
    )
    parser.add_argument(
        "--workbook",
        type=Path,
        required=True,
        help="Path to the untouched Finz XLSX workbook",
    )
    return parser.parse_args()


def ingestion_config() -> IngestionConfig:
    """Return the mapping selected from the verified workbook."""

    return IngestionConfig(
        file_type=FileType.XLSX,
        sheet_name="Raw Bank Transactions",
        header_row=4,
        date_format="%Y-%m-%d",
        column_mapping=ColumnMapping(
            source_transaction_id="Bank Transaction ID",
            transaction_date="Transaction Date",
            posted_date="Posted Date",
            description="Description",
            amount="Amount (USD)",
            currency="Currency",
            bank_account="Bank Account",
        ),
    )


def assert_classification_summary(
    summary: BatchClassificationSummary,
) -> None:
    """Validate accounting-safe batch coverage."""

    assert summary.total_records == 200
    assert summary.canonical_transactions == 195
    assert summary.ignored_noncanonical == 5
    assert summary.failed == 0
    assert summary.classified_by_gemini == 0

    accounted_for = (
        summary.already_classified
        + summary.classified_by_learned_pattern
        + summary.classified_by_deterministic_rule
        + summary.classified_by_gemini
        + summary.manual_review_required
    )

    assert accounted_for == 195
    assert len(summary.outcomes) == 195
    assert len({outcome.normalized_transaction_id for outcome in summary.outcomes}) == 195


async def approve_all_classifications(
    *,
    summary: BatchClassificationSummary,
    repository: ClassificationRepository,
) -> int:
    """Approve every stored deterministic classification."""

    transaction_ids = tuple(outcome.normalized_transaction_id for outcome in summary.outcomes)
    stored = await repository.find_by_transaction_ids(transaction_ids)

    assert len(stored) == 195

    reviewed_at = datetime.now(UTC)
    approved_count = 0

    for transaction_id in sorted(
        stored,
        key=str,
    ):
        classification = stored[transaction_id]

        assert classification.review_status is ReviewStatus.PENDING

        result = await finalize_classification_review(
            normalized_transaction_id=transaction_id,
            expected_version=classification.version,
            outcome=ReviewStatus.APPROVED,
            reviewer_id="challenge-dataset-validator",
            reviewed_at=reviewed_at,
            notes=(
                "Approved during real-workbook acceptance "
                "after deterministic classification validation."
            ),
            repository=repository,
        )

        assert result.updated is True
        assert result.classification.review_status is ReviewStatus.APPROVED
        approved_count += 1

    return approved_count


def assert_profit_and_loss_acceptance(
    *,
    report: ProfitAndLossReportSet,
    sources,
    chart_of_accounts,
) -> None:
    """Independently reconcile report totals to approved evidence."""

    assert len(report.monthly) == 3
    assert [
        (
            statement.start_date,
            statement.end_date,
        )
        for statement in report.monthly
    ] == [
        (
            date(2026, 4, 1),
            date(2026, 4, 30),
        ),
        (
            date(2026, 5, 1),
            date(2026, 5, 31),
        ),
        (
            date(2026, 6, 1),
            date(2026, 6, 30),
        ),
    ]

    assert report.consolidated.start_date == date(
        2026,
        4,
        1,
    )
    assert report.consolidated.end_date == date(
        2026,
        6,
        30,
    )
    assert report.consolidated.currency == "USD"

    assert len(sources) == 180
    assert report.consolidated.transaction_count == 180
    assert len(report.consolidated.account_lines) == 17

    expected_transaction_ids = frozenset(source.transaction.id for source in sources)

    assert report.consolidated.transaction_ids == expected_transaction_ids

    expected_account_totals = defaultdict(lambda: Decimal("0.00"))
    expected_account_counts = defaultdict(int)

    expected_month_totals = {
        (
            statement.start_date.year,
            statement.start_date.month,
        ): {
            "revenue": Decimal("0.00"),
            "cogs": Decimal("0.00"),
            "expenses": Decimal("0.00"),
            "count": 0,
        }
        for statement in report.monthly
    }

    for source in sources:
        transaction = source.transaction
        classification = source.classification

        assert transaction.transaction_date is not None
        assert transaction.amount is not None

        transaction_type = classification.decision.transaction_type
        account_number = classification.decision.qbo_account.account_number
        account = chart_of_accounts.require(account_number)

        if transaction_type in {
            TransactionType.REVENUE,
            TransactionType.REFUND,
        }:
            report_amount = transaction.amount
        else:
            report_amount = -transaction.amount

        expected_account_totals[account_number] += report_amount
        expected_account_counts[account_number] += 1

        month_key = (
            transaction.transaction_date.year,
            transaction.transaction_date.month,
        )
        month_totals = expected_month_totals[month_key]
        month_totals["count"] += 1

        if account.qbo_account_type is QBOAccountType.INCOME:
            month_totals["revenue"] += report_amount
        elif account.qbo_account_type is QBOAccountType.COST_OF_GOODS_SOLD:
            month_totals["cogs"] += report_amount
        elif account.qbo_account_type is QBOAccountType.EXPENSES:
            month_totals["expenses"] += report_amount
        else:
            raise AssertionError("Balance-sheet account reached P&L evidence")

    actual_account_totals = {
        line.account_number: line.total for line in report.consolidated.account_lines
    }
    actual_account_counts = {
        line.account_number: len(line.transactions) for line in report.consolidated.account_lines
    }

    assert actual_account_totals == dict(expected_account_totals)
    assert actual_account_counts == dict(expected_account_counts)

    for statement in report.monthly:
        month_key = (
            statement.start_date.year,
            statement.start_date.month,
        )
        expected = expected_month_totals[month_key]

        assert statement.total_revenue == expected["revenue"]
        assert statement.total_cost_of_goods_sold == expected["cogs"]
        assert statement.total_operating_expenses == expected["expenses"]
        assert statement.gross_profit == (expected["revenue"] - expected["cogs"])
        assert statement.net_profit == (
            expected["revenue"] - expected["cogs"] - expected["expenses"]
        )
        assert statement.transaction_count == expected["count"]

    assert (
        sum(statement.transaction_count for statement in report.monthly)
        == report.consolidated.transaction_count
    )


async def validate(workbook_path: Path) -> None:
    """Validate workbook ingestion, classification, approval, and P&L."""

    resolved_path = workbook_path.expanduser().resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Workbook does not exist: {resolved_path}")

    settings = get_settings()
    validation_database_name = f"finz_val_{uuid4().hex}"
    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=validation_database_name,
    )

    ingestion_repository = IngestionRepository(mongodb.database)
    classification_repository = ClassificationRepository(mongodb.database)
    pattern_repository = ClassificationPatternRepository(mongodb.database)
    reporting_repository = ProfitAndLossRepository(mongodb.database)
    pipeline = IngestionPipeline(ingestion_repository)

    chart_of_accounts = load_chart_of_accounts(CHART_OF_ACCOUNTS_PATH)
    rule_set = load_deterministic_rule_set(
        CLASSIFICATION_RULES_PATH,
        chart_of_accounts=chart_of_accounts,
    )

    try:
        await ingestion_repository.ensure_indexes()
        await classification_repository.ensure_indexes()
        await pattern_repository.ensure_indexes()

        result = await pipeline.process(
            file_name=resolved_path.name,
            content=resolved_path.read_bytes(),
            config=ingestion_config(),
        )

        assert result.file_sha256 == EXPECTED_SHA256
        assert result.status == UploadStatus.COMPLETED
        assert result.counts.physical == 200
        assert result.counts.valid == 195
        assert result.counts.invalid == 0
        assert result.counts.duplicate == 5
        assert result.duplicates.within_upload == 5
        assert result.duplicates.across_uploads == 0
        assert result.duplicates.source_id_conflicts == 0
        assert result.warnings == []

        uploads = ingestion_repository.uploads
        raw_records = ingestion_repository.raw_records
        transactions = ingestion_repository.transactions
        classifications = classification_repository.classifications

        assert await uploads.count_documents({}) == 1
        assert await raw_records.count_documents({}) == 200
        assert await transactions.count_documents({}) == 200

        assert (
            await transactions.count_documents(
                {
                    "status": RecordStatus.VALID.value,
                }
            )
            == 195
        )
        assert (
            await transactions.count_documents(
                {
                    "status": RecordStatus.DUPLICATE.value,
                }
            )
            == 5
        )
        assert (
            await transactions.count_documents(
                {
                    "status": RecordStatus.INVALID.value,
                }
            )
            == 0
        )

        duplicate_source_ids = set(
            await transactions.distinct(
                "source_transaction_id",
                {
                    "status": RecordStatus.DUPLICATE.value,
                },
            )
        )
        assert duplicate_source_ids == EXPECTED_DUPLICATE_SOURCE_IDS

        unique_source_ids = await transactions.distinct("source_transaction_id")
        assert len(unique_source_ids) == 195

        assert (
            await transactions.count_documents(
                {
                    "currency": "USD",
                }
            )
            == 200
        )
        assert (
            await transactions.count_documents(
                {
                    "bank_account": "Operating Checking",
                }
            )
            == 194
        )
        assert (
            await transactions.count_documents(
                {
                    "bank_account": "Tax Reserve",
                }
            )
            == 6
        )

        earliest = await transactions.find_one(
            {},
            sort=[("transaction_date", 1)],
        )
        latest = await transactions.find_one(
            {},
            sort=[("transaction_date", -1)],
        )

        assert earliest is not None
        assert latest is not None
        assert earliest["transaction_date"].date() == date(
            2026,
            4,
            1,
        )
        assert latest["transaction_date"].date() == date(
            2026,
            6,
            29,
        )

        first_raw = await raw_records.find_one(
            {
                "source_row_number": 5,
            }
        )
        assert first_raw is not None

        raw_columns = {item["column"]: item for item in first_raw["raw_values"]}
        assert set(raw_columns) == {
            "Source File",
            "Bank Transaction ID",
            "Transaction Date",
            "Posted Date",
            "Description",
            "Amount (USD)",
            "Currency",
            "Bank Account",
        }
        assert raw_columns["Source File"]["value"] == "operating_checking_2026_04.csv"

        duplicate_links = await transactions.count_documents(
            {
                "status": (RecordStatus.DUPLICATE.value),
                "duplicate_of": {
                    "$ne": None,
                },
            }
        )
        assert duplicate_links == 5

        first_classification = await classify_upload(
            upload_id=result.upload_id,
            transaction_reader=ingestion_repository,
            classification_repository=(classification_repository),
            pattern_lookup=pattern_repository,
            rule_set=rule_set,
            chart_of_accounts=chart_of_accounts,
            gemini_classifier=None,
        )

        assert_classification_summary(first_classification)
        assert first_classification.already_classified == 0
        assert first_classification.classified_by_learned_pattern == 0

        expected_stored = first_classification.classified_by_deterministic_rule

        assert await classifications.count_documents({}) == expected_stored
        assert (
            await classifications.count_documents(
                {"decision.source": {"$ne": (ClassificationSource.DETERMINISTIC_RULE.value)}}
            )
            == 0
        )
        assert (
            await classifications.count_documents(
                {
                    "review_status": {
                        "$ne": ReviewStatus.PENDING.value,
                    }
                }
            )
            == 0
        )

        duplicate_transaction_ids = await transactions.distinct(
            "_id",
            {
                "status": (RecordStatus.DUPLICATE.value),
            },
        )

        assert (
            await classifications.count_documents(
                {
                    "_id": {
                        "$in": duplicate_transaction_ids,
                    }
                }
            )
            == 0
        )

        account_distribution_cursor = await classifications.aggregate(
            [
                {
                    "$group": {
                        "_id": ("$decision.qbo_account.account_number"),
                        "count": {
                            "$sum": 1,
                        },
                    }
                },
                {
                    "$sort": {
                        "_id": 1,
                    }
                },
            ]
        )
        first_account_counts = [document async for document in account_distribution_cursor]

        second_classification = await classify_upload(
            upload_id=result.upload_id,
            transaction_reader=ingestion_repository,
            classification_repository=(classification_repository),
            pattern_lookup=pattern_repository,
            rule_set=rule_set,
            chart_of_accounts=chart_of_accounts,
            gemini_classifier=None,
        )

        assert_classification_summary(second_classification)
        assert second_classification.already_classified == expected_stored
        assert second_classification.classified_by_learned_pattern == 0
        assert second_classification.classified_by_deterministic_rule == 0
        assert second_classification.classified_by_gemini == 0
        assert (
            second_classification.manual_review_required
            == first_classification.manual_review_required
        )
        assert await classifications.count_documents({}) == expected_stored

        assert expected_stored == 195

        approved_count = await approve_all_classifications(
            summary=first_classification,
            repository=classification_repository,
        )

        assert approved_count == 195
        assert (
            await classifications.count_documents(
                {
                    "review_status": (ReviewStatus.APPROVED.value),
                }
            )
            == 195
        )
        assert (
            await classifications.count_documents(
                {
                    "review_status": (ReviewStatus.PENDING.value),
                }
            )
            == 0
        )

        report_sources = await reporting_repository.find_approved_sources(
            start_date=date(2026, 4, 1),
            end_date=date(2026, 6, 30),
            currency="USD",
        )

        report = await generate_profit_and_loss_report_set(
            source_reader=reporting_repository,
            start_date=date(2026, 4, 1),
            end_date=date(2026, 6, 30),
            currency="USD",
            chart_of_accounts=chart_of_accounts,
        )

        assert_profit_and_loss_acceptance(
            report=report,
            sources=report_sources,
            chart_of_accounts=chart_of_accounts,
        )

        print("Finz workbook ingestion, classification, and P&L validation: PASS")
        print(f"Temporary database: {validation_database_name}")
        print(f"Upload ID: {result.upload_id}")
        print(f"SHA-256: {result.file_sha256}")
        print("Physical records: 200")
        print("Canonical valid records: 195")
        print("Duplicate records ignored: 5")
        print("Invalid records: 0")
        print("Duplicate source IDs: " + ", ".join(sorted(duplicate_source_ids)))
        print("Date range: 2026-04-01 through 2026-06-29")
        print("Currencies: USD=200")
        print("Bank accounts: Operating Checking=194, Tax Reserve=6")
        print("Raw source-value preservation: PASS")
        print("Duplicate canonical links: PASS")
        print(
            "Deterministic classifications stored: "
            f"{first_classification.classified_by_deterministic_rule}"
        )
        print(f"Manual-review outcomes: {first_classification.manual_review_required}")
        print("Gemini classifications: 0")
        print("Classification failures: 0")
        print(f"Second-run already classified: {second_classification.already_classified}")
        print(f"Second-run manual-review outcomes: {second_classification.manual_review_required}")
        print("Classification retry idempotency: PASS")
        print(f"Approved classifications: {approved_count}")
        print(f"P&L transactions included: {report.consolidated.transaction_count}")
        print(
            "Balance-sheet transactions excluded: "
            f"{approved_count - report.consolidated.transaction_count}"
        )
        print("Monthly cash-basis P&L:")

        for statement in report.monthly:
            print(
                "  "
                f"{statement.start_date:%Y-%m}: "
                f"Revenue={statement.total_revenue:.2f}, "
                "COGS="
                f"{statement.total_cost_of_goods_sold:.2f}, "
                f"Gross Profit={statement.gross_profit:.2f}, "
                "Operating Expenses="
                f"{statement.total_operating_expenses:.2f}, "
                f"Net Profit={statement.net_profit:.2f}, "
                f"Transactions={statement.transaction_count}"
            )

        consolidated = report.consolidated
        print(
            "Consolidated cash-basis P&L: "
            f"Revenue={consolidated.total_revenue:.2f}, "
            "COGS="
            f"{consolidated.total_cost_of_goods_sold:.2f}, "
            f"Gross Profit={consolidated.gross_profit:.2f}, "
            "Operating Expenses="
            f"{consolidated.total_operating_expenses:.2f}, "
            f"Net Profit={consolidated.net_profit:.2f}, "
            f"Transactions={consolidated.transaction_count}"
        )
        print("Consolidated P&L account totals:")

        for line in consolidated.account_lines:
            print(
                f"  {line.account_number} "
                f"{line.account_name}: "
                f"{line.total:.2f} "
                f"({len(line.transactions)} transactions)"
            )

        print("Stored account distribution:")

        for account_count in first_account_counts:
            print(f"  {account_count['_id']}: {account_count['count']}")
    finally:
        await mongodb.client.drop_database(validation_database_name)
        await mongodb.close()
        print("Temporary validation database removed")


def main() -> None:
    """Run asynchronous validation from the command line."""

    arguments = parse_args()
    asyncio.run(validate(arguments.workbook))


if __name__ == "__main__":
    main()
