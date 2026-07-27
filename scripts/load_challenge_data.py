"""Load validated challenge evidence into the configured database."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings
from app.db.client import MongoDatabase
from app.models.classification import ReviewStatus
from app.models.ingestion import RecordStatus, UploadStatus
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
    classify_upload,
)
from app.services.classification.rule_config import (
    load_deterministic_rule_set,
)
from app.services.ingestion.pipeline import IngestionPipeline
from app.services.reporting.profit_and_loss import (
    generate_profit_and_loss_report_set,
)
from scripts.validate_challenge_dataset import (
    CHART_OF_ACCOUNTS_PATH,
    CLASSIFICATION_RULES_PATH,
    EXPECTED_SHA256,
    approve_all_classifications,
    assert_classification_summary,
    assert_profit_and_loss_acceptance,
    ingestion_config,
)

SOURCE_COLLECTIONS = (
    "upload_batches",
    "raw_records",
    "normalized_transactions",
    "transaction_classifications",
    "classification_patterns",
    "quickbooks_sync_records",
)


def parse_args() -> argparse.Namespace:
    """Parse the untouched workbook path."""

    parser = argparse.ArgumentParser(
        description=(
            "Load the validated Finz challenge workbook into "
            "the configured persistent MongoDB database."
        )
    )
    parser.add_argument(
        "--workbook",
        type=Path,
        required=True,
        help="Path to the untouched Finz XLSX workbook",
    )
    return parser.parse_args()


async def main(workbook_path: Path) -> None:
    """Persist and validate challenge accounting evidence."""

    resolved_path = workbook_path.expanduser().resolve()

    if not resolved_path.is_file():
        raise FileNotFoundError(f"Workbook does not exist: {resolved_path}")

    settings = get_settings()
    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=settings.mongodb_database,
    )

    ingestion_repository = IngestionRepository(mongodb.database)
    classification_repository = ClassificationRepository(mongodb.database)
    pattern_repository = ClassificationPatternRepository(mongodb.database)
    reporting_repository = ProfitAndLossRepository(mongodb.database)
    pipeline = IngestionPipeline(ingestion_repository)

    try:
        existing_counts = {
            collection_name: (await mongodb.database[collection_name].count_documents({}))
            for collection_name in SOURCE_COLLECTIONS
        }

        populated = {name: count for name, count in existing_counts.items() if count != 0}

        if populated:
            formatted = ", ".join(f"{name}={count}" for name, count in sorted(populated.items()))
            raise RuntimeError(f"Persistent accounting collections are not empty: {formatted}")

        connection_count = await mongodb.database["quickbooks_connections"].count_documents({})

        if connection_count != 1:
            raise RuntimeError(
                f"Expected exactly one preserved QuickBooks connection, received {connection_count}"
            )

        await ingestion_repository.ensure_indexes()
        await classification_repository.ensure_indexes()
        await pattern_repository.ensure_indexes()

        chart_of_accounts = load_chart_of_accounts(CHART_OF_ACCOUNTS_PATH)
        rule_set = load_deterministic_rule_set(
            CLASSIFICATION_RULES_PATH,
            chart_of_accounts=chart_of_accounts,
        )

        result = await pipeline.process(
            file_name=resolved_path.name,
            content=resolved_path.read_bytes(),
            config=ingestion_config(),
        )

        if result.file_sha256 != EXPECTED_SHA256:
            raise RuntimeError(
                "Workbook SHA-256 does not match the authoritative challenge dataset"
            )

        if result.status is not UploadStatus.COMPLETED:
            raise RuntimeError(f"Unexpected ingestion status: {result.status.value}")

        expected_ingestion_counts = {
            "physical": (
                result.counts.physical,
                200,
            ),
            "valid": (
                result.counts.valid,
                195,
            ),
            "duplicate": (
                result.counts.duplicate,
                5,
            ),
            "invalid": (
                result.counts.invalid,
                0,
            ),
        }

        for label, (
            actual,
            expected,
        ) in expected_ingestion_counts.items():
            if actual != expected:
                raise RuntimeError(
                    f"Unexpected {label} count: expected {expected}, received {actual}"
                )

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

        if first_classification.classified_by_deterministic_rule != 195:
            raise RuntimeError(
                "Expected all 195 canonical transactions to use deterministic classifications"
            )

        approved_count = await approve_all_classifications(
            summary=first_classification,
            repository=classification_repository,
        )

        if approved_count != 195:
            raise RuntimeError(f"Expected 195 approved classifications, received {approved_count}")

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

        if second_classification.already_classified != 195:
            raise RuntimeError("Classification retry did not recognize all 195 existing decisions")

        report = await generate_profit_and_loss_report_set(
            source_reader=reporting_repository,
            start_date=date(2026, 4, 1),
            end_date=date(2026, 6, 30),
            currency="USD",
            chart_of_accounts=chart_of_accounts,
        )
        report_sources = await reporting_repository.find_approved_sources(
            start_date=date(2026, 4, 1),
            end_date=date(2026, 6, 30),
            currency="USD",
        )

        assert_profit_and_loss_acceptance(
            report=report,
            sources=report_sources,
            chart_of_accounts=chart_of_accounts,
        )

        raw_count = await ingestion_repository.raw_records.count_documents({})
        transaction_count = await ingestion_repository.transactions.count_documents({})
        valid_count = await ingestion_repository.transactions.count_documents(
            {
                "status": RecordStatus.VALID.value,
            }
        )
        duplicate_count = await ingestion_repository.transactions.count_documents(
            {
                "status": (RecordStatus.DUPLICATE.value),
            }
        )
        classification_count = await classification_repository.classifications.count_documents({})
        approved_stored = await classification_repository.classifications.count_documents(
            {
                "review_status": (ReviewStatus.APPROVED.value),
            }
        )

        print("Persistent challenge-data load: PASS")
        print(f"Configured database: {settings.mongodb_database}")
        print(f"QuickBooks connections preserved: {connection_count}")
        print("Upload batches: 1")
        print(f"Raw records: {raw_count}")
        print(f"Normalized transactions: {transaction_count}")
        print(f"Canonical valid transactions: {valid_count}")
        print(f"Duplicate transactions: {duplicate_count}")
        print(f"Stored classifications: {classification_count}")
        print(f"Approved classifications: {approved_stored}")
        print(
            f"Classification retry already classified: {second_classification.already_classified}"
        )
        print("Internal cash-basis P&L acceptance: PASS")
        print("QuickBooks JournalEntries created: 0")
    finally:
        await mongodb.close()


if __name__ == "__main__":
    arguments = parse_args()
    asyncio.run(main(arguments.workbook))
