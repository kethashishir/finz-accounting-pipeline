"""Read normalized accounting evidence for QuickBooks planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db.serialization import (
    classification_from_document,
    transaction_from_document,
)
from app.models.classification import (
    TransactionClassification,
)
from app.models.ingestion import NormalizedTransaction


@dataclass(frozen=True, slots=True)
class QuickBooksSyncSourceSnapshot:
    """Immutable transaction and classification evidence."""

    transactions: tuple[NormalizedTransaction, ...]
    classifications: tuple[TransactionClassification, ...]


class QuickBooksSyncSourceRepository:
    """Read current source evidence from existing collections."""

    TRANSACTION_COLLECTION = "normalized_transactions"
    CLASSIFICATION_COLLECTION = "transaction_classifications"

    def __init__(
        self,
        database: Any,
    ) -> None:
        self.transactions = database[self.TRANSACTION_COLLECTION]
        self.classifications = database[self.CLASSIFICATION_COLLECTION]

    async def read_snapshot(
        self,
    ) -> QuickBooksSyncSourceSnapshot:
        """Read and validate all current synchronization evidence."""

        transaction_cursor = self.transactions.find({})
        classification_cursor = self.classifications.find({})

        transactions = [
            transaction_from_document(document) async for document in transaction_cursor
        ]
        classifications = [
            classification_from_document(document) async for document in classification_cursor
        ]

        transactions.sort(
            key=lambda transaction: (
                transaction.transaction_date is None,
                transaction.transaction_date,
                transaction.source_transaction_id or "",
                str(transaction.id),
            )
        )
        classifications.sort(
            key=lambda classification: str(classification.normalized_transaction_id)
        )

        return QuickBooksSyncSourceSnapshot(
            transactions=tuple(transactions),
            classifications=tuple(classifications),
        )
