"""Tests for BSON-safe accounting model serialization."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from bson import BSON
from bson.codec_options import CodecOptions, UuidRepresentation
from bson.decimal128 import Decimal128

from app.db.serialization import (
    MongoSerializationError,
    raw_record_from_document,
    raw_record_to_document,
    transaction_from_document,
    transaction_to_document,
    upload_from_document,
    upload_to_document,
)
from app.models.ingestion import (
    ColumnMapping,
    FileType,
    IngestionConfig,
    NormalizedTransaction,
    RawRecord,
    RecordStatus,
    TransactionDirection,
    UploadBatch,
)

CODEC_OPTIONS = CodecOptions(
    tz_aware=True,
    uuid_representation=UuidRepresentation.STANDARD,
)


def assert_bson_encodable(document: dict[str, object]) -> None:
    """Prove that MongoDB's BSON encoder accepts a document."""

    BSON.encode(document, codec_options=CODEC_OPTIONS)


def test_transaction_round_trip_preserves_decimal_and_dates() -> None:
    """Money stays exact and calendar dates survive BSON conversion."""

    transaction = NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-202604-0001",
        transaction_date=date(2026, 4, 1),
        posted_date=date(2026, 4, 2),
        description_original="Fuel payment",
        description_normalized="fuel payment",
        amount=Decimal("-123.45"),
        currency="USD",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint="a" * 64,
        status=RecordStatus.VALID,
    )

    document = transaction_to_document(transaction)

    assert document["_id"] == transaction.id
    assert document["amount"] == Decimal128("-123.45")
    assert document["transaction_date"] == datetime(
        2026,
        4,
        1,
        tzinfo=UTC,
    )
    assert_bson_encodable(document)
    assert transaction_from_document(document) == transaction


def test_raw_record_round_trip_preserves_values_and_unsafe_headers() -> None:
    """Raw headers remain values rather than MongoDB document keys."""

    record = RawRecord(
        upload_id=uuid4(),
        source_file_name="bank.xlsx",
        source_sheet="Transactions",
        source_row_number=7,
        raw_values={
            "$unsafe.header": "preserved",
            "Amount": Decimal("10.25"),
            "Date": date(2026, 4, 1),
            "Binary": b"raw",
        },
        raw_hash="b" * 64,
    )

    document = raw_record_to_document(record)

    assert isinstance(document["raw_values"], list)
    assert document["raw_values"][0]["column"] == "$unsafe.header"
    assert_bson_encodable(document)
    assert raw_record_from_document(document) == record


def test_upload_round_trip_preserves_ingestion_configuration() -> None:
    """Upload configuration survives MongoDB serialization."""

    config = IngestionConfig(
        file_type=FileType.CSV,
        date_format="%m/%d/%Y",
        column_mapping=ColumnMapping(
            transaction_date="Date",
            description="Description",
            amount="Amount",
        ),
        default_currency="USD",
        default_bank_account="Operating Checking",
    )
    upload = UploadBatch(
        source_file_name="bank.csv",
        file_type=FileType.CSV,
        file_sha256="c" * 64,
        config=config,
        physical_record_count=25,
    )

    document = upload_to_document(upload)

    assert document["_id"] == upload.id
    assert_bson_encodable(document)
    assert upload_from_document(document) == upload


def test_unsupported_raw_value_is_rejected_visibly() -> None:
    """Unsupported source values cannot be silently converted to strings."""

    record = RawRecord(
        upload_id=uuid4(),
        source_file_name="bank.xlsx",
        source_row_number=2,
        raw_values={"Unsupported": object()},
        raw_hash="d" * 64,
    )

    with pytest.raises(
        MongoSerializationError,
        match="Unsupported raw cell type",
    ):
        raw_record_to_document(record)
