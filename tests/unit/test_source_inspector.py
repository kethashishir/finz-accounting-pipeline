"""Unit tests for read-only source-file inspection."""

import hashlib
from datetime import datetime
from io import BytesIO

import pytest
from openpyxl import Workbook

from app.models.ingestion import FileType
from app.services.ingestion.inspector import (
    SourceFileInspector,
    SourceInspectionError,
    SourceInspectionErrorCode,
)


def build_test_workbook() -> bytes:
    """Create a small synthetic XLSX workbook in memory."""

    workbook = Workbook()

    setup = workbook.active
    setup.title = "Setup"
    setup.append(["Company", "Currency"])
    setup.append(["Example LLC", "USD"])

    transactions = workbook.create_sheet("Transactions")
    transactions.append(["Date", "Description", "Amount"])
    transactions.append(
        [
            datetime(2026, 4, 1),
            "Customer receipt",
            1250,
        ]
    )
    transactions["C3"] = "=C2*2"

    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_inspects_utf8_csv_and_sanitizes_file_name() -> None:
    """CSV inspection reports source shape, hash, and safe filename."""

    content = (
        "\ufeffDate,Description,Amount\n"
        "2026-04-01,Customer receipt,1250.00\n"
    ).encode()

    inspection = SourceFileInspector().inspect(
        file_name="../../bank.csv",
        content=content,
    )

    assert inspection.original_file_name == "../../bank.csv"
    assert inspection.safe_file_name == "bank.csv"
    assert inspection.file_type == FileType.CSV
    assert inspection.file_sha256 == hashlib.sha256(content).hexdigest()
    assert inspection.encoding == "utf-8-sig"
    assert inspection.delimiter == ","
    assert inspection.sheets[0].row_count == 2
    assert inspection.sheets[0].column_count == 3
    assert inspection.sheets[0].preview_rows[1].values[1] == "Customer receipt"


def test_detects_semicolon_delimited_csv() -> None:
    """Common non-comma delimiters are detected."""

    content = (
        b"Date;Description;Amount\n"
        b"2026-04-01;Customer receipt;1250.00\n"
    )

    inspection = SourceFileInspector().inspect(
        file_name="bank.csv",
        content=content,
    )

    assert inspection.delimiter == ";"
    assert inspection.sheets[0].column_count == 3


def test_reports_windows_1252_csv_encoding() -> None:
    """Non-UTF-8 text is decoded visibly without dropping characters."""

    content = (
        "Date,Description,Amount\n"
        "2026-04-01,Caf\xe9 supplies,-15.00\n"
    ).encode("cp1252")

    inspection = SourceFileInspector().inspect(
        file_name="bank.csv",
        content=content,
    )

    assert inspection.encoding == "cp1252"
    assert inspection.sheets[0].preview_rows[1].values[1] == "Café supplies"
    assert any("Windows-1252" in warning for warning in inspection.warnings)


def test_rejects_empty_file() -> None:
    """An empty upload is unsafe."""

    with pytest.raises(SourceInspectionError) as error:
        SourceFileInspector().inspect(
            file_name="empty.csv",
            content=b"",
        )

    assert error.value.code == SourceInspectionErrorCode.EMPTY_FILE


def test_rejects_unsupported_extension() -> None:
    """Legacy or executable file types are not accepted."""

    with pytest.raises(SourceInspectionError) as error:
        SourceFileInspector().inspect(
            file_name="bank.xls",
            content=b"legacy content",
        )

    assert error.value.code == SourceInspectionErrorCode.UNSUPPORTED_EXTENSION


def test_rejects_extension_signature_mismatch() -> None:
    """An XLSX filename cannot disguise plain-text content."""

    with pytest.raises(SourceInspectionError) as error:
        SourceFileInspector().inspect(
            file_name="fake.xlsx",
            content=b"not an XLSX archive",
        )

    assert error.value.code == SourceInspectionErrorCode.SIGNATURE_MISMATCH


def test_rejects_file_over_configured_size_limit() -> None:
    """Oversized files are rejected before parsing."""

    inspector = SourceFileInspector(max_file_bytes=5)

    with pytest.raises(SourceInspectionError) as error:
        inspector.inspect(
            file_name="large.csv",
            content=b"123456",
        )

    assert error.value.code == SourceInspectionErrorCode.FILE_TOO_LARGE


def test_inspects_all_xlsx_worksheets_and_warns_about_formulas() -> None:
    """XLSX inspection lists every sheet without evaluating formulas."""

    content = build_test_workbook()

    inspection = SourceFileInspector().inspect(
        file_name="transactions.xlsx",
        content=content,
    )

    assert inspection.file_type == FileType.XLSX
    assert [sheet.name for sheet in inspection.sheets] == [
        "Setup",
        "Transactions",
    ]

    transactions = inspection.sheets[1]
    assert transactions.row_count == 3
    assert transactions.column_count == 3
    assert transactions.preview_rows[1].values[1] == "Customer receipt"
    assert transactions.preview_rows[2].values[2] == "=C2*2"

    assert any(
        "formula cell" in warning
        for warning in inspection.warnings
    )
