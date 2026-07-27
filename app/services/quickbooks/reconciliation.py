"""Reconcile internal and QuickBooks Profit and Loss statements."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models.profit_and_loss import (
    ProfitAndLossStatement,
)
from app.services.quickbooks.profit_and_loss import (
    QuickBooksProfitAndLossStatement,
)

ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class ProfitAndLossReconciliationLine:
    """One internal-to-QBO account or total comparison."""

    key: str
    label: str
    internal_amount: Decimal
    quickbooks_amount: Decimal
    difference: Decimal
    reconciled: bool
    explanation: str


@dataclass(frozen=True, slots=True)
class ProfitAndLossReconciliationResult:
    """Complete reconciliation for one report period."""

    period_label: str
    lines: tuple[ProfitAndLossReconciliationLine, ...]

    @property
    def reconciled(self) -> bool:
        """Return whether every account and total agrees."""

        return all(line.reconciled for line in self.lines)


def reconcile_profit_and_loss(
    *,
    internal: ProfitAndLossStatement,
    quickbooks: QuickBooksProfitAndLossStatement,
) -> ProfitAndLossReconciliationResult:
    """Compare every P&L account and required total."""

    if internal.start_date != quickbooks.start_date or internal.end_date != quickbooks.end_date:
        raise ValueError("Internal and QuickBooks P&L periods differ")

    if internal.currency != quickbooks.currency:
        raise ValueError("Internal and QuickBooks P&L currencies differ")

    internal_accounts = {
        line.account_number: (
            line.account_name,
            line.total,
        )
        for line in (
            internal.revenue_accounts
            + internal.cost_of_goods_sold_accounts
            + internal.operating_expense_accounts
        )
    }
    quickbooks_accounts = {
        line.account_number: (
            line.account_name,
            line.amount,
        )
        for line in quickbooks.accounts
    }

    lines: list[ProfitAndLossReconciliationLine] = []

    for account_number in sorted(set(internal_accounts) | set(quickbooks_accounts)):
        internal_name, internal_amount = internal_accounts.get(
            account_number,
            (
                quickbooks_accounts[account_number][0],
                ZERO,
            ),
        )
        quickbooks_name, quickbooks_amount = quickbooks_accounts.get(
            account_number,
            (
                internal_name,
                ZERO,
            ),
        )
        label = (
            internal_name
            if internal_name == quickbooks_name
            else (f"{internal_name} / {quickbooks_name}")
        )

        lines.append(
            _line(
                key=account_number,
                label=label,
                internal_amount=internal_amount,
                quickbooks_amount=quickbooks_amount,
            )
        )

    totals = (
        (
            "total_revenue",
            "Total revenue",
            internal.total_revenue,
            quickbooks.total_revenue,
        ),
        (
            "total_cogs",
            "Total COGS",
            internal.total_cost_of_goods_sold,
            quickbooks.total_cost_of_goods_sold,
        ),
        (
            "gross_profit",
            "Gross profit",
            internal.gross_profit,
            quickbooks.gross_profit,
        ),
        (
            "total_operating_expenses",
            "Total operating expenses",
            internal.total_operating_expenses,
            quickbooks.total_operating_expenses,
        ),
        (
            "net_profit",
            "Net profit",
            internal.net_profit,
            quickbooks.net_profit,
        ),
    )

    for key, label, internal_amount, qbo_amount in totals:
        lines.append(
            _line(
                key=key,
                label=label,
                internal_amount=internal_amount,
                quickbooks_amount=qbo_amount,
            )
        )

    return ProfitAndLossReconciliationResult(
        period_label=(f"{internal.start_date.isoformat()} through {internal.end_date.isoformat()}"),
        lines=tuple(lines),
    )


def _line(
    *,
    key: str,
    label: str,
    internal_amount: Decimal,
    quickbooks_amount: Decimal,
) -> ProfitAndLossReconciliationLine:
    """Build one exact-cent reconciliation line."""

    difference = internal_amount - quickbooks_amount
    reconciled = difference == ZERO

    return ProfitAndLossReconciliationLine(
        key=key,
        label=label,
        internal_amount=internal_amount,
        quickbooks_amount=quickbooks_amount,
        difference=difference,
        reconciled=reconciled,
        explanation=(
            "Internal and QuickBooks amounts match."
            if reconciled
            else ("Internal amount differs from the QuickBooks cash-basis report.")
        ),
    )
