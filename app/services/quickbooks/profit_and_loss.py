"""Parse QuickBooks Online Profit and Loss report responses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from app.services.quickbooks.api_client import (
    QuickBooksApiAccount,
)

ZERO = Decimal("0.00")


class QuickBooksProfitAndLossError(ValueError):
    """A QBO Profit and Loss response cannot be trusted."""


@dataclass(frozen=True, slots=True)
class QuickBooksProfitAndLossAccountLine:
    """One QBO account amount for a reporting period."""

    account_number: str
    account_name: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class QuickBooksProfitAndLossStatement:
    """Parsed cash-basis QBO Profit and Loss statement."""

    company_name: str
    start_date: date
    end_date: date
    currency: str
    accounts: tuple[QuickBooksProfitAndLossAccountLine, ...]
    total_revenue: Decimal
    total_cost_of_goods_sold: Decimal
    gross_profit: Decimal
    total_operating_expenses: Decimal
    net_profit: Decimal


def parse_quickbooks_profit_and_loss(
    *,
    payload: dict[str, object],
    qbo_accounts: tuple[QuickBooksApiAccount, ...],
    expected_company_name: str,
    expected_start_date: date,
    expected_end_date: date,
) -> QuickBooksProfitAndLossStatement:
    """Parse one nested QBO cash-basis P&L response."""

    header = payload.get("Header")

    if not isinstance(header, dict):
        raise QuickBooksProfitAndLossError("QuickBooks P&L response omitted Header")

    if header.get("ReportName") != "ProfitAndLoss":
        raise QuickBooksProfitAndLossError("QuickBooks returned the wrong report type")

    if header.get("ReportBasis") != "Cash":
        raise QuickBooksProfitAndLossError("QuickBooks P&L is not cash basis")

    start_date = _date_value(
        header.get("StartPeriod"),
        field="StartPeriod",
    )
    end_date = _date_value(
        header.get("EndPeriod"),
        field="EndPeriod",
    )

    if start_date != expected_start_date or end_date != expected_end_date:
        raise QuickBooksProfitAndLossError(
            "QuickBooks P&L period does not match the requested period"
        )

    currency = header.get("Currency")

    if not isinstance(currency, str) or len(currency) != 3:
        raise QuickBooksProfitAndLossError("QuickBooks P&L omitted a valid currency")

    account_by_id = {account.id: account for account in qbo_accounts}
    account_amounts: dict[str, Decimal] = {}
    section_totals: dict[str, Decimal] = {}

    rows = payload.get("Rows")

    if not isinstance(rows, dict):
        raise QuickBooksProfitAndLossError("QuickBooks P&L response omitted Rows")

    raw_rows = rows.get("Row", [])

    if not isinstance(raw_rows, list):
        raise QuickBooksProfitAndLossError("QuickBooks P&L returned invalid rows")

    for row in raw_rows:
        _walk_row(
            row=row,
            account_by_id=account_by_id,
            account_amounts=account_amounts,
            section_totals=section_totals,
        )

    required_groups = {
        "Income",
        "COGS",
        "GrossProfit",
        "Expenses",
        "NetIncome",
    }
    missing_groups = required_groups - set(section_totals)

    if missing_groups:
        raise QuickBooksProfitAndLossError(
            "QuickBooks P&L omitted required totals: " + ", ".join(sorted(missing_groups))
        )

    unnumbered_accounts = sorted(
        {
            account_by_id[account_id].name
            for account_id in account_amounts
            if account_by_id[account_id].account_number is None
        }
    )

    if unnumbered_accounts:
        raise QuickBooksProfitAndLossError(
            "Nonzero QuickBooks P&L accounts without "
            "account numbers: " + ", ".join(unnumbered_accounts[:10])
        )

    account_lines = tuple(
        QuickBooksProfitAndLossAccountLine(
            account_number=number,
            account_name=account_by_id[account_id].name,
            amount=amount,
        )
        for account_id, amount in sorted(
            account_amounts.items(),
            key=lambda item: (
                account_by_id[item[0]].account_number or "",
                item[0],
            ),
        )
        for number in (account_by_id[account_id].account_number,)
        if number is not None
    )

    return QuickBooksProfitAndLossStatement(
        company_name=expected_company_name,
        start_date=start_date,
        end_date=end_date,
        currency=currency.upper(),
        accounts=account_lines,
        total_revenue=section_totals["Income"],
        total_cost_of_goods_sold=section_totals["COGS"],
        gross_profit=section_totals["GrossProfit"],
        total_operating_expenses=section_totals["Expenses"],
        net_profit=section_totals["NetIncome"],
    )


def _walk_row(
    *,
    row: object,
    account_by_id: dict[str, QuickBooksApiAccount],
    account_amounts: dict[str, Decimal],
    section_totals: dict[str, Decimal],
) -> None:
    """Recursively process QBO report sections and data rows."""

    if not isinstance(row, dict):
        raise QuickBooksProfitAndLossError("QuickBooks P&L contains an invalid row")

    row_type = row.get("type")

    if row_type == "Data":
        col_data = row.get("ColData")

        if not isinstance(col_data, list):
            raise QuickBooksProfitAndLossError("QuickBooks P&L data row omitted ColData")

        account_id = _account_id(col_data)
        amount = _last_amount(col_data)

        if amount == ZERO:
            return

        if account_id is None:
            raise QuickBooksProfitAndLossError(
                "A nonzero QuickBooks P&L row omitted its account identifier"
            )

        if account_id not in account_by_id:
            raise QuickBooksProfitAndLossError("QuickBooks P&L references an unknown account")

        if account_id in account_amounts:
            raise QuickBooksProfitAndLossError("QuickBooks P&L returned an account more than once")

        account_amounts[account_id] = amount
        return

    group = row.get("group")
    summary = row.get("Summary")

    if isinstance(group, str) and isinstance(
        summary,
        dict,
    ):
        summary_data = summary.get("ColData")

        if isinstance(summary_data, list):
            amount = _last_amount(summary_data)

            if group in section_totals:
                raise QuickBooksProfitAndLossError(
                    f"QuickBooks P&L returned duplicate section total {group}"
                )

            section_totals[group] = amount

    nested_rows = row.get("Rows")

    if nested_rows is None:
        return

    if not isinstance(nested_rows, dict):
        raise QuickBooksProfitAndLossError("QuickBooks P&L contains invalid nested rows")

    children = nested_rows.get("Row", [])

    if not isinstance(children, list):
        raise QuickBooksProfitAndLossError("QuickBooks P&L contains an invalid child list")

    for child in children:
        _walk_row(
            row=child,
            account_by_id=account_by_id,
            account_amounts=account_amounts,
            section_totals=section_totals,
        )


def _account_id(
    col_data: list[object],
) -> str | None:
    """Return the first report entity identifier."""

    for cell in col_data:
        if not isinstance(cell, dict):
            continue

        identifier = cell.get("id")

        if isinstance(identifier, str) and identifier:
            return identifier

    return None


def _last_amount(
    col_data: list[object],
) -> Decimal:
    """Return the final numeric report cell."""

    for cell in reversed(col_data):
        if not isinstance(cell, dict):
            continue

        value = cell.get("value")

        if not isinstance(value, str):
            continue

        normalized = value.strip().replace(",", "")

        if not normalized:
            continue

        try:
            return Decimal(normalized).quantize(Decimal("0.01"))
        except InvalidOperation:
            continue

    return ZERO


def _date_value(
    value: object,
    *,
    field: str,
) -> date:
    """Parse one ISO report date."""

    if not isinstance(value, str):
        raise QuickBooksProfitAndLossError(f"QuickBooks P&L omitted {field}")

    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise QuickBooksProfitAndLossError(f"QuickBooks P&L returned invalid {field}") from exc
