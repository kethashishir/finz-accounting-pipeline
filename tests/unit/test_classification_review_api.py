"""Tests for classification review and correction HTTP endpoints."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.models.classification import (
    ClassificationDecision,
    ClassificationSource,
    Counterparty,
    QuickBooksAccountMapping,
    TransactionClassification,
    TransactionType,
)
from app.models.ingestion import (
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
)
from app.models.review import ReviewQueueItem
from app.repositories.classification import (
    ClassificationRepository,
)
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")


class FakeClassificationRepository:
    """Provide review queue, lookup, and write operations."""

    def __init__(
        self,
        *,
        transaction: NormalizedTransaction,
        classification: TransactionClassification,
    ) -> None:
        self.transaction = transaction
        self.current = classification
        self.review_writes: list[tuple[TransactionClassification, int]] = []
        self.correction_writes: list[tuple[TransactionClassification, int]] = []

    async def find_review_queue(
        self,
        *,
        limit: int = 100,
    ) -> tuple[ReviewQueueItem, ...]:
        return (
            ReviewQueueItem(
                transaction=self.transaction,
                classification=self.current,
            ),
        )[:limit]

    async def find_by_transaction_id(
        self,
        normalized_transaction_id: UUID,
    ) -> TransactionClassification | None:
        if normalized_transaction_id != (self.current.normalized_transaction_id):
            return None

        return self.current

    async def save_review(
        self,
        classification: TransactionClassification,
        *,
        expected_version: int,
    ) -> bool:
        self.review_writes.append(
            (
                classification,
                expected_version,
            )
        )
        return True

    async def save_correction(
        self,
        classification: TransactionClassification,
        *,
        expected_version: int,
    ) -> bool:
        self.correction_writes.append(
            (
                classification,
                expected_version,
            )
        )
        return True


class FakeIngestionRepository:
    """Return the normalized transaction used by correction tests."""

    def __init__(
        self,
        transaction: NormalizedTransaction,
    ) -> None:
        self.transaction = transaction

    async def find_transaction_by_id(
        self,
        normalized_transaction_id: UUID,
    ) -> NormalizedTransaction | None:
        if normalized_transaction_id != self.transaction.id:
            return None

        return self.transaction


def create_transaction() -> NormalizedTransaction:
    """Create one valid canonical outflow."""

    return NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-API-REVIEW-0001",
        transaction_date=date(2026, 6, 20),
        description_original="UNRECOGNIZED MERCHANT PAYMENT",
        description_normalized=("unrecognized merchant payment"),
        amount=Decimal("-225.00"),
        currency="USD",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint="a" * 64,
        status=RecordStatus.VALID,
    )


def create_classification(
    transaction: NormalizedTransaction,
) -> TransactionClassification:
    """Create one pending Gemini classification."""

    return TransactionClassification(
        normalized_transaction_id=transaction.id,
        decision=ClassificationDecision(
            transaction_type=(TransactionType.OPERATING_EXPENSE),
            counterparty=Counterparty(
                raw_name=transaction.description_original,
                normalized_name="Unrecognized Merchant",
            ),
            qbo_account=QuickBooksAccountMapping(
                account_number="6090",
                account_name="Office & General",
            ),
            confidence_score=Decimal("0.700"),
            explanation="The transaction requires human review.",
            source=ClassificationSource.GEMINI,
            review_required=True,
        ),
    )


@pytest.fixture
def classification_client() -> Iterator[
    tuple[
        TestClient,
        FakeClassificationRepository,
        NormalizedTransaction,
    ]
]:
    """Create an API client with injected in-memory repositories."""

    settings = Settings(
        _env_file=None,
        app_env="test",
        mongodb_database="finz_api_review_test",
    )
    application = create_app(settings)
    transaction = create_transaction()
    classification = create_classification(transaction)
    classification_repository = FakeClassificationRepository(
        transaction=transaction,
        classification=classification,
    )

    with TestClient(application) as client:
        application.state.classification_repository = classification_repository
        application.state.ingestion_repository = FakeIngestionRepository(transaction)
        application.state.chart_of_accounts = load_chart_of_accounts(CATALOG_PATH)

        yield (
            client,
            classification_repository,
            transaction,
        )


def test_application_wires_classification_dependencies() -> None:
    """Application lifespan exposes classification dependencies."""

    settings = Settings(
        _env_file=None,
        app_env="test",
        mongodb_database="finz_api_state_test",
    )
    application = create_app(settings)

    with TestClient(application):
        assert isinstance(
            application.state.classification_repository,
            ClassificationRepository,
        )
        assert len(application.state.chart_of_accounts.accounts) == 21


def test_review_queue_returns_transaction_evidence(
    classification_client,
) -> None:
    """Review queue responses include evidence and classification."""

    client, _, transaction = classification_client

    response = client.get(
        "/api/v1/classification/review-queue",
        params={"limit": 10},
    )

    assert response.status_code == 200

    payload = response.json()

    assert len(payload) == 1
    assert payload[0]["transaction"]["id"] == str(transaction.id)
    assert payload[0]["classification"]["review_status"] == "pending"
    assert payload[0]["classification"]["decision"]["qbo_account"]["account_number"] == "6090"


def test_approval_endpoint_records_server_review_metadata(
    classification_client,
) -> None:
    """Approval uses the review service and a server UTC timestamp."""

    client, repository, transaction = classification_client

    response = client.post(
        f"/api/v1/classification/{transaction.id}/review",
        json={
            "expected_version": 1,
            "outcome": "approved",
            "reviewer_id": "shishir",
            "notes": "Confirmed against the source record.",
        },
    )

    assert response.status_code == 200

    payload = response.json()

    assert payload["updated"] is True
    assert payload["classification"]["review_status"] == "approved"
    assert payload["classification"]["reviewer"]["reviewer_id"] == "shishir"
    assert payload["classification"]["reviewer"]["reviewed_at"] is not None

    reviewed, expected_version = repository.review_writes[0]

    assert expected_version == 1
    assert reviewed.decision == repository.current.decision
    assert reviewed.reviewer is not None
    assert reviewed.reviewer.reviewed_at.utcoffset() is not None


def test_stale_review_version_returns_conflict(
    classification_client,
) -> None:
    """An outdated browser receives HTTP 409 without a write."""

    client, repository, transaction = classification_client

    response = client.post(
        f"/api/v1/classification/{transaction.id}/review",
        json={
            "expected_version": 2,
            "outcome": "approved",
            "reviewer_id": "shishir",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == ("classification_conflict")
    assert repository.review_writes == []


def test_correction_endpoint_appends_validated_version(
    classification_client,
) -> None:
    """A valid correction returns a pending version-two decision."""

    client, repository, transaction = classification_client

    response = client.post(
        f"/api/v1/classification/{transaction.id}/correction",
        json={
            "expected_version": 1,
            "corrected_transaction_type": ("operating_expense"),
            "corrected_account_number": "6030",
            "corrected_counterparty_name": "Software Vendor",
            "reviewer_id": "shishir",
            "reason": ("The description identifies a software subscription."),
            "notes": "Checked against the bank record.",
        },
    )

    assert response.status_code == 200

    payload = response.json()
    classification = payload["classification"]

    assert payload["updated"] is True
    assert classification["version"] == 2
    assert classification["review_status"] == "pending"
    assert classification["decision"]["source"] == ("manual_review")
    assert classification["decision"]["qbo_account"]["account_number"] == "6030"
    assert len(classification["corrections"]) == 1
    assert len(repository.correction_writes) == 1


def test_unknown_correction_account_returns_422(
    classification_client,
) -> None:
    """An invented account is rejected without persistence."""

    client, repository, transaction = classification_client

    response = client.post(
        f"/api/v1/classification/{transaction.id}/correction",
        json={
            "expected_version": 1,
            "corrected_transaction_type": ("operating_expense"),
            "corrected_account_number": "9999",
            "reviewer_id": "shishir",
            "reason": "Attempt to use an unsupported account.",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == ("invalid_classification_action")
    assert repository.correction_writes == []
