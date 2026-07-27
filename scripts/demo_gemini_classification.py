"""Run one safe live Gemini accounting-classification demonstration."""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import Settings
from app.db.client import MongoDatabase
from app.db.serialization import transaction_from_document
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)
from app.services.classification.gemini import (
    build_gemini_decision,
    build_gemini_request,
)
from app.services.classification.gemini_adapter import (
    create_google_gemini_classifier,
)

CHART_PATH = PROJECT_ROOT / "sample_config" / "chart_of_accounts.json"


async def main() -> None:
    """Classify one synthetic unmatched transaction without persisting it."""

    settings = Settings()

    if settings.gemini_api_key is None:
        raise RuntimeError("GEMINI_API_KEY is not configured in the local .env")

    if settings.gemini_model is None:
        raise RuntimeError("GEMINI_MODEL is not configured in the local .env")

    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=settings.mongodb_database,
    )

    try:
        cursor = mongodb.database["normalized_transactions"].find(
            {
                "status": "valid",
                "duplicate_of": None,
            }
        )

        source_transaction = None

        async for document in cursor:
            candidate = transaction_from_document(document)

            if (
                candidate.amount is not None
                and candidate.amount < 0
                and candidate.transaction_date is not None
                and candidate.currency is not None
                and candidate.bank_account is not None
                and candidate.direction is not None
            ):
                source_transaction = candidate
                break

        if source_transaction is None:
            raise RuntimeError(
                "No complete canonical payment transaction "
                "was available for the Gemini demonstration"
            )

        synthetic_data = source_transaction.model_dump(mode="python")
        synthetic_data.update(
            {
                "id": uuid4(),
                "description_original": ("NORTHSTAR ADVISORY SERVICES INV 8842"),
                "description_normalized": ("northstar advisory services inv 8842"),
            }
        )

        synthetic_transaction = type(source_transaction).model_validate(synthetic_data)

        chart = load_chart_of_accounts(CHART_PATH)
        request = build_gemini_request(
            transaction=synthetic_transaction,
            chart_of_accounts=chart,
        )

        async with create_google_gemini_classifier(settings) as classifier:
            response = await classifier.classify(request)

        decision = build_gemini_decision(
            transaction=synthetic_transaction,
            response=response,
            chart_of_accounts=chart,
        )

        if decision.source.value != "gemini":
            raise RuntimeError("Live classification was not marked as Gemini")

        explanation = " ".join(decision.explanation.split())[:500]

        print("Gemini live classification: PASS")
        print("Synthetic description: NORTHSTAR ADVISORY SERVICES INV 8842")
        print(f"Transaction type: {decision.transaction_type.value}")
        print(
            "QBO account: "
            f"{decision.qbo_account.account_number} "
            f"{decision.qbo_account.account_name}"
        )
        print(f"Confidence: {decision.confidence_score}")
        print(f"Review required: {decision.review_required}")
        print(f"Explanation: {explanation}")
        print("Persisted classification: False")
        print("QuickBooks write performed: False")
        print("Gemini API key displayed: False")
    finally:
        await mongodb.close()


if __name__ == "__main__":
    asyncio.run(main())
