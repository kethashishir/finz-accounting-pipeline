"""Accounting totals for a read-only QuickBooks synchronization preview."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models.quickbooks_sync import (
    QuickBooksJournalEntryPlan,
    QuickBooksPostingType,
)

ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class QuickBooksAccountPostingTotal:
    """Aggregated posting movement for one QBO account."""

    account_number: str
    account_name: str
    debits: Decimal
    credits: Decimal

    @property
    def net_debit(self) -> Decimal:
        """Return debit movement minus credit movement."""

        return self.debits - self.credits


@dataclass(frozen=True, slots=True)
class QuickBooksSyncAccountingSummary:
    """Accounting proof calculated before any QBO write."""

    total_debits: Decimal
    total_credits: Decimal
    revenue: Decimal
    cost_of_goods_sold: Decimal
    operating_expenses: Decimal
    account_totals: tuple[QuickBooksAccountPostingTotal, ...]

    @property
    def gross_profit(self) -> Decimal:
        """Return revenue less cost of goods sold."""

        return self.revenue - self.cost_of_goods_sold

    @property
    def net_income(self) -> Decimal:
        """Return revenue less COGS and operating expenses."""

        return self.revenue - self.cost_of_goods_sold - self.operating_expenses


def summarize_quickbooks_posting_plans(
    plans: tuple[QuickBooksJournalEntryPlan, ...],
) -> QuickBooksSyncAccountingSummary:
    """Aggregate balanced plans into accounting totals."""

    account_movements: dict[
        tuple[str, str],
        list[Decimal],
    ] = {}
    total_debits = ZERO
    total_credits = ZERO

    for plan in plans:
        for line in plan.lines:
            key = (
                line.account_number,
                line.account_name,
            )
            movement = account_movements.setdefault(
                key,
                [ZERO, ZERO],
            )

            if line.posting_type is QuickBooksPostingType.DEBIT:
                movement[0] += line.amount
                total_debits += line.amount
            else:
                movement[1] += line.amount
                total_credits += line.amount

    if total_debits != total_credits:
        raise ValueError(
            f"QuickBooks preview is not balanced: debits={total_debits}, credits={total_credits}"
        )

    account_totals = tuple(
        QuickBooksAccountPostingTotal(
            account_number=account_number,
            account_name=account_name,
            debits=movement[0],
            credits=movement[1],
        )
        for (
            account_number,
            account_name,
        ), movement in sorted(account_movements.items())
    )

    revenue = sum(
        (
            account.credits - account.debits
            for account in account_totals
            if account.account_number.startswith("4")
        ),
        ZERO,
    )
    cost_of_goods_sold = sum(
        (
            account.debits - account.credits
            for account in account_totals
            if account.account_number.startswith("5")
        ),
        ZERO,
    )
    operating_expenses = sum(
        (
            account.debits - account.credits
            for account in account_totals
            if account.account_number.startswith("6")
        ),
        ZERO,
    )

    return QuickBooksSyncAccountingSummary(
        total_debits=total_debits,
        total_credits=total_credits,
        revenue=revenue,
        cost_of_goods_sold=cost_of_goods_sold,
        operating_expenses=operating_expenses,
        account_totals=account_totals,
    )
