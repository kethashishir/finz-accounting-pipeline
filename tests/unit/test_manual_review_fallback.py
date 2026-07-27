"""Focused tests for unresolved initial-classification outcomes."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.models.classification import TransactionClassification
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
)
from app.services.classification.initial_classification import (
    ManualReviewReason,
    ManualReviewRequiredResult,
    classify_initial,
)
from app.services.classification.rule_config import (
    load_deterministic_rule_set,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
RULES_PATH = Path("sample_config/classification_rules.json")


class MissingPatternLookup:
    """Always represent a learned-pattern miss."""

    async def find_active(
        self,
        key: ClassificationPatternKey,
    ):
        return None


class RecordingWriter:
    """Record any accidental persistence attempts."""

    def __init__(self) -> None:
        self.saved: list[TransactionClassification] = []

    async def save_initial(
        self,
        classification: TransactionClassification,
    ) -> bool:
        self.saved.append(classification)
        return True


class UnavailableGemini:
    """Represent an operationally unavailable Gemini provider."""

    def __init__(self) -> None:
        self.requests: list[GeminiClassificationRequest] = []

    async def classify(
        self,
        request: GeminiClassificationRequest,
    ) -> GeminiClassificationResponse:
        self.requests.append(request)
        raise GeminiUnavailableError("temporary provider outage")


def create_transaction() -> NormalizedTransaction:
    """Create one valid transaction with no trusted classification rule."""

    return NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-MANUAL-0001",
        transaction_date=date(2026, 6, 20),
        description_original="UNRECOGNIZED LOCAL MERCHANT",
        description_normalized="unrecognized local merchant",
        amount=Decimal("-225.00"),
        currency="USD",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint="3" * 64,
        status=RecordStatus.VALID,
        duplicate_of=None,
    )


def supplied_dependencies():
    """Load the approved accounts and deterministic rules."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = load_deterministic_rule_set(
        RULES_PATH,
        chart_of_accounts=catalog,
    )
    return catalog, rule_set


@pytest.mark.asyncio
async def test_disabled_gemini_returns_manual_review_without_account_guess() -> None:
    """The fallback carries no fabricated accounting classification."""

    catalog, rule_set = supplied_dependencies()
    transaction = create_transaction()
    writer = RecordingWriter()

    result = await classify_initial(
        transaction=transaction,
        pattern_lookup=MissingPatternLookup(),
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
    )

    assert isinstance(result, ManualReviewRequiredResult)
    assert result.normalized_transaction_id == transaction.id
    assert result.reason is ManualReviewReason.GEMINI_DISABLED
    assert "disabled" in result.explanation.casefold()
    assert writer.saved == []
    assert not hasattr(result, "classification")


@pytest.mark.asyncio
async def test_unavailable_gemini_returns_manual_review_without_write() -> None:
    """Temporary provider failure does not invent or persist a decision."""

    catalog, rule_set = supplied_dependencies()
    transaction = create_transaction()
    writer = RecordingWriter()
    gemini = UnavailableGemini()

    result = await classify_initial(
        transaction=transaction,
        pattern_lookup=MissingPatternLookup(),
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
        gemini_classifier=gemini,
    )

    assert isinstance(result, ManualReviewRequiredResult)
    assert result.normalized_transaction_id == transaction.id
    assert result.reason is ManualReviewReason.GEMINI_UNAVAILABLE
    assert "temporarily unavailable" in result.explanation.casefold()
    assert len(gemini.requests) == 1
    assert writer.saved == []
