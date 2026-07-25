"""Typed contracts for source ingestion and normalized transactions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

CENT = Decimal("0.01")
SHA256_PATTERN = r"^[0-9a-f]{64}$"
SUPPORTED_DATE_FORMATS = frozenset(
    {
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%d/%m/%Y",
        "%Y/%m/%d",
        "%m-%d-%Y",
    }
)


def utc_now() -> datetime:
    """Return a UTC timestamp aligned to BSON millisecond precision."""

    current = datetime.now(UTC)
    milliseconds = current.microsecond // 1_000
    return current.replace(microsecond=milliseconds * 1_000)


class FileType(StrEnum):
    """Supported source-file formats."""

    CSV = "csv"
    XLSX = "xlsx"


class UploadStatus(StrEnum):
    """Processing state of one uploaded file."""

    PENDING = "pending"
    MAPPING_REQUIRED = "mapping_required"
    PROCESSING = "processing"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    FAILED = "failed"


class RecordStatus(StrEnum):
    """Safety and duplicate state of one normalized record."""

    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"
    DUPLICATE = "duplicate"


class TransactionDirection(StrEnum):
    """Direction of cash movement from the bank account's perspective."""

    INFLOW = "inflow"
    OUTFLOW = "outflow"


class IssueSeverity(StrEnum):
    """Severity of a normalization or validation issue."""

    WARNING = "warning"
    ERROR = "error"


class ValidationIssue(BaseModel):
    """A human-readable problem associated with one source field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    field: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: IssueSeverity = IssueSeverity.ERROR
    raw_value: Any = None


class ColumnMapping(BaseModel):
    """Map normalized fields to source-column names."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_transaction_id: str | None = Field(default=None, min_length=1)
    transaction_date: str = Field(min_length=1)
    posted_date: str | None = Field(default=None, min_length=1)
    description: str = Field(min_length=1)

    amount: str | None = Field(default=None, min_length=1)
    debit_amount: str | None = Field(default=None, min_length=1)
    credit_amount: str | None = Field(default=None, min_length=1)

    currency: str | None = Field(default=None, min_length=1)
    bank_account: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_amount_mapping(self) -> ColumnMapping:
        """Require either one signed amount or split debit/credit columns."""

        has_signed_amount = self.amount is not None
        has_split_amount = self.debit_amount is not None or self.credit_amount is not None

        if has_signed_amount == has_split_amount:
            raise ValueError(
                "Map exactly one amount representation: either a signed "
                "amount column or debit/credit columns"
            )

        return self


class IngestionConfig(BaseModel):
    """User-selected configuration for processing one source file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_type: FileType
    header_row: int = Field(default=1, ge=1)
    sheet_name: str | None = Field(default=None, min_length=1)
    date_format: str = "%Y-%m-%d"
    column_mapping: ColumnMapping
    default_currency: str | None = None
    default_bank_account: str | None = None

    @field_validator("date_format")
    @classmethod
    def validate_date_format(cls, value: str) -> str:
        """Allow explicit date formats instead of ambiguous guessing."""

        if value not in SUPPORTED_DATE_FORMATS:
            supported = ", ".join(sorted(SUPPORTED_DATE_FORMATS))
            raise ValueError(f"Unsupported date format; choose one of: {supported}")

        return value

    @field_validator("default_currency")
    @classmethod
    def normalize_default_currency(cls, value: str | None) -> str | None:
        """Normalize an optional ISO-style currency code."""

        if value is None:
            return None

        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("Default currency must be a three-letter code")

        return normalized

    @field_validator("default_bank_account")
    @classmethod
    def normalize_default_bank_account(cls, value: str | None) -> str | None:
        """Reject an empty default bank-account name."""

        if value is None:
            return None

        normalized = value.strip()
        if not normalized:
            raise ValueError("Default bank account cannot be blank")

        return normalized

    @model_validator(mode="after")
    def validate_fallback_fields(self) -> IngestionConfig:
        """Require mapped or configured currency and bank-account values."""

        if self.column_mapping.currency is None and self.default_currency is None:
            raise ValueError("Currency must come from a mapped column or a default value")

        if self.column_mapping.bank_account is None and self.default_bank_account is None:
            raise ValueError("Bank account must come from a mapped column or a default value")

        if self.file_type == FileType.CSV and self.sheet_name is not None:
            raise ValueError("CSV files cannot specify a worksheet")

        return self


class UploadBatch(BaseModel):
    """Metadata and configuration for one uploaded source file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    source_file_name: str = Field(min_length=1)
    file_type: FileType
    file_sha256: str = Field(pattern=SHA256_PATTERN)
    status: UploadStatus = UploadStatus.PENDING
    config: IngestionConfig | None = None
    physical_record_count: int = Field(default=0, ge=0)
    created_at: AwareDatetime = Field(default_factory=utc_now)


