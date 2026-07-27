"""Tests for validated manual classification corrections."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.models.classification import (
    ClassificationDecision,
    ClassificationSource,
    Counterparty,
    QuickBooksAccountMapping,
    ReviewStatus,
    TransactionClassification,
    TransactionType,
)
from app.models.ingestion import (
    NormalizedTransaction,
    RecordStatus,
    TransactionDirection,
)
from app.repositories.classification import (
    ClassificationNotFoundError,
    StaleClassificationVersionError,
    UnsafeClassificationTransactionError,
)
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)
from app.services.classification.correction_actions import (
    InvalidManualCorrectionError,
    correct_classification,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
REVIEWED_AT = datetime(
    2026,
    7,
    26,
    21,
    0,
    tzinfo=UTC,
)


class FakeClassificationRepository:
    """Record classification correction persistence activity."""

    def __init__(
        self,
        current: TransactionClassification | None,
        *,
        updated: bool = True,
    ) -> None:
        self.current = current
        self.updated = updated
        self.lookups: list[UUID] = []
        self.saved: list[tuple[TransactionClassification, int]] = []

    async def find_by_transaction_id(
        self,
        normalized_transaction_id: UUID,
    ) -> TransactionClassification | None:
        self.lookups.append(normalized_transaction_id)
        return self.current

    async def save_correction(
        self,
        classification: TransactionClassification,
        *,
        expected_version: int,
    ) -> bool:
        self.saved.append(
            (
                classification,
                expected_version,
            )
        )
        return self.updated


class FakeTransactionReader:
    """Return configured normalized transaction evidence."""

    def __init__(
        self,
        transaction: NormalizedTransaction | None,
    ) -> None:
        self.transaction = transaction
        self.lookups: list[UUID] = []

    async def find_transaction_by_id(
        self,
        normalized_transaction_id: UUID,
    ) -> NormalizedTransaction | None:
        self.lookups.append(normalized_transaction_id)
        return self.transaction


def create_transaction(
    *,
    transaction_id: UUID | None = None,
    direction: TransactionDirection = (TransactionDirection.OUTFLOW),
    status: RecordStatus = RecordStatus.VALID,
    duplicate_of: UUID | None = None,
    bank_account: str = "Operating Checking",
) -> NormalizedTransaction:
    """Create one normalized transaction for correction tests."""

    return NormalizedTransaction(
        id=transaction_id or uuid4(),
        upload_id=uuid4(),
        raw_record_id=uuid4(),
        source_transaction_id="BF-CORRECTION-0001",
        transaction_date=date(2026, 6, 20),
        description_original="UNRECOGNIZED MERCHANT PAYMENT",
        description_normalized="unrecognized merchant payment",
        amount=(
            Decimal("-225.00") if direction is TransactionDirection.OUTFLOW else Decimal("225.00")
        ),
        currency="USD",
        bank_account=bank_account,
        direction=direction,
        fingerprint="8" * 64,
        status=status,
        duplicate_of=duplicate_of,
    )


def create_classification(
    transaction: NormalizedTransaction,
) -> TransactionClassification:
    """Create one stored classification requiring correction."""

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
            explanation="Initial classification requires review.",
            source=ClassificationSource.GEMINI,
            review_required=True,
        ),
    )


async def run_correction(
    *,
    current: TransactionClassification | None,
    transaction: NormalizedTransaction | None,
    expected_version: int = 1,
    transaction_type: TransactionType = (TransactionType.OPERATING_EXPENSE),
    account_number: str = "6030",
    counterparty_name: str | None = None,
    updated: bool = True,
):
    """Execute one correction with standard reviewer metadata."""

    classification_repository = FakeClassificationRepository(
        current,
        updated=updated,
    )
    transaction_reader = FakeTransactionReader(transaction)

    result = await correct_classification(
        normalized_transaction_id=(
            current.normalized_transaction_id
            if current is not None
            else (transaction.id if transaction is not None else uuid4())
        ),
        expected_version=expected_version,
        corrected_transaction_type=transaction_type,
        corrected_account_number=account_number,
        corrected_counterparty_name=counterparty_name,
        reviewer_id="shishir",
        reviewed_at=REVIEWED_AT,
        reason="The bank description supports a different account.",
        notes="Reviewed against the source bank transaction.",
        chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
        classification_repository=classification_repository,
        transaction_reader=transaction_reader,
    )

    return (
        result,
        classification_repository,
        transaction_reader,
    )


@pytest.mark.asyncio
async def test_account_correction_appends_audit_history() -> None:
    """A valid account correction creates exactly one new version."""

    transaction = create_transaction()
    current = create_classification(transaction)

    result, repository, reader = await run_correction(
        current=current,
        transaction=transaction,
        account_number="6030",
        counterparty_name="Software Vendor",
    )

    corrected = result.classification

    assert result.updated is True
    assert corrected.normalized_transaction_id == transaction.id
    assert corrected.version == 2
    assert corrected.review_status is ReviewStatus.PENDING
    assert corrected.reviewer is None
    assert corrected.decision.transaction_type is (TransactionType.OPERATING_EXPENSE)
    assert corrected.decision.qbo_account.account_number == "6030"
    assert corrected.decision.qbo_account.account_name == ("Software & Subscriptions")
    assert corrected.decision.source is (ClassificationSource.MANUAL_REVIEW)
    assert corrected.decision.confidence_score == Decimal("1.000")
    assert corrected.decision.review_required is False
    assert corrected.decision.counterparty is not None
    assert corrected.decision.counterparty.normalized_name == ("Software Vendor")

    assert len(corrected.corrections) == 1
    correction = corrected.corrections[0]

    assert correction.from_version == 1
    assert correction.to_version == 2
    assert correction.previous_decision == current.decision
    assert correction.corrected_decision == corrected.decision
    assert correction.corrected_by.reviewer_id == "shishir"
    assert correction.corrected_by.reviewed_at == REVIEWED_AT
    assert repository.saved == [(corrected, 1)]
    assert reader.lookups == [transaction.id]


@pytest.mark.asyncio
async def test_transaction_type_and_account_can_change_together() -> None:
    """A fixed-asset correction uses the matching balance-sheet account."""

    transaction = create_transaction()
    current = create_classification(transaction)

    result, _, _ = await run_correction(
        current=current,
        transaction=transaction,
        transaction_type=(TransactionType.FIXED_ASSET_PURCHASE),
        account_number="1500",
    )

    assert result.classification.decision.transaction_type is (TransactionType.FIXED_ASSET_PURCHASE)
    assert result.classification.decision.qbo_account.account_number == "1500"


@pytest.mark.asyncio
async def test_unknown_account_is_rejected_before_persistence() -> None:
    """A reviewer cannot invent a chart-of-accounts number."""

    transaction = create_transaction()
    current = create_classification(transaction)
    repository = FakeClassificationRepository(current)

    with pytest.raises(
        InvalidManualCorrectionError,
        match="unknown or inactive account",
    ):
        await correct_classification(
            normalized_transaction_id=transaction.id,
            expected_version=1,
            corrected_transaction_type=(TransactionType.OPERATING_EXPENSE),
            corrected_account_number="9999",
            corrected_counterparty_name=None,
            reviewer_id="shishir",
            reviewed_at=REVIEWED_AT,
            reason="Use another account.",
            notes=None,
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
            classification_repository=repository,
            transaction_reader=FakeTransactionReader(transaction),
        )

    assert repository.saved == []


@pytest.mark.asyncio
async def test_direction_incompatible_type_is_rejected() -> None:
    """An outflow cannot be corrected into ordinary revenue."""

    transaction = create_transaction(direction=TransactionDirection.OUTFLOW)
    current = create_classification(transaction)

    with pytest.raises(
        InvalidManualCorrectionError,
        match="incompatible with transaction direction",
    ):
        await run_correction(
            current=current,
            transaction=transaction,
            transaction_type=TransactionType.REVENUE,
            account_number="4000",
        )


@pytest.mark.asyncio
async def test_account_type_mismatch_is_rejected() -> None:
    """An operating expense cannot use a fixed-asset account."""

    transaction = create_transaction()
    current = create_classification(transaction)

    with pytest.raises(
        InvalidManualCorrectionError,
        match="cannot use QuickBooks account type",
    ):
        await run_correction(
            current=current,
            transaction=transaction,
            transaction_type=(TransactionType.OPERATING_EXPENSE),
            account_number="1500",
        )


@pytest.mark.asyncio
async def test_missing_classification_is_rejected() -> None:
    """A correction cannot target a nonexistent classification."""

    transaction = create_transaction()
    repository = FakeClassificationRepository(None)

    with pytest.raises(
        ClassificationNotFoundError,
        match="has no classification",
    ):
        await correct_classification(
            normalized_transaction_id=transaction.id,
            expected_version=1,
            corrected_transaction_type=(TransactionType.OPERATING_EXPENSE),
            corrected_account_number="6030",
            corrected_counterparty_name=None,
            reviewer_id="shishir",
            reviewed_at=REVIEWED_AT,
            reason="Correct the account.",
            notes=None,
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
            classification_repository=repository,
            transaction_reader=FakeTransactionReader(transaction),
        )

    assert repository.saved == []


@pytest.mark.asyncio
async def test_missing_transaction_evidence_is_rejected() -> None:
    """A correction requires the original normalized bank record."""

    transaction = create_transaction()
    current = create_classification(transaction)
    repository = FakeClassificationRepository(current)

    with pytest.raises(
        UnsafeClassificationTransactionError,
        match="no normalized transaction evidence",
    ):
        await correct_classification(
            normalized_transaction_id=transaction.id,
            expected_version=1,
            corrected_transaction_type=(TransactionType.OPERATING_EXPENSE),
            corrected_account_number="6030",
            corrected_counterparty_name=None,
            reviewer_id="shishir",
            reviewed_at=REVIEWED_AT,
            reason="Correct the account.",
            notes=None,
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
            classification_repository=repository,
            transaction_reader=FakeTransactionReader(None),
        )

    assert repository.saved == []


@pytest.mark.asyncio
async def test_duplicate_transaction_evidence_is_rejected() -> None:
    """A duplicate row cannot receive a corrected classification."""

    canonical_id = uuid4()
    duplicate = create_transaction(
        status=RecordStatus.DUPLICATE,
        duplicate_of=canonical_id,
    )
    current = create_classification(duplicate)

    with pytest.raises(
        UnsafeClassificationTransactionError,
        match="valid canonical",
    ):
        await run_correction(
            current=current,
            transaction=duplicate,
        )


@pytest.mark.asyncio
async def test_stale_version_is_rejected_before_transaction_lookup() -> None:
    """An outdated correction page cannot overwrite a newer version."""

    transaction = create_transaction()
    current = create_classification(transaction)
    repository = FakeClassificationRepository(current)
    reader = FakeTransactionReader(transaction)

    with pytest.raises(
        StaleClassificationVersionError,
        match="stored version is 1",
    ):
        await correct_classification(
            normalized_transaction_id=transaction.id,
            expected_version=2,
            corrected_transaction_type=(TransactionType.OPERATING_EXPENSE),
            corrected_account_number="6030",
            corrected_counterparty_name=None,
            reviewer_id="shishir",
            reviewed_at=REVIEWED_AT,
            reason="Correct the account.",
            notes=None,
            chart_of_accounts=load_chart_of_accounts(CATALOG_PATH),
            classification_repository=repository,
            transaction_reader=reader,
        )

    assert reader.lookups == []
    assert repository.saved == []


@pytest.mark.asyncio
async def test_exact_persistence_retry_is_reported() -> None:
    """Repository correction idempotency remains visible."""

    transaction = create_transaction()
    current = create_classification(transaction)

    result, repository, _ = await run_correction(
        current=current,
        transaction=transaction,
        updated=False,
    )

    assert result.updated is False
    assert result.classification.version == 2
    assert len(repository.saved) == 1
