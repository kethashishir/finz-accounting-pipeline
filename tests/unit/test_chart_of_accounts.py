"""Tests for the configured accounting catalog."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.accounting import (
    ChartOfAccount,
    ChartOfAccountsConfig,
    FinancialStatement,
    QBOAccountType,
)
from app.services.accounting.chart_of_accounts import (
    ChartOfAccountsConfigurationError,
    load_chart_of_accounts,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")


def test_supplied_catalog_loads_all_required_accounts() -> None:
    """The catalog contains the complete workbook-defined account list."""

    catalog = load_chart_of_accounts(CATALOG_PATH)

    assert catalog.company_name == "BrightFix Home Services LLC"
    assert len(catalog.accounts) == 21
    assert len(catalog.balance_sheet_accounts) == 4
    assert len(catalog.profit_and_loss_accounts) == 17

    assert {account.number for account in catalog.accounts} == {
        "1000",
        "1010",
        "1500",
        "3000",
        "4000",
        "4010",
        "4020",
        "4100",
        "5000",
        "5010",
        "6000",
        "6010",
        "6020",
        "6030",
        "6040",
        "6050",
        "6060",
        "6070",
        "6080",
        "6090",
        "6100",
    }


def test_catalog_preserves_quickbooks_account_metadata() -> None:
    """Account mappings retain workbook names, types, and details."""

    catalog = load_chart_of_accounts(CATALOG_PATH)

    fixed_asset = catalog.require("1500")
    assert fixed_asset.name == "Tools & Equipment"
    assert fixed_asset.qbo_account_type == QBOAccountType.FIXED_ASSETS
    assert fixed_asset.suggested_detail_type == "Machinery and Equipment"
    assert fixed_asset.statement == FinancialStatement.BALANCE_SHEET

    refunds = catalog.require("4100")
    assert refunds.name == "Customer Refunds"
    assert refunds.qbo_account_type == QBOAccountType.INCOME
    assert refunds.suggested_detail_type == "Discounts/Refunds Given"
    assert refunds.statement == FinancialStatement.PROFIT_AND_LOSS


def test_unknown_account_mapping_is_rejected() -> None:
    """Classifiers cannot silently map to an unknown account."""

    catalog = load_chart_of_accounts(CATALOG_PATH)

    with pytest.raises(
        KeyError,
        match="Unknown chart-of-accounts number",
    ):
        catalog.require("9999")


def test_duplicate_account_numbers_are_rejected() -> None:
    """Configuration cannot define an ambiguous account number."""

    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    payload["accounts"].append(payload["accounts"][0].copy())

    with pytest.raises(
        ValidationError,
        match="account numbers must be unique",
    ):
        ChartOfAccountsConfig.model_validate(payload)


def test_duplicate_account_names_are_case_insensitive() -> None:
    """Account names remain unique regardless of capitalization."""

    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    duplicate = payload["accounts"][0].copy()
    duplicate["number"] = "9999"
    duplicate["name"] = "operating checking"
    payload["accounts"].append(duplicate)

    with pytest.raises(
        ValidationError,
        match="account names must be unique",
    ):
        ChartOfAccountsConfig.model_validate(payload)


def test_statement_and_account_type_must_agree() -> None:
    """A balance-sheet account cannot be mislabeled as P&L."""

    with pytest.raises(
        ValidationError,
        match="Bank accounts must use balance_sheet",
    ):
        ChartOfAccount(
            number="1000",
            name="Operating Checking",
            qbo_account_type=QBOAccountType.BANK,
            suggested_detail_type="Checking",
            statement=FinancialStatement.PROFIT_AND_LOSS,
            purpose="Primary operating account",
        )


def test_invalid_json_is_reported_as_configuration_error(
    tmp_path: Path,
) -> None:
    """Malformed configuration produces a domain-specific error."""

    invalid_path = tmp_path / "invalid.json"
    invalid_path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(
        ChartOfAccountsConfigurationError,
        match="Unable to load chart of accounts",
    ):
        load_chart_of_accounts(invalid_path)
