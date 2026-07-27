"""Tests for the internal cash-basis P&L API."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError

import app.main as main_module
from app.core.config import Settings
from app.models.classification import (
    ClassificationDecision,
    ClassificationSource,
    QuickBooksAccountMapping,
    ReviewerMetadata,
    ReviewStatus,
    TransactionClassification,
    TransactionType,
)
from app.models.ingestion import (
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
)
from app.models.profit_and_loss import ProfitAndLossSource
from app.repositories.reporting import ProfitAndLossRepository
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
REVIEWED_AT = datetime(
    2026,
    7,
    27,
    1,
    0,
    tzinfo=UTC,
)


class FakeProfitAndLossRepository:
    """Return configured approved reporting evidence."""

    def __init__(
        self,
        *,
        sources: tuple[ProfitAndLossSource, ...] = (),
        error: PyMongoError | None = None,
    ) -> None:
        self.sources = sources
        self.error = error
        self.calls: list[tuple[date, date, str]] = []

    async def find_approved_sources(
        self,
        *,
        start_date: date,
        end_date: date,
        currency: str,
    ) -> tuple[ProfitAndLossSource, ...]:
        self.calls.append(
            (
                start_date,
                end_date,
                currency,
            )
        )

        if self.error is not None:
            raise self.error

        return self.sources


def create_revenue_source() -> ProfitAndLossSource:
    """Create one approved revenue transaction."""

    transaction = NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-PNL-API-0001",
        transaction_date=date(2026, 4, 5),
        description_original="Repair service receipt",
        description_normalized="repair service receipt",
        amount=Decimal("1250.25"),
        currency="USD",
        bank_account="Operating Checking",
        direction=TransactionDirection.INFLOW,
        fingerprint="a" * 64,
        status=RecordStatus.VALID,
    )

    classification = TransactionClassification(
        normalized_transaction_id=transaction.id,
        decision=ClassificationDecision(
            transaction_type=TransactionType.REVENUE,
            qbo_account=QuickBooksAccountMapping(
                account_number="4000",
                account_name="Repair Service Revenue",
            ),
            confidence_score=Decimal("1.000"),
            explanation="Approved repair revenue.",
            source=ClassificationSource.DETERMINISTIC_RULE,
            review_required=False,
        ),
        review_status=ReviewStatus.APPROVED,
        reviewer=ReviewerMetadata(
            reviewer_id="api-reviewer",
            reviewed_at=REVIEWED_AT,
            notes="Approved for reporting.",
        ),
    )

    return ProfitAndLossSource(
        transaction=transaction,
        classification=classification,
    )


@contextmanager
def reporting_client(
    *,
    sources: tuple[ProfitAndLossSource, ...] = (),
    error: PyMongoError | None = None,
) -> Iterator[
    tuple[
        TestClient,
        FakeProfitAndLossRepository,
    ]
]:
    """Create a test client with an in-memory reporting reader."""

    settings = Settings(
        _env_file=None,
        app_env="test",
        mongodb_database="finz_pnl_api_test",
    )
    application = main_module.create_app(settings)
    repository = FakeProfitAndLossRepository(
        sources=sources,
        error=error,
    )

    with TestClient(application) as client:
        application.state.profit_and_loss_repository = repository
        application.state.chart_of_accounts = load_chart_of_accounts(CATALOG_PATH)

        yield client, repository


def test_application_wires_profit_and_loss_repository() -> None:
    """Application startup exposes the reporting repository."""

    settings = Settings(
        _env_file=None,
        app_env="test",
        mongodb_database="finz_pnl_state_test",
    )
    application = main_module.create_app(settings)

    with TestClient(application):
        assert isinstance(
            application.state.profit_and_loss_repository,
            ProfitAndLossRepository,
        )


def test_endpoint_returns_decimal_safe_monthly_and_consolidated_pnl() -> None:
    """The endpoint preserves exact monetary values as JSON strings."""

    reporting_source = create_revenue_source()

    with reporting_client(
        sources=(reporting_source,),
    ) as (client, repository):
        response = client.get(
            "/api/v1/reports/profit-and-loss",
            params={
                "start_date": "2026-04-01",
                "end_date": "2026-06-30",
                "currency": "usd",
            },
        )

    assert response.status_code == 200

    payload = response.json()

    assert len(payload["monthly"]) == 3
    assert payload["monthly"][0]["start_date"] == ("2026-04-01")
    assert payload["monthly"][1]["transaction_count"] == 0
    assert payload["monthly"][2]["transaction_count"] == 0

    consolidated = payload["consolidated"]

    assert consolidated["currency"] == "USD"
    assert consolidated["total_revenue"] == "1250.25"
    assert consolidated["gross_profit"] == "1250.25"
    assert consolidated["net_profit"] == "1250.25"
    assert consolidated["transaction_count"] == 1

    drilldown = consolidated["revenue_accounts"][0]["transactions"][0]

    assert drilldown["source_amount"] == "1250.25"
    assert drilldown["report_amount"] == "1250.25"
    assert drilldown["classification_version"] == 1

    assert repository.calls == [
        (
            date(2026, 4, 1),
            date(2026, 6, 30),
            "USD",
        )
    ]


def test_empty_period_returns_zero_activity_months() -> None:
    """No approved activity still produces a valid report set."""

    with reporting_client() as (client, repository):
        response = client.get(
            "/api/v1/reports/profit-and-loss",
            params={
                "start_date": "2026-04-01",
                "end_date": "2026-06-30",
            },
        )

    assert response.status_code == 200

    payload = response.json()

    assert len(payload["monthly"]) == 3
    assert all(statement["transaction_count"] == 0 for statement in payload["monthly"])
    assert payload["consolidated"]["net_profit"] == "0.00"
    assert repository.calls == [
        (
            date(2026, 4, 1),
            date(2026, 6, 30),
            "USD",
        )
    ]


def test_partial_month_is_rejected_before_repository_access() -> None:
    """Incomplete calendar periods fail before querying MongoDB."""

    with reporting_client() as (client, repository):
        response = client.get(
            "/api/v1/reports/profit-and-loss",
            params={
                "start_date": "2026-04-02",
                "end_date": "2026-06-30",
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == ("invalid_profit_and_loss_request")
    assert "first day" in response.json()["detail"]["message"]
    assert repository.calls == []


def test_database_failure_returns_stable_503() -> None:
    """MongoDB failures do not leak implementation details."""

    with reporting_client(
        error=PyMongoError("database unavailable"),
    ) as (client, repository):
        response = client.get(
            "/api/v1/reports/profit-and-loss",
            params={
                "start_date": "2026-04-01",
                "end_date": "2026-06-30",
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "mongodb_unavailable",
        "message": ("The Profit and Loss reporting database is unavailable"),
    }
    assert len(repository.calls) == 1
