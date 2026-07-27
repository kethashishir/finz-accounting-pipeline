"""Read models for human classification review workflows."""

from __future__ import annotations

from typing import Self

from pydantic import model_validator

from app.models.classification import (
    ImmutableAccountingModel,
    TransactionClassification,
)
from app.models.ingestion import NormalizedTransaction


class ReviewQueueItem(ImmutableAccountingModel):
    """Normalized source evidence paired with its current classification."""

    transaction: NormalizedTransaction
    classification: TransactionClassification

    @model_validator(mode="after")
    def require_matching_transaction(self) -> Self:
        """Prevent a classification from being shown with unrelated evidence."""

        if self.transaction.id != self.classification.normalized_transaction_id:
            raise ValueError("Review transaction and classification identifiers must match")

        return self
