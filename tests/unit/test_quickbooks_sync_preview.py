"""Tests for read-only QBO synchronization accounting totals."""

from datetime import date
from decimal import Decimal
from uuid import UUID

from app.models.quickbooks_sync import (
    QuickBooksJournalEntryPlan,
    QuickBooksJournalLine,
    QuickBooksPostingType,
    QuickBooksSourceReference,
    build_quickbooks_request_id,
)
from app.services.quickbooks.sync_preview import (
    summarize_quickbooks_posting_plans,
)

SOURCE_IDS = tuple(
    UUID(value)
    for value in (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
        "55555555-5555-4555-8555-555555555555",
    )
)


def plan(
    *,
    source_ids: tuple[UUID, ...],
    debit_number: str,
    debit_name: str,
    credit_number: str,
    credit_name: str,
    amount: Decimal,
) -> QuickBooksJournalEntryPlan:
    """Create one balanced posting-plan fixture."""

    return QuickBooksJournalEntryPlan(
        request_id=build_quickbooks_request_id(source_ids),
        sources=tuple(
            QuickBooksSourceReference(
                normalized_transaction_id=source_id,
                classification_version=1,
            )
            for source_id in source_ids
        ),
        transaction_date=date(2026, 4, 1),
        currency="USD",
        private_note="Read-only preview fixture",
        lines=(
            QuickBooksJournalLine(
                account_number=debit_number,
                account_name=debit_name,
                qbo_account_id=f"qbo-{debit_number}",
                posting_type=(QuickBooksPostingType.DEBIT),
                amount=amount,
            ),
            QuickBooksJournalLine(
                account_number=credit_number,
                account_name=credit_name,
                qbo_account_id=f"qbo-{credit_number}",
                posting_type=(QuickBooksPostingType.CREDIT),
                amount=amount,
            ),
        ),
    )


def test_summary_reconstructs_profit_and_loss() -> None:
    """P&L totals follow debit and credit accounting behavior."""

    plans = (
        plan(
            source_ids=(SOURCE_IDS[0],),
            debit_number="1000",
            debit_name="Operating Checking",
            credit_number="4000",
            credit_name="Repair Service Revenue",
            amount=Decimal("300.00"),
        ),
        plan(
            source_ids=(SOURCE_IDS[1],),
            debit_number="5000",
            debit_name="Materials & Supplies",
            credit_number="1000",
            credit_name="Operating Checking",
            amount=Decimal("90.00"),
        ),
        plan(
            source_ids=(SOURCE_IDS[2],),
            debit_number="6000",
            debit_name="Payroll Expense",
            credit_number="1000",
            credit_name="Operating Checking",
            amount=Decimal("40.00"),
        ),
        plan(
            source_ids=(
                SOURCE_IDS[3],
                SOURCE_IDS[4],
            ),
            debit_number="1010",
            debit_name="Tax Reserve",
            credit_number="1000",
            credit_name="Operating Checking",
            amount=Decimal("20.00"),
        ),
    )

    summary = summarize_quickbooks_posting_plans(plans)

    assert summary.total_debits == Decimal("450.00")
    assert summary.total_credits == Decimal("450.00")
    assert summary.revenue == Decimal("300.00")
    assert summary.cost_of_goods_sold == Decimal("90.00")
    assert summary.operating_expenses == Decimal("40.00")
    assert summary.gross_profit == Decimal("210.00")
    assert summary.net_income == Decimal("170.00")


def test_empty_preview_is_balanced_at_zero() -> None:
    """An empty preview produces exact zero totals."""

    summary = summarize_quickbooks_posting_plans(())

    assert summary.total_debits == Decimal("0.00")
    assert summary.total_credits == Decimal("0.00")
    assert summary.revenue == Decimal("0.00")
    assert summary.net_income == Decimal("0.00")
    assert summary.account_totals == ()