class RawRecord(BaseModel):
    """Immutable copy of one complete source row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    upload_id: UUID
    source_file_name: str = Field(min_length=1)
    source_sheet: str | None = None
    source_row_number: int = Field(ge=1)
    raw_values: dict[str, Any]
    raw_hash: str = Field(pattern=SHA256_PATTERN)
    ingested_at: AwareDatetime = Field(default_factory=utc_now)


class NormalizedTransaction(BaseModel):
    """Safe normalized representation linked to an immutable raw record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID = Field(default_factory=uuid4)
    upload_id: UUID
    raw_record_id: UUID

    source_transaction_id: str | None = None
    transaction_date: date | None = None
    posted_date: date | None = None

    description_original: str | None = None
    description_normalized: str | None = None

    amount: Decimal | None = Field(
        default=None,
        max_digits=18,
        decimal_places=2,
    )
    currency: str | None = None
    bank_account: str | None = None
    direction: TransactionDirection | None = None

    fingerprint: str | None = Field(
        default=None,
        pattern=SHA256_PATTERN,
    )
    status: RecordStatus = RecordStatus.PENDING
    duplicate_of: UUID | None = None
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("amount", mode="before")
    @classmethod
    def validate_decimal_amount(cls, value: Any) -> Decimal | None:
        """Accept exact decimal inputs while rejecting binary floats."""

        if value is None:
            return None

        if isinstance(value, float):
            raise ValueError("Binary floating-point values are not accepted for money")

        try:
            decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Amount is not a valid decimal value") from exc

        if not decimal_value.is_finite():
            raise ValueError("Amount must be finite")

        quantized = decimal_value.quantize(CENT)
        if decimal_value != quantized:
            raise ValueError("Amount cannot contain more than two decimal places")

        return quantized

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str | None) -> str | None:
        """Normalize a three-letter currency code."""

        if value is None:
            return None

        normalized = value.strip().upper()
        if len(normalized) != 3 or not normalized.isalpha():
            raise ValueError("Currency must be a three-letter code")

        return normalized

    @field_validator(
        "source_transaction_id",
        "description_normalized",
        "bank_account",
    )
    @classmethod
    def strip_normalized_text(cls, value: str | None) -> str | None:
        """Strip normalized text fields without changing raw source values."""

        if value is None:
            return None

        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def validate_record_state(self) -> NormalizedTransaction:
        """Enforce state-specific safety and duplicate requirements."""

        error_issues = [
            issue for issue in self.validation_issues if issue.severity == IssueSeverity.ERROR
        ]

        if self.amount is not None and self.amount != Decimal("0.00"):
            expected_direction = (
                TransactionDirection.INFLOW
                if self.amount > Decimal("0.00")
                else TransactionDirection.OUTFLOW
            )
            if self.direction is not None and self.direction != expected_direction:
                raise ValueError("Transaction direction conflicts with amount sign")

        if self.status in {RecordStatus.VALID, RecordStatus.DUPLICATE}:
            required_values = {
                "transaction_date": self.transaction_date,
                "description_normalized": self.description_normalized,
                "amount": self.amount,
                "currency": self.currency,
                "bank_account": self.bank_account,
                "direction": self.direction,
                "fingerprint": self.fingerprint,
            }
            missing_fields = [name for name, value in required_values.items() if value is None]

            if missing_fields:
                raise ValueError(
                    "Safe normalized transaction is missing: " + ", ".join(missing_fields)
                )

            if self.amount == Decimal("0.00"):
                raise ValueError("A safe normalized transaction cannot have zero amount")

            if error_issues:
                raise ValueError("A safe normalized transaction cannot contain error issues")

        if self.status == RecordStatus.INVALID and not error_issues:
            raise ValueError("An invalid transaction must contain at least one error issue")

        if self.status == RecordStatus.DUPLICATE and self.duplicate_of is None:
            raise ValueError("A duplicate transaction must reference its canonical transaction")

        if self.status != RecordStatus.DUPLICATE and self.duplicate_of is not None:
            raise ValueError("Only duplicate transactions may reference a canonical transaction")

        return self
