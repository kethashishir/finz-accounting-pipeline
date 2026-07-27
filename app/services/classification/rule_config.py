"""Load and validate deterministic classification-rule configuration."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.models.accounting import (
    ChartOfAccount,
    ChartOfAccountsConfig,
    QBOAccountType,
)
from app.models.classification import TransactionType
from app.models.classification_rule import (
    DeterministicClassificationRule,
    DeterministicRuleSet,
)


class DeterministicRuleConfigurationError(ValueError):
    """A deterministic rule configuration is unsafe or malformed."""


_EXPECTED_ACCOUNT_TYPES = {
    TransactionType.REVENUE: frozenset({QBOAccountType.INCOME}),
    TransactionType.COST_OF_GOODS_SOLD: frozenset({QBOAccountType.COST_OF_GOODS_SOLD}),
    TransactionType.OPERATING_EXPENSE: frozenset({QBOAccountType.EXPENSES}),
    TransactionType.REFUND: frozenset({QBOAccountType.INCOME}),
    TransactionType.TRANSFER: frozenset({QBOAccountType.BANK}),
    TransactionType.OWNER_CONTRIBUTION: frozenset({QBOAccountType.EQUITY}),
    TransactionType.OWNER_DISTRIBUTION: frozenset({QBOAccountType.EQUITY}),
    TransactionType.FIXED_ASSET_PURCHASE: frozenset({QBOAccountType.FIXED_ASSETS}),
}


def load_deterministic_rule_set(
    path: Path,
    *,
    chart_of_accounts: ChartOfAccountsConfig,
) -> DeterministicRuleSet:
    """Load rules and validate every accounting target against the catalog."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rule_set = DeterministicRuleSet.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise DeterministicRuleConfigurationError(
            f"Unable to load deterministic rules from {path}: {exc}"
        ) from exc

    for rule in rule_set.rules:
        try:
            account = chart_of_accounts.require(rule.outcome.account_number)
        except (KeyError, ValueError) as exc:
            raise DeterministicRuleConfigurationError(
                f"Rule {rule.id!r} references an unknown or inactive "
                f"account: {rule.outcome.account_number}"
            ) from exc

        _validate_rule_accounting(
            rule=rule,
            account=account,
        )

    return rule_set


def _validate_rule_accounting(
    *,
    rule: DeterministicClassificationRule,
    account: ChartOfAccount,
) -> None:
    """Require the configured transaction type and account to agree."""

    if account.name != rule.outcome.account_name:
        raise DeterministicRuleConfigurationError(
            f"Rule {rule.id!r} account name does not match catalog "
            f"account {account.number}: expected {account.name!r}, "
            f"received {rule.outcome.account_name!r}"
        )

    expected_types = _EXPECTED_ACCOUNT_TYPES[rule.outcome.transaction_type]

    if account.qbo_account_type not in expected_types:
        raise DeterministicRuleConfigurationError(
            f"Rule {rule.id!r} transaction type "
            f"{rule.outcome.transaction_type.value!r} cannot use "
            f"QuickBooks account type "
            f"{account.qbo_account_type.value!r}"
        )

    if rule.outcome.transaction_type is TransactionType.REFUND and account.number != "4100":
        raise DeterministicRuleConfigurationError(f"Rule {rule.id!r} refunds must use account 4100")

    if rule.outcome.transaction_type is TransactionType.REVENUE and account.number == "4100":
        raise DeterministicRuleConfigurationError(
            f"Rule {rule.id!r} ordinary revenue cannot use refund account 4100"
        )

    if (
        rule.outcome.transaction_type is TransactionType.TRANSFER
        and rule.match.bank_account is not None
        and rule.match.bank_account == account.name.casefold()
    ):
        raise DeterministicRuleConfigurationError(
            f"Rule {rule.id!r} transfer counterpart cannot be the same as the source bank account"
        )
