"""Integration tests for safe learned-pattern persistence."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.db.client import MongoDatabase
from app.models.classification import (
    ClassificationCorrection,
    ClassificationDecision,
    ClassificationSource,
    Counterparty,
    QuickBooksAccountMapping,
    ReviewerMetadata,
    ReviewStatus,
    TransactionClassification,
    TransactionType,
)
from app.models.classification_pattern import (
    ClassificationPatternKey,
    LearnedClassificationPattern,
)
from app.models.ingestion import (
    FileType,
    NormalizedTransaction,
    RawRecord,
    RecordStatus,
    TransactionDirection,
    UploadBatch,
)
from app.repositories.classification import ClassificationRepository
from app.repositories.classification_pattern import (
    ClassificationPatternConflictError,
    ClassificationPatternRepository,
    ClassificationPatternSourceNotFoundError,
    UnsafeClassificationPatternSourceError,
)
from app.repositories.ingestion import IngestionRepository

REVIEWED_AT = datetime(
    2026,
    7,
    25,
    18,
    0,
    tzinfo=UTC,
)


@pytest.fixture
async def repositories() -> AsyncIterator[
    tuple[
        ClassificationPatternRepository,
        ClassificationRepository,
        IngestionRepository,
    ]
]:
    """Create isolated pattern, classification, and ingestion repositories."""

    settings = get_settings()
    database_name = f"{settings.mongodb_database[:30]}_pat_{uuid4().hex[:16]}"
    mongodb = MongoDatabase(
        uri=settings.mongodb_uri,
        database_name=database_name,
    )

    ingestion_repository = IngestionRepository(mongodb.database)
    classification_repository = ClassificationRepository(mongodb.database)
    pattern_repository = ClassificationPatternRepository(mongodb.database)

    await ingestion_repository.ensure_indexes()
    await classification_repository.ensure_indexes()
    await pattern_repository.ensure_indexes()

    try:
        yield (
            pattern_repository,
            classification_repository,
            ingestion_repository,
        )
    finally:
        await mongodb.client.drop_database(database_name)
        await mongodb.close()


async def persist_valid_transaction(
    repository: IngestionRepository,
    *,
    sequence: int = 1,
    amount: Decimal = Decimal("-100.00"),
) -> NormalizedTransaction:
    """Persist one valid canonical transaction."""

    marker = str(sequence % 10)
    upload = UploadBatch(
        source_file_name=f"bank-{sequence}.csv",
        file_type=FileType.CSV,
        file_sha256=marker * 64,
        physical_record_count=1,
    )
    raw_record = RawRecord(
        upload_id=upload.id,
        source_file_name=upload.source_file_name,
        source_row_number=2,
        raw_values={
            "Date": f"2026-04-{sequence:02d}",
            "Description": "BrightFix Fuel Stop",
            "Amount": str(amount),
        },
        raw_hash=str((sequence + 1) % 10) * 64,
    )
    transaction = NormalizedTransaction(
        upload_id=upload.id,
        raw_record_id=raw_record.id,
        source_transaction_id=f"BF-202604-{sequence:04d}",
        transaction_date=date(2026, 4, sequence),
        description_original="BrightFix Fuel Stop",
        description_normalized="brightfix fuel stop",
        amount=amount,
        currency="USD",
        bank_account="Operating Checking",
        direction=TransactionDirection.OUTFLOW,
        fingerprint=str((sequence + 2) % 10) * 64,
        status=RecordStatus.VALID,
    )

    await repository.save_batch(
        upload=upload,
        raw_records=[raw_record],
        transactions=[transaction],
    )

    return transaction


def create_reviewer(
    *,
    reviewer_id: str = "shishir",
) -> ReviewerMetadata:
    return ReviewerMetadata(
        reviewer_id=reviewer_id,
        reviewed_at=REVIEWED_AT,
        notes="Approved after reviewing the source bank transaction.",
    )


def create_initial_classification(
    transaction: NormalizedTransaction,
    *,
    account_number: str = "6090",
    account_name: str = "Office & General",
) -> TransactionClassification:
    """Create the pre-correction classification."""

    return TransactionClassification(
        normalized_transaction_id=transaction.id,
        decision=ClassificationDecision(
            transaction_type=TransactionType.OPERATING_EXPENSE,
            counterparty=Counterparty(
                raw_name=transaction.description_original,
                normalized_name="BrightFix Fuel Stop",
            ),
            qbo_account=QuickBooksAccountMapping(
                account_number=account_number,
                account_name=account_name,
            ),
            confidence_score=Decimal("0.700"),
            explanation="The automated account mapping was uncertain.",
            source=ClassificationSource.GEMINI,
            review_required=True,
        ),
    )


def create_corrected_classification(
    initial: TransactionClassification,
    *,
    account_number: str = "6020",
    account_name: str = "Vehicle & Fuel",
) -> TransactionClassification:
    """Append one manual accounting correction."""

    reviewer = create_reviewer()
    corrected_decision = ClassificationDecision(
        transaction_type=TransactionType.OPERATING_EXPENSE,
        counterparty=initial.decision.counterparty,
        qbo_account=QuickBooksAccountMapping(
            account_number=account_number,
            account_name=account_name,
        ),
        confidence_score=Decimal("1.000"),
        explanation="A reviewer confirmed the payment was vehicle fuel.",
        source=ClassificationSource.MANUAL_REVIEW,
        review_required=False,
    )
    correction = ClassificationCorrection(
        from_version=1,
        to_version=2,
        previous_decision=initial.decision,
        corrected_decision=corrected_decision,
        corrected_by=reviewer,
        reason="Correct the expense account using the bank description.",
    )

    return TransactionClassification(
        normalized_transaction_id=initial.normalized_transaction_id,
        version=2,
        decision=corrected_decision,
        review_status=ReviewStatus.PENDING,
        corrections=(correction,),
    )


def create_approved_classification(
    corrected: TransactionClassification,
) -> TransactionClassification:
    """Approve a corrected accounting decision."""

    return TransactionClassification(
        normalized_transaction_id=corrected.normalized_transaction_id,
        version=corrected.version,
        decision=corrected.decision,
        review_status=ReviewStatus.APPROVED,
        reviewer=create_reviewer(),
        corrections=corrected.corrections,
    )


async def persist_approved_source(
    classification_repository: ClassificationRepository,
    ingestion_repository: IngestionRepository,
    *,
    sequence: int = 1,
    amount: Decimal = Decimal("-100.00"),
    corrected_account_number: str = "6020",
    corrected_account_name: str = "Vehicle & Fuel",
) -> tuple[
    NormalizedTransaction,
    TransactionClassification,
]:
    """Persist one canonical transaction and its approved correction."""

    transaction = await persist_valid_transaction(
        ingestion_repository,
        sequence=sequence,
        amount=amount,
    )
    initial = create_initial_classification(transaction)
    corrected = create_corrected_classification(
        initial,
        account_number=corrected_account_number,
        account_name=corrected_account_name,
    )
    approved = create_approved_classification(corrected)

    await classification_repository.save_initial(initial)
    await classification_repository.save_correction(
        corrected,
        expected_version=1,
    )
    await classification_repository.save_review(
        approved,
        expected_version=2,
    )

    return transaction, approved


def create_pattern(
    transaction: NormalizedTransaction,
    approved: TransactionClassification,
    *,
    key: ClassificationPatternKey | None = None,
    approved_by: ReviewerMetadata | None = None,
) -> LearnedClassificationPattern:
    """Create a reusable pattern from an approved manual correction."""

    source_correction = approved.corrections[-1]
    corrected = source_correction.corrected_decision

    return LearnedClassificationPattern(
        key=key
        or ClassificationPatternKey(
            description_normalized=transaction.description_normalized,
            bank_account=transaction.bank_account,
            direction=transaction.direction,
            currency=transaction.currency,
        ),
        decision=ClassificationDecision(
            transaction_type=corrected.transaction_type,
            counterparty=corrected.counterparty,
            qbo_account=QuickBooksAccountMapping(
                account_number=corrected.qbo_account.account_number,
                account_name=corrected.qbo_account.account_name,
            ),
            confidence_score=Decimal("1.000"),
            explanation=("Matched an exact pattern learned from an approved correction."),
            source=ClassificationSource.STORED_CORRECTION,
            review_required=False,
        ),
        source_transaction_id=transaction.id,
        source_classification_version=approved.version,
        source_correction=source_correction,
        source_review_status=ReviewStatus.APPROVED,
        approved_by=approved_by or approved.reviewer,
        learned_at=REVIEWED_AT + timedelta(seconds=1),
    )


@pytest.mark.asyncio
async def test_pattern_indexes_are_idempotent(
    repositories: tuple[
        ClassificationPatternRepository,
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """Startup may safely ensure all pattern indexes repeatedly."""

    pattern_repository, _, _ = repositories

    await pattern_repository.ensure_indexes()
    await pattern_repository.ensure_indexes()

    indexes = await pattern_repository.patterns.index_information()
    active_key_index = indexes["ux_classification_pattern_active_key"]

    assert active_key_index["unique"] is True
    assert active_key_index["partialFilterExpression"] == {"active": True}
    assert "ix_classification_pattern_source" in indexes
    assert "ix_classification_pattern_account_type" in indexes


@pytest.mark.asyncio
async def test_save_is_idempotent_and_exact_lookup_round_trips(
    repositories: tuple[
        ClassificationPatternRepository,
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """An exact retry stores one pattern and exact lookup returns it."""

    (
        pattern_repository,
        classification_repository,
        ingestion_repository,
    ) = repositories
    transaction, approved = await persist_approved_source(
        classification_repository,
        ingestion_repository,
    )
    pattern = create_pattern(transaction, approved)

    first = await pattern_repository.save(pattern)
    retry = await pattern_repository.save(pattern)
    stored = await pattern_repository.find_active(pattern.key)
    stored_by_id = await pattern_repository.find_by_id(pattern.id)

    assert first is True
    assert retry is False
    assert stored == pattern
    assert stored_by_id == pattern
    assert await pattern_repository.patterns.count_documents({}) == 1


@pytest.mark.asyncio
async def test_missing_source_classification_is_rejected(
    repositories: tuple[
        ClassificationPatternRepository,
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """Embedded provenance cannot replace a real stored classification."""

    pattern_repository, _, ingestion_repository = repositories
    transaction = await persist_valid_transaction(ingestion_repository)
    initial = create_initial_classification(transaction)
    corrected = create_corrected_classification(initial)
    approved = create_approved_classification(corrected)
    pattern = create_pattern(transaction, approved)

    with pytest.raises(
        ClassificationPatternSourceNotFoundError,
        match="has no classification",
    ):
        await pattern_repository.save(pattern)


@pytest.mark.asyncio
async def test_pending_corrected_source_is_rejected(
    repositories: tuple[
        ClassificationPatternRepository,
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """A correction cannot be learned before final approval."""

    (
        pattern_repository,
        classification_repository,
        ingestion_repository,
    ) = repositories
    transaction = await persist_valid_transaction(ingestion_repository)
    initial = create_initial_classification(transaction)
    corrected = create_corrected_classification(initial)
    simulated_approval = create_approved_classification(corrected)
    pattern = create_pattern(transaction, simulated_approval)

    await classification_repository.save_initial(initial)
    await classification_repository.save_correction(
        corrected,
        expected_version=1,
    )

    with pytest.raises(
        UnsafeClassificationPatternSourceError,
        match="approved classification",
    ):
        await pattern_repository.save(pattern)


@pytest.mark.asyncio
async def test_pattern_key_must_match_source_transaction(
    repositories: tuple[
        ClassificationPatternRepository,
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """A valid approval cannot be attached to an unrelated match key."""

    (
        pattern_repository,
        classification_repository,
        ingestion_repository,
    ) = repositories
    transaction, approved = await persist_approved_source(
        classification_repository,
        ingestion_repository,
    )
    forged_key = ClassificationPatternKey(
        description_normalized="unrelated merchant",
        bank_account=transaction.bank_account,
        direction=transaction.direction,
        currency=transaction.currency,
    )
    pattern = create_pattern(
        transaction,
        approved,
        key=forged_key,
    )

    with pytest.raises(
        UnsafeClassificationPatternSourceError,
        match="does not match the stored source transaction",
    ):
        await pattern_repository.save(pattern)


@pytest.mark.asyncio
async def test_approval_metadata_must_match_stored_review(
    repositories: tuple[
        ClassificationPatternRepository,
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """A client cannot invent the identity of the final approver."""

    (
        pattern_repository,
        classification_repository,
        ingestion_repository,
    ) = repositories
    transaction, approved = await persist_approved_source(
        classification_repository,
        ingestion_repository,
    )
    pattern = create_pattern(
        transaction,
        approved,
        approved_by=create_reviewer(reviewer_id="different-reviewer"),
    )

    with pytest.raises(
        UnsafeClassificationPatternSourceError,
        match="approval metadata",
    ):
        await pattern_repository.save(pattern)


@pytest.mark.asyncio
async def test_conflicting_active_exact_key_is_rejected(
    repositories: tuple[
        ClassificationPatternRepository,
        ClassificationRepository,
        IngestionRepository,
    ],
) -> None:
    """Only one active accounting answer may exist for an exact key."""

    (
        pattern_repository,
        classification_repository,
        ingestion_repository,
    ) = repositories
    first_transaction, first_approved = await persist_approved_source(
        classification_repository,
        ingestion_repository,
        sequence=1,
        amount=Decimal("-100.00"),
        corrected_account_number="6020",
        corrected_account_name="Vehicle & Fuel",
    )
    second_transaction, second_approved = await persist_approved_source(
        classification_repository,
        ingestion_repository,
        sequence=2,
        amount=Decimal("-125.00"),
        corrected_account_number="6090",
        corrected_account_name="Office & General",
    )

    first_pattern = create_pattern(
        first_transaction,
        first_approved,
    )
    conflicting_pattern = create_pattern(
        second_transaction,
        second_approved,
    )

    assert first_pattern.key == conflicting_pattern.key

    await pattern_repository.save(first_pattern)

    with pytest.raises(
        ClassificationPatternConflictError,
        match="active learned pattern already exists",
    ):
        await pattern_repository.save(conflicting_pattern)

    stored = await pattern_repository.find_active(first_pattern.key)
    assert stored == first_pattern
