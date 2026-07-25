"""Unit tests for ingestion data contracts."""

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.ingestion import (
    ColumnMapping,
    FileType,
    IngestionConfig,
    IssueSeverity,
    NormalizedTransaction,
    RawRecord,
    RecordStatus,
    TransactionDirection,
    ValidationIssue,
)


def valid_transaction_data() -> dict[str, object]:
    """Return a complete safe normalized-transaction payload."""

    return {
        "upload_id": uuid4(),
        "raw_record_id": uuid4(),
        "source_transaction_id": "BF-202604-0001",
        "transaction_date": "2026-04-02",
        "posted_date": "2026-04-02",
        "description_original": " ACH CREDIT EXAMPLE ",
        "description_normalized": "ACH CREDIT EXAMPLE",
        "amount": Decimal("3425.00"),
        "currency": "usd",
        "bank_account": "Operating Checking",
        "direction": TransactionDirection.INFLOW,
        "fingerprint": "a" * 64,
        "status": RecordStatus.VALID,
    }


def test_column_mapping_accepts_signed_amount() -> None:
    """A single signed amount column is supported."""

    mapping = ColumnMapping(
        source_transaction_id="Bank Transaction ID",
        transaction_date="Transaction Date",
        posted_date="Posted Date",
        description="Description",
        amount="Amount (USD)",
        currency="Currency",
        bank_account="Bank Account",
    )

    assert mapping.amount == "Amount (USD)"
    assert mapping.debit_amount is None
    assert mapping.credit_amount is None


def test_column_mapping_accepts_split_debit_credit() -> None:
    """Separate debit and credit columns are supported."""

    mapping = ColumnMapping(
        transaction_date="Date",
        description="Memo",
        debit_amount="Debit",
        credit_amount="Credit",
        currency="Currency",
        bank_account="Account",
    )

    assert mapping.amount is None
    assert mapping.debit_amount == "Debit"
    assert mapping.credit_amount == "Credit"


@pytest.mark.parametrize(
    ("amount", "debit", "credit"),
    [
        (None, None, None),
        ("Amount", "Debit", "Credit"),
    ],
)
def test_column_mapping_rejects_ambiguous_amount_sources(
    amount: str | None,
    debit: str | None,
    credit: str | None,
) -> None:
    """A mapping must choose exactly one amount representation."""

    with pytest.raises(ValidationError, match="exactly one amount"):
        ColumnMapping(
            transaction_date="Date",
            description="Description",
            amount=amount,
            debit_amount=debit,
            credit_amount=credit,
        )


def test_ingestion_config_requires_account_and_currency_sources() -> None:
    """Currency and account must come from columns or configured defaults."""

    mapping = ColumnMapping(
        transaction_date="Date",
        description="Description",
        amount="Amount",
    )

    with pytest.raises(ValidationError, match="Currency must come"):
        IngestionConfig(
            file_type=FileType.CSV,
            column_mapping=mapping,
        )


def test_raw_record_preserves_source_values_without_coercion() -> None:
    """Raw source values remain unchanged, including a source float."""

    source_values = {
        "Description": "  ORIGINAL VALUE  ",
        "Amount": 10.1,
        "Unexpected Column": "keep me",
    }

    record = RawRecord(
        upload_id=uuid4(),
        source_file_name="example.csv",
        source_row_number=2,
        raw_values=source_values,
        raw_hash="b" * 64,
    )

    assert record.raw_values == source_values
    assert record.raw_values["Description"] == "  ORIGINAL VALUE  "
    assert record.raw_values["Amount"] == 10.1
    assert record.raw_values["Unexpected Column"] == "keep me"


def test_normalized_transaction_rejects_binary_float_money() -> None:
    """Normalized money cannot originate as a binary float."""

    payload = valid_transaction_data()
    payload["amount"] = 3425.00

    with pytest.raises(ValidationError, match="Binary floating-point"):
        NormalizedTransaction(**payload)


def test_valid_transaction_uses_decimal_and_expected_direction() -> None:
    """A valid inflow retains exact cents and normalized currency."""

    transaction = NormalizedTransaction(**valid_transaction_data())

    assert transaction.amount == Decimal("3425.00")
    assert transaction.currency == "USD"
    assert transaction.direction == TransactionDirection.INFLOW
    assert transaction.model_dump(mode="json")["amount"] == "3425.00"


def test_valid_transaction_requires_safe_fields() -> None:
    """A transaction cannot be marked valid while required fields are absent."""

    payload = valid_transaction_data()
    payload["transaction_date"] = None

    with pytest.raises(ValidationError, match="transaction_date"):
        NormalizedTransaction(**payload)


def test_invalid_transaction_requires_error_issue() -> None:
    """Invalid records must explain why they are unsafe."""

    with pytest.raises(ValidationError, match="at least one error issue"):
        NormalizedTransaction(
            upload_id=uuid4(),
            raw_record_id=uuid4(),
            status=RecordStatus.INVALID,
        )

    transaction = NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        status=RecordStatus.INVALID,
        validation_issues=[
            ValidationIssue(
                code="invalid_amount",
                field="amount",
                message="Amount could not be parsed",
                severity=IssueSeverity.ERROR,
                raw_value="not-money",
            )
        ],
    )

    assert transaction.status == RecordStatus.INVALID


def test_duplicate_transaction_requires_canonical_reference() -> None:
    """A duplicate must point to the transaction processed first."""

    payload = valid_transaction_data()
    payload["status"] = RecordStatus.DUPLICATE

    with pytest.raises(ValidationError, match="canonical transaction"):
        NormalizedTransaction(**payload)

    canonical_id = uuid4()
    payload["duplicate_of"] = canonical_id
    duplicate = NormalizedTransaction(**payload)

    assert duplicate.duplicate_of == canonical_id
