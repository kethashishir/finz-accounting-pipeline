"""Tests for deterministic initial-classification persistence."""

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
from app.models.ingestion import (
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
)
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)
from app.services.classification.initial_classification import (
    classify_from_deterministic_rule,
)
from app.services.classification.rule_config import (
    load_deterministic_rule_set,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
RULES_PATH = Path("sample_config/classification_rules.json")


class FakeClassificationWriter:
    """Record classification writes without using MongoDB."""

    def __init__(
        self,
        *,
        inserted: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.inserted = inserted
        self.error = error
        self.classifications: list[TransactionClassification] = []

    async def save_initial(
        self,
        classification: TransactionClassification,
    ) -> bool:
        """Record one requested initial classification."""

        if self.error is not None:
            raise self.error

        self.classifications.append(classification)
        return self.inserted


def create_transaction(
    *,
    description: str = "MONTHLY SERVICE FEE",
    amount: Decimal = Decimal("-35.00"),
    direction: TransactionDirection = TransactionDirection.OUTFLOW,
    bank_account: str = "Operating Checking",
) -> NormalizedTransaction:
    """Create one valid canonical transaction."""

    return NormalizedTransaction(
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-TEST-INITIAL-0001",
        transaction_date=date(2026, 4, 30),
        description_original=description,
        description_normalized=description.casefold(),
        amount=amount,
        currency="USD",
        bank_account=bank_account,
        direction=direction,
        fingerprint="d" * 64,
        status=RecordStatus.VALID,
        duplicate_of=None,
    )


def supplied_dependencies():
    """Load the validated account catalog and deterministic rules."""

    catalog = load_chart_of_accounts(CATALOG_PATH)
    rule_set = load_deterministic_rule_set(
        RULES_PATH,
        chart_of_accounts=catalog,
    )
    return catalog, rule_set


@pytest.mark.asyncio
async def test_deterministic_match_is_persisted_as_initial_classification() -> None:
    """A safe rule match becomes a pending version-one classification."""

    catalog, rule_set = supplied_dependencies()
    transaction = create_transaction()
    writer = FakeClassificationWriter()

    result = await classify_from_deterministic_rule(
        transaction=transaction,
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
    )

    assert result is not None
    assert result.inserted is True
    assert result.rule_id == "bank_fees"
    assert result.priority == 380

    assert len(writer.classifications) == 1

    classification = writer.classifications[0]

    assert result.classification == classification
    assert classification.normalized_transaction_id == transaction.id
    assert classification.version == 1
    assert classification.review_status is ReviewStatus.PENDING
    assert classification.reviewer is None
    assert classification.corrections == ()

    assert classification.decision.source is (ClassificationSource.DETERMINISTIC_RULE)
    assert classification.decision.transaction_type is (TransactionType.OPERATING_EXPENSE)
    assert classification.decision.qbo_account.account_number == "6080"
    assert classification.decision.qbo_account.account_name == "Bank Fees"
    assert classification.decision.review_required is False
    assert "bank_fees" in classification.decision.explanation


@pytest.mark.asyncio
async def test_no_deterministic_match_does_not_write_classification() -> None:
    """An unmatched transaction remains available for later classifiers."""

    catalog, rule_set = supplied_dependencies()
    writer = FakeClassificationWriter()

    result = await classify_from_deterministic_rule(
        transaction=create_transaction(
            description="UNRECOGNIZED MERCHANT PAYMENT",
            amount=Decimal("-125.00"),
        ),
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
    )

    assert result is None
    assert writer.classifications == []


@pytest.mark.asyncio
async def test_exact_persistence_retry_is_reported_without_duplication() -> None:
    """Repository idempotency is retained in the orchestration result."""

    catalog, rule_set = supplied_dependencies()
    writer = FakeClassificationWriter(inserted=False)

    result = await classify_from_deterministic_rule(
        transaction=create_transaction(),
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
    )

    assert result is not None
    assert result.inserted is False
    assert len(writer.classifications) == 1


@pytest.mark.asyncio
async def test_safety_sensitive_rule_precedes_broad_revenue_rule() -> None:
    """An explicit customer refund cannot become repair revenue."""

    catalog, rule_set = supplied_dependencies()
    writer = FakeClassificationWriter()
    transaction = create_transaction(
        description=("ACH REFUND TO HARBORVIEW APARTMENTS JOB 4800"),
        amount=Decimal("-1250.00"),
        direction=TransactionDirection.OUTFLOW,
    )

    result = await classify_from_deterministic_rule(
        transaction=transaction,
        rule_set=rule_set,
        classification_writer=writer,
        chart_of_accounts=catalog,
    )

    assert result is not None
    assert result.rule_id == "customer_refund"
    assert result.priority == 30
    assert result.classification.decision.transaction_type is (TransactionType.REFUND)
    assert result.classification.decision.qbo_account.account_number == "4100"


@pytest.mark.asyncio
async def test_persistence_conflict_is_not_hidden() -> None:
    """A conflicting existing classification must stop orchestration."""

    catalog, rule_set = supplied_dependencies()
    writer = FakeClassificationWriter(
        error=RuntimeError("classification conflict"),
    )

    with pytest.raises(
        RuntimeError,
        match="classification conflict",
    ):
        await classify_from_deterministic_rule(
            transaction=create_transaction(),
            rule_set=rule_set,
            classification_writer=writer,
            chart_of_accounts=catalog,
        )
