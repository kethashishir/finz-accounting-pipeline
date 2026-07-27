"""Tests for accounting-safe QBO posting-plan construction."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

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
from app.models.quickbooks_sync import (
    QuickBooksPostingType,
)
from app.services.quickbooks.api_client import (
    QuickBooksApiAccount,
)
from app.services.quickbooks.posting_plans import (
    QuickBooksPostingPlanError,
    build_single_transaction_posting_plan,
)

REVIEWED_AT = datetime(
    2026,
    7,
    27,
    9,
    0,
    tzinfo=UTC,
)

ACCOUNT_NAMES = {
    "1000": "Operating Checking",
    "1010": "Tax Reserve",
    "1500": "Tools & Equipment",
    "3000": "Owner's Equity",
    "4000": "Repair Service Revenue",
    "4100": "Customer Refunds",
    "5000": "Materials & Supplies",
    "6000": "Payroll Expense",
}


def qbo_account(
    *,
    identifier: str,
    number: str,
    name: str,
    account_type: str,
    active: bool = True,
) -> QuickBooksApiAccount:
    """Create one QBO account fixture."""

    return QuickBooksApiAccount(
        id=identifier,
        sync_token="0",
        name=name,
        account_number=number,
        account_type=account_type,
        account_sub_type=None,
        active=active,
    )


def qbo_accounts() -> tuple[
    QuickBooksApiAccount,
    ...,
]:
    """Return the QBO accounts required by plan tests."""

    return (
        qbo_account(
            identifier="qbo-bank-1000",
            number="1000",
            name="Operating Checking",
            account_type="Bank",
        ),
        qbo_account(
            identifier="qbo-bank-1010",
            number="1010",
            name="Tax Reserve",
            account_type="Bank",
        ),
        qbo_account(
            identifier="qbo-asset-1500",
            number="1500",
            name="Tools & Equipment",
            account_type="Fixed Asset",
        ),
        qbo_account(
            identifier="qbo-equity-3000",
            number="3000",
            name="Owner's Equity",
            account_type="Equity",
        ),
        qbo_account(
            identifier="qbo-income-4000",
            number="4000",
            name="Repair Service Revenue",
            account_type="Income",
        ),
        qbo_account(
            identifier="qbo-income-4100",
            number="4100",
            name="Customer Refunds",
            account_type="Income",
        ),
        qbo_account(
            identifier="qbo-cogs-5000",
            number="5000",
            name="Materials & Supplies",
            account_type="Cost of Goods Sold",
        ),
        qbo_account(
            identifier="qbo-expense-6000",
            number="6000",
            name="Payroll Expense",
            account_type="Expense",
        ),
    )


def transaction(
    *,
    amount: Decimal,
    bank_account: str = "Operating Checking",
    status: RecordStatus = RecordStatus.VALID,
    duplicate_of=None,
) -> NormalizedTransaction:
    """Create one normalized bank transaction."""

    direction = (
        TransactionDirection.INFLOW if amount > Decimal("0.00") else TransactionDirection.OUTFLOW
    )

    return NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-QBO-PLAN-0001",
        transaction_date=date(2026, 4, 1),
        description_original="BRIGHTFIX TEST TRANSACTION",
        description_normalized=("brightfix test transaction"),
        amount=amount,
        currency="USD",
        bank_account=bank_account,
        direction=direction,
        fingerprint="a" * 64,
        status=status,
        duplicate_of=duplicate_of,
    )


def classification(
    transaction: NormalizedTransaction,
    *,
    transaction_type: TransactionType,
    account_number: str,
    source: ClassificationSource = (ClassificationSource.GEMINI),
    review_status: ReviewStatus = (ReviewStatus.APPROVED),
    review_required: bool = True,
    qbo_account_id: str | None = None,
) -> TransactionClassification:
    """Create one classification for plan construction."""

    reviewer = (
        ReviewerMetadata(
            reviewer_id="shishir",
            reviewed_at=REVIEWED_AT,
            notes="Approved for QuickBooks synchronization.",
        )
        if review_status
        in {
            ReviewStatus.APPROVED,
            ReviewStatus.REJECTED,
        }
        else None
    )

    return TransactionClassification(
        normalized_transaction_id=transaction.id,
        decision=ClassificationDecision(
            transaction_type=transaction_type,
            counterparty=None,
            qbo_account=QuickBooksAccountMapping(
                account_number=account_number,
                account_name=ACCOUNT_NAMES[account_number],
                qbo_account_id=qbo_account_id,
            ),
            confidence_score=Decimal("1.000"),
            explanation=("Validated accounting classification."),
            source=source,
            review_required=review_required,
        ),
        review_status=review_status,
        reviewer=reviewer,
    )


def test_approved_revenue_builds_bank_debit_and_income_credit() -> None:
    """A cash receipt debits bank and credits revenue."""

    source = transaction(amount=Decimal("1250.00"))
    posting_plan = build_single_transaction_posting_plan(
        transaction=source,
        classification=classification(
            source,
            transaction_type=TransactionType.REVENUE,
            account_number="4000",
        ),
        qbo_accounts=qbo_accounts(),
    )

    assert posting_plan.transaction_date == date(
        2026,
        4,
        1,
    )
    assert posting_plan.currency == "USD"
    assert len(posting_plan.sources) == 1
    assert posting_plan.sources[0].normalized_transaction_id == source.id
    assert posting_plan.sources[0].classification_version == 1

    debit, credit = posting_plan.lines

    assert debit.posting_type is (QuickBooksPostingType.DEBIT)
    assert debit.account_number == "1000"
    assert debit.qbo_account_id == "qbo-bank-1000"
    assert debit.amount == Decimal("1250.00")

    assert credit.posting_type is (QuickBooksPostingType.CREDIT)
    assert credit.account_number == "4000"
    assert credit.qbo_account_id == ("qbo-income-4000")


def test_safe_pending_deterministic_classification_is_eligible() -> None:
    """A safe deterministic decision need not be manually approved."""

    source = transaction(amount=Decimal("-35.00"))
    posting_plan = build_single_transaction_posting_plan(
        transaction=source,
        classification=classification(
            source,
            transaction_type=(TransactionType.OPERATING_EXPENSE),
            account_number="6000",
            source=(ClassificationSource.DETERMINISTIC_RULE),
            review_status=ReviewStatus.PENDING,
            review_required=False,
        ),
        qbo_accounts=qbo_accounts(),
    )

    assert posting_plan.lines[0].account_number == "6000"
    assert posting_plan.lines[1].account_number == "1000"


def test_unapproved_gemini_classification_is_rejected() -> None:
    """Gemini output cannot bypass explicit human approval."""

    source = transaction(amount=Decimal("-35.00"))

    with pytest.raises(
        QuickBooksPostingPlanError,
        match="requires explicit approval",
    ):
        build_single_transaction_posting_plan(
            transaction=source,
            classification=classification(
                source,
                transaction_type=(TransactionType.OPERATING_EXPENSE),
                account_number="6000",
                source=ClassificationSource.GEMINI,
                review_status=ReviewStatus.PENDING,
                review_required=True,
            ),
            qbo_accounts=qbo_accounts(),
        )


def test_rejected_classification_is_rejected() -> None:
    """A rejected decision can never enter a QBO plan."""

    source = transaction(amount=Decimal("-35.00"))

    with pytest.raises(
        QuickBooksPostingPlanError,
        match="Rejected classifications",
    ):
        build_single_transaction_posting_plan(
            transaction=source,
            classification=classification(
                source,
                transaction_type=(TransactionType.OPERATING_EXPENSE),
                account_number="6000",
                review_status=ReviewStatus.REJECTED,
            ),
            qbo_accounts=qbo_accounts(),
        )


def test_duplicate_transaction_is_rejected() -> None:
    """Duplicate physical rows never create another QBO posting."""

    canonical_id = uuid4()
    duplicate = transaction(
        amount=Decimal("-35.00"),
        status=RecordStatus.DUPLICATE,
        duplicate_of=canonical_id,
    )

    with pytest.raises(
        QuickBooksPostingPlanError,
        match="valid canonical",
    ):
        build_single_transaction_posting_plan(
            transaction=duplicate,
            classification=classification(
                duplicate,
                transaction_type=(TransactionType.OPERATING_EXPENSE),
                account_number="6000",
            ),
            qbo_accounts=qbo_accounts(),
        )


@pytest.mark.parametrize(
    ("transaction_type", "account_number"),
    [
        (
            TransactionType.COST_OF_GOODS_SOLD,
            "5000",
        ),
        (
            TransactionType.OPERATING_EXPENSE,
            "6000",
        ),
        (
            TransactionType.REFUND,
            "4100",
        ),
        (
            TransactionType.OWNER_DISTRIBUTION,
            "3000",
        ),
        (
            TransactionType.FIXED_ASSET_PURCHASE,
            "1500",
        ),
    ],
)
def test_outflow_debits_target_and_credits_bank(
    transaction_type: TransactionType,
    account_number: str,
) -> None:
    """Payments debit their classified account and credit cash."""

    source = transaction(amount=Decimal("-225.50"))
    posting_plan = build_single_transaction_posting_plan(
        transaction=source,
        classification=classification(
            source,
            transaction_type=transaction_type,
            account_number=account_number,
        ),
        qbo_accounts=qbo_accounts(),
    )

    debit, credit = posting_plan.lines

    assert debit.posting_type is (QuickBooksPostingType.DEBIT)
    assert debit.account_number == account_number
    assert credit.posting_type is (QuickBooksPostingType.CREDIT)
    assert credit.account_number == "1000"
    assert debit.amount == Decimal("225.50")
    assert credit.amount == Decimal("225.50")


def test_owner_contribution_debits_bank_and_credits_equity() -> None:
    """Owner cash invested increases both bank and equity."""

    source = transaction(amount=Decimal("5000.00"))
    posting_plan = build_single_transaction_posting_plan(
        transaction=source,
        classification=classification(
            source,
            transaction_type=(TransactionType.OWNER_CONTRIBUTION),
            account_number="3000",
        ),
        qbo_accounts=qbo_accounts(),
    )

    assert posting_plan.lines[0].account_number == "1000"
    assert posting_plan.lines[0].posting_type is QuickBooksPostingType.DEBIT
    assert posting_plan.lines[1].account_number == "3000"
    assert posting_plan.lines[1].posting_type is QuickBooksPostingType.CREDIT


def test_incompatible_direction_is_rejected() -> None:
    """Revenue cannot be posted from a bank outflow."""

    source = transaction(amount=Decimal("-100.00"))

    with pytest.raises(
        QuickBooksPostingPlanError,
        match="incompatible with direction",
    ):
        build_single_transaction_posting_plan(
            transaction=source,
            classification=classification(
                source,
                transaction_type=TransactionType.REVENUE,
                account_number="4000",
            ),
            qbo_accounts=qbo_accounts(),
        )


def test_transfer_requires_two_source_plan() -> None:
    """One transfer side cannot independently create a JournalEntry."""

    source = transaction(amount=Decimal("-1000.00"))

    with pytest.raises(
        QuickBooksPostingPlanError,
        match="two-source",
    ):
        build_single_transaction_posting_plan(
            transaction=source,
            classification=classification(
                source,
                transaction_type=TransactionType.TRANSFER,
                account_number="1010",
            ),
            qbo_accounts=qbo_accounts(),
        )


def test_missing_qbo_target_account_is_rejected() -> None:
    """A classification cannot reference an absent sandbox account."""

    source = transaction(amount=Decimal("1250.00"))
    accounts = tuple(account for account in qbo_accounts() if account.account_number != "4000")

    with pytest.raises(
        QuickBooksPostingPlanError,
        match="exactly one QBO account",
    ):
        build_single_transaction_posting_plan(
            transaction=source,
            classification=classification(
                source,
                transaction_type=TransactionType.REVENUE,
                account_number="4000",
            ),
            qbo_accounts=accounts,
        )


def test_stale_stored_qbo_account_id_is_rejected() -> None:
    """A stale persisted QBO ID cannot redirect the posting."""

    source = transaction(amount=Decimal("1250.00"))

    with pytest.raises(
        QuickBooksPostingPlanError,
        match="does not match",
    ):
        build_single_transaction_posting_plan(
            transaction=source,
            classification=classification(
                source,
                transaction_type=TransactionType.REVENUE,
                account_number="4000",
                qbo_account_id="stale-qbo-id",
            ),
            qbo_accounts=qbo_accounts(),
        )
