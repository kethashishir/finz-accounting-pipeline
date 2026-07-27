"""Persist initial classifications produced by trusted matchers."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.models.accounting import ChartOfAccountsConfig
from app.models.classification import (
    ImmutableAccountingModel,
    TransactionClassification,
)
from app.models.classification_rule import (
    DeterministicRuleSet,
    RuleIdentifier,
)
from app.models.ingestion import NormalizedTransaction
from app.services.classification.pattern_matching import (
    PatternLookup,
    match_learned_pattern,
)
from app.services.classification.rule_matching import (
    match_deterministic_rule,
)


class InitialClassificationWriter(Protocol):
    """Persistence boundary for one initial transaction classification."""

    async def save_initial(
        self,
        classification: TransactionClassification,
    ) -> bool:
        """Insert an initial classification or recognize an exact retry."""


class LearnedPatternClassificationResult(ImmutableAccountingModel):
    """Persisted classification produced by an approved learned pattern."""

    inserted: bool
    pattern_id: UUID
    source_transaction_id: UUID
    classification: TransactionClassification


class DeterministicRuleClassificationResult(ImmutableAccountingModel):
    """Persisted classification produced by one deterministic rule."""

    inserted: bool
    rule_id: RuleIdentifier
    priority: int
    classification: TransactionClassification


async def classify_from_learned_pattern(
    *,
    transaction: NormalizedTransaction,
    pattern_lookup: PatternLookup,
    classification_writer: InitialClassificationWriter,
    chart_of_accounts: ChartOfAccountsConfig,
) -> LearnedPatternClassificationResult | None:
    """Match and persist one approved learned-pattern classification."""

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


async def classify_from_deterministic_rule(
    *,
    transaction: NormalizedTransaction,
    rule_set: DeterministicRuleSet,
    classification_writer: InitialClassificationWriter,
    chart_of_accounts: ChartOfAccountsConfig,
) -> DeterministicRuleClassificationResult | None:
    """Match and persist one deterministic initial classification."""

    match = match_deterministic_rule(
        transaction=transaction,
        rule_set=rule_set,
        chart_of_accounts=chart_of_accounts,
    )

    if match is None:
        return None

    classification = TransactionClassification(
        normalized_transaction_id=transaction.id,
        decision=match.decision,
    )

    inserted = await classification_writer.save_initial(classification)

    return DeterministicRuleClassificationResult(
        inserted=inserted,
        rule_id=match.rule_id,
        priority=match.priority,
        classification=classification,
    )
