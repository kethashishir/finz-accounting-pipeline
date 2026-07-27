"""Tests for deterministic accounting-rule matching."""

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.classification import (
    ClassificationSource,
    TransactionType,
)
from app.models.classification_rule import (
    DeterministicClassificationRule,
    DeterministicRuleMatch,
    DeterministicRuleOutcome,
    DeterministicRuleSet,
)
from app.models.ingestion import (
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
)
from app.services.accounting.chart_of_accounts import load_chart_of_accounts
from app.services.classification.rule_config import (
    load_deterministic_rule_set,
)
from app.services.classification.rule_matching import (
    InvalidDeterministicRuleAccountError,
    UnsafeDeterministicRuleTransactionError,
    match_deterministic_rule,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
RULES_PATH = Path("sample_config/classification_rules.json")


def create_transaction(
    *,
    description_original: str = "MONTHLY SERVICE FEE",
    description_normalized: str = "monthly service fee",
    amount: Decimal = Decimal("-35.00"),
    bank_account: str = "Operating Checking",
    currency: str = "USD",
    direction: TransactionDirection = TransactionDirection.OUTFLOW,
    status: RecordStatus = RecordStatus.VALID,
    duplicate_of=None,
) -> NormalizedTransaction:
    """Create one normalized transaction for matcher tests."""

    return NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-TEST-0001",
        transaction_date=date(2026, 4, 1),
        description_original=description_original,
        description_normalized=description_normalized,
        amount=amount,
        currency=currency,
        bank_account=bank_account,
        direction=direction,
        fingerprint="a" * 64,
        status=status,
        duplicate_of=duplicate_of,
    )


def create_rule(
    *,
    rule_id: str,
    priority: int,
    match: DeterministicRuleMatch,
    account_number: str = "6080",
    account_name: str = "Bank Fees",
    transaction_type: TransactionType = (TransactionType.OPERATING_EXPENSE),
    counterparty_name: str | None = None,
) -> DeterministicClassificationRule:
    """Create one deterministic rule for isolated matcher tests."""

    return DeterministicClassificationRule(
        id=rule_id,
        priority=priority,
        match=match,
        outcome=DeterministicRuleOutcome(
            transaction_type=transaction_type,
            account_number=account_number,
            account_name=account_name,
            counterparty_name=counterparty_name,
            confidence_score=Decimal("0.990"),
            explanation="The configured transaction evidence is exact.",
            review_required=False,
        ),
    )


def supplied_dependencies():
    """Load the validated workbook-derived rules and account catalog."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rules = load_deterministic_rule_set(
        RULES_PATH,
        chart_of_accounts=catalog,
    )
    return catalog, rules


def test_exact_matching_normalizes_case_and_whitespace() -> None:
    """Transaction text is normalized before exact-rule evaluation."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = DeterministicRuleSet(
        schema_version="test",
        rules=(
            create_rule(
                rule_id="monthly_bank_fee",
                priority=10,
                match=DeterministicRuleMatch(
                    description_exact=("monthly service fee",),
                    direction=TransactionDirection.OUTFLOW,
                    bank_account="Operating Checking",
                    currency="USD",
                ),
            ),
        ),
    )

    result = match_deterministic_rule(
        transaction=create_transaction(
            description_normalized="  MONTHLY   SERVICE FEE ",
            bank_account=" operating   checking ",
            currency="usd",
        ),
        rule_set=rule_set,
        chart_of_accounts=catalog,
    )

    assert result is not None
    assert result.rule_id == "monthly_bank_fee"
    assert result.decision.source is (ClassificationSource.DETERMINISTIC_RULE)
    assert result.decision.qbo_account.account_number == "6080"


