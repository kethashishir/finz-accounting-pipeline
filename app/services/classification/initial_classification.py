"""Persist initial classifications produced by trusted and optional AI matchers."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.models.accounting import ChartOfAccountsConfig
from app.models.classification import (
    ClassificationDecision,
    ImmutableAccountingModel,
    TransactionClassification,
)
from app.models.classification_rule import (
    DeterministicRuleSet,
    RuleIdentifier,
)
from app.models.ingestion import NormalizedTransaction
from app.services.classification.gemini import (
    GeminiClassifier,
    build_gemini_decision,
    build_gemini_request,
)
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


class GeminiClassificationResult(ImmutableAccountingModel):
    """Persisted classification produced by validated Gemini output."""

    inserted: bool
    classification: TransactionClassification


InitialClassificationResult = (
    LearnedPatternClassificationResult
    | DeterministicRuleClassificationResult
    | GeminiClassificationResult
)


async def classify_initial(
    *,
    transaction: NormalizedTransaction,
    pattern_lookup: PatternLookup,
    rule_set: DeterministicRuleSet,
    classification_writer: InitialClassificationWriter,
    chart_of_accounts: ChartOfAccountsConfig,
    gemini_classifier: GeminiClassifier | None = None,
) -> InitialClassificationResult | None:
    """Classify once using approved corrections, rules, then optional Gemini."""

    pattern_match = await match_learned_pattern(
        transaction=transaction,
        pattern_lookup=pattern_lookup,
        chart_of_accounts=chart_of_accounts,
    )

    if pattern_match is not None:
        inserted, classification = await _persist_initial_decision(
            transaction=transaction,
            decision=pattern_match.decision,
            classification_writer=classification_writer,
        )

        return LearnedPatternClassificationResult(
            inserted=inserted,
            pattern_id=pattern_match.pattern_id,
            source_transaction_id=pattern_match.source_transaction_id,
            classification=classification,
        )

    rule_match = match_deterministic_rule(
        transaction=transaction,
        rule_set=rule_set,
        chart_of_accounts=chart_of_accounts,
    )

    if rule_match is not None:
        inserted, classification = await _persist_initial_decision(
            transaction=transaction,
            decision=rule_match.decision,
            classification_writer=classification_writer,
        )

        return DeterministicRuleClassificationResult(
            inserted=inserted,
            rule_id=rule_match.rule_id,
            priority=rule_match.priority,
            classification=classification,
        )

    if gemini_classifier is None:
        return None

    request = build_gemini_request(
        transaction=transaction,
        chart_of_accounts=chart_of_accounts,
    )
    response = await gemini_classifier.classify(request)
    decision = build_gemini_decision(
        transaction=transaction,
        response=response,
        chart_of_accounts=chart_of_accounts,
    )

    inserted, classification = await _persist_initial_decision(
        transaction=transaction,
        decision=decision,
        classification_writer=classification_writer,
    )

    return GeminiClassificationResult(
        inserted=inserted,
        classification=classification,
    )


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

    inserted, classification = await _persist_initial_decision(
        transaction=transaction,
        decision=match.decision,
        classification_writer=classification_writer,
    )

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

    inserted, classification = await _persist_initial_decision(
        transaction=transaction,
        decision=match.decision,
        classification_writer=classification_writer,
    )

    return DeterministicRuleClassificationResult(
        inserted=inserted,
        rule_id=match.rule_id,
        priority=match.priority,
        classification=classification,
    )


async def _persist_initial_decision(
    *,
    transaction: NormalizedTransaction,
    decision: ClassificationDecision,
    classification_writer: InitialClassificationWriter,
) -> tuple[bool, TransactionClassification]:
    """Construct and save exactly one version-one classification."""

    classification = TransactionClassification(
        normalized_transaction_id=transaction.id,
        decision=decision,
    )

    inserted = await classification_writer.save_initial(classification)

    return inserted, classification
