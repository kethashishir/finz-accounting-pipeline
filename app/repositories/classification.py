"""MongoDB persistence for transaction classifications."""

from __future__ import annotations

from uuid import UUID

from pymongo import ASCENDING
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from app.db.client import MongoDocument
from app.db.serialization import (
    classification_from_document,
    classification_to_document,
)
from app.models.classification import TransactionClassification
from app.models.ingestion import RecordStatus


class ClassificationPersistenceConflictError(RuntimeError):
    """A transaction already has a different classification document."""


class ClassificationTransactionNotFoundError(RuntimeError):
    """The referenced normalized transaction does not exist."""


class UnsafeClassificationTransactionError(RuntimeError):
    """The referenced transaction is not a valid canonical record."""


class ClassificationRepository:
    """Persist current classifications for canonical transactions."""

    CLASSIFICATION_COLLECTION = "transaction_classifications"
    TRANSACTION_COLLECTION = "normalized_transactions"

    def __init__(
        self,
        database: AsyncDatabase[MongoDocument],
    ) -> None:
        self.classifications: AsyncCollection[MongoDocument] = database[
            self.CLASSIFICATION_COLLECTION
        ]
        self.transactions: AsyncCollection[MongoDocument] = database[self.TRANSACTION_COLLECTION]

    async def ensure_indexes(self) -> None:
        """Create review-queue and financial-reporting indexes."""

        await self.classifications.create_index(
            [
                ("review_status", ASCENDING),
                ("decision.review_required", ASCENDING),
                ("decision.confidence_score", ASCENDING),
            ],
            name="ix_classification_review_queue",
        )

        await self.classifications.create_index(
            [
                ("decision.qbo_account.account_number", ASCENDING),
                ("decision.transaction_type", ASCENDING),
            ],
            name="ix_classification_account_type",
        )

        await self.classifications.create_index(
            [("decision.source", ASCENDING)],
            name="ix_classification_source",
        )

    async def save_initial(
        self,
        classification: TransactionClassification,
    ) -> bool:
        """Insert an initial classification or recognize an exact retry."""

        if classification.version != 1 or classification.corrections:
            raise ValueError(
                "save_initial accepts only version 1 classifications without correction history"
            )

        await self._require_valid_canonical_transaction(classification.normalized_transaction_id)

        document = classification_to_document(classification)

        result = await self.classifications.update_one(
            {"_id": classification.normalized_transaction_id},
            {"$setOnInsert": document},
            upsert=True,
        )

        if result.upserted_id is not None:
            return True

        existing_document = await self.classifications.find_one(
            {"_id": classification.normalized_transaction_id}
        )

        if existing_document is None:
            raise ClassificationPersistenceConflictError(
                "Classification disappeared during an idempotent save"
            )

        existing = classification_from_document(existing_document)

        if existing != classification:
            raise ClassificationPersistenceConflictError(
                "Normalized transaction "
                f"{classification.normalized_transaction_id} "
                "already has a different classification"
            )

        return False

    async def find_by_transaction_id(
        self,
        normalized_transaction_id: UUID,
    ) -> TransactionClassification | None:
        """Return the current classification for one transaction."""

        document = await self.classifications.find_one({"_id": normalized_transaction_id})

        if document is None:
            return None

        return classification_from_document(document)

    async def _require_valid_canonical_transaction(
        self,
        normalized_transaction_id: UUID,
    ) -> None:
        transaction = await self.transactions.find_one(
            {"_id": normalized_transaction_id},
            projection={
                "status": 1,
                "duplicate_of": 1,
            },
        )

        if transaction is None:
            raise ClassificationTransactionNotFoundError(
                f"Normalized transaction {normalized_transaction_id} does not exist"
            )

        if (
            transaction.get("status") != RecordStatus.VALID.value
            or transaction.get("duplicate_of") is not None
        ):
            raise UnsafeClassificationTransactionError(
                "Only valid canonical normalized transactions may be classified"
            )
