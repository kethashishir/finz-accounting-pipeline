"""Tests for complete QuickBooks synchronization inventories."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from app.models.classification import (
    ClassificationDecision,
    ClassificationSource,
    QuickBooksAccountMapping,
    ReviewStatus,
    TransactionClassification,
    TransactionType,
)
from app.models.ingestion import (
    IssueSeverity,
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
    ValidationIssue,
)
from app.services.quickbooks.api_client import (
    QuickBooksApiAccount,
)
from app.services.quickbooks.sync_inventory import (
    build_quickbooks_sync_inventory,
)

REVENUE_ID = UUID("11111111-1111-4111-8111-111111111111")
TRANSFER_OUT_ID = UUID("22222222-2222-4222-8222-222222222222")
TRANSFER_IN_ID = UUID("33333333-3333-4333-8333-333333333333")
MISSING_ID = UUID("44444444-4444-4444-8444-444444444444")
DUPLICATE_ID = UUID("55555555-5555-4555-8555-555555555555")

ACCOUNT_NAMES = {
    "1000": "Operating Checking",
    "1010": "Tax Reserve",
    "4000": "Repair Service Revenue",
}


def account(
    identifier: str,
    number: str,
    name: str,
    account_type: str,
) -> QuickBooksApiAccount:
    """Create one QBO account fixture."""

    return QuickBooksApiAccount(
        id=identifier,
        sync_token="0",
        name=name,
        account_number=number,
        account_type=account_type,
        active=True,
    )


def accounts() -> tuple[QuickBooksApiAccount, ...]:
    """Return the accounts used by the inventory."""

    return (
        account(
            "qbo-bank-1000",
            "1000",
            "Operating Checking",
            "Bank",
        ),
        account(
            "qbo-bank-1010",
            "1010",
            "Tax Reserve",
            "Bank",
        ),
        account(
            "qbo-income-4000",
            "4000",
            "Repair Service Revenue",
            "Income",
        ),
    )


def transaction(
    identifier: UUID,
    *,
    amount: Decimal,
    bank_account: str,
    source_id: str,
    status: RecordStatus = RecordStatus.VALID,
    duplicate_of: UUID | None = None,
) -> NormalizedTransaction:
    """Create one normalized transaction."""

    return NormalizedTransaction(
        id=identifier,
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id=source_id,
        transaction_date=date(2026, 4, 1),
        description_original=source_id,
        description_normalized=source_id.casefold(),
        amount=amount,
        currency="USD",
        bank_account=bank_account,
        direction=(
            TransactionDirection.INFLOW
            if amount > Decimal("0.00")
            else TransactionDirection.OUTFLOW
        ),
        fingerprint=identifier.hex * 2,
        status=status,
        duplicate_of=duplicate_of,
        validation_issues=(
            [
                ValidationIssue(
                    code="test_invalid_record",
                    field="source_transaction_id",
                    message=("Synthetic invalid transaction fixture."),
                    severity=IssueSeverity.ERROR,
                    raw_value=source_id,
                )
            ]
            if status is RecordStatus.INVALID
            else []
        ),
    )


def classification(
    transaction_id: UUID,
    *,
    transaction_type: TransactionType,
    account_number: str,
    review_status: ReviewStatus = ReviewStatus.PENDING,
) -> TransactionClassification:
    """Create one safe deterministic classification."""

    return TransactionClassification(
        normalized_transaction_id=transaction_id,
        decision=ClassificationDecision(
            transaction_type=transaction_type,
            counterparty=None,
            qbo_account=QuickBooksAccountMapping(
                account_number=account_number,
                account_name=ACCOUNT_NAMES[account_number],
            ),
            confidence_score=Decimal("1.000"),
            explanation="Deterministic challenge classification.",
            source=ClassificationSource.DETERMINISTIC_RULE,
            review_required=False,
        ),
        review_status=review_status,
    )


def test_inventory_accounts_for_every_canonical_transaction() -> None:
    """Singles, transfers, duplicates, and blockers are counted."""

    revenue = transaction(
        REVENUE_ID,
        amount=Decimal("1250.00"),
        bank_account="Operating Checking",
        source_id="REVENUE",
    )
    transfer_out = transaction(
        TRANSFER_OUT_ID,
        amount=Decimal("-1000.00"),
        bank_account="Operating Checking",
        source_id="TRANSFER-OUT",
    )
    transfer_in = transaction(
        TRANSFER_IN_ID,
        amount=Decimal("1000.00"),
        bank_account="Tax Reserve",
        source_id="TRANSFER-IN",
    )
    missing = transaction(
        MISSING_ID,
        amount=Decimal("-50.00"),
        bank_account="Operating Checking",
        source_id="MISSING",
    )
    duplicate = transaction(
        DUPLICATE_ID,
        amount=Decimal("1250.00"),
        bank_account="Operating Checking",
        source_id="REVENUE",
        status=RecordStatus.DUPLICATE,
        duplicate_of=REVENUE_ID,
    )

    inventory = build_quickbooks_sync_inventory(
        transactions=(
            duplicate,
            transfer_in,
            missing,
            revenue,
            transfer_out,
        ),
        classifications=(
            classification(
                REVENUE_ID,
                transaction_type=TransactionType.REVENUE,
                account_number="4000",
            ),
            classification(
                TRANSFER_OUT_ID,
                transaction_type=TransactionType.TRANSFER,
                account_number="1010",
            ),
            classification(
                TRANSFER_IN_ID,
                transaction_type=TransactionType.TRANSFER,
                account_number="1000",
            ),
        ),
        qbo_accounts=accounts(),
    )

    assert inventory.total_transactions == 5
    assert inventory.canonical_transactions == 4
    assert inventory.duplicate_transactions == 1
    assert inventory.invalid_transactions == 0
    assert inventory.classifications == 3
    assert inventory.single_plans == 1
    assert inventory.transfer_plans == 1
    assert inventory.plan_count == 2
    assert inventory.syncable_transactions == 3
    assert inventory.blocked_transactions == 1
    assert inventory.issues[0].code == ("missing_classification")


def test_transfer_pair_produces_one_plan_for_two_sources() -> None:
    """The inventory never creates two writes for one transfer."""

    transfer_out = transaction(
        TRANSFER_OUT_ID,
        amount=Decimal("-1000.00"),
        bank_account="Operating Checking",
        source_id="TRANSFER-OUT",
    )
    transfer_in = transaction(
        TRANSFER_IN_ID,
        amount=Decimal("1000.00"),
        bank_account="Tax Reserve",
        source_id="TRANSFER-IN",
    )

    inventory = build_quickbooks_sync_inventory(
        transactions=(
            transfer_in,
            transfer_out,
        ),
        classifications=(
            classification(
                TRANSFER_IN_ID,
                transaction_type=TransactionType.TRANSFER,
                account_number="1000",
            ),
            classification(
                TRANSFER_OUT_ID,
                transaction_type=TransactionType.TRANSFER,
                account_number="1010",
            ),
        ),
        qbo_accounts=accounts(),
    )

    assert inventory.plan_count == 1
    assert inventory.transfer_plans == 1
    assert len(inventory.plans[0].sources) == 2
    assert inventory.issues == ()


def test_unpaired_transfer_is_visible_and_blocked() -> None:
    """A single transfer side cannot silently become a QBO write."""

    transfer_out = transaction(
        TRANSFER_OUT_ID,
        amount=Decimal("-1000.00"),
        bank_account="Operating Checking",
        source_id="TRANSFER-OUT",
    )

    inventory = build_quickbooks_sync_inventory(
        transactions=(transfer_out,),
        classifications=(
            classification(
                TRANSFER_OUT_ID,
                transaction_type=TransactionType.TRANSFER,
                account_number="1010",
            ),
        ),
        qbo_accounts=accounts(),
    )

    assert inventory.plan_count == 0
    assert inventory.syncable_transactions == 0
    assert inventory.blocked_transactions == 1
    assert inventory.issues[0].code == ("ambiguous_transfer_pair")


def test_invalid_and_duplicate_rows_are_not_planned() -> None:
    """Only valid canonical transactions enter planning."""

    canonical = transaction(
        REVENUE_ID,
        amount=Decimal("100.00"),
        bank_account="Operating Checking",
        source_id="CANONICAL",
    )
    duplicate = transaction(
        DUPLICATE_ID,
        amount=Decimal("100.00"),
        bank_account="Operating Checking",
        source_id="CANONICAL",
        status=RecordStatus.DUPLICATE,
        duplicate_of=REVENUE_ID,
    )
    invalid = transaction(
        uuid4(),
        amount=Decimal("-10.00"),
        bank_account="Operating Checking",
        source_id="INVALID",
        status=RecordStatus.INVALID,
    )

    inventory = build_quickbooks_sync_inventory(
        transactions=(
            canonical,
            duplicate,
            invalid,
        ),
        classifications=(
            classification(
                REVENUE_ID,
                transaction_type=TransactionType.REVENUE,
                account_number="4000",
            ),
        ),
        qbo_accounts=accounts(),
    )

    assert inventory.total_transactions == 3
    assert inventory.canonical_transactions == 1
    assert inventory.duplicate_transactions == 1
    assert inventory.invalid_transactions == 1
    assert inventory.syncable_transactions == 1
    assert inventory.plan_count == 1
