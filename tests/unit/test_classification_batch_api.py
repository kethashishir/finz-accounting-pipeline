"""Tests for upload batch-classification HTTP workflows."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.core.config import Settings
from app.models.classification import (
    TransactionClassification,
)
from app.models.ingestion import (
    FileType,
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
    UploadBatch,
)
from app.repositories.classification_pattern import (
    ClassificationPatternRepository,
)
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)
from app.services.classification.rule_config import (
    load_deterministic_rule_set,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
RULES_PATH = Path("sample_config/classification_rules.json")


class FakeBatchIngestionRepository:
    """Return one configured upload and transaction sequence."""

    def __init__(
        self,
        *,
        upload: UploadBatch | None,
        transactions: tuple[
            NormalizedTransaction,
            ...,
        ],
    ) -> None:
        self.upload = upload
        self.transactions = transactions
        self.upload_lookups: list[UUID] = []
        self.transaction_lookups: list[UUID] = []

    async def find_upload_by_id(
        self,
        upload_id: UUID,
    ) -> UploadBatch | None:
        self.upload_lookups.append(upload_id)

        if self.upload is None or self.upload.id != upload_id:
            return None

        return self.upload

    async def transactions_for_upload(
        self,
        upload_id: UUID,
    ) -> tuple[NormalizedTransaction, ...]:
        self.transaction_lookups.append(upload_id)
        return tuple(
            transaction for transaction in self.transactions if transaction.upload_id == upload_id
        )


class FakeBatchClassificationRepository:
    """Persist initial classifications in memory."""

    def __init__(self) -> None:
        self.classifications: dict[
            UUID,
            TransactionClassification,
        ] = {}
        self.lookup_calls: list[tuple[UUID, ...]] = []
        self.save_calls: list[TransactionClassification] = []

    async def find_by_transaction_ids(
        self,
        normalized_transaction_ids,
    ) -> dict[UUID, TransactionClassification]:
        identifiers = tuple(normalized_transaction_ids)
        self.lookup_calls.append(identifiers)

        return {
            identifier: self.classifications[identifier]
            for identifier in identifiers
            if identifier in self.classifications
        }

    async def save_initial(
        self,
        classification: TransactionClassification,
    ) -> bool:
        identifier = classification.normalized_transaction_id
        existing = self.classifications.get(identifier)

        if existing is not None:
            if existing == classification:
                return False

            raise RuntimeError("Conflicting in-memory classification")

        self.classifications[identifier] = classification
        self.save_calls.append(classification)
        return True


class MissingPatternLookup:
    """Represent a learned-pattern repository with no match."""

    async def find_active(self, key):
        return None


class FakeOwnedGeminiClassifier:
    """Record application-shutdown resource cleanup."""

    def __init__(self) -> None:
        self.closed = False

    async def classify(self, request: object) -> object:
        raise AssertionError("Lifecycle test must not classify a transaction")

    async def close(self) -> None:
        self.closed = True


def create_upload(
    *,
    physical_record_count: int = 1,
) -> UploadBatch:
    """Create one persisted upload identity."""

    return UploadBatch(
        source_file_name="bank.csv",
        file_type=FileType.CSV,
        file_sha256="a" * 64,
        physical_record_count=physical_record_count,
    )


def create_transaction(
    upload: UploadBatch,
    *,
    description: str = "MONTHLY SERVICE FEE",
) -> NormalizedTransaction:
    """Create one canonical transaction for batch classification."""

    return NormalizedTransaction(
        upload_id=upload.id,
        raw_record_id=uuid4(),
        source_transaction_id="BF-BATCH-API-0001",
        transaction_date=date(2026, 6, 30),
        description_original=description,
        description_normalized=description.casefold(),
        amount=Decimal("-35.00"),
        currency="USD",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint="b" * 64,
        status=RecordStatus.VALID,
    )


@contextmanager
def batch_client(
    *,
    upload: UploadBatch | None,
    transactions: tuple[
        NormalizedTransaction,
        ...,
    ],
) -> Iterator[
    tuple[
        TestClient,
        FakeBatchIngestionRepository,
        FakeBatchClassificationRepository,
    ]
]:
    """Create a client with in-memory batch dependencies."""

    settings = Settings(
        _env_file=None,
        app_env="test",
        mongodb_database="finz_batch_api_test",
    )
    application = main_module.create_app(settings)
    ingestion_repository = FakeBatchIngestionRepository(
        upload=upload,
        transactions=transactions,
    )
    classification_repository = FakeBatchClassificationRepository()
    chart_of_accounts = load_chart_of_accounts(CATALOG_PATH)
    rule_set = load_deterministic_rule_set(
        RULES_PATH,
        chart_of_accounts=chart_of_accounts,
    )

    with TestClient(application) as client:
        application.state.ingestion_repository = ingestion_repository
        application.state.classification_repository = classification_repository
        application.state.classification_pattern_repository = MissingPatternLookup()
        application.state.chart_of_accounts = chart_of_accounts
        application.state.classification_rule_set = rule_set
        application.state.gemini_classifier = None

        yield (
            client,
            ingestion_repository,
            classification_repository,
        )


def test_application_wires_batch_dependencies() -> None:
    """Application startup exposes all batch dependencies."""

    settings = Settings(
        _env_file=None,
        app_env="test",
        mongodb_database="finz_batch_state_test",
    )
    application = main_module.create_app(settings)

    with TestClient(application):
        assert isinstance(
            application.state.classification_pattern_repository,
            ClassificationPatternRepository,
        )
        assert len(application.state.classification_rule_set.rules) == 22
        assert application.state.gemini_classifier is None


def test_owned_gemini_classifier_closes_on_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Application shutdown closes its configured Gemini client."""

    classifier = FakeOwnedGeminiClassifier()

    monkeypatch.setattr(
        main_module,
        "create_google_gemini_classifier",
        lambda settings: classifier,
    )

    settings = Settings(
        _env_file=None,
        app_env="test",
        mongodb_database="finz_gemini_lifecycle_test",
        gemini_api_key="test-secret",
        gemini_model="test-model",
    )
    application = main_module.create_app(settings)

    with TestClient(application):
        assert application.state.gemini_classifier is classifier
        assert classifier.closed is False

    assert classifier.closed is True


