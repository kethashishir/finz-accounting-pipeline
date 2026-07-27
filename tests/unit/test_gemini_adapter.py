"""Tests for the optional Google Gemini SDK adapter."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.classification import TransactionType
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
    build_gemini_request,
)
from app.services.classification.gemini_adapter import (
    GeminiGenerationError,
    GeminiNotConfiguredError,
    GeminiResponseValidationError,
    GoogleGeminiClassifier,
    create_google_gemini_classifier,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")


class FakeResponse:
    """Minimal SDK response carrying parsed structured output."""

    def __init__(self, parsed: object) -> None:
        self.parsed = parsed


class FakeModels:
    """Record async Gemini generation calls."""

    def __init__(
        self,
        *,
        parsed: object = None,
        error: Exception | None = None,
    ) -> None:
        self.parsed = parsed
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def generate_content(
        self,
        *,
        model: str,
        contents: object,
        config: object | None = None,
    ) -> FakeResponse:
        self.calls.append(
            {
                "model": model,
                "contents": contents,
                "config": config,
            }
        )

        if self.error is not None:
            raise self.error

        return FakeResponse(self.parsed)


class FakeAsyncClient:
    """Expose fake models and track async shutdown."""

    def __init__(self, models: FakeModels) -> None:
        self.models = models
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


class FakeClient:
    """Minimal top-level Google Gemini client."""

    def __init__(self, models: FakeModels) -> None:
        self.aio = FakeAsyncClient(models)


def create_request():
    """Build one validated Gemini request."""

    transaction = NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-ADAPTER-0001",
        transaction_date=date(2026, 5, 20),
        description_original="UNRECOGNIZED MERCHANT PAYMENT",
        description_normalized="unrecognized merchant payment",
        amount=Decimal("-125.00"),
        currency="USD",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint="1" * 64,
        status=RecordStatus.VALID,
        duplicate_of=None,
    )

    return build_gemini_request(
        transaction=transaction,
        chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
    )


def create_response() -> GeminiClassificationResponse:
    """Create one valid structured Gemini response."""

    return GeminiClassificationResponse(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        account_number="6090",
        counterparty_name="Unrecognized Merchant",
        confidence_score=Decimal("0.950"),
        explanation="The payment appears to be a general business cost.",
    )


def test_settings_normalize_blank_values_and_protect_secret() -> None:
    """Gemini remains disabled for blanks and secrets are not exposed."""

    disabled = Settings(
        _env_file=None,
        gemini_api_key="  ",
        gemini_model="  ",
    )
    enabled = Settings(
        _env_file=None,
        gemini_api_key="  private-test-key  ",
        gemini_model="  gemini-test-model  ",
    )

    assert disabled.gemini_api_key is None
    assert disabled.gemini_model is None

    assert enabled.gemini_api_key is not None
    assert enabled.gemini_api_key.get_secret_value() == "private-test-key"
    assert enabled.gemini_model == "gemini-test-model"
    assert "private-test-key" not in repr(enabled)


def test_factory_rejects_missing_configuration() -> None:
    """No SDK client is created while Gemini is disabled."""

    with pytest.raises(
        GeminiNotConfiguredError,
        match="GEMINI_API_KEY",
    ):
        create_google_gemini_classifier(
            Settings(
                _env_file=None,
                gemini_api_key=None,
                gemini_model=None,
            )
        )

    with pytest.raises(
        GeminiNotConfiguredError,
        match="GEMINI_MODEL",
    ):
        create_google_gemini_classifier(
            Settings(
                _env_file=None,
                gemini_api_key="test-key",
                gemini_model=None,
            )
        )


@pytest.mark.asyncio
async def test_adapter_requests_strict_structured_output() -> None:
    """The SDK receives deterministic settings and the response schema."""

    response = create_response()
    models = FakeModels(parsed=response)
    client = FakeClient(models)
    classifier = GoogleGeminiClassifier(
        client=client,
        model="gemini-test-model",
    )

    result = await classifier.classify(create_request())

    assert result == response
    assert len(models.calls) == 1

    call = models.calls[0]

    assert call["model"] == "gemini-test-model"
    assert isinstance(call["contents"], str)
    assert '"number":"6090"' in call["contents"]
    assert '"number":"1000"' in call["contents"]
    assert '"number":"6100"' in call["contents"]

    config = call["config"]

    assert config is not None
    assert config.temperature == 0
    assert config.candidate_count == 1
    assert config.response_mime_type == "application/json"
    assert config.response_schema is GeminiClassificationResponse


@pytest.mark.asyncio
async def test_adapter_validates_parsed_dictionary() -> None:
    """Dictionary output is revalidated by the application model."""

    models = FakeModels(
        parsed=create_response().model_dump(mode="json"),
    )
    classifier = GoogleGeminiClassifier(
        client=FakeClient(models),
        model="gemini-test-model",
    )

    result = await classifier.classify(create_request())

    assert isinstance(result, GeminiClassificationResponse)
    assert result.account_number == "6090"


@pytest.mark.asyncio
async def test_missing_structured_output_is_rejected() -> None:
    """The adapter never falls back to unvalidated free-form text."""

    classifier = GoogleGeminiClassifier(
        client=FakeClient(FakeModels(parsed=None)),
        model="gemini-test-model",
    )

    with pytest.raises(
        GeminiResponseValidationError,
        match="no structured classification",
    ):
        await classifier.classify(create_request())


@pytest.mark.asyncio
async def test_invalid_structured_output_is_rejected() -> None:
    """Malformed parsed output cannot cross the SDK boundary."""

    classifier = GoogleGeminiClassifier(
        client=FakeClient(
            FakeModels(
                parsed={
                    "transaction_type": "not_supported",
                    "account_number": "9999",
                }
            )
        ),
        model="gemini-test-model",
    )

    with pytest.raises(
        GeminiResponseValidationError,
        match="invalid structured classification",
    ):
        await classifier.classify(create_request())


@pytest.mark.asyncio
async def test_sdk_failure_is_translated() -> None:
    """Provider exceptions become a stable application error."""

    classifier = GoogleGeminiClassifier(
        client=FakeClient(
            FakeModels(
                error=RuntimeError("provider unavailable"),
            )
        ),
        model="gemini-test-model",
    )

    with pytest.raises(
        GeminiGenerationError,
        match="request failed",
    ) as error:
        await classifier.classify(create_request())

    assert isinstance(error.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_owned_client_is_closed_once() -> None:
    """The adapter closes only the async resources it owns."""

    client = FakeClient(FakeModels(parsed=create_response()))
    classifier = GoogleGeminiClassifier(
        client=client,
        model="gemini-test-model",
        owns_client=True,
    )

    await classifier.close()
    await classifier.close()

    assert client.aio.close_count == 1

    with pytest.raises(
        RuntimeError,
        match="already closed",
    ):
        await classifier.classify(create_request())
