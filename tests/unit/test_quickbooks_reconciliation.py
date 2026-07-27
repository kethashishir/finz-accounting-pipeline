"""Focused tests for QBO P&L parsing and reconciliation."""

from datetime import date
from decimal import Decimal
from uuid import UUID

from app.models.accounting import QBOAccountType
from app.models.classification import TransactionType
from app.models.profit_and_loss import (
    ProfitAndLossAccountLine,
    ProfitAndLossStatement,
    ProfitAndLossTransaction,
)
from app.services.quickbooks.api_client import (
    QuickBooksApiAccount,
)
from app.services.quickbooks.profit_and_loss import (
    parse_quickbooks_profit_and_loss,
)
from app.services.quickbooks.reconciliation import (
    reconcile_profit_and_loss,
)


def accounts():
    """Return the controlled QBO account catalog."""

    return (
        QuickBooksApiAccount(
            Id="1",
            SyncToken="0",
            Name="Repair Service Revenue",
            AcctNum="4000",
            AccountType="Income",
            Active=True,
        ),
        QuickBooksApiAccount(
            Id="2",
            SyncToken="0",
            Name="Materials & Supplies",
            AcctNum="5000",
            AccountType="Cost of Goods Sold",
            Active=True,
        ),
        QuickBooksApiAccount(
            Id="3",
            SyncToken="0",
            Name="Payroll Expense",
            AcctNum="6000",
            AccountType="Expense",
            Active=True,
        ),
    )


def qbo_payload():
    """Return one representative nested QBO report."""

    def section(group, account_id, name, amount):
        return {
            "type": "Section",
            "group": group,
            "Rows": {
                "Row": [
                    {
                        "type": "Data",
                        "ColData": [
                            {
                                "id": account_id,
                                "value": name,
                            },
                            {
                                "value": str(amount),
                            },
                        ],
                    }
                ]
            },
            "Summary": {
                "ColData": [
                    {
                        "value": f"Total {name}",
                    },
                    {
                        "value": str(amount),
                    },
                ]
            },
        }

    return {
        "Header": {
            "ReportName": "ProfitAndLoss",
            "ReportBasis": "Cash",
            "StartPeriod": "2026-04-01",
            "EndPeriod": "2026-04-30",
            "Currency": "USD",
        },
        "Rows": {
            "Row": [
                section(
                    "Income",
                    "1",
                    "Repair Service Revenue",
                    "300.00",
                ),
                section(
                    "COGS",
                    "2",
                    "Materials & Supplies",
                    "90.00",
                ),
                {
                    "type": "Section",
                    "group": "GrossProfit",
                    "Summary": {
                        "ColData": [
                            {"value": "Gross Profit"},
                            {"value": "210.00"},
                        ]
                    },
                },
                section(
                    "Expenses",
                    "3",
                    "Payroll Expense",
                    "40.00",
                ),
                {
                    "type": "Section",
                    "group": "NetIncome",
                    "Summary": {
                        "ColData": [
                            {"value": "Net Income"},
                            {"value": "170.00"},
                        ]
                    },
                },
            ]
        },
    }


def _transaction(
    *,
    transaction_id: str,
    source_amount: Decimal,
    report_amount: Decimal,
    transaction_type: TransactionType,
) -> ProfitAndLossTransaction:
    """Build one valid supporting P&L transaction."""

    return ProfitAndLossTransaction(
        normalized_transaction_id=UUID(transaction_id),
        transaction_date=date(2026, 4, 15),
        description="Reconciliation test transaction",
        bank_account="Operating Checking",
        currency="USD",
        source_amount=source_amount,
        report_amount=report_amount,
        classification_version=1,
        transaction_type=transaction_type,
    )


