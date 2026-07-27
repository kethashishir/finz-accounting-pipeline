"""Tests for cash-basis Profit and Loss calculation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.models.classification import (
    ClassificationDecision,
    ClassificationSource,
    QuickBooksAccountMapping,
    ReviewerMetadata,
    ReviewStatus,
    TransactionClassification,
    TransactionType,
)
from app.models.ingestion import (
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
)
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)
from app.services.reporting.profit_and_loss import (
    ProfitAndLossBuildError,
    ProfitAndLossSource,
    build_profit_and_loss_report_set,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
REVIEWED_AT = datetime(
    2026,
    7,
    27,
    1,
    0,
    tzinfo=UTC,
)


def account_for_type(
    transaction_type: TransactionType,
) -> tuple[str, str]:
    """Return the default valid account for a transaction type."""

    return {
        TransactionType.REVENUE: (
            "4000",
            "Repair Service Revenue",
        ),
        TransactionType.REFUND: (
            "4100",
            "Customer Refunds",
        ),
        TransactionType.COST_OF_GOODS_SOLD: (
            "5000",
            "Materials & Supplies",
        ),
        TransactionType.OPERATING_EXPENSE: (
            "6000",
            "Payroll Expense",
        ),
        TransactionType.TRANSFER: (
            "1010",
            "Tax Reserve",
        ),
    }[transaction_type]


def create_transaction(
    *,
    transaction_date: date,
    amount: str,
    transaction_id: UUID | None = None,
    currency: str = "USD",
    status: RecordStatus = RecordStatus.VALID,
    duplicate_of: UUID | None = None,
) -> NormalizedTransaction:
    """Create one normalized bank transaction."""

    decimal_amount = Decimal(amount)
    direction = (
        TransactionDirection.INFLOW
        if decimal_amount > Decimal("0.00")
        else TransactionDirection.OUTFLOW
    )

    return NormalizedTransaction(
        id=transaction_id or uuid4(),
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-PNL-TEST",
        transaction_date=transaction_date,
        description_original="P&L test transaction",
        description_normalized="p&l test transaction",
        amount=decimal_amount,
        currency=currency,
        bank_account="Operating Checking",
        direction=direction,
        fingerprint=uuid4().hex * 2,
        status=status,
        duplicate_of=duplicate_of,
    )


def create_classification(
    transaction: NormalizedTransaction,
    *,
    transaction_type: TransactionType,
    review_status: ReviewStatus = ReviewStatus.APPROVED,
    account_number: str | None = None,
    account_name: str | None = None,
    normalized_transaction_id: UUID | None = None,
) -> TransactionClassification:
    """Create one current classification."""

    default_number, default_name = account_for_type(transaction_type)

    reviewer = (
        ReviewerMetadata(
            reviewer_id="shishir",
            reviewed_at=REVIEWED_AT,
            notes="Approved for P&L testing.",
        )
        if review_status is not ReviewStatus.PENDING
        else None
    )

    return TransactionClassification(
        normalized_transaction_id=(normalized_transaction_id or transaction.id),
        decision=ClassificationDecision(
            transaction_type=transaction_type,
            qbo_account=QuickBooksAccountMapping(
                account_number=(account_number or default_number),
                account_name=account_name or default_name,
            ),
            confidence_score=Decimal("1.000"),
            explanation="Validated test classification.",
            source=ClassificationSource.DETERMINISTIC_RULE,
            review_required=False,
        ),
        review_status=review_status,
        reviewer=reviewer,
    )


def source(
    *,
    transaction_date: date,
    amount: str,
    transaction_type: TransactionType,
    review_status: ReviewStatus = ReviewStatus.APPROVED,
) -> ProfitAndLossSource:
    """Create one paired reporting source."""

    transaction = create_transaction(
        transaction_date=transaction_date,
        amount=amount,
    )
    classification = create_classification(
        transaction,
        transaction_type=transaction_type,
        review_status=review_status,
    )

    return ProfitAndLossSource(
        transaction=transaction,
        classification=classification,
    )


def build(
    sources: tuple[ProfitAndLossSource, ...],
):
    """Build the standard April through June test report."""

    return build_profit_and_loss_report_set(
        sources=sources,
        start_date=date(2026, 4, 1),
        end_date=date(2026, 6, 30),
        currency="USD",
        chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
    )


def test_builds_monthly_and_consolidated_cash_basis_pnl() -> None:
    """Revenue, refunds, COGS, and expenses use correct signs."""

    report = build(
        (
            source(
                transaction_date=date(2026, 4, 5),
                amount="1000.00",
                transaction_type=TransactionType.REVENUE,
            ),
            source(
                transaction_date=date(2026, 4, 8),
                amount="-100.00",
                transaction_type=TransactionType.REFUND,
            ),
            source(
                transaction_date=date(2026, 5, 10),
                amount="-300.00",
                transaction_type=(TransactionType.COST_OF_GOODS_SOLD),
            ),
            source(
                transaction_date=date(2026, 6, 12),
                amount="-200.00",
                transaction_type=(TransactionType.OPERATING_EXPENSE),
            ),
        )
    )

    assert len(report.monthly) == 3

    april, may, june = report.monthly

    assert april.total_revenue == Decimal("900.00")
    assert april.net_profit == Decimal("900.00")
    assert april.transaction_count == 2

    assert may.total_cost_of_goods_sold == (Decimal("300.00"))
    assert may.net_profit == Decimal("-300.00")
    assert may.transaction_count == 1

    assert june.total_operating_expenses == (Decimal("200.00"))
    assert june.net_profit == Decimal("-200.00")
    assert june.transaction_count == 1

    assert report.consolidated.total_revenue == (Decimal("900.00"))
    assert report.consolidated.total_cost_of_goods_sold == Decimal("300.00")
    assert report.consolidated.gross_profit == (Decimal("600.00"))
    assert report.consolidated.total_operating_expenses == Decimal("200.00")
    assert report.consolidated.net_profit == (Decimal("400.00"))
    assert report.consolidated.transaction_count == 4


def test_zero_activity_month_is_still_reported() -> None:
    """A complete report includes empty calendar months."""

    report = build(
        (
            source(
                transaction_date=date(2026, 4, 5),
                amount="1000.00",
                transaction_type=TransactionType.REVENUE,
            ),
            source(
                transaction_date=date(2026, 6, 5),
                amount="-100.00",
                transaction_type=(TransactionType.OPERATING_EXPENSE),
            ),
        )
    )

    may = report.monthly[1]

    assert may.start_date == date(2026, 5, 1)
    assert may.end_date == date(2026, 5, 31)
    assert may.transaction_count == 0
    assert may.account_lines == ()
    assert may.net_profit == Decimal("0.00")


def test_pending_classification_is_excluded() -> None:
    """Unapproved classifications cannot affect the P&L."""

    report = build(
        (
            source(
                transaction_date=date(2026, 4, 5),
                amount="1000.00",
                transaction_type=TransactionType.REVENUE,
                review_status=ReviewStatus.PENDING,
            ),
        )
    )

    assert report.consolidated.transaction_count == 0
    assert report.consolidated.net_profit == Decimal("0.00")


def test_balance_sheet_activity_is_excluded() -> None:
    """Transfers and other balance-sheet activity stay out of P&L."""

    report = build(
        (
            source(
                transaction_date=date(2026, 4, 5),
                amount="-500.00",
                transaction_type=TransactionType.TRANSFER,
            ),
        )
    )

    assert report.consolidated.transaction_count == 0
    assert report.consolidated.account_lines == ()


def test_duplicate_transaction_is_excluded() -> None:
    """A duplicate source row cannot be independently reported."""

    canonical_id = uuid4()
    transaction = create_transaction(
        transaction_date=date(2026, 4, 5),
        amount="-100.00",
        status=RecordStatus.DUPLICATE,
        duplicate_of=canonical_id,
    )
    classification = create_classification(
        transaction,
        transaction_type=(TransactionType.OPERATING_EXPENSE),
    )

    report = build(
        (
            ProfitAndLossSource(
                transaction=transaction,
                classification=classification,
            ),
        )
    )

    assert report.consolidated.transaction_count == 0


def test_unknown_account_is_rejected() -> None:
    """An approved decision cannot bypass the account catalog."""

    transaction = create_transaction(
        transaction_date=date(2026, 4, 5),
        amount="-100.00",
    )
    classification = create_classification(
        transaction,
        transaction_type=(TransactionType.OPERATING_EXPENSE),
        account_number="9999",
        account_name="Unknown Expense",
    )

    with pytest.raises(
        ProfitAndLossBuildError,
        match="unknown or inactive",
    ):
        build(
            (
                ProfitAndLossSource(
                    transaction=transaction,
                    classification=classification,
                ),
            )
        )


def test_account_name_mismatch_is_rejected() -> None:
    """Stored mapping names must match the approved catalog."""

    transaction = create_transaction(
        transaction_date=date(2026, 4, 5),
        amount="-100.00",
    )
    classification = create_classification(
        transaction,
        transaction_type=(TransactionType.OPERATING_EXPENSE),
        account_number="6000",
        account_name="Incorrect Expense Name",
    )

    with pytest.raises(
        ProfitAndLossBuildError,
        match="account name does not match",
    ):
        build(
            (
                ProfitAndLossSource(
                    transaction=transaction,
                    classification=classification,
                ),
            )
        )


def test_mixed_currency_is_rejected() -> None:
    """A report cannot silently combine different currencies."""

    transaction = create_transaction(
        transaction_date=date(2026, 4, 5),
        amount="100.00",
        currency="CAD",
    )
    classification = create_classification(
        transaction,
        transaction_type=TransactionType.REVENUE,
    )

    with pytest.raises(
        ProfitAndLossBuildError,
        match="currency does not match",
    ):
        build(
            (
                ProfitAndLossSource(
                    transaction=transaction,
                    classification=classification,
                ),
            )
        )


def test_classification_transaction_mismatch_is_rejected() -> None:
    """A classification cannot be joined to the wrong transaction."""

    transaction = create_transaction(
        transaction_date=date(2026, 4, 5),
        amount="100.00",
    )
    classification = create_classification(
        transaction,
        transaction_type=TransactionType.REVENUE,
        normalized_transaction_id=uuid4(),
    )

    with pytest.raises(
        ProfitAndLossBuildError,
        match="does not belong",
    ):
        build(
            (
                ProfitAndLossSource(
                    transaction=transaction,
                    classification=classification,
                ),
            )
        )


def test_partial_month_period_is_rejected() -> None:
    """Monthly report sets must use complete calendar months."""

    with pytest.raises(
        ProfitAndLossBuildError,
        match="first day",
    ):
        build_profit_and_loss_report_set(
            sources=(),
            start_date=date(2026, 4, 2),
            end_date=date(2026, 6, 30),
            currency="USD",
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )

    with pytest.raises(
        ProfitAndLossBuildError,
        match="last day",
    ):
        build_profit_and_loss_report_set(
            sources=(),
            start_date=date(2026, 4, 1),
            end_date=date(2026, 6, 29),
            currency="USD",
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )


def test_repeated_source_transaction_is_rejected() -> None:
    """Repeated input evidence cannot double-count one transaction."""

    reporting_source = source(
        transaction_date=date(2026, 4, 5),
        amount="100.00",
        transaction_type=TransactionType.REVENUE,
    )

    with pytest.raises(
        ProfitAndLossBuildError,
        match="supplied more than once",
    ):
        build(
            (
                reporting_source,
                reporting_source,
            )
        )