def test_batch_endpoint_classifies_by_deterministic_rule() -> None:
    """A known bank fee is classified without Gemini."""

    upload = create_upload()
    transaction = create_transaction(upload)

    with batch_client(
        upload=upload,
        transactions=(transaction,),
    ) as (
        client,
        ingestion_repository,
        classification_repository,
    ):
        response = client.post(f"/api/v1/classification/uploads/{upload.id}/classify")

    assert response.status_code == 200

    payload = response.json()

    assert payload["upload_id"] == str(upload.id)
    assert payload["total_records"] == 1
    assert payload["canonical_transactions"] == 1
    assert payload["classified_by_deterministic_rule"] == 1
    assert payload["classified_by_gemini"] == 0
    assert payload["manual_review_required"] == 0
    assert payload["failed"] == 0
    assert payload["outcomes"][0]["outcome"] == "deterministic_rule"

    stored = classification_repository.classifications[transaction.id]

    assert stored.decision.qbo_account.account_number == "6080"
    assert ingestion_repository.upload_lookups == [upload.id]
    assert ingestion_repository.transaction_lookups == [upload.id]


def test_batch_endpoint_is_idempotent_across_retries() -> None:
    """A repeated request reports the stored classification."""

    upload = create_upload()
    transaction = create_transaction(upload)

    with batch_client(
        upload=upload,
        transactions=(transaction,),
    ) as (
        client,
        _,
        classification_repository,
    ):
        first = client.post(f"/api/v1/classification/uploads/{upload.id}/classify")
        second = client.post(f"/api/v1/classification/uploads/{upload.id}/classify")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["classified_by_deterministic_rule"] == 1
    assert second.json()["already_classified"] == 1
    assert second.json()["outcomes"][0]["outcome"] == "already_classified"
    assert len(classification_repository.save_calls) == 1


def test_unknown_upload_returns_404() -> None:
    """An unknown upload cannot be confused with an empty batch."""

    missing_upload_id = uuid4()

    with batch_client(
        upload=None,
        transactions=(),
    ) as (
        client,
        ingestion_repository,
        classification_repository,
    ):
        response = client.post(f"/api/v1/classification/uploads/{missing_upload_id}/classify")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == ("upload_not_found")
    assert ingestion_repository.transaction_lookups == []
    assert classification_repository.lookup_calls == []


def test_existing_empty_upload_returns_zero_summary() -> None:
    """An existing upload may safely contain no transactions."""

    upload = create_upload(physical_record_count=0)

    with batch_client(
        upload=upload,
        transactions=(),
    ) as (
        client,
        _,
        classification_repository,
    ):
        response = client.post(f"/api/v1/classification/uploads/{upload.id}/classify")

    assert response.status_code == 200

    payload = response.json()

    assert payload["total_records"] == 0
    assert payload["canonical_transactions"] == 0
    assert payload["ignored_noncanonical"] == 0
    assert payload["failed"] == 0
    assert payload["outcomes"] == []
    assert classification_repository.lookup_calls == []


def test_unmatched_transaction_requires_manual_review_when_gemini_disabled() -> None:
    """An unmatched transaction remains visible for human review."""

    upload = create_upload()
    transaction = create_transaction(
        upload,
        description="UNRECOGNIZED MERCHANT PAYMENT",
    )

    with batch_client(
        upload=upload,
        transactions=(transaction,),
    ) as (
        client,
        _,
        classification_repository,
    ):
        response = client.post(f"/api/v1/classification/uploads/{upload.id}/classify")

    assert response.status_code == 200

    payload = response.json()

    assert payload["manual_review_required"] == 1
    assert payload["outcomes"][0]["outcome"] == "manual_review_required"
    assert payload["classified_by_gemini"] == 0
    assert payload["failed"] == 0
    assert classification_repository.save_calls == []
