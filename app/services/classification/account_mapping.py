"""Shared accounting validation for classification account targets."""

from __future__ import annotations

from app.models.accounting import ChartOfAccount, QBOAccountType
from app.models.classification import TransactionType

EXPECTED_QBO_ACCOUNT_TYPES = {
    TransactionType.REVENUE: frozenset({QBOAccountType.INCOME}),
    TransactionType.COST_OF_GOODS_SOLD: frozenset({QBOAccountType.COST_OF_GOODS_SOLD}),
    TransactionType.OPERATING_EXPENSE: frozenset({QBOAccountType.EXPENSES}),
    TransactionType.REFUND: frozenset({QBOAccountType.INCOME}),
    TransactionType.TRANSFER: frozenset({QBOAccountType.BANK}),
    TransactionType.OWNER_CONTRIBUTION: frozenset({QBOAccountType.EQUITY}),
    TransactionType.OWNER_DISTRIBUTION: frozenset({QBOAccountType.EQUITY}),
    TransactionType.FIXED_ASSET_PURCHASE: frozenset({QBOAccountType.FIXED_ASSETS}),
}


class InvalidClassificationAccountMappingError(ValueError):
    """A classification type cannot safely use the selected account."""


def validate_classification_account_target(
    *,
    transaction_type: TransactionType,
    account: ChartOfAccount,
    source_bank_account: str | None = None,
    subject: str = "Classification",
) -> None:
    """Require a transaction type and chart-of-accounts target to agree."""

    expected_types = EXPECTED_QBO_ACCOUNT_TYPES[transaction_type]

    if account.qbo_account_type not in expected_types:
        raise InvalidClassificationAccountMappingError(
            f"{subject} transaction type "
            f"{transaction_type.value!r} cannot use QuickBooks "
            f"account type {account.qbo_account_type.value!r}"
        )

    if transaction_type is TransactionType.REFUND and account.number != "4100":
        raise InvalidClassificationAccountMappingError(f"{subject} refunds must use account 4100")

    if transaction_type is TransactionType.REVENUE and account.number == "4100":
        raise InvalidClassificationAccountMappingError(
            f"{subject} ordinary revenue cannot use refund account 4100"
        )

    if (
        transaction_type is TransactionType.TRANSFER
        and source_bank_account is not None
        and _normalize_text(source_bank_account) == _normalize_text(account.name)
    ):
        raise InvalidClassificationAccountMappingError(
            f"{subject} transfer counterpart cannot be the same as the source bank account"
        )


def _normalize_text(value: str) -> str:
    """Normalize controlled text for case-insensitive comparison."""

    return " ".join(value.strip().casefold().split())