def test_first_matching_rule_is_selected_by_priority() -> None:
    """Configuration order cannot override explicit rule priority."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = DeterministicRuleSet(
        schema_version="test",
        rules=(
            create_rule(
                rule_id="later_generic_rule",
                priority=200,
                match=DeterministicRuleMatch(
                    description_contains_any=("service",),
                ),
                account_number="6090",
                account_name="Office & General",
            ),
            create_rule(
                rule_id="earlier_specific_rule",
                priority=100,
                match=DeterministicRuleMatch(
                    description_contains_all=(
                        "monthly",
                        "service fee",
                    ),
                ),
            ),
        ),
    )

    result = match_deterministic_rule(
        transaction=create_transaction(),
        rule_set=rule_set,
        chart_of_accounts=catalog,
    )

    assert result is not None
    assert result.rule_id == "earlier_specific_rule"
    assert result.priority == 100


def test_required_any_and_excluded_phrases_are_enforced() -> None:
    """Every positive condition and all exclusions affect matching."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = DeterministicRuleSet(
        schema_version="test",
        rules=(
            create_rule(
                rule_id="repair_receipt",
                priority=10,
                match=DeterministicRuleMatch(
                    description_contains_all=("zelle from",),
                    description_contains_any=("repair", "service"),
                    description_excludes=("service plan",),
                    direction=TransactionDirection.INFLOW,
                ),
                transaction_type=TransactionType.REVENUE,
                account_number="4000",
                account_name="Repair Service Revenue",
            ),
        ),
    )

    matching = match_deterministic_rule(
        transaction=create_transaction(
            description_original=("ZELLE FROM GREENLINE FITNESS REPAIR 4203"),
            description_normalized=("zelle from greenline fitness repair 4203"),
            amount=Decimal("2300.00"),
            direction=TransactionDirection.INFLOW,
        ),
        rule_set=rule_set,
        chart_of_accounts=catalog,
    )
    excluded = match_deterministic_rule(
        transaction=create_transaction(
            description_original=("AUTO PAY GREENLINE FITNESS SERVICE PLAN MAY"),
            description_normalized=("auto pay greenline fitness service plan may"),
            amount=Decimal("1250.00"),
            direction=TransactionDirection.INFLOW,
        ),
        rule_set=rule_set,
        chart_of_accounts=catalog,
    )

    assert matching is not None
    assert matching.rule_id == "repair_receipt"
    assert excluded is None


def test_direction_account_and_currency_constraints_prevent_false_match() -> None:
    """Non-description constraints must also match exactly."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = DeterministicRuleSet(
        schema_version="test",
        rules=(
            create_rule(
                rule_id="bank_fee",
                priority=10,
                match=DeterministicRuleMatch(
                    description_exact=("monthly service fee",),
                    direction=TransactionDirection.OUTFLOW,
                    bank_account="Operating Checking",
                    currency="USD",
                ),
            ),
        ),
    )

    result = match_deterministic_rule(
        transaction=create_transaction(
            bank_account="Tax Reserve",
        ),
        rule_set=rule_set,
        chart_of_accounts=catalog,
    )

    assert result is None


def test_duplicate_transaction_is_rejected_before_matching() -> None:
    """Duplicate evidence cannot receive another classification."""

    catalog, rules = supplied_dependencies()

    with pytest.raises(
        UnsafeDeterministicRuleTransactionError,
        match="valid canonical transactions",
    ):
        match_deterministic_rule(
            transaction=create_transaction(
                status=RecordStatus.DUPLICATE,
                duplicate_of=uuid4(),
            ),
            rule_set=rules,
            chart_of_accounts=catalog,
        )


def test_matched_account_name_is_revalidated_against_catalog() -> None:
    """A forged rule outcome cannot silently use a valid account number."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = DeterministicRuleSet(
        schema_version="test",
        rules=(
            create_rule(
                rule_id="forged_bank_fee",
                priority=10,
                match=DeterministicRuleMatch(
                    description_exact=("monthly service fee",),
                ),
                account_number="6080",
                account_name="Forged Fee Account",
            ),
        ),
    )

    with pytest.raises(
        InvalidDeterministicRuleAccountError,
        match="account name does not match",
    ):
        match_deterministic_rule(
            transaction=create_transaction(),
            rule_set=rule_set,
            chart_of_accounts=catalog,
        )


