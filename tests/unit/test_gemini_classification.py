"""Tests for the validated Gemini classification boundary."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.classification import (
    ClassificationSource,
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
from app.services.classification.gemini import (
    GeminiClassificationResponse,
    InvalidGeminiClassificationError,
    UnsafeGeminiTransactionError,
    build_gemini_decision,
    build_gemini_request,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")


def create_transaction(
    *,
    description: str = "UNRECOGNIZED MERCHANT PAYMENT",
    amount: Decimal = Decimal("-125.00"),
    direction: TransactionDirection = TransactionDirection.OUTFLOW,
    bank_account: str = "Operating Checking",
    status: RecordStatus = RecordStatus.VALID,
    duplicate_of=None,
) -> NormalizedTransaction:
    """Create one normalized transaction for Gemini boundary tests."""

    return NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-GEMINI-0001",
        transaction_date=date(2026, 5, 15),
        description_original=description,
        description_normalized=description.casefold(),
        amount=amount,
        currency="USD",
        bank_account=bank_account,
        direction=direction,
        fingerprint="f" * 64,
        status=status,
        duplicate_of=duplicate_of,
    )


def create_response(
    *,
    transaction_type: TransactionType = (TransactionType.OPERATING_EXPENSE),
    account_number: str = "6090",
    counterparty_name: str | None = "Unrecognized Merchant",
    confidence_score: Decimal = Decimal("0.950"),
) -> GeminiClassificationResponse:
    """Create one structured Gemini response."""

    return GeminiClassificationResponse(
        transaction_type=transaction_type,
        account_number=account_number,
        counterparty_name=counterparty_name,
        confidence_score=confidence_score,
        explanation=("The description appears to be a general business purchase."),
    )


def test_request_contains_only_canonical_transaction_and_catalog_data() -> None:
    """Gemini receives the transaction and all active approved accounts."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    transaction = create_transaction()

    request = build_gemini_request(
        transaction=transaction,
        chart_of_accounts=catalog,
    )

    assert request.transaction_id == transaction.id
    assert request.amount == Decimal("-125.00")
    assert request.direction is TransactionDirection.OUTFLOW
    assert len(request.allowed_accounts) == 21
    assert {account.number for account in request.allowed_accounts} == {
        account.number for account in catalog.accounts if account.active
    }


def test_duplicate_transaction_is_rejected_before_gemini() -> None:
    """Duplicate evidence cannot be classified again by Gemini."""

    with pytest.raises(
        UnsafeGeminiTransactionError,
        match="valid canonical transactions",
    ):
        build_gemini_request(
            transaction=create_transaction(
                status=RecordStatus.DUPLICATE,
                duplicate_of=uuid4(),
            ),
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )


def test_high_confidence_expense_response_builds_domain_decision() -> None:
    """The catalog supplies the authoritative account name."""

    transaction = create_transaction()
    decision = build_gemini_decision(
        transaction=transaction,
        response=create_response(),
        chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
    )

    assert decision.source is ClassificationSource.GEMINI
    assert decision.transaction_type is (TransactionType.OPERATING_EXPENSE)
    assert decision.qbo_account.account_number == "6090"
    assert decision.qbo_account.account_name == "Office & General"
    assert decision.qbo_account.qbo_account_id is None
    assert decision.confidence_score == Decimal("0.950")
    assert decision.review_required is False
    assert decision.counterparty is not None
    assert decision.counterparty.raw_name == (transaction.description_original)
    assert decision.counterparty.normalized_name == "Unrecognized Merchant"


def test_low_confidence_response_requires_review() -> None:
    """Gemini cannot waive review below the application threshold."""

    decision = build_gemini_decision(
        transaction=create_transaction(),
        response=create_response(
            confidence_score=Decimal("0.700"),
        ),
        chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
    )

    assert decision.review_required is True


def test_sensitive_high_confidence_response_still_requires_review() -> None:
    """Balance-sheet-sensitive Gemini decisions always receive review."""

    decision = build_gemini_decision(
        transaction=create_transaction(),
        response=create_response(
            transaction_type=TransactionType.TRANSFER,
            account_number="1010",
            confidence_score=Decimal("0.990"),
        ),
        chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
    )

    assert decision.review_required is True
    assert decision.qbo_account.account_number == "1010"


def test_unknown_account_is_rejected() -> None:
    """Gemini cannot invent an account outside the approved catalog."""

    with pytest.raises(
        InvalidGeminiClassificationError,
        match="unknown or inactive account",
    ):
        build_gemini_decision(
            transaction=create_transaction(),
            response=create_response(account_number="9999"),
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )


def test_transaction_type_must_match_amount_direction() -> None:
    """A payment cannot be classified as ordinary revenue."""

    with pytest.raises(
        InvalidGeminiClassificationError,
        match="incompatible with transaction direction",
    ):
        build_gemini_decision(
            transaction=create_transaction(),
            response=create_response(
                transaction_type=TransactionType.REVENUE,
                account_number="4000",
            ),
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )


def test_transaction_type_must_match_qbo_account_type() -> None:
    """An operating expense cannot target an income account."""

    with pytest.raises(
        InvalidGeminiClassificationError,
        match="cannot use QuickBooks account type",
    ):
        build_gemini_decision(
            transaction=create_transaction(),
            response=create_response(
                transaction_type=TransactionType.OPERATING_EXPENSE,
                account_number="4000",
            ),
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )


def test_transfer_cannot_target_source_bank_account() -> None:
    """Gemini must identify the other side of an internal transfer."""

    with pytest.raises(
        InvalidGeminiClassificationError,
        match="counterpart cannot be the same",
    ):
        build_gemini_decision(
            transaction=create_transaction(),
            response=create_response(
                transaction_type=TransactionType.TRANSFER,
                account_number="1000",
            ),
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        )


def test_structured_response_rejects_unexpected_fields() -> None:
    """Gemini output cannot smuggle unsupported classification fields."""

    with pytest.raises(
        ValidationError,
        match="extra_forbidden",
    ):
        GeminiClassificationResponse.model_validate(
            {
                "transaction_type": "operating_expense",
                "account_number": "6090",
                "counterparty_name": "Merchant",
                "confidence_score": "0.950",
                "explanation": "General business purchase.",
                "review_required": False,
            }
        )
