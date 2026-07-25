"""Tests for configurable bank-record normalization."""

from datetime import date
from decimal import Decimal
from io import BytesIO
from uuid import uuid4

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from app.models.ingestion import (
    ColumnMapping,
    FileType,
    IngestionConfig,
    RecordStatus,
    TransactionDirection,
)
from app.services.ingestion.normalizer import (
    SourceFileNormalizer,
    SourceNormalizationError,
    SourceNormalizationErrorCode,
)


def signed_config(
    *,
    file_type: FileType = FileType.CSV,
    sheet_name: str | None = None,
    header_row: int = 1,
    date_format: str = "%Y-%m-%d",
) -> IngestionConfig:
    """Return a common signed-amount ingestion configuration."""

    return IngestionConfig(
        file_type=file_type,
        sheet_name=sheet_name,
        header_row=header_row,
        date_format=date_format,
        column_mapping=ColumnMapping(
            source_transaction_id="Transaction ID",
            transaction_date="Date",
            description="Description",
            amount="Amount",
            currency="Currency",
            bank_account="Account",
        ),
    )


def workbook_bytes(rows: list[list[object]]) -> bytes:
    """Create an in-memory XLSX fixture."""

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Transactions"

    for row in rows:
        worksheet.append(row)

    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_ingestion_config_rejects_unsupported_date_format() -> None:
    """Ambiguous date parsing cannot be configured accidentally."""

    with pytest.raises(ValidationError):
        signed_config(date_format="automatic")


def test_normalizes_signed_csv_and_preserves_raw_values() -> None:
    """A valid signed-amount row produces linked raw and normalized records."""

    content = (
        b"Transaction ID,Date,Description,Amount,Currency,Account,Unused\n"
        b'BF-1,2026-04-01,"  ACME   CUSTOMER  ","$1,250.00",usd,'
        b"Operating Checking,keep me\n"
    )

    result = SourceFileNormalizer().normalize(
        upload_id=uuid4(),
        file_name="../../bank.csv",
        content=content,
        config=signed_config(),
    )

    assert len(result.raw_records) == 1
    assert len(result.transactions) == 1

    raw = result.raw_records[0]
    transaction = result.transactions[0]

    assert raw.source_file_name == "bank.csv"
    assert raw.raw_values["Description"] == "  ACME   CUSTOMER  "
    assert raw.raw_values["Unused"] == "keep me"
    assert transaction.raw_record_id == raw.id
    assert transaction.transaction_date == date(2026, 4, 1)
    assert transaction.description_original == "  ACME   CUSTOMER  "
    assert transaction.description_normalized == "acme customer"
    assert transaction.amount == Decimal("1250.00")
    assert transaction.currency == "USD"
    assert transaction.direction == TransactionDirection.INFLOW
    assert transaction.status == RecordStatus.VALID
    assert transaction.fingerprint is not None


def test_normalizes_split_debit_and_credit_amounts() -> None:
    """Debits become negative and credits become positive."""

    content = (
        b"Date,Description,Debit,Credit,Currency,Account\n"
        b"04/01/2026,Fuel,125.50,,USD,Operating Checking\n"
        b"04/02/2026,Customer payment,,900.00,USD,Operating Checking\n"
    )
    config = IngestionConfig(
        file_type=FileType.CSV,
        date_format="%m/%d/%Y",
        column_mapping=ColumnMapping(
            transaction_date="Date",
            description="Description",
            debit_amount="Debit",
            credit_amount="Credit",
            currency="Currency",
            bank_account="Account",
        ),
    )

    result = SourceFileNormalizer().normalize(
        upload_id=uuid4(),
        file_name="split.csv",
        content=content,
        config=config,
    )

    assert [item.amount for item in result.transactions] == [
        Decimal("-125.50"),
        Decimal("900.00"),
    ]
    assert [item.direction for item in result.transactions] == [
        TransactionDirection.OUTFLOW,
        TransactionDirection.INFLOW,
    ]
    assert all(item.status == RecordStatus.VALID for item in result.transactions)


def test_invalid_rows_are_flagged_instead_of_dropped() -> None:
    """Malformed and blank physical rows remain visible as invalid records."""

    content = (
        b"Transaction ID,Date,Description,Amount,Currency,Account\n"
        b"BF-1,not-a-date,Fuel,not-money,USD,Operating Checking\n"
        b"\n"
    )

    result = SourceFileNormalizer().normalize(
        upload_id=uuid4(),
        file_name="invalid.csv",
        content=content,
        config=signed_config(),
    )

    assert len(result.raw_records) == 2
    assert len(result.transactions) == 2
    assert all(item.status == RecordStatus.INVALID for item in result.transactions)

    first_codes = {issue.code for issue in result.transactions[0].validation_issues}
    second_codes = {issue.code for issue in result.transactions[1].validation_issues}

    assert {"invalid_date", "invalid_amount"} <= first_codes
    assert {
        "missing_date",
        "missing_description",
        "missing_amount",
        "missing_currency",
        "missing_bank_account",
    } <= second_codes


