"""Tests for immutable cash-basis Profit and Loss models."""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.models.accounting import QBOAccountType
from app.models.classification import TransactionType
from app.models.profit_and_loss import (
    ProfitAndLossAccountLine,
    ProfitAndLossReportSet,
    ProfitAndLossStatement,
    ProfitAndLossTransaction,
)


def create_transaction(
    *,
    transaction_date: date,
    transaction_type: TransactionType,
    source_amount: str,
    report_amount: str,
    transaction_id: UUID | None = None,
) -> ProfitAndLossTransaction:
    """Create one P&L drilldown transaction."""

    return ProfitAndLossTransaction(
        normalized_transaction_id=(transaction_id or uuid4()),
        transaction_date=transaction_date,
        description="Test transaction",
        bank_account="Operating Checking",
        currency="USD",
        source_amount=Decimal(source_amount),
        report_amount=Decimal(report_amount),
        classification_version=1,
        transaction_type=transaction_type,
    )


def create_line(
    *,
    account_number: str,
    account_name: str,
    account_type: QBOAccountType,
    transactions: tuple[
        ProfitAndLossTransaction,
        ...,
    ],
) -> ProfitAndLossAccountLine:
    """Create a reconciled P&L account line."""

    return ProfitAndLossAccountLine(
        account_number=account_number,
        account_name=account_name,
        qbo_account_type=account_type,
        total=sum(
            (transaction.report_amount for transaction in transactions),
            Decimal("0.00"),
        ),
        transactions=transactions,
    )


