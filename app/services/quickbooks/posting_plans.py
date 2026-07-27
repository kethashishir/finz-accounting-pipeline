"""Build accounting-safe QuickBooks posting plans."""

from __future__ import annotations

from decimal import Decimal

from app.models.classification import (
    ClassificationSource,
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
    QuickBooksJournalEntryPlan,
    QuickBooksJournalLine,
    QuickBooksPostingType,
    QuickBooksSourceReference,
    build_quickbooks_request_id,
)
from app.services.quickbooks.api_client import (
    QuickBooksApiAccount,
)

ZERO = Decimal("0.00")

SAFE_PENDING_SOURCES = frozenset(
    {
        ClassificationSource.DETERMINISTIC_RULE,
        ClassificationSource.STORED_CORRECTION,
    }
)

INFLOW_TRANSACTION_TYPES = frozenset(
    {
        TransactionType.REVENUE,
        TransactionType.OWNER_CONTRIBUTION,
    }
)

OUTFLOW_TRANSACTION_TYPES = frozenset(
    {
        TransactionType.COST_OF_GOODS_SOLD,
        TransactionType.OPERATING_EXPENSE,
        TransactionType.REFUND,
        TransactionType.OWNER_DISTRIBUTION,
        TransactionType.FIXED_ASSET_PURCHASE,
    }
)


class QuickBooksPostingPlanError(ValueError):
    """A transaction cannot be converted to a safe QBO post."""


def build_single_transaction_posting_plan(
    *,
    transaction: NormalizedTransaction,
    classification: TransactionClassification,
    qbo_accounts: tuple[QuickBooksApiAccount, ...],
) -> QuickBooksJournalEntryPlan:
    """Build one balanced JournalEntry plan from approved evidence."""

    _validate_source_transaction(transaction)
    _validate_classification_identity(
        transaction=transaction,
        classification=classification,
    )
    _require_sync_eligible(classification)

    if (
        transaction.amount is None
        or transaction.direction is None
        or transaction.currency is None
        or transaction.bank_account is None
        or transaction.transaction_date is None
    ):
        raise QuickBooksPostingPlanError("Transaction lacks complete normalized posting fields")

    amount = abs(transaction.amount)

    if amount == ZERO:
        raise QuickBooksPostingPlanError("A zero-value transaction cannot be posted")

    transaction_type = classification.decision.transaction_type

    if transaction_type is TransactionType.TRANSFER:
        raise QuickBooksPostingPlanError("Transfers require a validated two-source posting plan")

    expected_direction = _expected_direction(transaction_type)

    if transaction.direction is not expected_direction:
        raise QuickBooksPostingPlanError(
            f"Transaction type {transaction_type.value!r} "
            "is incompatible with direction "
            f"{transaction.direction.value!r}"
        )

    bank_account = _require_source_bank_account(
        qbo_accounts,
        bank_account_name=transaction.bank_account,
    )
    target_account = _require_target_account(
        qbo_accounts,
        classification=classification,
    )

    if bank_account.id == target_account.id:
        raise QuickBooksPostingPlanError(
            "The source bank and classified account cannot be the same QuickBooks account"
        )

    description = (
        transaction.description_original
        or transaction.description_normalized
        or transaction.source_transaction_id
        or "Finz bank transaction"
    )

    if transaction.direction is TransactionDirection.INFLOW:
        debit_account = bank_account
        credit_account = target_account
    else:
        debit_account = target_account
        credit_account = bank_account

    lines = (
        _journal_line(
            account=debit_account,
            posting_type=QuickBooksPostingType.DEBIT,
            amount=amount,
            description=description,
        ),
        _journal_line(
            account=credit_account,
            posting_type=QuickBooksPostingType.CREDIT,
            amount=amount,
            description=description,
        ),
    )

    request_id = build_quickbooks_request_id((transaction.id,))
    source_label = transaction.source_transaction_id or str(transaction.id)

    return QuickBooksJournalEntryPlan(
        request_id=request_id,
        sources=(
            QuickBooksSourceReference(
                normalized_transaction_id=transaction.id,
                classification_version=classification.version,
                source_transaction_id=(transaction.source_transaction_id),
            ),
        ),
        transaction_date=transaction.transaction_date,
        currency=transaction.currency,
        private_note=(
            f"Finz {source_label} | "
            f"{transaction_type.value} | "
            f"classification v{classification.version}"
        ),
        lines=lines,
    )


