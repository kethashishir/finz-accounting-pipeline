"""Immutable cash-basis Profit and Loss reporting models."""

from __future__ import annotations

from calendar import monthrange
from datetime import date
from decimal import Decimal
from typing import Annotated, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.models.accounting import QBOAccountType
from app.models.classification import (
    ImmutableAccountingModel,
    NonEmptyString,
    TransactionClassification,
    TransactionType,
)
from app.models.ingestion import NormalizedTransaction

Money = Annotated[
    Decimal,
    Field(
        max_digits=18,
        decimal_places=2,
    ),
]
CurrencyCode = Annotated[
    str,
    Field(pattern=r"^[A-Z]{3}$"),
]

ZERO = Decimal("0.00")

PROFIT_AND_LOSS_TRANSACTION_TYPES = frozenset(
    {
        TransactionType.REVENUE,
        TransactionType.REFUND,
        TransactionType.COST_OF_GOODS_SOLD,
        TransactionType.OPERATING_EXPENSE,
    }
)

EXPECTED_TRANSACTION_TYPES_BY_ACCOUNT_TYPE = {
    QBOAccountType.INCOME: frozenset(
        {
            TransactionType.REVENUE,
            TransactionType.REFUND,
        }
    ),
    QBOAccountType.COST_OF_GOODS_SOLD: frozenset(
        {
            TransactionType.COST_OF_GOODS_SOLD,
        }
    ),
    QBOAccountType.EXPENSES: frozenset(
        {
            TransactionType.OPERATING_EXPENSE,
        }
    ),
}


class ProfitAndLossSource(ImmutableAccountingModel):
    """Stored transaction and classification evidence for reporting."""

    transaction: NormalizedTransaction
    classification: TransactionClassification


class ProfitAndLossTransaction(ImmutableAccountingModel):
    """One approved canonical transaction included in a P&L."""

    normalized_transaction_id: UUID
    transaction_date: date
    description: NonEmptyString
    bank_account: NonEmptyString
    currency: CurrencyCode
    source_amount: Money
    report_amount: Money
    classification_version: int = Field(
        ge=1,
        strict=True,
    )
    transaction_type: TransactionType

    @model_validator(mode="after")
    def validate_reporting_sign(self) -> Self:
        """Require cash-flow signs to agree with P&L presentation."""

        if self.transaction_type not in (PROFIT_AND_LOSS_TRANSACTION_TYPES):
            raise ValueError("Only Profit and Loss transaction types may appear in a P&L drilldown")

        if self.source_amount == ZERO:
            raise ValueError("A P&L transaction cannot have a zero source amount")

        if self.transaction_type is TransactionType.REVENUE:
            if self.source_amount <= ZERO:
                raise ValueError("Revenue must originate from a positive cash receipt")
            expected_report_amount = self.source_amount
        elif self.transaction_type is TransactionType.REFUND:
            if self.source_amount >= ZERO:
                raise ValueError("A customer refund must originate from a negative cash payment")
            expected_report_amount = self.source_amount
        else:
            if self.source_amount >= ZERO:
                raise ValueError(
                    "COGS and operating expenses must originate from negative cash payments"
                )
            expected_report_amount = -self.source_amount

        if self.report_amount != expected_report_amount:
            raise ValueError(
                "P&L report amount does not match the required cash-basis sign convention"
            )

        return self


class ProfitAndLossAccountLine(ImmutableAccountingModel):
    """One chart-of-accounts line and its transaction drilldown."""

    account_number: str = Field(pattern=r"^\d{4}$")
    account_name: NonEmptyString
    qbo_account_type: QBOAccountType
    total: Money
    transactions: tuple[
        ProfitAndLossTransaction,
        ...,
    ] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_account_line(self) -> Self:
        """Require account type, transaction types, and total to agree."""

        allowed_transaction_types = EXPECTED_TRANSACTION_TYPES_BY_ACCOUNT_TYPE.get(
            self.qbo_account_type
        )

        if allowed_transaction_types is None:
            raise ValueError("Balance-sheet accounts cannot appear in a Profit and Loss statement")

        for transaction in self.transactions:
            if transaction.transaction_type not in allowed_transaction_types:
                raise ValueError(
                    "P&L transaction type does not match the account's QuickBooks account type"
                )

        calculated_total = sum(
            (transaction.report_amount for transaction in self.transactions),
            ZERO,
        )

        if self.total != calculated_total:
            raise ValueError("P&L account total does not equal its transaction drilldown")

        if (
            self.qbo_account_type
            in {
                QBOAccountType.COST_OF_GOODS_SOLD,
                QBOAccountType.EXPENSES,
            }
            and self.total < ZERO
        ):
            raise ValueError("COGS and operating-expense totals cannot be negative")

        return self


