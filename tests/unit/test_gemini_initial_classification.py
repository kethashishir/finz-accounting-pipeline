"""Tests for Gemini fallback in the initial-classification pipeline."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.classification import (
    ClassificationSource,
    ReviewStatus,
    TransactionClassification,
    TransactionType,
)
from app.models.classification_pattern import ClassificationPatternKey
from app.models.ingestion import (
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
)
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)
from app.services.classification.gemini import (
    GeminiClassificationRequest,
    GeminiClassificationResponse,
    GeminiUnavailableError,
    InvalidGeminiClassificationError,
)
from app.services.classification.initial_classification import (
    DeterministicRuleClassificationResult,
    GeminiClassificationResult,
    ManualReviewReason,
    ManualReviewRequiredResult,
    classify_initial,
)
from app.services.classification.rule_config import (
    load_deterministic_rule_set,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
RULES_PATH = Path("sample_config/classification_rules.json")


class FakePatternLookup:
    """Represent an exact learned-pattern miss."""

    def __init__(self) -> None:
        self.requested_keys: list[ClassificationPatternKey] = []

    async def find_active(
        self,
        key: ClassificationPatternKey,
    ):
        self.requested_keys.append(key)
        return None


class FakeClassificationWriter:
    """Record initial classification persistence calls."""

    def __init__(
        self,
        *,
        inserted: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.inserted = inserted
        self.error = error
        self.saved: list[TransactionClassification] = []

    async def save_initial(
        self,
        classification: TransactionClassification,
    ) -> bool:
        self.saved.append(classification)

        if self.error is not None:
            raise self.error

        return self.inserted


class FakeGeminiClassifier:
    """Return one configured structured Gemini response."""

    def __init__(
        self,
        *,
        response: GeminiClassificationResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.requests: list[GeminiClassificationRequest] = []

    async def classify(
        self,
        request: GeminiClassificationRequest,
    ) -> GeminiClassificationResponse:
        self.requests.append(request)

        if self.error is not None:
            raise self.error

        if self.response is None:
            raise AssertionError("Fake Gemini response was not configured")

        return self.response


def create_transaction(
    *,
    description: str = "UNRECOGNIZED MERCHANT PAYMENT",
    amount: Decimal = Decimal("-125.00"),
    direction: TransactionDirection = TransactionDirection.OUTFLOW,
) -> NormalizedTransaction:
    """Create one valid canonical transaction."""

    return NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-GEMINI-PIPELINE-0001",
        transaction_date=date(2026, 5, 20),
        description_original=description,
        description_normalized=description.casefold(),
        amount=amount,
        currency="USD",
        bank_account="Operating Checking",
        direction=direction,
        fingerprint="2" * 64,
        status=RecordStatus.VALID,
        duplicate_of=None,
    )


def create_response(
    *,
    account_number: str = "6090",
    confidence_score: Decimal = Decimal("0.950"),
) -> GeminiClassificationResponse:
    """Create one valid structured Gemini expense response."""

    return GeminiClassificationResponse(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        account_number=account_number,
        counterparty_name="Unrecognized Merchant",
        confidence_score=confidence_score,
        explanation="The payment appears to be a general business cost.",
    )


def supplied_dependencies():
    """Load the approved account catalog and deterministic rules."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = load_deterministic_rule_set(
        RULES_PATH,
        chart_of_accounts=catalog,
    )
    return catalog, rule_set


@pytest.mark.asyncio
async def test_deterministic_match_prevents_gemini_call() -> None:
    """Gemini is not used when an earlier trusted rule succeeds."""

    catalog, rule_set = supplied_dependencies()
    gemini = FakeGeminiClassifier(response=create_response())
    writer = FakeClassificationWriter()

    result = await classify_initial(
        transaction=create_transaction(
            description="MONTHLY SERVICE FEE",
            amount=Decimal("-35.00"),
        ),
        pattern_lookup=FakePatternLookup(),
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
        gemini_classifier=gemini,
    )

    assert isinstance(result, DeterministicRuleClassificationResult)
    assert result.rule_id == "bank_fees"
    assert gemini.requests == []
    assert writer.saved == [result.classification]


