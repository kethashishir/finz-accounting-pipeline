"""Database-backed classification API workflow tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app
from app.models.classification import (
    ClassificationSource,
    ReviewStatus,
)
from app.models.ingestion import (
    FileType,
    NormalizedTransaction,
    RawRecord,
    RecordStatus,
    TransactionDirection,
    UploadBatch,
)


@pytest.mark.asyncio
async def test_classification_api_preserves_corrected_audit_history() -> None:
    """Run classify, queue, correct, and approve through MongoDB."""

    database_name = f"finz_api_flow_{uuid4().hex[:16]}"
    settings = Settings(
        _env_file=None,
        app_env="development",
        mongodb_database=database_name,
        gemini_api_key=None,
        gemini_model=None,
    )
    application = create_app(settings)

    async with application.router.lifespan_context(application):
        mongodb = application.state.mongodb
        ingestion_repository = application.state.ingestion_repository
        classification_repository = application.state.classification_repository
        pattern_repository = application.state.classification_pattern_repository

        try:
            upload_indexes = await ingestion_repository.uploads.index_information()
            classification_indexes = (
                await classification_repository.classifications.index_information()
            )
            pattern_indexes = await pattern_repository.patterns.index_information()

            assert "uq_upload_file_sha256" in upload_indexes
            assert "ix_classification_review_queue" in classification_indexes
            assert "ux_classification_pattern_active_key" in pattern_indexes

            upload = UploadBatch(
                source_file_name="classification-flow.csv",
                file_type=FileType.CSV,
                file_sha256="d" * 64,
                physical_record_count=1,
            )
            raw_record = RawRecord(
                upload_id=upload.id,
                source_file_name=upload.source_file_name,
                source_row_number=2,
                raw_values={
                    "Date": "2026-06-30",
                    "Description": "MONTHLY SERVICE FEE",
                    "Amount": "-35.00",
                },
                raw_hash="e" * 64,
            )
            transaction = NormalizedTransaction(
                upload_id=upload.id,
                raw_record_id=raw_record.id,
                source_transaction_id=("BF-API-INTEGRATION-0001"),
                transaction_date=date(2026, 6, 30),
                description_original=("MONTHLY SERVICE FEE"),
                description_normalized=("monthly service fee"),
                amount=Decimal("-35.00"),
                currency="USD",
                bank_account="Operating Checking",
                direction=TransactionDirection.OUTFLOW,
                fingerprint="f" * 64,
                status=RecordStatus.VALID,
            )

            await ingestion_repository.save_batch(
                upload=upload,
                raw_records=[raw_record],
                transactions=[transaction],
            )

            transport = ASGITransport(app=application)

            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                classify_response = await client.post(
                    f"/api/v1/classification/uploads/{upload.id}/classify"
                )

                assert classify_response.status_code == 200

                classify_payload = classify_response.json()

                assert classify_payload["classified_by_deterministic_rule"] == 1
                assert classify_payload["failed"] == 0

                initial = await classification_repository.find_by_transaction_id(transaction.id)

                assert initial is not None
                assert initial.version == 1
                assert initial.review_status is ReviewStatus.PENDING
                assert initial.decision.source is ClassificationSource.DETERMINISTIC_RULE
                assert initial.decision.qbo_account.account_number == "6080"

                queue_response = await client.get(
                    "/api/v1/classification/review-queue",
                    params={"limit": 10},
                )

                assert queue_response.status_code == 200

                queue_payload = queue_response.json()

                queue_item = next(
                    item
                    for item in queue_payload
                    if item["transaction"]["id"] == str(transaction.id)
                )

                assert queue_item["classification"]["version"] == 1

                correction_response = await client.post(
                    f"/api/v1/classification/{transaction.id}/correction",
                    json={
                        "expected_version": 1,
                        "corrected_transaction_type": ("operating_expense"),
                        "corrected_account_number": "6030",
                        "corrected_counterparty_name": ("Bank Software Service"),
                        "reviewer_id": ("integration-reviewer"),
                        "reason": ("The reviewed evidence identifies a software subscription."),
                        "notes": ("Verified in the normalized bank transaction."),
                    },
                )

                assert correction_response.status_code == 200

                corrected_payload = correction_response.json()["classification"]

                assert corrected_payload["version"] == 2
                assert corrected_payload["review_status"] == "pending"
                assert corrected_payload["decision"]["qbo_account"]["account_number"] == "6030"
                assert len(corrected_payload["corrections"]) == 1

                approval_response = await client.post(
                    f"/api/v1/classification/{transaction.id}/review",
                    json={
                        "expected_version": 2,
                        "outcome": "approved",
                        "reviewer_id": ("integration-approver"),
                        "notes": ("Approved after reviewing the correction."),
                    },
                )

                assert approval_response.status_code == 200
                assert approval_response.json()["classification"]["review_status"] == "approved"

                retry_response = await client.post(
                    f"/api/v1/classification/uploads/{upload.id}/classify"
                )

                assert retry_response.status_code == 200
                assert retry_response.json()["already_classified"] == 1

            stored = await classification_repository.find_by_transaction_id(transaction.id)

            assert stored is not None
            assert stored.version == 2
            assert stored.review_status is ReviewStatus.APPROVED
            assert stored.reviewer is not None
            assert stored.reviewer.reviewer_id == "integration-approver"
            assert len(stored.corrections) == 1

            correction = stored.corrections[0]

            assert correction.from_version == 1
            assert correction.to_version == 2
            assert correction.previous_decision.qbo_account.account_number == "6080"
            assert correction.corrected_decision.qbo_account.account_number == "6030"
            assert correction.corrected_by.reviewer_id == "integration-reviewer"
            assert stored.decision == (correction.corrected_decision)
        finally:
            await mongodb.client.drop_database(database_name)