def create_statement(
    *,
    year: int,
    month: int,
    transaction_ids: tuple[
        UUID,
        UUID,
        UUID,
        UUID,
    ]
    | None = None,
) -> ProfitAndLossStatement:
    """Create one complete monthly P&L statement."""

    identifiers = transaction_ids or (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    statement_date = date(year, month, 10)
    end_date = date(
        year,
        month,
        monthrange(year, month)[1],
    )

    revenue = create_transaction(
        transaction_date=statement_date,
        transaction_type=TransactionType.REVENUE,
        source_amount="1000.00",
        report_amount="1000.00",
        transaction_id=identifiers[0],
    )
    refund = create_transaction(
        transaction_date=statement_date,
        transaction_type=TransactionType.REFUND,
        source_amount="-100.00",
        report_amount="-100.00",
        transaction_id=identifiers[1],
    )
    cogs = create_transaction(
        transaction_date=statement_date,
        transaction_type=(TransactionType.COST_OF_GOODS_SOLD),
        source_amount="-300.00",
        report_amount="300.00",
        transaction_id=identifiers[2],
    )
    expense = create_transaction(
        transaction_date=statement_date,
        transaction_type=(TransactionType.OPERATING_EXPENSE),
        source_amount="-200.00",
        report_amount="200.00",
        transaction_id=identifiers[3],
    )

    return ProfitAndLossStatement(
        company_name="BrightFix Home Services LLC",
        start_date=date(year, month, 1),
        end_date=end_date,
        currency="USD",
        revenue_accounts=(
            create_line(
                account_number="4000",
                account_name="Repair Service Revenue",
                account_type=QBOAccountType.INCOME,
                transactions=(revenue,),
            ),
            create_line(
                account_number="4100",
                account_name="Customer Refunds",
                account_type=QBOAccountType.INCOME,
                transactions=(refund,),
            ),
        ),
        cost_of_goods_sold_accounts=(
            create_line(
                account_number="5000",
                account_name="Materials & Supplies",
                account_type=(QBOAccountType.COST_OF_GOODS_SOLD),
                transactions=(cogs,),
            ),
        ),
        operating_expense_accounts=(
            create_line(
                account_number="6000",
                account_name="Payroll Expense",
                account_type=QBOAccountType.EXPENSES,
                transactions=(expense,),
            ),
        ),
        total_revenue=Decimal("900.00"),
        total_cost_of_goods_sold=Decimal("300.00"),
        gross_profit=Decimal("600.00"),
        total_operating_expenses=Decimal("200.00"),
        net_profit=Decimal("400.00"),
        transaction_count=4,
    )


def combine_statements(
    statements: tuple[
        ProfitAndLossStatement,
        ...,
    ],
) -> ProfitAndLossStatement:
    """Create one consolidated statement from monthly drilldowns."""

    def combine_lines(
        attribute: str,
    ) -> tuple[ProfitAndLossAccountLine, ...]:
        grouped: dict[
            str,
            list[ProfitAndLossTransaction],
        ] = {}
        metadata: dict[
            str,
            tuple[str, QBOAccountType],
        ] = {}

        for statement in statements:
            for line in getattr(statement, attribute):
                grouped.setdefault(
                    line.account_number,
                    [],
                ).extend(line.transactions)
                metadata[line.account_number] = (
                    line.account_name,
                    line.qbo_account_type,
                )

        return tuple(
            create_line(
                account_number=account_number,
                account_name=metadata[account_number][0],
                account_type=metadata[account_number][1],
                transactions=tuple(grouped[account_number]),
            )
            for account_number in sorted(grouped)
        )

    return ProfitAndLossStatement(
        company_name=statements[0].company_name,
        start_date=statements[0].start_date,
        end_date=statements[-1].end_date,
        currency="USD",
        revenue_accounts=combine_lines("revenue_accounts"),
        cost_of_goods_sold_accounts=combine_lines("cost_of_goods_sold_accounts"),
        operating_expense_accounts=combine_lines("operating_expense_accounts"),
        total_revenue=sum(
            (statement.total_revenue for statement in statements),
            Decimal("0.00"),
        ),
        total_cost_of_goods_sold=sum(
            (statement.total_cost_of_goods_sold for statement in statements),
            Decimal("0.00"),
        ),
        gross_profit=sum(
            (statement.gross_profit for statement in statements),
            Decimal("0.00"),
        ),
        total_operating_expenses=sum(
            (statement.total_operating_expenses for statement in statements),
            Decimal("0.00"),
        ),
        net_profit=sum(
            (statement.net_profit for statement in statements),
            Decimal("0.00"),
        ),
        transaction_count=sum(statement.transaction_count for statement in statements),
    )


def test_valid_statement_reconciles_all_totals() -> None:
    """A valid statement preserves cash and presentation signs."""

    statement = create_statement(
        year=2026,
        month=4,
    )

    assert statement.total_revenue == Decimal("900.00")
    assert statement.total_cost_of_goods_sold == (Decimal("300.00"))
    assert statement.gross_profit == Decimal("600.00")
    assert statement.total_operating_expenses == (Decimal("200.00"))
    assert statement.net_profit == Decimal("400.00")
    assert statement.transaction_count == 4
    assert len(statement.transaction_ids) == 4


def test_balance_sheet_transaction_type_is_rejected() -> None:
    """Transfers and other balance-sheet activity cannot enter P&L."""

    with pytest.raises(
        ValidationError,
        match="Only Profit and Loss transaction types",
    ):
        create_transaction(
            transaction_date=date(2026, 4, 1),
            transaction_type=TransactionType.TRANSFER,
            source_amount="-100.00",
            report_amount="100.00",
        )


@pytest.mark.parametrize(
    (
        "transaction_type",
        "source_amount",
        "report_amount",
        "message",
    ),
    [
        (
            TransactionType.REVENUE,
            "-100.00",
            "-100.00",
            "Revenue must originate",
        ),
        (
            TransactionType.REFUND,
            "100.00",
            "100.00",
            "refund must originate",
        ),
        (
            TransactionType.OPERATING_EXPENSE,
            "-100.00",
            "-100.00",
            "sign convention",
        ),
    ],
)
def test_transaction_sign_rules_are_enforced(
    transaction_type: TransactionType,
    source_amount: str,
    report_amount: str,
    message: str,
) -> None:
    """Cash-flow direction cannot be obscured during reporting."""

    with pytest.raises(
        ValidationError,
        match=message,
    ):
        create_transaction(
            transaction_date=date(2026, 4, 1),
            transaction_type=transaction_type,
            source_amount=source_amount,
            report_amount=report_amount,
        )


def test_account_type_must_match_transaction_type() -> None:
    """Revenue cannot be placed in an expense account line."""

    revenue = create_transaction(
        transaction_date=date(2026, 4, 1),
        transaction_type=TransactionType.REVENUE,
        source_amount="100.00",
        report_amount="100.00",
    )

    with pytest.raises(
        ValidationError,
        match="does not match",
    ):
        create_line(
            account_number="6000",
            account_name="Payroll Expense",
            account_type=QBOAccountType.EXPENSES,
            transactions=(revenue,),
        )


def test_account_total_must_equal_drilldown() -> None:
    """An account line cannot hide a transaction mismatch."""

    expense = create_transaction(
        transaction_date=date(2026, 4, 1),
        transaction_type=(TransactionType.OPERATING_EXPENSE),
        source_amount="-100.00",
        report_amount="100.00",
    )

    with pytest.raises(
        ValidationError,
        match="account total",
    ):
        ProfitAndLossAccountLine(
            account_number="6000",
            account_name="Payroll Expense",
            qbo_account_type=QBOAccountType.EXPENSES,
            total=Decimal("99.00"),
            transactions=(expense,),
        )


def test_statement_formula_mismatch_is_rejected() -> None:
    """Gross profit cannot diverge from revenue minus COGS."""

    statement = create_statement(
        year=2026,
        month=4,
    )
    values = statement.model_dump()
    values["gross_profit"] = Decimal("601.00")

    with pytest.raises(
        ValidationError,
        match="Gross profit",
    ):
        ProfitAndLossStatement.model_validate(values)


def test_duplicate_transaction_is_rejected() -> None:
    """The same bank transaction cannot be counted twice."""

    duplicate_id = uuid4()
    transaction = create_transaction(
        transaction_date=date(2026, 4, 1),
        transaction_type=TransactionType.REVENUE,
        source_amount="100.00",
        report_amount="100.00",
        transaction_id=duplicate_id,
    )

    line_one = create_line(
        account_number="4000",
        account_name="Repair Service Revenue",
        account_type=QBOAccountType.INCOME,
        transactions=(transaction,),
    )
    line_two = create_line(
        account_number="4010",
        account_name="Installation Revenue",
        account_type=QBOAccountType.INCOME,
        transactions=(transaction,),
    )

    with pytest.raises(
        ValidationError,
        match="only once",
    ):
        ProfitAndLossStatement(
            company_name="BrightFix Home Services LLC",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 30),
            currency="USD",
            revenue_accounts=(line_one, line_two),
            total_revenue=Decimal("200.00"),
            total_cost_of_goods_sold=Decimal("0.00"),
            gross_profit=Decimal("200.00"),
            total_operating_expenses=Decimal("0.00"),
            net_profit=Decimal("200.00"),
            transaction_count=2,
        )


