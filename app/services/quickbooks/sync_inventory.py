"""Build a complete, read-only QuickBooks synchronization inventory."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from app.models.classification import (
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
)
from app.services.quickbooks.api_client import (
    QuickBooksApiAccount,
)
from app.services.quickbooks.posting_plans import (
    QuickBooksPostingPlanError,
    build_single_transaction_posting_plan,
    build_transfer_posting_plan,
)


@dataclass(frozen=True, slots=True)
class QuickBooksSyncPlanIssue:
    """One visible reason a canonical transaction is blocked."""

    normalized_transaction_id: UUID
    source_transaction_id: str | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class QuickBooksSyncInventory:
    """Complete planning result before any QuickBooks write."""

    total_transactions: int
    canonical_transactions: int
    duplicate_transactions: int
    invalid_transactions: int
    classifications: int
    single_plans: int
    transfer_plans: int
    syncable_transactions: int
    plans: tuple[QuickBooksJournalEntryPlan, ...]
    issues: tuple[QuickBooksSyncPlanIssue, ...]

    @property
    def plan_count(self) -> int:
        """Return the number of QuickBooks writes planned."""

        return len(self.plans)

    @property
    def blocked_transactions(self) -> int:
        """Return canonical transactions without a safe plan."""

        return len(self.issues)


TransferKey = tuple[
    date,
    Decimal,
    str,
    str,
    str,
]


def build_quickbooks_sync_inventory(
    *,
    transactions: tuple[NormalizedTransaction, ...],
    classifications: tuple[TransactionClassification, ...],
    qbo_accounts: tuple[QuickBooksApiAccount, ...],
) -> QuickBooksSyncInventory:
    """Build all safe single and paired-transfer posting plans."""

    classification_by_transaction: dict[
        UUID,
        TransactionClassification,
    ] = {}

    for classification in classifications:
        transaction_id = classification.normalized_transaction_id

        if transaction_id in classification_by_transaction:
            raise ValueError(
                f"Multiple current classifications exist for transaction {transaction_id}"
            )

        classification_by_transaction[transaction_id] = classification

    canonical = tuple(
        transaction
        for transaction in transactions
        if (transaction.status is RecordStatus.VALID and transaction.duplicate_of is None)
    )
    duplicate_count = sum(
        transaction.status is RecordStatus.DUPLICATE for transaction in transactions
    )
    invalid_count = sum(transaction.status is RecordStatus.INVALID for transaction in transactions)

    plans: list[QuickBooksJournalEntryPlan] = []
    issues: dict[UUID, QuickBooksSyncPlanIssue] = {}
    transfer_groups: dict[
        TransferKey,
        list[
            tuple[
                NormalizedTransaction,
                TransactionClassification,
            ]
        ],
    ] = defaultdict(list)
    single_plan_count = 0
    transfer_plan_count = 0

    for transaction in canonical:
        classification = classification_by_transaction.get(transaction.id)

        if classification is None:
            _add_issue(
                issues,
                transaction=transaction,
                code="missing_classification",
                message=("The canonical transaction does not have a current classification."),
            )
            continue

        if classification.decision.transaction_type is TransactionType.TRANSFER:
            try:
                key = _transfer_key(
                    transaction=transaction,
                    classification=classification,
                    qbo_accounts=qbo_accounts,
                )
            except QuickBooksPostingPlanError as exc:
                _add_issue(
                    issues,
                    transaction=transaction,
                    code="invalid_transfer_reference",
                    message=str(exc),
                )
                continue

            transfer_groups[key].append(
                (
                    transaction,
                    classification,
                )
            )
            continue

        try:
            plan = build_single_transaction_posting_plan(
                transaction=transaction,
                classification=classification,
                qbo_accounts=qbo_accounts,
            )
        except QuickBooksPostingPlanError as exc:
            _add_issue(
                issues,
                transaction=transaction,
                code="posting_plan_blocked",
                message=str(exc),
            )
            continue

        plans.append(plan)
        single_plan_count += 1

    for group in transfer_groups.values():
        ordered_group = sorted(
            group,
            key=lambda item: str(item[0].id),
        )

        if len(ordered_group) != 2 or {item[0].direction for item in ordered_group} != {
            TransactionDirection.INFLOW,
            TransactionDirection.OUTFLOW,
        }:
            for transaction, _ in ordered_group:
                _add_issue(
                    issues,
                    transaction=transaction,
                    code="ambiguous_transfer_pair",
                    message=("The transfer did not resolve to exactly one inflow and one outflow."),
                )
            continue

        first_transaction, first_classification = ordered_group[0]
        second_transaction, second_classification = ordered_group[1]

        try:
            plan = build_transfer_posting_plan(
                first_transaction=first_transaction,
                first_classification=first_classification,
                second_transaction=second_transaction,
                second_classification=second_classification,
                qbo_accounts=qbo_accounts,
            )
        except QuickBooksPostingPlanError as exc:
            for transaction in (
                first_transaction,
                second_transaction,
            ):
                _add_issue(
                    issues,
                    transaction=transaction,
                    code="transfer_plan_blocked",
                    message=str(exc),
                )
            continue

        plans.append(plan)
        transfer_plan_count += 1

    plans.sort(
        key=lambda plan: (
            plan.transaction_date,
            plan.request_id,
        )
    )
    ordered_issues = tuple(
        sorted(
            issues.values(),
            key=lambda issue: (
                issue.source_transaction_id is None,
                issue.source_transaction_id or "",
                str(issue.normalized_transaction_id),
            ),
        )
    )

    covered_transaction_ids = {
        source.normalized_transaction_id for plan in plans for source in plan.sources
    }
    blocked_transaction_ids = set(issues)
    canonical_transaction_ids = {transaction.id for transaction in canonical}

    if covered_transaction_ids & blocked_transaction_ids:
        raise RuntimeError("A canonical transaction is both planned and blocked")

    if covered_transaction_ids | blocked_transaction_ids != canonical_transaction_ids:
        raise RuntimeError(
            "Synchronization inventory does not account for every canonical transaction"
        )

    return QuickBooksSyncInventory(
        total_transactions=len(transactions),
        canonical_transactions=len(canonical),
        duplicate_transactions=duplicate_count,
        invalid_transactions=invalid_count,
        classifications=len(classifications),
        single_plans=single_plan_count,
        transfer_plans=transfer_plan_count,
        syncable_transactions=len(covered_transaction_ids),
        plans=tuple(plans),
        issues=ordered_issues,
    )


def _transfer_key(
    *,
    transaction: NormalizedTransaction,
    classification: TransactionClassification,
    qbo_accounts: tuple[QuickBooksApiAccount, ...],
) -> TransferKey:
    """Create one reciprocal bank-transfer grouping key."""

    if (
        transaction.transaction_date is None
        or transaction.amount is None
        or transaction.currency is None
        or transaction.bank_account is None
        or transaction.direction is None
    ):
        raise QuickBooksPostingPlanError("Transfer side lacks complete normalized fields")

    source_bank = _resolve_bank_by_name(
        qbo_accounts,
        transaction.bank_account,
    )
    target_bank = _resolve_bank_by_number(
        qbo_accounts,
        classification.decision.qbo_account.account_number,
    )

    if source_bank.id == target_bank.id:
        raise QuickBooksPostingPlanError(
            "Transfer source and destination cannot be the same bank account"
        )

    if source_bank.account_number is None:
        raise QuickBooksPostingPlanError("Transfer source bank lacks an account number")

    if transaction.direction is TransactionDirection.OUTFLOW:
        source_number = source_bank.account_number
        destination_number = target_bank.account_number
    else:
        source_number = target_bank.account_number
        destination_number = source_bank.account_number

    if destination_number is None:
        raise QuickBooksPostingPlanError("Transfer destination bank lacks an account number")

    return (
        transaction.transaction_date,
        abs(transaction.amount),
        transaction.currency,
        source_number,
        destination_number,
    )


def _resolve_bank_by_name(
    accounts: tuple[QuickBooksApiAccount, ...],
    name: str,
) -> QuickBooksApiAccount:
    """Resolve one active bank account by controlled name."""

    normalized_name = _normalize_text(name)
    matches = [
        account
        for account in accounts
        if (
            account.active
            and account.account_type == "Bank"
            and _normalize_text(account.name) == normalized_name
        )
    ]

    if len(matches) != 1:
        raise QuickBooksPostingPlanError(
            f"Bank account {name!r} does not resolve to exactly one active QBO account"
        )

    return matches[0]


def _resolve_bank_by_number(
    accounts: tuple[QuickBooksApiAccount, ...],
    number: str,
) -> QuickBooksApiAccount:
    """Resolve one active bank account by account number."""

    matches = [
        account
        for account in accounts
        if (account.active and account.account_type == "Bank" and account.account_number == number)
    ]

    if len(matches) != 1:
        raise QuickBooksPostingPlanError(
            f"Transfer account {number} does not resolve to exactly one active QBO bank account"
        )

    return matches[0]


def _add_issue(
    issues: dict[UUID, QuickBooksSyncPlanIssue],
    *,
    transaction: NormalizedTransaction,
    code: str,
    message: str,
) -> None:
    """Add one stable issue for a canonical transaction."""

    if transaction.id in issues:
        raise RuntimeError("A canonical transaction received multiple synchronization issues")

    issues[transaction.id] = QuickBooksSyncPlanIssue(
        normalized_transaction_id=transaction.id,
        source_transaction_id=(transaction.source_transaction_id),
        code=code,
        message=" ".join(message.split())[:500],
    )


def _normalize_text(value: str) -> str:
    """Normalize controlled account names."""

    return " ".join(value.strip().casefold().split())