def _validate_source_transaction(
    transaction: NormalizedTransaction,
) -> None:
    """Require one valid canonical normalized transaction."""

    if transaction.status is not RecordStatus.VALID or transaction.duplicate_of is not None:
        raise QuickBooksPostingPlanError("Only valid canonical transactions may be posted")


def _validate_classification_identity(
    *,
    transaction: NormalizedTransaction,
    classification: TransactionClassification,
) -> None:
    """Prevent attaching another transaction's classification."""

    if classification.normalized_transaction_id != transaction.id:
        raise QuickBooksPostingPlanError(
            "Classification does not belong to the normalized transaction"
        )


def _require_sync_eligible(
    classification: TransactionClassification,
) -> None:
    """Require approval or a narrowly defined safe decision."""

    if classification.review_status is ReviewStatus.APPROVED:
        return

    if classification.review_status is ReviewStatus.REJECTED:
        raise QuickBooksPostingPlanError("Rejected classifications cannot be posted")

    decision = classification.decision

    if not decision.review_required and decision.source in SAFE_PENDING_SOURCES:
        return

    raise QuickBooksPostingPlanError(
        "Classification requires explicit approval before QuickBooks synchronization"
    )


def _expected_direction(
    transaction_type: TransactionType,
) -> TransactionDirection:
    """Return the only safe bank direction for the type."""

    if transaction_type in INFLOW_TRANSACTION_TYPES:
        return TransactionDirection.INFLOW

    if transaction_type in OUTFLOW_TRANSACTION_TYPES:
        return TransactionDirection.OUTFLOW

    raise QuickBooksPostingPlanError(
        f"Unsupported single-source transaction type: {transaction_type.value}"
    )


def _require_source_bank_account(
    accounts: tuple[QuickBooksApiAccount, ...],
    *,
    bank_account_name: str,
) -> QuickBooksApiAccount:
    """Resolve the source bank name to one active QBO account."""

    normalized_name = _normalize_text(bank_account_name)
    matches = [
        account
        for account in accounts
        if account.active
        and account.account_type == "Bank"
        and _normalize_text(account.name) == normalized_name
    ]

    if len(matches) != 1:
        raise QuickBooksPostingPlanError(
            "Source bank account must resolve to exactly "
            f"one active QBO bank account: "
            f"{bank_account_name!r}"
        )

    account = matches[0]

    if account.account_number is None:
        raise QuickBooksPostingPlanError(
            f"QBO bank account {account.name!r} does not have an account number"
        )

    return account


def _require_target_account(
    accounts: tuple[QuickBooksApiAccount, ...],
    *,
    classification: TransactionClassification,
) -> QuickBooksApiAccount:
    """Resolve and validate the classified QBO account."""

    mapping = classification.decision.qbo_account
    matches = [account for account in accounts if account.account_number == mapping.account_number]

    if len(matches) != 1:
        raise QuickBooksPostingPlanError(
            f"Classified account must resolve to exactly one QBO account: {mapping.account_number}"
        )

    account = matches[0]

    if not account.active:
        raise QuickBooksPostingPlanError(
            f"Classified QBO account {mapping.account_number} is inactive"
        )

    if _normalize_text(account.name) != _normalize_text(mapping.account_name):
        raise QuickBooksPostingPlanError(
            f"Classification account name does not match QBO account {mapping.account_number}"
        )

    if mapping.qbo_account_id is not None and mapping.qbo_account_id != account.id:
        raise QuickBooksPostingPlanError(
            "Stored QBO account ID does not match the current sandbox account"
        )

    return account


def _journal_line(
    *,
    account: QuickBooksApiAccount,
    posting_type: QuickBooksPostingType,
    amount: Decimal,
    description: str,
) -> QuickBooksJournalLine:
    """Create one validated debit or credit line."""

    if account.account_number is None:
        raise QuickBooksPostingPlanError(
            f"QBO account {account.name!r} does not have an account number"
        )

    return QuickBooksJournalLine(
        account_number=account.account_number,
        account_name=account.name,
        qbo_account_id=account.id,
        posting_type=posting_type,
        amount=amount,
        description=description,
    )


def _normalize_text(value: str) -> str:
    """Normalize controlled names for exact comparison."""

    return " ".join(value.strip().casefold().split())
