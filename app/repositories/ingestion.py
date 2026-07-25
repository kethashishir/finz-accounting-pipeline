"""MongoDB persistence for ingestion records."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pymongo import ASCENDING, UpdateOne
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import BulkWriteError, DuplicateKeyError

from app.db.client import MongoDocument
from app.db.serialization import (
    raw_record_to_document,
    transaction_from_document,
    transaction_to_document,
    upload_from_document,
    upload_to_document,
)
from app.models.ingestion import (
    NormalizedTransaction,
    RawRecord,
    RecordStatus,
    UploadBatch,
)


class PersistenceConflictError(RuntimeError):
    """An existing UUID is associated with different immutable data."""


class DuplicateFileUploadError(RuntimeError):
    """An identical source file was already registered."""

    def __init__(self, existing_upload_id: UUID) -> None:
        self.existing_upload_id = existing_upload_id
        super().__init__(f"Source file already belongs to upload {existing_upload_id}")


@dataclass(frozen=True, slots=True)
class BatchPersistenceResult:
    """Counts produced by one idempotent persistence attempt."""

    upload_inserted: bool
    raw_inserted: int
    raw_existing: int
    transactions_inserted: int
    transactions_existing: int


class IngestionRepository:
    """Persist ingestion models with retry-safe MongoDB operations."""

    UPLOAD_COLLECTION = "upload_batches"
    RAW_COLLECTION = "raw_records"
    TRANSACTION_COLLECTION = "normalized_transactions"

    def __init__(
        self,
        database: AsyncDatabase[MongoDocument],
    ) -> None:
        self.uploads: AsyncCollection[MongoDocument] = database[self.UPLOAD_COLLECTION]
        self.raw_records: AsyncCollection[MongoDocument] = database[self.RAW_COLLECTION]
        self.transactions: AsyncCollection[MongoDocument] = database[self.TRANSACTION_COLLECTION]

    async def ensure_indexes(self) -> None:
        """Create uniqueness, duplicate-search, and reporting indexes."""

        await self.uploads.create_index(
            [("file_sha256", ASCENDING)],
            name="uq_upload_file_sha256",
            unique=True,
        )

        await self.raw_records.create_index(
            [
                ("upload_id", ASCENDING),
                ("source_sheet", ASCENDING),
                ("source_row_number", ASCENDING),
            ],
            name="uq_raw_source_row",
            unique=True,
        )

        await self.raw_records.create_index(
            [("raw_hash", ASCENDING)],
            name="ix_raw_hash",
        )

        await self.transactions.create_index(
            [("upload_id", ASCENDING)],
            name="ix_transaction_upload",
        )

        await self.transactions.create_index(
            [
                ("bank_account", ASCENDING),
                ("source_transaction_id", ASCENDING),
            ],
            name="ix_transaction_source_identity",
            partialFilterExpression={"source_transaction_id": {"$type": "string"}},
        )

        await self.transactions.create_index(
            [("fingerprint", ASCENDING)],
            name="ix_transaction_fingerprint",
            partialFilterExpression={"fingerprint": {"$type": "string"}},
        )

        await self.transactions.create_index(
            [
                ("transaction_date", ASCENDING),
                ("status", ASCENDING),
                ("bank_account", ASCENDING),
            ],
            name="ix_transaction_reporting_period",
        )

    async def save_batch(
        self,
        *,
        upload: UploadBatch,
        raw_records: Sequence[RawRecord],
        transactions: Sequence[NormalizedTransaction],
    ) -> BatchPersistenceResult:
        """Persist an upload in independently retry-safe stages."""

        upload_inserted = await self.save_upload(upload)
        raw_inserted = await self.save_raw_records(raw_records)
        transactions_inserted = await self.save_transactions(transactions)

        return BatchPersistenceResult(
            upload_inserted=upload_inserted,
            raw_inserted=raw_inserted,
            raw_existing=len(raw_records) - raw_inserted,
            transactions_inserted=transactions_inserted,
            transactions_existing=(len(transactions) - transactions_inserted),
        )

    async def save_upload(self, upload: UploadBatch) -> bool:
        """Insert an upload once or recognize an exact retry."""

        document = upload_to_document(upload)
        identity_filter = {
            "_id": upload.id,
            "file_sha256": upload.file_sha256,
        }

        try:
            result = await self.uploads.update_one(
                identity_filter,
                {"$setOnInsert": document},
                upsert=True,
            )
        except DuplicateKeyError as exc:
            existing = await self.uploads.find_one({"file_sha256": upload.file_sha256})

            if existing is not None and existing["_id"] != upload.id:
                raise DuplicateFileUploadError(existing["_id"]) from exc

            raise PersistenceConflictError(
                f"Upload UUID {upload.id} has conflicting immutable data"
            ) from exc

        return result.upserted_id is not None

    async def save_raw_records(
        self,
        records: Sequence[RawRecord],
    ) -> int:
        """Insert immutable raw rows and return the inserted count."""

        operations = [
            UpdateOne(
                {
                    "_id": record.id,
                    "upload_id": record.upload_id,
                    "raw_hash": record.raw_hash,
                },
                {"$setOnInsert": raw_record_to_document(record)},
                upsert=True,
            )
            for record in records
        ]

        return await self._bulk_set_on_insert(
            collection=self.raw_records,
            operations=operations,
            entity_name="raw record",
        )

    async def save_transactions(
        self,
        transactions: Sequence[NormalizedTransaction],
    ) -> int:
        """Insert normalized records without overwriting later reviews."""

        operations = [
            UpdateOne(
                {
                    "_id": transaction.id,
                    "upload_id": transaction.upload_id,
                    "raw_record_id": transaction.raw_record_id,
                },
                {"$setOnInsert": transaction_to_document(transaction)},
                upsert=True,
            )
            for transaction in transactions
        ]

        return await self._bulk_set_on_insert(
            collection=self.transactions,
            operations=operations,
            entity_name="normalized transaction",
        )

    async def find_upload_by_hash(
        self,
        file_sha256: str,
    ) -> UploadBatch | None:
        """Return the upload previously registered for a file hash."""

        document = await self.uploads.find_one({"file_sha256": file_sha256})
        return upload_from_document(document) if document is not None else None

    async def find_existing_transactions(
        self,
        transactions: Sequence[NormalizedTransaction],
    ) -> tuple[NormalizedTransaction, ...]:
        """Find valid canonical records matching incoming identities."""

        conditions: list[dict[str, Any]] = []
        fingerprints: set[str] = set()

        for transaction in transactions:
            if (
                transaction.source_transaction_id is not None
                and transaction.bank_account is not None
            ):
                conditions.append(
                    {
                        "bank_account": transaction.bank_account,
                        "source_transaction_id": (transaction.source_transaction_id),
                    }
                )

            if transaction.fingerprint is not None:
                fingerprints.add(transaction.fingerprint)

        if fingerprints:
            conditions.append({"fingerprint": {"$in": sorted(fingerprints)}})

        if not conditions:
            return ()

        cursor = self.transactions.find(
            {
                "status": RecordStatus.VALID.value,
                "$or": conditions,
            }
        )

        transactions = [transaction_from_document(document) async for document in cursor]
        return tuple(transactions)

    async def transactions_for_upload(
        self,
        upload_id: UUID,
    ) -> tuple[NormalizedTransaction, ...]:
        """Return normalized records for one upload."""

        cursor = self.transactions.find({"upload_id": upload_id}).sort("created_at", ASCENDING)

        transactions = [transaction_from_document(document) async for document in cursor]
        return tuple(transactions)

    @staticmethod
    async def _bulk_set_on_insert(
        *,
        collection: AsyncCollection[MongoDocument],
        operations: Sequence[UpdateOne],
        entity_name: str,
    ) -> int:
        if not operations:
            return 0

        try:
            result = await collection.bulk_write(
                operations,
                ordered=False,
            )
        except (BulkWriteError, DuplicateKeyError) as exc:
            raise PersistenceConflictError(f"Conflicting {entity_name} identity detected") from exc

        return result.upserted_count
