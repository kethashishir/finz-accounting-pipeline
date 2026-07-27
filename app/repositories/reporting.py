"""Read-only MongoDB access for Profit and Loss reporting evidence."""

from __future__ import annotations

from datetime import UTC, date, datetime, time

from pymongo.asynchronous.collection import AsyncCollection
from pymongo.asynchronous.database import AsyncDatabase

from app.db.client import MongoDocument
from app.db.serialization import (
    classification_from_document,
    transaction_from_document,
)
from app.models.classification import (
    ReviewStatus,
    TransactionType,
)
from app.models.ingestion import RecordStatus
from app.models.profit_and_loss import ProfitAndLossSource


class ProfitAndLossQueryError(ValueError):
    """A requested reporting period or currency is invalid."""


class ProfitAndLossRepository:
    """Load approved canonical evidence for cash-basis reporting."""

    TRANSACTION_COLLECTION = "normalized_transactions"
    CLASSIFICATION_COLLECTION = "transaction_classifications"

    PROFIT_AND_LOSS_TYPES = (
        TransactionType.REVENUE.value,
        TransactionType.REFUND.value,
        TransactionType.COST_OF_GOODS_SOLD.value,
        TransactionType.OPERATING_EXPENSE.value,
    )

    def __init__(
        self,
        database: AsyncDatabase[MongoDocument],
    ) -> None:
        self.transactions: AsyncCollection[MongoDocument] = database[self.TRANSACTION_COLLECTION]

    async def find_approved_sources(
        self,
        *,
        start_date: date,
        end_date: date,
        currency: str,
    ) -> tuple[ProfitAndLossSource, ...]:
        """Return approved canonical P&L sources in deterministic order."""

        normalized_currency = self._validate_query(
            start_date=start_date,
            end_date=end_date,
            currency=currency,
        )

        start_datetime = datetime.combine(
            start_date,
            time.min,
            tzinfo=UTC,
        )
        end_datetime = datetime.combine(
            end_date,
            time.min,
            tzinfo=UTC,
        )

        pipeline = [
            {
                "$match": {
                    "transaction_date": {
                        "$gte": start_datetime,
                        "$lte": end_datetime,
                    },
                    "status": RecordStatus.VALID.value,
                    "duplicate_of": None,
                    "currency": normalized_currency,
                }
            },
            {
                "$lookup": {
                    "from": self.CLASSIFICATION_COLLECTION,
                    "localField": "_id",
                    "foreignField": "_id",
                    "as": "_classification",
                }
            },
            {
                "$unwind": "$_classification",
            },
            {
                "$match": {
                    "_classification.review_status": (ReviewStatus.APPROVED.value),
                    "_classification.decision.transaction_type": {
                        "$in": list(self.PROFIT_AND_LOSS_TYPES)
                    },
                }
            },
            {
                "$sort": {
                    "transaction_date": 1,
                    "source_transaction_id": 1,
                    "_id": 1,
                }
            },
        ]

        cursor = await self.transactions.aggregate(pipeline)

        sources: list[ProfitAndLossSource] = []

        async for joined_document in cursor:
            classification_document = joined_document.pop("_classification")

            sources.append(
                ProfitAndLossSource(
                    transaction=transaction_from_document(joined_document),
                    classification=classification_from_document(classification_document),
                )
            )

        return tuple(sources)

    @staticmethod
    def _validate_query(
        *,
        start_date: date,
        end_date: date,
        currency: str,
    ) -> str:
        """Validate and normalize repository query parameters."""

        if start_date > end_date:
            raise ProfitAndLossQueryError("P&L start date cannot be after its end date")

        normalized_currency = currency.strip().upper()

        if len(normalized_currency) != 3 or not normalized_currency.isalpha():
            raise ProfitAndLossQueryError("P&L currency must be a three-letter code")

        return normalized_currency
