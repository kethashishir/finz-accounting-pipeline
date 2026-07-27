"""Cash-basis Profit and Loss calculation from approved classifications."""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from app.models.accounting import (
    ChartOfAccount,
    ChartOfAccountsConfig,
    QBOAccountType,
)
from app.models.classification import (
    ReviewStatus,
    TransactionType,
)
from app.models.ingestion import (
    RecordStatus,
)
from app.models.profit_and_loss import (
    ZERO,
    ProfitAndLossAccountLine,
    ProfitAndLossReportSet,
    ProfitAndLossSource,
    ProfitAndLossStatement,
    ProfitAndLossTransaction,
)
from app.services.classification.account_mapping import (
    InvalidClassificationAccountMappingError,
    validate_classification_account_target,
)


class ProfitAndLossBuildError(ValueError):
    """Stored accounting evidence cannot safely produce a P&L."""


class ProfitAndLossSourceReader(Protocol):
    """Read approved transaction evidence for one reporting period."""

    async def find_approved_sources(
        self,
        *,
        start_date: date,
        end_date: date,
        currency: str,
    ) -> tuple[ProfitAndLossSource, ...]:
        """Return approved canonical P&L evidence."""


async def generate_profit_and_loss_report_set(
    *,
    source_reader: ProfitAndLossSourceReader,
    start_date: date,
    end_date: date,
    currency: str,
    chart_of_accounts: ChartOfAccountsConfig,
) -> ProfitAndLossReportSet:
    """Load approved evidence and produce reconciled P&L reports."""

    normalized_currency = _validate_report_period(
        start_date=start_date,
        end_date=end_date,
        currency=currency,
    )

    sources = await source_reader.find_approved_sources(
        start_date=start_date,
        end_date=end_date,
        currency=normalized_currency,
    )

    return build_profit_and_loss_report_set(
        sources=sources,
        start_date=start_date,
        end_date=end_date,
        currency=normalized_currency,
        chart_of_accounts=chart_of_accounts,
    )


@dataclass(frozen=True, slots=True)
class _PreparedEntry:
    """Validated P&L transaction paired with its catalog account."""

    account: ChartOfAccount
    transaction: ProfitAndLossTransaction


def build_profit_and_loss_report_set(
    *,
    sources: Iterable[ProfitAndLossSource],
    start_date: date,
    end_date: date,
    currency: str,
    chart_of_accounts: ChartOfAccountsConfig,
) -> ProfitAndLossReportSet:
    """Build complete monthly statements and one consolidated statement."""

    normalized_currency = _validate_report_period(
        start_date=start_date,
        end_date=end_date,
        currency=currency,
    )

    prepared_entries = _prepare_entries(
        sources=sources,
        start_date=start_date,
        end_date=end_date,
        currency=normalized_currency,
        chart_of_accounts=chart_of_accounts,
    )

    monthly_periods = _calendar_months(
        start_date=start_date,
        end_date=end_date,
    )

    monthly_statements = tuple(
        _build_statement(
            entries=tuple(
                entry
                for entry in prepared_entries
                if month_start <= entry.transaction.transaction_date <= month_end
            ),
            company_name=chart_of_accounts.company_name,
            start_date=month_start,
            end_date=month_end,
            currency=normalized_currency,
        )
        for month_start, month_end in monthly_periods
    )

    consolidated = _build_statement(
        entries=prepared_entries,
        company_name=chart_of_accounts.company_name,
        start_date=start_date,
        end_date=end_date,
        currency=normalized_currency,
    )

    return ProfitAndLossReportSet(
        monthly=monthly_statements,
        consolidated=consolidated,
    )


def _validate_report_period(
    *,
    start_date: date,
    end_date: date,
    currency: str,
) -> str:
    """Require a nonempty range made of complete calendar months."""

    if start_date > end_date:
        raise ProfitAndLossBuildError("P&L start date cannot be after its end date")

    if start_date.day != 1:
        raise ProfitAndLossBuildError("P&L start date must be the first day of a month")

    expected_end_day = monthrange(
        end_date.year,
        end_date.month,
    )[1]

    if end_date.day != expected_end_day:
        raise ProfitAndLossBuildError("P&L end date must be the last day of a month")

    normalized_currency = currency.strip().upper()

    if len(normalized_currency) != 3 or not normalized_currency.isalpha():
        raise ProfitAndLossBuildError("P&L currency must be a three-letter code")

    return normalized_currency


