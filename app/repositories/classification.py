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
from app.models.classification import (
    ReviewStatus,
    TransactionClassification,
)
from app.models.ingestion import RecordStatus


class ClassificationPersistenceConflictError(RuntimeError):
    """A transaction already has a different classification document."""


class ClassificationTransactionNotFoundError(RuntimeError):
    """The referenced normalized transaction does not exist."""


class ClassificationNotFoundError(RuntimeError):
    """The requested transaction has no stored classification."""


class UnsafeClassificationTransactionError(RuntimeError):
    """The referenced transaction is not a valid canonical record."""


class StaleClassificationVersionError(RuntimeError):
    """A write was based on an outdated classification version."""


class InvalidClassificationTransitionError(ValueError):
    """A proposed classification change is not a valid state transition."""


class ClassificationReviewConflictError(RuntimeError):
    """A classification already has a competing final review outcome."""


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

        existing = await self._find_required_classification(
            classification.normalized_transaction_id
        )

        if existing != classification:
            raise ClassificationPersistenceConflictError(
                "Normalized transaction "
                f"{classification.normalized_transaction_id} "
                "already has a different classification"
            )

        return False

    async def save_correction(
        self,
        classification: TransactionClassification,
        *,
        expected_version: int,
    ) -> bool:
        """Atomically replace a classification with its next correction."""

        if expected_version < 1:
            raise ValueError("expected_version must be at least 1")

        await self._require_valid_canonical_transaction(classification.normalized_transaction_id)

        current = await self._find_required_classification(classification.normalized_transaction_id)

        if current == classification:
            return False

        if current.version != expected_version:
            raise StaleClassificationVersionError(
                "Expected classification version "
                f"{expected_version}, but stored version is "
                f"{current.version}"
            )

        self._validate_correction_transition(
            current=current,
            updated=classification,
            expected_version=expected_version,
        )

        result = await self.classifications.replace_one(
            {
                "_id": classification.normalized_transaction_id,
                "version": expected_version,
            },
            classification_to_document(classification),
        )

        if result.matched_count == 1:
            return True

        latest = await self._find_required_classification(classification.normalized_transaction_id)

        if latest == classification:
            return False

        raise StaleClassificationVersionError(
            "Classification changed before the correction could be saved"
        )

    async def save_review(
        self,
        classification: TransactionClassification,
        *,
        expected_version: int,
    ) -> bool:
        """Atomically approve or reject an unchanged classification decision."""

        if expected_version < 1:
            raise ValueError("expected_version must be at least 1")

        await self._require_valid_canonical_transaction(classification.normalized_transaction_id)

        current = await self._find_required_classification(classification.normalized_transaction_id)

        if current == classification:
            return False

        if current.version != expected_version:
            raise StaleClassificationVersionError(
                "Expected classification version "
                f"{expected_version}, but stored version is "
                f"{current.version}"
            )

        self._validate_review_transition(
            current=current,
            updated=classification,
        )

        result = await self.classifications.replace_one(
            {
                "_id": classification.normalized_transaction_id,
                "version": expected_version,
                "review_status": ReviewStatus.PENDING.value,
            },
            classification_to_document(classification),
        )

        if result.matched_count == 1:
            return True

        latest = await self._find_required_classification(classification.normalized_transaction_id)

        if latest == classification:
            return False

        raise ClassificationReviewConflictError(
            "Classification received another review outcome before this review could be saved"
        )

    async def find_by_transaction_id(
        self,
        normalized_transaction_id: UUID,
    ) -> TransactionClassification | None:
        """Return the current classification for one transaction."""

        document = await self.classifications.find_one({"_id": normalized_transaction_id})

        if document is None:
            return None

        return classification_from_document(document)

    async def _find_required_classification(
        self,
        normalized_transaction_id: UUID,
    ) -> TransactionClassification:
        classification = await self.find_by_transaction_id(normalized_transaction_id)

        if classification is None:
            raise ClassificationNotFoundError(
                f"Normalized transaction {normalized_transaction_id} has no classification"
            )

        return classification

    @staticmethod
    def _validate_correction_transition(
        *,
        current: TransactionClassification,
        updated: TransactionClassification,
        expected_version: int,
    ) -> None:
        if updated.version != expected_version + 1:
            raise InvalidClassificationTransitionError(
                "A correction must increment the classification version by exactly one"
            )

        if len(updated.corrections) != len(current.corrections) + 1:
            raise InvalidClassificationTransitionError(
                "A correction must append exactly one history entry"
            )

        if updated.corrections[:-1] != current.corrections:
            raise InvalidClassificationTransitionError(
                "Existing correction history cannot be changed"
            )

        latest_correction = updated.corrections[-1]

        if (
            latest_correction.from_version != expected_version
            or latest_correction.to_version != updated.version
        ):
            raise InvalidClassificationTransitionError(
                "The latest correction versions do not match the expected transition"
            )

        if latest_correction.previous_decision != current.decision:
            raise InvalidClassificationTransitionError(
                "The correction must begin with the stored decision"
            )

        if updated.review_status is not ReviewStatus.PENDING:
            raise InvalidClassificationTransitionError(
                "A corrected classification must return to pending review"
            )

        if updated.reviewer is not None:
            raise InvalidClassificationTransitionError(
                "A corrected classification cannot retain final reviewer metadata"
            )

    @staticmethod
    def _validate_review_transition(
        *,
        current: TransactionClassification,
        updated: TransactionClassification,
    ) -> None:
        if current.review_status is not ReviewStatus.PENDING:
            raise ClassificationReviewConflictError(
                "Only a pending classification may receive a final review outcome"
            )

        if updated.review_status not in {
            ReviewStatus.APPROVED,
            ReviewStatus.REJECTED,
        }:
            raise InvalidClassificationTransitionError(
                "A review must approve or reject the classification"
            )

        if updated.version != current.version:
            raise InvalidClassificationTransitionError(
                "A review cannot change the classification version"
            )

        if updated.decision != current.decision:
            raise InvalidClassificationTransitionError(
                "A review cannot change the classification decision"
            )

        if updated.corrections != current.corrections:
            raise InvalidClassificationTransitionError("A review cannot change correction history")

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
