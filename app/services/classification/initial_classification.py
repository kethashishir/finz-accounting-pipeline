"""Persist initial classifications produced by approved learned patterns."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.models.accounting import ChartOfAccountsConfig
from app.models.classification import (
    ImmutableAccountingModel,
    TransactionClassification,
)
from app.models.ingestion import NormalizedTransaction
from app.services.classification.pattern_matching import (
    PatternLookup,
    match_learned_pattern,
)


class InitialClassificationWriter(Protocol):
    """Minimal persistence contract for initial classifications."""

    async def save_initial(
        self,
        classification: TransactionClassification,
    ) -> bool:
        """Insert an initial classification or recognize an exact retry."""


class LearnedPatternClassificationResult(ImmutableAccountingModel):
    """Result of matching and persisting one learned-pattern decision."""

    inserted: bool
    pattern_id: UUID
    source_transaction_id: UUID
    classification: TransactionClassification


async def classify_from_learned_pattern(
    *,
    transaction: NormalizedTransaction,
    pattern_lookup: PatternLookup,
    classification_writer: InitialClassificationWriter,
    chart_of_accounts: ChartOfAccountsConfig,
) -> LearnedPatternClassificationResult | None:
    """Match and persist an initial learned-pattern classification."""

    match = await match_learned_pattern(
        transaction=transaction,
        pattern_lookup=pattern_lookup,
        chart_of_accounts=chart_of_accounts,
    )

    if match is None:
        return None

    classification = TransactionClassification(
        normalized_transaction_id=transaction.id,
        decision=match.decision,
    )

    inserted = await classification_writer.save_initial(classification)

    return LearnedPatternClassificationResult(
        inserted=inserted,
        pattern_id=match.pattern_id,
        source_transaction_id=match.source_transaction_id,
        classification=classification,
    )