class ProfitAndLossStatement(ImmutableAccountingModel):
    """One cash-basis Profit and Loss statement for a date range."""

    company_name: NonEmptyString
    start_date: date
    end_date: date
    currency: CurrencyCode

    revenue_accounts: tuple[
        ProfitAndLossAccountLine,
        ...,
    ] = ()
    cost_of_goods_sold_accounts: tuple[
        ProfitAndLossAccountLine,
        ...,
    ] = ()
    operating_expense_accounts: tuple[
        ProfitAndLossAccountLine,
        ...,
    ] = ()

    total_revenue: Money
    total_cost_of_goods_sold: Money
    gross_profit: Money
    total_operating_expenses: Money
    net_profit: Money
    transaction_count: int = Field(
        ge=0,
        strict=True,
    )

    @property
    def account_lines(
        self,
    ) -> tuple[ProfitAndLossAccountLine, ...]:
        """Return every account line in statement order."""

        return (
            *self.revenue_accounts,
            *self.cost_of_goods_sold_accounts,
            *self.operating_expense_accounts,
        )

    @property
    def transaction_ids(self) -> frozenset[UUID]:
        """Return every included normalized transaction UUID."""

        return frozenset(
            transaction.normalized_transaction_id
            for line in self.account_lines
            for transaction in line.transactions
        )

    @model_validator(mode="after")
    def validate_statement(self) -> Self:
        """Require sections, totals, and drilldown to reconcile."""

        if self.start_date > self.end_date:
            raise ValueError("P&L start date cannot be after its end date")

        self._require_section_type(
            lines=self.revenue_accounts,
            expected_type=QBOAccountType.INCOME,
            section_name="revenue",
        )
        self._require_section_type(
            lines=self.cost_of_goods_sold_accounts,
            expected_type=QBOAccountType.COST_OF_GOODS_SOLD,
            section_name="cost of goods sold",
        )
        self._require_section_type(
            lines=self.operating_expense_accounts,
            expected_type=QBOAccountType.EXPENSES,
            section_name="operating expenses",
        )

        account_numbers = [line.account_number for line in self.account_lines]

        if len(account_numbers) != len(set(account_numbers)):
            raise ValueError("A P&L account may appear only once in a statement")

        transaction_ids: list[UUID] = []

        for line in self.account_lines:
            for transaction in line.transactions:
                if not (self.start_date <= transaction.transaction_date <= self.end_date):
                    raise ValueError("P&L transaction date is outside the statement period")

                if transaction.currency != self.currency:
                    raise ValueError(
                        "P&L transaction currency does not match the statement currency"
                    )

                transaction_ids.append(transaction.normalized_transaction_id)

        if len(transaction_ids) != len(set(transaction_ids)):
            raise ValueError("A normalized transaction may appear only once in a P&L statement")

        if self.transaction_count != len(transaction_ids):
            raise ValueError("P&L transaction count does not match its drilldown")

        calculated_revenue = sum(
            (line.total for line in self.revenue_accounts),
            ZERO,
        )
        calculated_cogs = sum(
            (line.total for line in self.cost_of_goods_sold_accounts),
            ZERO,
        )
        calculated_operating_expenses = sum(
            (line.total for line in self.operating_expense_accounts),
            ZERO,
        )

        if self.total_revenue != calculated_revenue:
            raise ValueError("Total revenue does not equal the revenue accounts")

        if self.total_cost_of_goods_sold != calculated_cogs:
            raise ValueError("Total COGS does not equal the COGS accounts")

        if self.total_operating_expenses != calculated_operating_expenses:
            raise ValueError("Total operating expenses do not equal the expense accounts")

        expected_gross_profit = self.total_revenue - self.total_cost_of_goods_sold

        if self.gross_profit != expected_gross_profit:
            raise ValueError("Gross profit must equal revenue minus COGS")

        expected_net_profit = self.gross_profit - self.total_operating_expenses

        if self.net_profit != expected_net_profit:
            raise ValueError("Net profit must equal gross profit minus operating expenses")

        return self

    @staticmethod
    def _require_section_type(
        *,
        lines: tuple[
            ProfitAndLossAccountLine,
            ...,
        ],
        expected_type: QBOAccountType,
        section_name: str,
    ) -> None:
        for line in lines:
            if line.qbo_account_type is not expected_type:
                raise ValueError(
                    f"The {section_name} section contains an incompatible account type"
                )


