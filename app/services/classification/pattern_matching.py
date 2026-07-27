"""Apply active learned patterns to canonical normalized transactions."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.models.accounting import ChartOfAccountsConfig
from app.models.classification import (
    ClassificationDecision,
    Counterparty,
    ImmutableAccountingModel,
    QuickBooksAccountMapping,
)
from app.models.classification_pattern import (
    ClassificationPatternKey,
    LearnedClassificationPattern,
)
from app.models.ingestion import NormalizedTransaction, RecordStatus


class PatternLookup(Protocol):
    """Minimal repository contract required for exact pattern matching."""

    async def find_active(
        self,
        key: ClassificationPatternKey,
    ) -> LearnedClassificationPattern | None:
        """Return the active pattern for an exact normalized key."""


class PatternMatchingError(ValueError):
    """A learned pattern cannot be safely applied."""


class UnsafePatternMatchTransactionError(PatternMatchingError):
    """The transaction is not an eligible canonical matching input."""


class InvalidMatchedPatternError(PatternMatchingError):
    """The repository returned an unsafe or inconsistent learned pattern."""


class InvalidMatchedPatternAccountError(PatternMatchingError):
    """The learned account no longer matches the configured catalog."""


class PatternMatchResult(ImmutableAccountingModel):
    """Auditable result of applying one active learned pattern."""

    pattern_id: UUID
    source_transaction_id: UUID
    key: ClassificationPatternKey
    decision: ClassificationDecision


async def match_learned_pattern(
    *,
    transaction: NormalizedTransaction,
    pattern_lookup: PatternLookup,
    chart_of_accounts: ChartOfAccountsConfig,
) -> PatternMatchResult | None:
    """Return a reusable decision when an exact active pattern exists."""

    key = pattern_key_for_transaction(transaction)
    pattern = await pattern_lookup.find_active(key)

    if pattern is None:
        return None

    if not pattern.active:
        raise InvalidMatchedPatternError("Pattern lookup returned an inactive learned pattern")

    if pattern.key != key:
        raise InvalidMatchedPatternError(
            "Returned learned pattern does not match the transaction key"
        )

    pattern_account = pattern.decision.qbo_account

    try:
        configured_account = chart_of_accounts.require(pattern_account.account_number)
    except (KeyError, ValueError) as exc:
        raise InvalidMatchedPatternAccountError(
            "The learned account is not an active configured account"
        ) from exc

    if configured_account.name != pattern_account.account_name:
        raise InvalidMatchedPatternAccountError(
            "The learned account name does not match the configured catalog"
        )

    if pattern.decision.counterparty is None:
        counterparty = None
    else:
        counterparty = Counterparty(
            raw_name=transaction.description_original,
            normalized_name=(pattern.decision.counterparty.normalized_name),
        )

    decision = ClassificationDecision(
        transaction_type=pattern.decision.transaction_type,
        counterparty=counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number=configured_account.number,
            account_name=configured_account.name,
        ),
        confidence_score=pattern.decision.confidence_score,
        explanation=(
            f"Matched active learned pattern {pattern.id} from approved "
            f"source transaction {pattern.source_transaction_id}."
        ),
        source=pattern.decision.source,
        review_required=pattern.decision.review_required,
    )

    return PatternMatchResult(
        pattern_id=pattern.id,
        source_transaction_id=pattern.source_transaction_id,
        key=key,
        decision=decision,
    )


def pattern_key_for_transaction(
    transaction: NormalizedTransaction,
) -> ClassificationPatternKey:
    """Build the exact reusable-pattern key for one canonical transaction."""

    if transaction.status is not RecordStatus.VALID or transaction.duplicate_of is not None:
        raise UnsafePatternMatchTransactionError(
            "Patterns may match only valid canonical transactions"
        )

    if (
        transaction.description_normalized is None
        or transaction.bank_account is None
        or transaction.direction is None
        or transaction.currency is None
    ):
        raise UnsafePatternMatchTransactionError("Transaction lacks complete pattern-match fields")

    return ClassificationPatternKey(
        description_normalized=transaction.description_normalized,
        bank_account=transaction.bank_account,
        direction=transaction.direction,
        currency=transaction.currency,
    )
