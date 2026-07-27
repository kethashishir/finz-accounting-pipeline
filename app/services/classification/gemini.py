"""Validated boundary for optional Gemini transaction classification."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from pydantic import Field

from app.models.accounting import (
    ChartOfAccountsConfig,
    FinancialStatement,
    QBOAccountType,
)
from app.models.classification import (
    AccountNumber,
    ClassificationDecision,
    ClassificationSource,
    ConfidenceScore,
    Counterparty,
    ImmutableAccountingModel,
    NonEmptyString,
    QuickBooksAccountMapping,
    TransactionType,
)
from app.models.ingestion import (
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
)
from app.services.classification.account_mapping import (
    InvalidClassificationAccountMappingError,
    validate_classification_account_target,
)

GEMINI_REVIEW_THRESHOLD = Decimal("0.900")

SENSITIVE_GEMINI_TRANSACTION_TYPES = frozenset(
    {
        TransactionType.REFUND,
        TransactionType.TRANSFER,
        TransactionType.OWNER_CONTRIBUTION,
        TransactionType.OWNER_DISTRIBUTION,
        TransactionType.FIXED_ASSET_PURCHASE,
    }
)

ALLOWED_TRANSACTION_TYPES_BY_DIRECTION = {
    TransactionDirection.INFLOW: frozenset(
        {
            TransactionType.REVENUE,
            TransactionType.TRANSFER,
            TransactionType.OWNER_CONTRIBUTION,
        }
    ),
    TransactionDirection.OUTFLOW: frozenset(
        {
            TransactionType.COST_OF_GOODS_SOLD,
            TransactionType.OPERATING_EXPENSE,
            TransactionType.REFUND,
            TransactionType.TRANSFER,
            TransactionType.OWNER_DISTRIBUTION,
            TransactionType.FIXED_ASSET_PURCHASE,
        }
    ),
}


class UnsafeGeminiTransactionError(ValueError):
    """The transaction is not safe input for AI classification."""


class InvalidGeminiClassificationError(ValueError):
    """Gemini returned an unsupported accounting classification."""


class GeminiAllowedAccount(ImmutableAccountingModel):
    """One approved account supplied to the Gemini prompt boundary."""

    number: AccountNumber
    name: NonEmptyString
    qbo_account_type: QBOAccountType
    statement: FinancialStatement
    purpose: NonEmptyString


class GeminiClassificationRequest(ImmutableAccountingModel):
    """Strict transaction and account context sent to Gemini."""

    transaction_id: UUID
    transaction_date: date
    description_original: str | None = None
    description_normalized: NonEmptyString
    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    bank_account: NonEmptyString
    direction: TransactionDirection
    allowed_accounts: tuple[GeminiAllowedAccount, ...] = Field(min_length=1)


class GeminiClassificationResponse(ImmutableAccountingModel):
    """Minimal structured response accepted from Gemini."""

    transaction_type: TransactionType
    account_number: AccountNumber
    counterparty_name: NonEmptyString | None = None
    confidence_score: ConfidenceScore
    explanation: NonEmptyString


class GeminiClassifier(Protocol):
    """Async classifier implemented later by the Google Gemini SDK adapter."""

    async def classify(
        self,
        request: GeminiClassificationRequest,
    ) -> GeminiClassificationResponse:
        """Return one strictly structured accounting classification."""


def build_gemini_request(
    *,
    transaction: NormalizedTransaction,
    chart_of_accounts: ChartOfAccountsConfig,
) -> GeminiClassificationRequest:
    """Build safe Gemini input from one valid canonical transaction."""

    if transaction.status is not RecordStatus.VALID or transaction.duplicate_of is not None:
        raise UnsafeGeminiTransactionError("Gemini may classify only valid canonical transactions")

    if (
        transaction.transaction_date is None
        or transaction.description_normalized is None
        or transaction.amount is None
        or transaction.currency is None
        or transaction.bank_account is None
        or transaction.direction is None
    ):
        raise UnsafeGeminiTransactionError(
            "Transaction lacks complete Gemini classification fields"
        )

    allowed_accounts = tuple(
        GeminiAllowedAccount(
            number=account.number,
            name=account.name,
            qbo_account_type=account.qbo_account_type,
            statement=account.statement,
            purpose=account.purpose,
        )
        for account in chart_of_accounts.accounts
        if account.active
    )

    return GeminiClassificationRequest(
        transaction_id=transaction.id,
        transaction_date=transaction.transaction_date,
        description_original=transaction.description_original,
        description_normalized=transaction.description_normalized,
        amount=transaction.amount,
        currency=transaction.currency,
        bank_account=transaction.bank_account,
        direction=transaction.direction,
        allowed_accounts=allowed_accounts,
    )


def build_gemini_decision(
    *,
    transaction: NormalizedTransaction,
    response: GeminiClassificationResponse,
    chart_of_accounts: ChartOfAccountsConfig,
) -> ClassificationDecision:
    """Validate a Gemini response and convert it to a domain decision."""

    if transaction.direction is None or transaction.bank_account is None:
        raise UnsafeGeminiTransactionError("Transaction lacks direction or bank-account context")

    try:
        account = chart_of_accounts.require(response.account_number)
    except (KeyError, ValueError) as exc:
        raise InvalidGeminiClassificationError(
            f"Gemini referenced an unknown or inactive account: {response.account_number}"
        ) from exc

    allowed_types = ALLOWED_TRANSACTION_TYPES_BY_DIRECTION[transaction.direction]

    if response.transaction_type not in allowed_types:
        raise InvalidGeminiClassificationError(
            "Gemini transaction type "
            f"{response.transaction_type.value!r} is incompatible with "
            f"transaction direction {transaction.direction.value!r}"
        )

    try:
        validate_classification_account_target(
            transaction_type=response.transaction_type,
            account=account,
            source_bank_account=transaction.bank_account,
            subject="Gemini response",
        )
    except InvalidClassificationAccountMappingError as exc:
        raise InvalidGeminiClassificationError(str(exc)) from exc

    counterparty = (
        Counterparty(
            raw_name=transaction.description_original,
            normalized_name=response.counterparty_name,
        )
        if response.counterparty_name is not None
        else None
    )

    review_required = (
        response.confidence_score < GEMINI_REVIEW_THRESHOLD
        or response.transaction_type in SENSITIVE_GEMINI_TRANSACTION_TYPES
    )

    return ClassificationDecision(
        transaction_type=response.transaction_type,
        counterparty=counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number=account.number,
            account_name=account.name,
        ),
        confidence_score=response.confidence_score,
        explanation=(
            f"Gemini returned a structured accounting classification: {response.explanation}"
        ),
        source=ClassificationSource.GEMINI,
        review_required=review_required,
    )