def test_monthly_and_consolidated_reports_reconcile() -> None:
    """Consolidated totals and transactions equal the monthly set."""

    april = create_statement(
        year=2026,
        month=4,
    )
    may = create_statement(
        year=2026,
        month=5,
    )
    consolidated = combine_statements(
        (
            april,
            may,
        )
    )

    report_set = ProfitAndLossReportSet(
        monthly=(
            april,
            may,
        ),
        consolidated=consolidated,
    )

    assert report_set.consolidated.total_revenue == (Decimal("1800.00"))
    assert report_set.consolidated.net_profit == (Decimal("800.00"))
    assert report_set.consolidated.transaction_count == 8


def test_consolidated_transaction_mismatch_is_rejected() -> None:
    """A consolidated statement cannot replace monthly drilldown."""

    april = create_statement(
        year=2026,
        month=4,
    )
    may = create_statement(
        year=2026,
        month=5,
    )
    consolidated = combine_statements(
        (
            april,
            may,
        )
    )

    mismatched_values = consolidated.model_dump()
    mismatched_values["revenue_accounts"][0]["transactions"][0]["normalized_transaction_id"] = (
        uuid4()
    )

    mismatched_consolidated = ProfitAndLossStatement.model_validate(mismatched_values)

    with pytest.raises(
        ValidationError,
        match="transactions must equal",
    ):
        ProfitAndLossReportSet(
            monthly=(
                april,
                may,
            ),
            consolidated=mismatched_consolidated,
        )