def test_rejects_rows_with_both_debit_and_credit() -> None:
    """A split-amount row cannot represent both cash directions."""

    content = (
        b"Date,Description,Debit,Credit,Currency,Account\n"
        b"2026-04-01,Ambiguous,10.00,20.00,USD,Operating Checking\n"
    )
    config = IngestionConfig(
        file_type=FileType.CSV,
        column_mapping=ColumnMapping(
            transaction_date="Date",
            description="Description",
            debit_amount="Debit",
            credit_amount="Credit",
            currency="Currency",
            bank_account="Account",
        ),
    )

    result = SourceFileNormalizer().normalize(
        upload_id=uuid4(),
        file_name="ambiguous.csv",
        content=content,
        config=config,
    )

    transaction = result.transactions[0]
    assert transaction.status == RecordStatus.INVALID
    assert transaction.amount is None
    assert {issue.code for issue in transaction.validation_issues} >= {"ambiguous_split_amount"}


def test_normalizes_xlsx_with_configurable_header_row() -> None:
    """XLSX title rows are skipped only through explicit configuration."""

    content = workbook_bytes(
        [
            ["Bank export generated by example bank"],
            [
                "Transaction ID",
                "Date",
                "Description",
                "Amount",
                "Currency",
                "Account",
            ],
            [
                "BF-2",
                date(2026, 4, 2),
                "Customer payment",
                1250.25,
                "USD",
                "Operating Checking",
            ],
        ]
    )

    result = SourceFileNormalizer().normalize(
        upload_id=uuid4(),
        file_name="bank.xlsx",
        content=content,
        config=signed_config(
            file_type=FileType.XLSX,
            sheet_name="Transactions",
            header_row=2,
        ),
    )

    transaction = result.transactions[0]
    assert transaction.amount == Decimal("1250.25")
    assert transaction.transaction_date == date(2026, 4, 2)
    assert transaction.status == RecordStatus.VALID
    assert result.raw_records[0].source_row_number == 3
    assert result.raw_records[0].source_sheet == "Transactions"


def test_fingerprint_is_stable_across_overlapping_uploads() -> None:
    """Source formatting and upload identity do not change transaction identity."""

    first_content = (
        b"Transaction ID,Date,Description,Amount,Currency,Account\n"
        b"FIRST,2026-04-01,ACME CUSTOMER,100.00,USD,Operating Checking\n"
    )
    second_content = (
        b"Transaction ID,Date,Description,Amount,Currency,Account\n"
        b'SECOND,2026-04-01,"  acme   customer ",100.00,usd,'
        b"Operating Checking\n"
    )
    normalizer = SourceFileNormalizer()

    first = normalizer.normalize(
        upload_id=uuid4(),
        file_name="first.csv",
        content=first_content,
        config=signed_config(),
    )
    second = normalizer.normalize(
        upload_id=uuid4(),
        file_name="second.csv",
        content=second_content,
        config=signed_config(),
    )

    assert first.transactions[0].fingerprint == second.transactions[0].fingerprint
    assert first.raw_records[0].raw_hash != second.raw_records[0].raw_hash


def test_missing_mapped_column_is_a_file_level_error() -> None:
    """A mapping typo stops processing instead of invalidating every row."""

    content = (
        b"Transaction ID,Date,Description,Currency,Account\n"
        b"BF-1,2026-04-01,Fuel,USD,Operating Checking\n"
    )

    with pytest.raises(SourceNormalizationError) as error:
        SourceFileNormalizer().normalize(
            upload_id=uuid4(),
            file_name="missing.csv",
            content=content,
            config=signed_config(),
        )

    assert error.value.code == SourceNormalizationErrorCode.MISSING_MAPPED_COLUMN


def test_duplicate_headers_are_rejected() -> None:
    """Duplicate headers cannot safely produce a raw-value dictionary."""

    content = (
        b"Transaction ID,Date,Description,Amount,Currency,Account,Amount\n"
        b"BF-1,2026-04-01,Fuel,10.00,USD,Operating Checking,20.00\n"
    )

    with pytest.raises(SourceNormalizationError) as error:
        SourceFileNormalizer().normalize(
            upload_id=uuid4(),
            file_name="duplicate-header.csv",
            content=content,
            config=signed_config(),
        )

    assert error.value.code == SourceNormalizationErrorCode.DUPLICATE_HEADER