@pytest.mark.asyncio
async def test_unmatched_transaction_uses_gemini_and_persists_once() -> None:
    """Validated Gemini output becomes one initial classification."""

    catalog, rule_set = supplied_dependencies()
    transaction = create_transaction()
    gemini = FakeGeminiClassifier(response=create_response())
    writer = FakeClassificationWriter()

    result = await classify_initial(
        transaction=transaction,
        pattern_lookup=FakePatternLookup(),
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
        gemini_classifier=gemini,
    )

    assert isinstance(result, GeminiClassificationResult)
    assert result.inserted is True
    assert len(gemini.requests) == 1
    assert writer.saved == [result.classification]

    classification = result.classification

    assert classification.normalized_transaction_id == transaction.id
    assert classification.version == 1
    assert classification.review_status is ReviewStatus.PENDING
    assert classification.reviewer is None
    assert classification.corrections == ()
    assert classification.decision.source is ClassificationSource.GEMINI
    assert classification.decision.transaction_type is (TransactionType.OPERATING_EXPENSE)
    assert classification.decision.qbo_account.account_number == "6090"
    assert classification.decision.qbo_account.account_name == ("Office & General")
    assert classification.decision.review_required is False


@pytest.mark.asyncio
async def test_unmatched_transaction_without_gemini_requires_review() -> None:
    """Disabled Gemini returns an explicit non-persisted review result."""

    catalog, rule_set = supplied_dependencies()
    writer = FakeClassificationWriter()
    transaction = create_transaction()

    result = await classify_initial(
        transaction=transaction,
        pattern_lookup=FakePatternLookup(),
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
    )

    assert isinstance(result, ManualReviewRequiredResult)
    assert result.normalized_transaction_id == transaction.id
    assert result.reason is ManualReviewReason.GEMINI_DISABLED
    assert writer.saved == []


@pytest.mark.asyncio
async def test_low_confidence_gemini_result_requires_review() -> None:
    """The application review threshold survives pipeline persistence."""

    catalog, rule_set = supplied_dependencies()

    result = await classify_initial(
        transaction=create_transaction(),
        pattern_lookup=FakePatternLookup(),
        rule_set=rule_set,
        classification_writer=FakeClassificationWriter(),
        chart_of_accounts=catalog,
        gemini_classifier=FakeGeminiClassifier(
            response=create_response(
                confidence_score=Decimal("0.700"),
            )
        ),
    )

    assert isinstance(result, GeminiClassificationResult)
    assert result.classification.decision.review_required is True


@pytest.mark.asyncio
async def test_invalid_gemini_mapping_is_not_persisted() -> None:
    """An invented Gemini account stops before the repository write."""

    catalog, rule_set = supplied_dependencies()
    writer = FakeClassificationWriter()
    gemini = FakeGeminiClassifier(
        response=create_response(account_number="9999"),
    )

    with pytest.raises(
        InvalidGeminiClassificationError,
        match="unknown or inactive account",
    ):
        await classify_initial(
            transaction=create_transaction(),
            pattern_lookup=FakePatternLookup(),
            rule_set=rule_set,
            classification_writer=writer,
            chart_of_accounts=catalog,
            gemini_classifier=gemini,
        )

    assert len(gemini.requests) == 1
    assert writer.saved == []


@pytest.mark.asyncio
async def test_gemini_provider_unavailability_requires_review() -> None:
    """Provider availability failures return a non-persisted fallback."""

    catalog, rule_set = supplied_dependencies()
    writer = FakeClassificationWriter()
    transaction = create_transaction()

    result = await classify_initial(
        transaction=transaction,
        pattern_lookup=FakePatternLookup(),
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
        gemini_classifier=FakeGeminiClassifier(
            error=GeminiUnavailableError("provider unavailable"),
        ),
    )

    assert isinstance(result, ManualReviewRequiredResult)
    assert result.normalized_transaction_id == transaction.id
    assert result.reason is ManualReviewReason.GEMINI_UNAVAILABLE
    assert writer.saved == []


@pytest.mark.asyncio
async def test_gemini_persistence_retry_is_reported() -> None:
    """Repository idempotency remains visible for Gemini classifications."""

    catalog, rule_set = supplied_dependencies()
    writer = FakeClassificationWriter(inserted=False)

    result = await classify_initial(
        transaction=create_transaction(),
        pattern_lookup=FakePatternLookup(),
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
        gemini_classifier=FakeGeminiClassifier(
            response=create_response(),
        ),
    )

    assert isinstance(result, GeminiClassificationResult)
    assert result.inserted is False
    assert len(writer.saved) == 1
