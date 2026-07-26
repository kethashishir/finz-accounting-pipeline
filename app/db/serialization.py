"""BSON-safe serialization for accounting models."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from bson import Binary
from bson.decimal128 import Decimal128

from app.models.classification import TransactionClassification
from app.models.ingestion import (
    NormalizedTransaction,
    RawRecord,
    UploadBatch,
)


class MongoSerializationError(ValueError):
    """A model value cannot be represented safely in MongoDB."""


def upload_to_document(upload: UploadBatch) -> dict[str, Any]:
    """Convert an upload batch to a BSON-safe MongoDB document."""

    values = upload.model_dump(mode="python")
    identifier = values.pop("id")

    return {
        "_id": identifier,
        **_encode_value(values),
    }


def upload_from_document(document: dict[str, Any]) -> UploadBatch:
    """Reconstruct and validate an upload batch document."""

    values = _decode_value({key: value for key, value in document.items() if key != "_id"})
    values["id"] = document["_id"]
    return UploadBatch.model_validate(values)


def classification_to_document(
    classification: TransactionClassification,
) -> dict[str, Any]:
    """Convert a transaction classification to a BSON-safe document."""

    values = classification.model_dump(mode="python")
    normalized_transaction_id = values.pop("normalized_transaction_id")

    return {
        "_id": normalized_transaction_id,
        **_encode_value(values),
    }


def classification_from_document(
    document: dict[str, Any],
) -> TransactionClassification:
    """Reconstruct and validate a transaction classification document."""

    values = _decode_value({key: value for key, value in document.items() if key != "_id"})
    values["normalized_transaction_id"] = document["_id"]

    return TransactionClassification.model_validate(values)


def raw_record_to_document(record: RawRecord) -> dict[str, Any]:
    """Convert a raw record without using untrusted headers as BSON keys."""

    raw_values = [
        {
            "column": column,
            **_encode_raw_scalar(value),
        }
        for column, value in record.raw_values.items()
    ]

    return {
        "_id": record.id,
        "upload_id": record.upload_id,
        "source_file_name": record.source_file_name,
        "source_sheet": record.source_sheet,
        "source_row_number": record.source_row_number,
        "raw_values": raw_values,
        "raw_hash": record.raw_hash,
        "ingested_at": record.ingested_at,
    }


def raw_record_from_document(document: dict[str, Any]) -> RawRecord:
    """Reconstruct and validate an immutable raw record."""

    raw_values = {item["column"]: _decode_raw_scalar(item) for item in document["raw_values"]}

    return RawRecord.model_validate(
        {
            "id": document["_id"],
            "upload_id": document["upload_id"],
            "source_file_name": document["source_file_name"],
            "source_sheet": document.get("source_sheet"),
            "source_row_number": document["source_row_number"],
            "raw_values": raw_values,
            "raw_hash": document["raw_hash"],
            "ingested_at": document["ingested_at"],
        }
    )


def transaction_to_document(
    transaction: NormalizedTransaction,
) -> dict[str, Any]:
    """Convert a normalized transaction to a BSON-safe document."""

    return {
        "_id": transaction.id,
        "upload_id": transaction.upload_id,
        "raw_record_id": transaction.raw_record_id,
        "source_transaction_id": transaction.source_transaction_id,
        "transaction_date": _date_to_datetime(transaction.transaction_date),
        "posted_date": _date_to_datetime(transaction.posted_date),
        "description_original": transaction.description_original,
        "description_normalized": transaction.description_normalized,
        "amount": (Decimal128(transaction.amount) if transaction.amount is not None else None),
        "currency": transaction.currency,
        "bank_account": transaction.bank_account,
        "direction": (transaction.direction.value if transaction.direction is not None else None),
        "fingerprint": transaction.fingerprint,
        "status": transaction.status.value,
        "duplicate_of": transaction.duplicate_of,
        "validation_issues": _encode_value(
            [issue.model_dump(mode="python") for issue in transaction.validation_issues]
        ),
        "created_at": transaction.created_at,
    }


def transaction_from_document(
    document: dict[str, Any],
) -> NormalizedTransaction:
    """Reconstruct and validate a normalized transaction document."""

    amount_value = document.get("amount")
    if isinstance(amount_value, Decimal128):
        amount: Decimal | None = amount_value.to_decimal()
    elif amount_value is None:
        amount = None
    else:
        raise MongoSerializationError("Stored transaction amount is not BSON Decimal128")

    transaction_date = _datetime_to_date(document.get("transaction_date"))
    posted_date = _datetime_to_date(document.get("posted_date"))

    return NormalizedTransaction.model_validate(
        {
            "id": document["_id"],
            "upload_id": document["upload_id"],
            "raw_record_id": document["raw_record_id"],
            "source_transaction_id": document.get("source_transaction_id"),
            "transaction_date": transaction_date,
            "posted_date": posted_date,
            "description_original": document.get("description_original"),
            "description_normalized": document.get("description_normalized"),
            "amount": amount,
            "currency": document.get("currency"),
            "bank_account": document.get("bank_account"),
            "direction": document.get("direction"),
            "fingerprint": document.get("fingerprint"),
            "status": document["status"],
            "duplicate_of": document.get("duplicate_of"),
            "validation_issues": _decode_value(document.get("validation_issues", [])),
            "created_at": document["created_at"],
        }
    )


def _date_to_datetime(value: date | None) -> datetime | None:
    """Represent a calendar date as midnight UTC for MongoDB queries."""

    if value is None:
        return None
    return datetime.combine(value, time.min, tzinfo=UTC)


def _datetime_to_date(value: Any) -> date | None:
    """Recover a calendar date from a BSON datetime."""

    if value is None:
        return None
    if not isinstance(value, datetime):
        raise MongoSerializationError("Stored transaction date is not a datetime")
    return value.date()


def _encode_value(value: Any) -> Any:
    """Recursively convert controlled model metadata to BSON values."""

    if value is None or isinstance(
        value,
        (str, bool, int, UUID, datetime, Binary),
    ):
        return value

    if isinstance(value, Decimal):
        return Decimal128(value)

    if isinstance(value, Enum):
        return value.value

    if isinstance(value, date):
        return _date_to_datetime(value)

    if isinstance(value, bytes):
        return Binary(value)

    if isinstance(value, float):
        if not math.isfinite(value):
            raise MongoSerializationError("Non-finite metadata floats are not supported")
        return value

    if isinstance(value, (list, tuple)):
        return [_encode_value(item) for item in value]

    if isinstance(value, dict):
        encoded: dict[str, Any] = {}

        for key, item in value.items():
            if not isinstance(key, str):
                raise MongoSerializationError("MongoDB document keys must be strings")
            if key.startswith("$") or "." in key:
                raise MongoSerializationError(f"Unsafe MongoDB metadata key: {key}")
            encoded[key] = _encode_value(item)

        return encoded

    raise MongoSerializationError(f"Unsupported MongoDB value type: {type(value).__name__}")


def _decode_value(value: Any) -> Any:
    """Recursively recover controlled metadata from BSON values."""

    if isinstance(value, Decimal128):
        return value.to_decimal()

    if isinstance(value, Binary):
        return bytes(value)

    if isinstance(value, list):
        return [_decode_value(item) for item in value]

    if isinstance(value, dict):
        return {key: _decode_value(item) for key, item in value.items()}

    return value


def _encode_raw_scalar(value: Any) -> dict[str, Any]:
    """Encode one untrusted raw cell while preserving its Python type."""

    if value is None:
        return {"kind": "none", "value": None}

    if isinstance(value, bool):
        return {"kind": "bool", "value": value}

    if isinstance(value, str):
        return {"kind": "str", "value": value}

    if isinstance(value, int):
        return {"kind": "int", "value": value}

    if isinstance(value, float):
        if math.isfinite(value):
            return {"kind": "float", "value": value}
        return {"kind": "float_special", "value": repr(value)}

    if isinstance(value, Decimal):
        return {"kind": "decimal", "value": Decimal128(value)}

    if isinstance(value, datetime):
        return {"kind": "datetime", "value": value.isoformat()}

    if isinstance(value, date):
        return {"kind": "date", "value": value.isoformat()}

    if isinstance(value, time):
        return {"kind": "time", "value": value.isoformat()}

    if isinstance(value, bytes):
        return {"kind": "bytes", "value": Binary(value)}

    raise MongoSerializationError(f"Unsupported raw cell type: {type(value).__name__}")


def _decode_raw_scalar(document: dict[str, Any]) -> Any:
    """Recover one type-tagged raw source value."""

    kind = document["kind"]
    value = document.get("value")

    if kind == "none":
        return None
    if kind in {"bool", "str", "int", "float"}:
        return value
    if kind == "float_special":
        return float(value)
    if kind == "decimal":
        if not isinstance(value, Decimal128):
            raise MongoSerializationError("Stored raw decimal is not BSON Decimal128")
        return value.to_decimal()
    if kind == "datetime":
        return datetime.fromisoformat(value)
    if kind == "date":
        return date.fromisoformat(value)
    if kind == "time":
        return time.fromisoformat(value)
    if kind == "bytes":
        return bytes(value)

    raise MongoSerializationError(f"Unsupported stored raw value kind: {kind}")
