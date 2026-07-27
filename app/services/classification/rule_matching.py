"""Apply validated deterministic rules to normalized transactions."""

from __future__ import annotations

from app.models.accounting import ChartOfAccountsConfig
from app.models.classification import (
    ClassificationDecision,
    ClassificationSource,
    Counterparty,
    ImmutableAccountingModel,
    QuickBooksAccountMapping,
)
from app.models.classification_rule import (
    DeterministicClassificationRule,
    DeterministicRuleSet,
)
from app.models.ingestion import NormalizedTransaction, RecordStatus


class DeterministicRuleMatchingError(ValueError):
    """A deterministic rule cannot be safely evaluated or applied."""


class UnsafeDeterministicRuleTransactionError(DeterministicRuleMatchingError):
    """The transaction is not an eligible canonical rule input."""


class InvalidDeterministicRuleAccountError(DeterministicRuleMatchingError):
    """A matched rule no longer agrees with the account catalog."""


class DeterministicRuleMatchResult(ImmutableAccountingModel):
    """Auditable result of applying one deterministic rule."""

    rule_id: str
    priority: int
    decision: ClassificationDecision


def match_deterministic_rule(
    *,
    transaction: NormalizedTransaction,
    rule_set: DeterministicRuleSet,
    chart_of_accounts: ChartOfAccountsConfig,
) -> DeterministicRuleMatchResult | None:
    """Return the first safe deterministic classification by priority."""

    description, bank_account, currency = _validated_transaction_match_values(transaction)

    for rule in rule_set.active_rules:
        if not _rule_matches(
            rule=rule,
            transaction=transaction,
            description=description,
            bank_account=bank_account,
            currency=currency,
        ):
            continue

        try:
            configured_account = chart_of_accounts.require(rule.outcome.account_number)
        except (KeyError, ValueError) as exc:
            raise InvalidDeterministicRuleAccountError(
                f"Rule {rule.id!r} references an unknown or inactive account"
            ) from exc

        if configured_account.name != rule.outcome.account_name:
            raise InvalidDeterministicRuleAccountError(
                f"Rule {rule.id!r} account name does not match the configured catalog"
            )

        if rule.outcome.counterparty_name is None:
            counterparty = None
        else:
            counterparty = Counterparty(
                raw_name=transaction.description_original,
                normalized_name=rule.outcome.counterparty_name,
            )

        decision = ClassificationDecision(
            transaction_type=rule.outcome.transaction_type,
            counterparty=counterparty,
            qbo_account=QuickBooksAccountMapping(
                account_number=configured_account.number,
                account_name=configured_account.name,
            ),
            confidence_score=rule.outcome.confidence_score,
            explanation=(f"Matched deterministic rule {rule.id}: {rule.outcome.explanation}"),
            source=ClassificationSource.DETERMINISTIC_RULE,
            review_required=rule.outcome.review_required,
        )

        return DeterministicRuleMatchResult(
            rule_id=rule.id,
            priority=rule.priority,
            decision=decision,
        )

    return None


def _validated_transaction_match_values(
    transaction: NormalizedTransaction,
) -> tuple[str, str, str]:
    """Return stable transaction fields or reject unsafe inputs."""

    if transaction.status is not RecordStatus.VALID or transaction.duplicate_of is not None:
        raise UnsafeDeterministicRuleTransactionError(
            "Deterministic rules may classify only valid canonical transactions"
        )

    if (
        transaction.description_normalized is None
        or transaction.bank_account is None
        or transaction.direction is None
        or transaction.currency is None
    ):
        raise UnsafeDeterministicRuleTransactionError(
            "Transaction lacks complete deterministic match fields"
        )

    return (
        _normalize_text(transaction.description_normalized),
        _normalize_text(transaction.bank_account),
        transaction.currency.strip().upper(),
    )


def _rule_matches(
    *,
    rule: DeterministicClassificationRule,
    transaction: NormalizedTransaction,
    description: str,
    bank_account: str,
    currency: str,
) -> bool:
    """Return whether every configured rule condition is satisfied."""

    match = rule.match

    if match.direction is not None and transaction.direction is not match.direction:
        return False

    if match.bank_account is not None and bank_account != match.bank_account:
        return False

    if match.currency is not None and currency != match.currency:
        return False

    if match.description_exact and description not in match.description_exact:
        return False

    if match.description_contains_all and not all(
        phrase in description for phrase in match.description_contains_all
    ):
        return False

    if match.description_contains_any and not any(
        phrase in description for phrase in match.description_contains_any
    ):
        return False

    return not any(phrase in description for phrase in match.description_excludes)


def _normalize_text(value: str) -> str:
    """Normalize controlled text for stable case-insensitive matching."""

    return " ".join(value.strip().casefold().split())
