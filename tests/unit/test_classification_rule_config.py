"""Tests for deterministic classification-rule configuration loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.classification import TransactionType
from app.services.accounting.chart_of_accounts import load_chart_of_accounts
from app.services.classification.rule_config import (
    DeterministicRuleConfigurationError,
    load_deterministic_rule_set,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
RULES_PATH = Path("sample_config/classification_rules.json")


def load_payload() -> dict[str, object]:
    """Return a fresh mutable copy of the supplied rule configuration."""

    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def write_payload(
    tmp_path: Path,
    payload: dict[str, object],
) -> Path:
    """Write one temporary rule configuration."""

    path = tmp_path / "classification_rules.json"
    path.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return path


def test_supplied_rules_cover_all_required_accounting_categories() -> None:
    """The configuration references all 21 approved accounts."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = load_deterministic_rule_set(
        RULES_PATH,
        chart_of_accounts=catalog,
    )

    assert rule_set.schema_version == "1.0"
    assert len(rule_set.rules) == 22

    assert {rule.outcome.account_number for rule in rule_set.rules} == {
        account.number for account in catalog.accounts
    }

    assert {rule.outcome.transaction_type for rule in rule_set.rules} == set(TransactionType)


def test_safety_sensitive_rules_have_highest_priority() -> None:
    """Transfers, refunds, owner activity, and assets run first."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = load_deterministic_rule_set(
        RULES_PATH,
        chart_of_accounts=catalog,
    )

    assert [rule.id for rule in rule_set.active_rules[:6]] == [
        "transfer_to_tax_reserve",
        "transfer_from_operating",
        "customer_refund",
        "owner_contribution",
        "owner_distribution",
        "commercial_tool_package",
    ]


def test_rules_preserve_workbook_derived_description_evidence() -> None:
    """Known workbook phrases remain visible in version control."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = load_deterministic_rule_set(
        RULES_PATH,
        chart_of_accounts=catalog,
    )
    by_id = {rule.id: rule for rule in rule_set.rules}

    assert "milwaukee commercial tool package" in (
        by_id["commercial_tool_package"].match.description_exact
    )
    assert "supplyhouse.com" in (by_id["materials_and_supplies"].match.description_contains_any)
    assert "adp payroll" in (by_id["payroll_expense"].match.description_contains_any)
    assert "monthly service fee" in (by_id["bank_fees"].match.description_exact)
    assert "maint plan" in (by_id["maintenance_plan_revenue"].match.description_contains_any)


def test_invalid_json_is_reported_as_configuration_error(
    tmp_path: Path,
) -> None:
    """Malformed rules fail with a domain-specific error."""

    path = tmp_path / "invalid.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(
        DeterministicRuleConfigurationError,
        match="Unable to load deterministic rules",
    ):
        load_deterministic_rule_set(
            path,
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )


def test_unknown_account_is_rejected(
    tmp_path: Path,
) -> None:
    """Rules cannot invent an account outside the catalog."""

    payload = load_payload()
    rules = payload["rules"]
    assert isinstance(rules, list)

    rules[0]["outcome"]["account_number"] = "9999"
    rules[0]["outcome"]["account_name"] = "Invented Bank"

    with pytest.raises(
        DeterministicRuleConfigurationError,
        match="unknown or inactive account",
    ):
        load_deterministic_rule_set(
            write_payload(tmp_path, payload),
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )


def test_account_name_must_match_catalog(
    tmp_path: Path,
) -> None:
    """A valid number cannot be paired with a misleading account name."""

    payload = load_payload()
    rules = payload["rules"]
    assert isinstance(rules, list)

    rules[0]["outcome"]["account_name"] = "Incorrect Reserve Name"

    with pytest.raises(
        DeterministicRuleConfigurationError,
        match="account name does not match",
    ):
        load_deterministic_rule_set(
            write_payload(tmp_path, payload),
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )


def test_transaction_type_must_match_qbo_account_type(
    tmp_path: Path,
) -> None:
    """An operating expense cannot target an income account."""

    payload = load_payload()
    rules = payload["rules"]
    assert isinstance(rules, list)

    payroll = next(item for item in rules if item["id"] == "payroll_expense")
    payroll["outcome"]["account_number"] = "4000"
    payroll["outcome"]["account_name"] = "Repair Service Revenue"

    with pytest.raises(
        DeterministicRuleConfigurationError,
        match="cannot use QuickBooks account type",
    ):
        load_deterministic_rule_set(
            write_payload(tmp_path, payload),
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )


def test_transfer_cannot_target_its_source_bank_account(
    tmp_path: Path,
) -> None:
    """A transfer must point to the other bank account."""

    payload = load_payload()
    rules = payload["rules"]
    assert isinstance(rules, list)

    transfer = next(item for item in rules if item["id"] == "transfer_to_tax_reserve")
    transfer["outcome"]["account_number"] = "1000"
    transfer["outcome"]["account_name"] = "Operating Checking"

    with pytest.raises(
        DeterministicRuleConfigurationError,
        match="counterpart cannot be the same",
    ):
        load_deterministic_rule_set(
            write_payload(tmp_path, payload),
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )
