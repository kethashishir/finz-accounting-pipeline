"""Validate the supplied Finz workbook through the real pipeline."""

from __future__ import annotations

import argparse
import asyncio
from datetime import date
from pathlib import Path
from uuid import uuid4

from app.core.config import get_settings
from app.db.client import MongoDatabase
from app.models.ingestion import (
    ColumnMapping,
    FileType,
    IngestionConfig,
    RecordStatus,
    UploadStatus,
)
from app.repositories.ingestion import IngestionRepository
from app.services.ingestion.pipeline import IngestionPipeline

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
            "Run the supplied Finz workbook through an isolated end-to-end ingestion validation."
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


async def validate(workbook_path: Path) -> None:
    """Validate the workbook and persisted records."""

    resolved_path = workbook_path.expanduser().resolve()
    if not resolved_path.is_file():
        raise FileNotFoundError(f"Workbook does not exist: {resolved_path}")

    settings = get_settings()
    validation_database_name = f"finz_val_{uuid4().hex}"
    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=validation_database_name,
    )
    repository = IngestionRepository(mongodb.database)
    pipeline = IngestionPipeline(repository)

    try:
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

        uploads = repository.uploads
        raw_records = repository.raw_records
        transactions = repository.transactions

        assert await uploads.count_documents({}) == 1
        assert await raw_records.count_documents({}) == 200
        assert await transactions.count_documents({}) == 200

        assert await transactions.count_documents({"status": RecordStatus.VALID.value}) == 195
        assert await transactions.count_documents({"status": RecordStatus.DUPLICATE.value}) == 5
        assert await transactions.count_documents({"status": RecordStatus.INVALID.value}) == 0

        duplicate_source_ids = set(
            await transactions.distinct(
                "source_transaction_id",
                {"status": RecordStatus.DUPLICATE.value},
            )
        )
        assert duplicate_source_ids == EXPECTED_DUPLICATE_SOURCE_IDS

        unique_source_ids = await transactions.distinct("source_transaction_id")
        assert len(unique_source_ids) == 195

        assert await transactions.count_documents({"currency": "USD"}) == 200
        assert await transactions.count_documents({"bank_account": "Operating Checking"}) == 194
        assert await transactions.count_documents({"bank_account": "Tax Reserve"}) == 6

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

        first_raw = await raw_records.find_one({"source_row_number": 5})
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
                "status": RecordStatus.DUPLICATE.value,
                "duplicate_of": {"$ne": None},
            }
        )
        assert duplicate_links == 5

        print("Finz workbook pipeline validation: PASS")
        print(f"Temporary database: {validation_database_name}")
        print(f"Upload ID: {result.upload_id}")
        print(f"SHA-256: {result.file_sha256}")
        print("Physical records: 200")
        print("Canonical valid records: 195")
        print("Duplicate records: 5")
        print("Invalid records: 0")
        print("Duplicate source IDs: " + ", ".join(sorted(duplicate_source_ids)))
        print("Date range: 2026-04-01 through 2026-06-29")
        print("Currencies: USD=200")
        print("Bank accounts: Operating Checking=194, Tax Reserve=6")
        print("Raw source-value preservation: PASS")
        print("Duplicate canonical links: PASS")
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