def test_supplied_rules_classify_safety_sensitive_transactions() -> None:
    """Transfers, owner activity, refunds, and assets take precedence."""

    catalog, rules = supplied_dependencies()

    cases = (
        (
            create_transaction(
                description_original=("ONLINE TRANSFER TO TAX RESERVE TAX RESERVE TRANSFER APR-1"),
                description_normalized=(
                    "online transfer to tax reserve tax reserve transfer apr-1"
                ),
                amount=Decimal("-5000.00"),
            ),
            "transfer_to_tax_reserve",
            TransactionType.TRANSFER,
            "1010",
        ),
        (
            create_transaction(
                description_original=("WIRE FROM MAYA PATEL OWNER CAPITAL"),
                description_normalized=("wire from maya patel owner capital"),
                amount=Decimal("25000.00"),
                direction=TransactionDirection.INFLOW,
            ),
            "owner_contribution",
            TransactionType.OWNER_CONTRIBUTION,
            "3000",
        ),
        (
            create_transaction(
                description_original=("ACH REFUND TO HARBORVIEW APARTMENTS JOB 4800"),
                description_normalized=("ach refund to harborview apartments job 4800"),
                amount=Decimal("-1250.00"),
            ),
            "customer_refund",
            TransactionType.REFUND,
            "4100",
        ),
        (
            create_transaction(
                description_original=("MILWAUKEE COMMERCIAL TOOL PACKAGE"),
                description_normalized=("milwaukee commercial tool package"),
                amount=Decimal("-6800.00"),
            ),
            "commercial_tool_package",
            TransactionType.FIXED_ASSET_PURCHASE,
            "1500",
        ),
    )

    for transaction, rule_id, transaction_type, account_number in cases:
        result = match_deterministic_rule(
            transaction=transaction,
            rule_set=rules,
            chart_of_accounts=catalog,
        )

        assert result is not None
        assert result.rule_id == rule_id
        assert result.decision.transaction_type is transaction_type
        assert result.decision.qbo_account.account_number == account_number


def test_supplied_rules_respect_revenue_and_expense_boundaries() -> None:
    """Workbook-derived phrases map to distinct accounting categories."""

    catalog, rules = supplied_dependencies()

    cases = (
        (
            "AUTO PAY PARKVIEW SENIOR LIVING SERVICE PLAN APR",
            Decimal("1250.00"),
            TransactionDirection.INFLOW,
            "maintenance_plan_revenue",
            "4020",
        ),
        (
            "WIRE PARKVIEW SENIOR LIVING EQUIPMENT INSTALL 4505",
            Decimal("7250.00"),
            TransactionDirection.INFLOW,
            "installation_revenue",
            "4010",
        ),
        (
            "ZELLE FROM GREENLINE FITNESS REPAIR 4203",
            Decimal("2300.00"),
            TransactionDirection.INFLOW,
            "repair_service_revenue",
            "4000",
        ),
        (
            "SUPPLYHOUSE.COM REF 405",
            Decimal("-1125.00"),
            TransactionDirection.OUTFLOW,
            "materials_and_supplies",
            "5000",
        ),
        (
            "ADP PAYROLL APR FIRST HALF",
            Decimal("-12650.00"),
            TransactionDirection.OUTFLOW,
            "payroll_expense",
            "6000",
        ),
    )

    for (
        description,
        amount,
        direction,
        rule_id,
        account_number,
    ) in cases:
        result = match_deterministic_rule(
            transaction=create_transaction(
                description_original=description,
                description_normalized=description.casefold(),
                amount=amount,
                direction=direction,
            ),
            rule_set=rules,
            chart_of_accounts=catalog,
        )

        assert result is not None
        assert result.rule_id == rule_id
        assert result.decision.qbo_account.account_number == account_number
