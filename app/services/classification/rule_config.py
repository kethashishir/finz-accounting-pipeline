"""Load and validate deterministic classification-rule configuration."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from app.models.accounting import (
    ChartOfAccount,
    ChartOfAccountsConfig,
)
from app.models.classification_rule import (
    DeterministicClassificationRule,
    DeterministicRuleSet,
)
from app.services.classification.account_mapping import (
    InvalidClassificationAccountMappingError,
    validate_classification_account_target,
)


class DeterministicRuleConfigurationError(ValueError):
    """A deterministic rule configuration is unsafe or malformed."""


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

    try:
        validate_classification_account_target(
            transaction_type=rule.outcome.transaction_type,
            account=account,
            source_bank_account=rule.match.bank_account,
            subject=f"Rule {rule.id!r}",
        )
    except InvalidClassificationAccountMappingError as exc:
        raise DeterministicRuleConfigurationError(str(exc)) from exc