def internal_statement():
    """Return the matching internal statement."""

    revenue = ProfitAndLossAccountLine(
        account_number="4000",
        account_name="Repair Service Revenue",
        qbo_account_type=QBOAccountType.INCOME,
        total=Decimal("300.00"),
        transactions=(
            _transaction(
                transaction_id=("00000000-0000-0000-0000-000000000001"),
                source_amount=Decimal("300.00"),
                report_amount=Decimal("300.00"),
                transaction_type=TransactionType.REVENUE,
            ),
        ),
    )
    cogs = ProfitAndLossAccountLine(
        account_number="5000",
        account_name="Materials & Supplies",
        qbo_account_type=(QBOAccountType.COST_OF_GOODS_SOLD),
        total=Decimal("90.00"),
        transactions=(
            _transaction(
                transaction_id=("00000000-0000-0000-0000-000000000002"),
                source_amount=Decimal("-90.00"),
                report_amount=Decimal("90.00"),
                transaction_type=(TransactionType.COST_OF_GOODS_SOLD),
            ),
        ),
    )
    expense = ProfitAndLossAccountLine(
        account_number="6000",
        account_name="Payroll Expense",
        qbo_account_type=QBOAccountType.EXPENSES,
        total=Decimal("40.00"),
        transactions=(
            _transaction(
                transaction_id=("00000000-0000-0000-0000-000000000003"),
                source_amount=Decimal("-40.00"),
                report_amount=Decimal("40.00"),
                transaction_type=(TransactionType.OPERATING_EXPENSE),
            ),
        ),
    )

    return ProfitAndLossStatement(
        company_name="BrightFix Home Services LLC",
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        currency="USD",
        revenue_accounts=(revenue,),
        cost_of_goods_sold_accounts=(cogs,),
        operating_expense_accounts=(expense,),
        total_revenue=Decimal("300.00"),
        total_cost_of_goods_sold=Decimal("90.00"),
        gross_profit=Decimal("210.00"),
        total_operating_expenses=Decimal("40.00"),
        net_profit=Decimal("170.00"),
        transaction_count=3,
    )


def test_nested_qbo_report_parses_account_and_totals() -> None:
    """Nested QBO rows map through account IDs."""

    statement = parse_quickbooks_profit_and_loss(
        payload=qbo_payload(),
        qbo_accounts=accounts(),
        expected_company_name=("BrightFix Home Services LLC"),
        expected_start_date=date(2026, 4, 1),
        expected_end_date=date(2026, 4, 30),
    )

    assert {line.account_number: line.amount for line in statement.accounts} == {
        "4000": Decimal("300.00"),
        "5000": Decimal("90.00"),
        "6000": Decimal("40.00"),
    }
    assert statement.net_profit == Decimal("170.00")


def test_matching_statements_reconcile() -> None:
    """Every account and total reconciles at exact cents."""

    qbo = parse_quickbooks_profit_and_loss(
        payload=qbo_payload(),
        qbo_accounts=accounts(),
        expected_company_name=("BrightFix Home Services LLC"),
        expected_start_date=date(2026, 4, 1),
        expected_end_date=date(2026, 4, 30),
    )
    result = reconcile_profit_and_loss(
        internal=internal_statement(),
        quickbooks=qbo,
    )

    assert result.reconciled is True
    assert all(line.difference == Decimal("0.00") for line in result.lines)


def test_mismatch_is_visible() -> None:
    """A QBO discrepancy remains explicit."""

    payload = qbo_payload()
    payload["Rows"]["Row"][-1]["Summary"]["ColData"][1]["value"] = "169.00"

    qbo = parse_quickbooks_profit_and_loss(
        payload=payload,
        qbo_accounts=accounts(),
        expected_company_name=("BrightFix Home Services LLC"),
        expected_start_date=date(2026, 4, 1),
        expected_end_date=date(2026, 4, 30),
    )
    result = reconcile_profit_and_loss(
        internal=internal_statement(),
        quickbooks=qbo,
    )

    net_profit = next(line for line in result.lines if line.key == "net_profit")

    assert result.reconciled is False
    assert net_profit.difference == Decimal("1.00")
