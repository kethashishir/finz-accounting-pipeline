"""Create reusable patterns from approved accounting corrections."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.models.accounting import ChartOfAccountsConfig
from app.models.classification import (
    ClassificationDecision,
    ClassificationSource,
    QuickBooksAccountMapping,
    ReviewStatus,
    TransactionClassification,
)
from app.models.classification_pattern import (
    ClassificationPatternKey,
    LearnedClassificationPattern,
)
from app.models.ingestion import NormalizedTransaction, RecordStatus


class PatternLearningError(ValueError):
    """An approved correction cannot safely become a reusable pattern."""


class UnsafePatternLearningSourceError(PatternLearningError):
    """The transaction or classification is not an eligible learning source."""


class InvalidPatternAccountError(PatternLearningError):
    """The corrected account does not match the approved account catalog."""


def learn_pattern(
    *,
    transaction: NormalizedTransaction,
    classification: TransactionClassification,
    chart_of_accounts: ChartOfAccountsConfig,
    learned_at: datetime,
) -> LearnedClassificationPattern:
    """Create an auditable exact-match pattern from an approved correction."""

    if transaction.status is not RecordStatus.VALID or transaction.duplicate_of is not None:
        raise UnsafePatternLearningSourceError(
            "Patterns may be learned only from valid canonical transactions"
        )

    if transaction.id != classification.normalized_transaction_id:
        raise UnsafePatternLearningSourceError(
            "Transaction and classification identifiers do not match"
        )

    if classification.review_status is not ReviewStatus.APPROVED or classification.reviewer is None:
        raise UnsafePatternLearningSourceError(
            "Patterns require an approved classification with reviewer metadata"
        )

    if not classification.corrections:
        raise UnsafePatternLearningSourceError("Patterns require an approved correction history")

    source_correction = classification.corrections[-1]
    corrected_decision = source_correction.corrected_decision

    if corrected_decision.source is not ClassificationSource.MANUAL_REVIEW:
        raise UnsafePatternLearningSourceError("The latest correction must come from manual review")

    if classification.decision != corrected_decision:
        raise UnsafePatternLearningSourceError(
            "The approved decision must match the latest correction"
        )

    if (
        transaction.description_normalized is None
        or transaction.bank_account is None
        or transaction.direction is None
        or transaction.currency is None
    ):
        raise UnsafePatternLearningSourceError(
            "The source transaction lacks complete pattern-match fields"
        )

    corrected_account = corrected_decision.qbo_account

    try:
        configured_account = chart_of_accounts.require(corrected_account.account_number)
    except (KeyError, ValueError) as exc:
        raise InvalidPatternAccountError(
            "The corrected account is not an active configured account"
        ) from exc

    if configured_account.name != corrected_account.account_name:
        raise InvalidPatternAccountError(
            "The corrected account name does not match the configured catalog"
        )

    reusable_decision = ClassificationDecision(
        transaction_type=corrected_decision.transaction_type,
        counterparty=corrected_decision.counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number=configured_account.number,
            account_name=configured_account.name,
        ),
        confidence_score=Decimal("1.000"),
        explanation=(
            "Matched an exact transaction pattern learned from an approved manual correction."
        ),
        source=ClassificationSource.STORED_CORRECTION,
        review_required=False,
    )

    return LearnedClassificationPattern(
        key=ClassificationPatternKey(
            description_normalized=transaction.description_normalized,
            bank_account=transaction.bank_account,
            direction=transaction.direction,
            currency=transaction.currency,
        ),
        decision=reusable_decision,
        source_transaction_id=transaction.id,
        source_classification_version=classification.version,
        source_correction=source_correction,
        source_review_status=classification.review_status,
        approved_by=classification.reviewer,
        learned_at=learned_at,
    )
