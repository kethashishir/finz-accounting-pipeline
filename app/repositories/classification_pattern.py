"""MongoDB persistence for approved learned classification patterns."""

from __future__ import annotations

from uuid import UUID

from pymongo import ASCENDING
from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.db.client import MongoDocument
from app.db.serialization import (
    classification_from_document,
    learned_pattern_from_document,
    learned_pattern_to_document,
)
from app.models.classification import ReviewStatus
from app.models.classification_pattern import (
    ClassificationPatternKey,
    LearnedClassificationPattern,
)
from app.models.ingestion import RecordStatus


class ClassificationPatternConflictError(RuntimeError):
    """An active exact-match key already belongs to another pattern."""


class ClassificationPatternSourceNotFoundError(RuntimeError):
    """The pattern's source transaction or classification does not exist."""


class UnsafeClassificationPatternSourceError(RuntimeError):
    """The supplied pattern does not match its stored approved evidence."""


class ClassificationPatternRepository:
    """Persist and retrieve approved exact-match classification patterns."""

    PATTERN_COLLECTION = "classification_patterns"
    CLASSIFICATION_COLLECTION = "transaction_classifications"
    TRANSACTION_COLLECTION = "normalized_transactions"

    def __init__(
        self,
        database: AsyncDatabase[MongoDocument],
    ) -> None:
        self.patterns: AsyncCollection[MongoDocument] = database[self.PATTERN_COLLECTION]
        self.classifications: AsyncCollection[MongoDocument] = database[
            self.CLASSIFICATION_COLLECTION
        ]
        self.transactions: AsyncCollection[MongoDocument] = database[self.TRANSACTION_COLLECTION]

    async def ensure_indexes(self) -> None:
        """Create exact-match, provenance, and reporting indexes."""

        await self.patterns.create_index(
            [
                ("key.description_normalized", ASCENDING),
                ("key.bank_account", ASCENDING),
                ("key.direction", ASCENDING),
                ("key.currency", ASCENDING),
            ],
            name="ux_classification_pattern_active_key",
            unique=True,
            partialFilterExpression={"active": True},
        )

        await self.patterns.create_index(
            [
                ("source_transaction_id", ASCENDING),
                ("source_classification_version", ASCENDING),
            ],
            name="ix_classification_pattern_source",
        )

        await self.patterns.create_index(
            [
                ("decision.qbo_account.account_number", ASCENDING),
                ("decision.transaction_type", ASCENDING),
            ],
            name="ix_classification_pattern_account_type",
        )

    async def save(
        self,
        pattern: LearnedClassificationPattern,
    ) -> bool:
        """Insert a learned pattern or recognize an exact retry."""

        await self._require_valid_approved_source(pattern)

        document = learned_pattern_to_document(pattern)

        try:
            await self.patterns.insert_one(document)
            return True
        except DuplicateKeyError as exc:
            existing_by_id = await self.patterns.find_one({"_id": pattern.id})

            if existing_by_id is not None:
                existing = learned_pattern_from_document(existing_by_id)

                if existing == pattern:
                    return False

                raise ClassificationPatternConflictError(
                    f"Pattern identifier {pattern.id} already stores different pattern data"
                ) from exc

            existing_active = await self.patterns.find_one(self._active_key_filter(pattern.key))

            if existing_active is not None:
                existing = learned_pattern_from_document(existing_active)

                raise ClassificationPatternConflictError(
                    "An active learned pattern already exists for exact key "
                    f"{existing.key.description_normalized!r}, "
                    f"{existing.key.bank_account!r}, "
                    f"{existing.key.direction.value!r}, "
                    f"{existing.key.currency!r}"
                ) from exc

            raise

    async def find_active(
        self,
        key: ClassificationPatternKey,
    ) -> LearnedClassificationPattern | None:
        """Return the active pattern for an exact normalized key."""

        document = await self.patterns.find_one(self._active_key_filter(key))

        if document is None:
            return None

        return learned_pattern_from_document(document)

    async def find_by_id(
        self,
        pattern_id: UUID,
    ) -> LearnedClassificationPattern | None:
        """Return one pattern by immutable identifier."""

        document = await self.patterns.find_one({"_id": pattern_id})

        if document is None:
            return None

        return learned_pattern_from_document(document)

    @staticmethod
    def _active_key_filter(
        key: ClassificationPatternKey,
    ) -> dict[str, object]:
        return {
            "key.description_normalized": key.description_normalized,
            "key.bank_account": key.bank_account,
            "key.direction": key.direction.value,
            "key.currency": key.currency,
            "active": True,
        }

    async def _require_valid_approved_source(
        self,
        pattern: LearnedClassificationPattern,
    ) -> None:
        transaction = await self.transactions.find_one(
            {"_id": pattern.source_transaction_id},
            projection={
                "description_normalized": 1,
                "bank_account": 1,
                "direction": 1,
                "currency": 1,
                "status": 1,
                "duplicate_of": 1,
            },
        )

        if transaction is None:
            raise ClassificationPatternSourceNotFoundError(
                f"Source normalized transaction {pattern.source_transaction_id} does not exist"
            )

        if (
            transaction.get("status") != RecordStatus.VALID.value
            or transaction.get("duplicate_of") is not None
        ):
            raise UnsafeClassificationPatternSourceError(
                "Learned patterns require a valid canonical transaction"
            )

        description = transaction.get("description_normalized")
        bank_account = transaction.get("bank_account")
        direction = transaction.get("direction")
        currency = transaction.get("currency")

        if (
            not isinstance(description, str)
            or not isinstance(bank_account, str)
            or not isinstance(direction, str)
            or not isinstance(currency, str)
        ):
            raise UnsafeClassificationPatternSourceError(
                "Source transaction lacks complete pattern-match fields"
            )

        stored_key = ClassificationPatternKey(
            description_normalized=description,
            bank_account=bank_account,
            direction=direction,
            currency=currency,
        )

        if stored_key != pattern.key:
            raise UnsafeClassificationPatternSourceError(
                "Pattern key does not match the stored source transaction"
            )

        classification_document = await self.classifications.find_one(
            {"_id": pattern.source_transaction_id}
        )

        if classification_document is None:
            raise ClassificationPatternSourceNotFoundError(
                "Source normalized transaction "
                f"{pattern.source_transaction_id} has no classification"
            )

        classification = classification_from_document(classification_document)

        if classification.review_status is not ReviewStatus.APPROVED:
            raise UnsafeClassificationPatternSourceError(
                "Learned patterns require an approved classification"
            )

        if classification.reviewer is None:
            raise UnsafeClassificationPatternSourceError(
                "Approved source classification lacks reviewer metadata"
            )

        if classification.version != pattern.source_classification_version:
            raise UnsafeClassificationPatternSourceError(
                "Pattern source version does not match the stored classification version"
            )

        if classification.reviewer != pattern.approved_by:
            raise UnsafeClassificationPatternSourceError(
                "Pattern approval metadata does not match the stored review"
            )

        if not classification.corrections:
            raise UnsafeClassificationPatternSourceError(
                "Learned patterns require stored correction history"
            )

        if classification.corrections[-1] != pattern.source_correction:
            raise UnsafeClassificationPatternSourceError(
                "Pattern correction does not match the latest stored correction"
            )

        if classification.decision != pattern.source_correction.corrected_decision:
            raise UnsafeClassificationPatternSourceError(
                "Approved decision does not match the pattern correction"
            )