def _prepare_entries(
    *,
    sources: Iterable[ProfitAndLossSource],
    start_date: date,
    end_date: date,
    currency: str,
    chart_of_accounts: ChartOfAccountsConfig,
) -> tuple[_PreparedEntry, ...]:
    """Validate, filter, and convert reporting evidence."""

    prepared: list[_PreparedEntry] = []
    seen_transaction_ids = set()

    for source in sources:
        transaction = source.transaction
        classification = source.classification

        if transaction.id in seen_transaction_ids:
            raise ProfitAndLossBuildError("A normalized transaction was supplied more than once")

        seen_transaction_ids.add(transaction.id)

        if classification.normalized_transaction_id != transaction.id:
            raise ProfitAndLossBuildError(
                "Classification does not belong to its normalized transaction"
            )

        if transaction.status is not RecordStatus.VALID or transaction.duplicate_of is not None:
            continue

        if classification.review_status is not ReviewStatus.APPROVED:
            continue

        transaction_type = classification.decision.transaction_type

        if transaction_type not in {
            TransactionType.REVENUE,
            TransactionType.REFUND,
            TransactionType.COST_OF_GOODS_SOLD,
            TransactionType.OPERATING_EXPENSE,
        }:
            continue

        if transaction.transaction_date is None:
            raise ProfitAndLossBuildError("Approved P&L transaction has no transaction date")

        if not (start_date <= transaction.transaction_date <= end_date):
            continue

        if transaction.amount is None:
            raise ProfitAndLossBuildError("Approved P&L transaction has no amount")

        if transaction.currency != currency:
            raise ProfitAndLossBuildError(
                "Approved P&L transaction currency does not match the requested report currency"
            )

        if transaction.bank_account is None:
            raise ProfitAndLossBuildError("Approved P&L transaction has no bank account")

        account_mapping = classification.decision.qbo_account

        try:
            account = chart_of_accounts.require(account_mapping.account_number)
        except (KeyError, ValueError) as exc:
            raise ProfitAndLossBuildError(
                "Approved classification references an unknown or inactive account"
            ) from exc

        if account.name != account_mapping.account_name:
            raise ProfitAndLossBuildError(
                "Approved classification account name does not match the chart of accounts"
            )

        try:
            validate_classification_account_target(
                transaction_type=transaction_type,
                account=account,
                source_bank_account=transaction.bank_account,
                subject="Approved P&L classification",
            )
        except InvalidClassificationAccountMappingError as exc:
            raise ProfitAndLossBuildError(str(exc)) from exc

        description = transaction.description_original or transaction.description_normalized

        if description is None:
            raise ProfitAndLossBuildError("Approved P&L transaction has no description")

        report_amount = _report_amount(
            transaction_type=transaction_type,
            source_amount=transaction.amount,
        )

        prepared.append(
            _PreparedEntry(
                account=account,
                transaction=ProfitAndLossTransaction(
                    normalized_transaction_id=transaction.id,
                    transaction_date=transaction.transaction_date,
                    description=description,
                    bank_account=transaction.bank_account,
                    currency=transaction.currency,
                    source_amount=transaction.amount,
                    report_amount=report_amount,
                    classification_version=classification.version,
                    transaction_type=transaction_type,
                ),
            )
        )

    return tuple(
        sorted(
            prepared,
            key=lambda entry: (
                entry.transaction.transaction_date,
                entry.account.number,
                str(entry.transaction.normalized_transaction_id),
            ),
        )
    )


def _report_amount(
    *,
    transaction_type: TransactionType,
    source_amount: Decimal,
) -> Decimal:
    """Translate bank signs into standard P&L presentation signs."""

    if transaction_type in {
        TransactionType.REVENUE,
        TransactionType.REFUND,
    }:
        return source_amount

    return -source_amount


def _build_statement(
    *,
    entries: tuple[_PreparedEntry, ...],
    company_name: str,
    start_date: date,
    end_date: date,
    currency: str,
) -> ProfitAndLossStatement:
    """Build one internally reconciled P&L statement."""

    grouped: dict[
        str,
        list[ProfitAndLossTransaction],
    ] = defaultdict(list)
    account_by_number: dict[str, ChartOfAccount] = {}

    for entry in entries:
        grouped[entry.account.number].append(entry.transaction)
        account_by_number[entry.account.number] = entry.account

    account_lines = tuple(
        ProfitAndLossAccountLine(
            account_number=account_number,
            account_name=account_by_number[account_number].name,
            qbo_account_type=account_by_number[account_number].qbo_account_type,
            total=sum(
                (transaction.report_amount for transaction in grouped[account_number]),
                ZERO,
            ),
            transactions=tuple(
                sorted(
                    grouped[account_number],
                    key=lambda transaction: (
                        transaction.transaction_date,
                        str(transaction.normalized_transaction_id),
                    ),
                )
            ),
        )
        for account_number in sorted(grouped)
    )

    revenue_accounts = tuple(
        line for line in account_lines if line.qbo_account_type is QBOAccountType.INCOME
    )
    cost_of_goods_sold_accounts = tuple(
        line for line in account_lines if line.qbo_account_type is QBOAccountType.COST_OF_GOODS_SOLD
    )
    operating_expense_accounts = tuple(
        line for line in account_lines if line.qbo_account_type is QBOAccountType.EXPENSES
    )

    total_revenue = sum(
        (line.total for line in revenue_accounts),
        ZERO,
    )
    total_cost_of_goods_sold = sum(
        (line.total for line in cost_of_goods_sold_accounts),
        ZERO,
    )
    total_operating_expenses = sum(
        (line.total for line in operating_expense_accounts),
        ZERO,
    )
    gross_profit = total_revenue - total_cost_of_goods_sold
    net_profit = gross_profit - total_operating_expenses

    return ProfitAndLossStatement(
        company_name=company_name,
        start_date=start_date,
        end_date=end_date,
        currency=currency,
        revenue_accounts=revenue_accounts,
        cost_of_goods_sold_accounts=(cost_of_goods_sold_accounts),
        operating_expense_accounts=(operating_expense_accounts),
        total_revenue=total_revenue,
        total_cost_of_goods_sold=(total_cost_of_goods_sold),
        gross_profit=gross_profit,
        total_operating_expenses=(total_operating_expenses),
        net_profit=net_profit,
        transaction_count=len(entries),
    )


def _calendar_months(
    *,
    start_date: date,
    end_date: date,
) -> tuple[tuple[date, date], ...]:
    """Return each complete calendar month in the report range."""

    periods: list[tuple[date, date]] = []
    current = start_date

    while current <= end_date:
        current_end = date(
            current.year,
            current.month,
            monthrange(
                current.year,
                current.month,
            )[1],
        )
        periods.append((current, current_end))

        if current.month == 12:
            current = date(
                current.year + 1,
                1,
                1,
            )
        else:
            current = date(
                current.year,
                current.month + 1,
                1,
            )

    return tuple(periods)
