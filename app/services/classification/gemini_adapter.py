"""Optional Google Gemini SDK adapter for structured classification."""

from __future__ import annotations

import json
from typing import Any, Protocol, Self

from google import genai
from google.genai import types
from pydantic import ValidationError

from app.core.config import Settings
from app.services.classification.gemini import (
    GeminiClassificationRequest,
    GeminiClassificationResponse,
    GeminiClassifier,
    GeminiUnavailableError,
)


class GeminiNotConfiguredError(RuntimeError):
    """Gemini classification is disabled by missing settings."""


class GeminiGenerationError(GeminiUnavailableError):
    """The Gemini SDK request failed before returning a response."""


class GeminiResponseValidationError(RuntimeError):
    """Gemini failed to return the required structured response."""


class AsyncGeminiModels(Protocol):
    """Minimal async SDK model interface required by the adapter."""

    async def generate_content(
        self,
        *,
        model: str,
        contents: object,
        config: object | None = None,
    ) -> Any:
        """Generate one structured response."""


class AsyncGeminiClient(Protocol):
    """Minimal async SDK client interface required by the adapter."""

    models: AsyncGeminiModels

    async def aclose(self) -> None:
        """Close async HTTP resources."""


class GeminiSDKClient(Protocol):
    """Minimal top-level SDK client interface required by the adapter."""

    aio: AsyncGeminiClient


_SYSTEM_INSTRUCTION = """
You are an accounting transaction classifier for a cash-basis small
business bookkeeping system.

Choose exactly one transaction type and exactly one account number from
the supplied allowed_accounts list.

Never invent an account, account number, transaction type, amount,
merchant, or business fact.

Use the transaction direction and the account purpose when selecting the
classification. Treat transfers, owner activity, refunds, and fixed
assets conservatively.

Return only the structured response required by the response schema.
Keep the explanation concise and grounded in the supplied transaction.
""".strip()


class GoogleGeminiClassifier(GeminiClassifier):
    """Classify transactions through an injected Google Gemini client."""

    def __init__(
        self,
        *,
        client: GeminiSDKClient,
        model: str,
        owns_client: bool = False,
    ) -> None:
        normalized_model = model.strip()

        if not normalized_model:
            raise ValueError("Gemini model cannot be empty")

        self._client = client
        self._model = normalized_model
        self._owns_client = owns_client
        self._closed = False

    async def classify(
        self,
        request: GeminiClassificationRequest,
    ) -> GeminiClassificationResponse:
        """Generate and validate one structured Gemini classification."""

        if self._closed:
            raise RuntimeError("Gemini classifier is already closed")

        config = types.GenerateContentConfig(
            system_instruction=_SYSTEM_INSTRUCTION,
            candidate_count=1,
            response_mime_type="application/json",
            response_json_schema=(
                GeminiClassificationResponse.model_json_schema(mode="validation")
            ),
        )

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=_build_user_prompt(request),
                config=config,
            )
        except Exception as exc:
            raise GeminiGenerationError("Gemini classification request failed") from exc

        parsed = getattr(response, "parsed", None)

        if parsed is None:
            raise GeminiResponseValidationError("Gemini returned no structured classification")

        if isinstance(parsed, GeminiClassificationResponse):
            return parsed

        try:
            return GeminiClassificationResponse.model_validate(parsed)
        except ValidationError as exc:
            raise GeminiResponseValidationError(
                "Gemini returned an invalid structured classification"
            ) from exc

    async def close(self) -> None:
        """Close SDK resources when this adapter owns the client."""

        if self._closed:
            return

        self._closed = True

        if self._owns_client:
            await self._client.aio.aclose()

    async def __aenter__(self) -> Self:
        """Return this open adapter."""

        if self._closed:
            raise RuntimeError("Gemini classifier is already closed")

        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        """Close owned SDK resources when leaving a context."""

        await self.close()


def create_google_gemini_classifier(
    settings: Settings,
) -> GoogleGeminiClassifier:
    """Create an enabled SDK adapter from validated application settings."""

    if settings.gemini_api_key is None:
        raise GeminiNotConfiguredError("GEMINI_API_KEY is not configured")

    if settings.gemini_model is None:
        raise GeminiNotConfiguredError("GEMINI_MODEL is not configured")

    client = genai.Client(
        api_key=settings.gemini_api_key.get_secret_value(),
    )

    return GoogleGeminiClassifier(
        client=client,
        model=settings.gemini_model,
        owns_client=True,
    )


def _build_user_prompt(
    request: GeminiClassificationRequest,
) -> str:
    """Serialize only the validated request model into the user prompt."""

    payload = request.model_dump(
        mode="json",
        exclude_none=False,
    )

    return (
        "Classify this canonical bank transaction using only the "
        "supplied allowed accounts.\n\n"
        + json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