class ProfitAndLossReportSet(ImmutableAccountingModel):
    """Monthly statements plus one consolidated statement."""

    monthly: tuple[
        ProfitAndLossStatement,
        ...,
    ] = Field(min_length=1)
    consolidated: ProfitAndLossStatement

    @model_validator(mode="after")
    def validate_report_set(self) -> Self:
        """Require monthly and consolidated statements to reconcile."""

        ordered = tuple(
            sorted(
                self.monthly,
                key=lambda statement: statement.start_date,
            )
        )

        if self.monthly != ordered:
            raise ValueError("Monthly P&L statements must be ordered by date")

        previous_end: date | None = None
        monthly_transaction_ids: set[UUID] = set()

        for statement in self.monthly:
            month_end = monthrange(
                statement.start_date.year,
                statement.start_date.month,
            )[1]

            if statement.start_date.day != 1 or statement.end_date != date(
                statement.start_date.year,
                statement.start_date.month,
                month_end,
            ):
                raise ValueError("Each monthly P&L must cover one complete calendar month")

            if previous_end is not None:
                expected_start = _next_month_start(previous_end)

                if statement.start_date != expected_start:
                    raise ValueError("Monthly P&L statements must cover consecutive months")

            overlap = monthly_transaction_ids & statement.transaction_ids

            if overlap:
                raise ValueError("A transaction cannot appear in more than one monthly P&L")

            monthly_transaction_ids.update(statement.transaction_ids)
            previous_end = statement.end_date

        first = self.monthly[0]
        last = self.monthly[-1]

        if (
            self.consolidated.start_date != first.start_date
            or self.consolidated.end_date != last.end_date
        ):
            raise ValueError("Consolidated P&L period must span all monthly statements")

        if any(
            statement.company_name != self.consolidated.company_name for statement in self.monthly
        ):
            raise ValueError("Monthly and consolidated company names must match")

        if any(statement.currency != self.consolidated.currency for statement in self.monthly):
            raise ValueError("Monthly and consolidated currencies must match")

        if self.consolidated.transaction_ids != frozenset(monthly_transaction_ids):
            raise ValueError("Consolidated P&L transactions must equal the monthly transaction set")

        self._require_sum(
            field_name="total_revenue",
        )
        self._require_sum(
            field_name="total_cost_of_goods_sold",
        )
        self._require_sum(
            field_name="gross_profit",
        )
        self._require_sum(
            field_name="total_operating_expenses",
        )
        self._require_sum(
            field_name="net_profit",
        )

        expected_count = sum(statement.transaction_count for statement in self.monthly)

        if self.consolidated.transaction_count != expected_count:
            raise ValueError("Consolidated transaction count must equal the monthly counts")

        return self

    def _require_sum(
        self,
        *,
        field_name: str,
    ) -> None:
        expected = sum(
            (getattr(statement, field_name) for statement in self.monthly),
            ZERO,
        )

        if getattr(self.consolidated, field_name) != expected:
            raise ValueError("Consolidated P&L totals must equal the monthly statements")


def _next_month_start(value: date) -> date:
    """Return the first calendar day after the supplied month."""

    if value.month == 12:
        return date(
            value.year + 1,
            1,
            1,
        )

    return date(
        value.year,
        value.month + 1,
        1,
    )
