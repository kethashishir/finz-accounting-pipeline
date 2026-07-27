"""Tests for immutable QuickBooks synchronization contracts."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.models.quickbooks import QuickBooksEnvironment
from app.models.quickbooks_sync import (
    QuickBooksJournalEntryPlan,
    QuickBooksJournalLine,
    QuickBooksPostingType,
    QuickBooksSourceReference,
    QuickBooksSyncError,
    QuickBooksSyncRecord,
    QuickBooksSyncStatus,
    build_quickbooks_request_id,
)

REALM_ID = "9341456789012345"

SOURCE_ID = UUID("11111111-1111-4111-8111-111111111111")
SECOND_SOURCE_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime(
    2026,
    7,
    27,
    7,
    0,
    tzinfo=UTC,
)


def source(
    transaction_id: UUID = SOURCE_ID,
    *,
    version: int = 1,
) -> QuickBooksSourceReference:
    """Create one immutable source reference."""

    return QuickBooksSourceReference(
        normalized_transaction_id=transaction_id,
        classification_version=version,
        source_transaction_id="BF-202604-0001",
    )


def line(
    *,
    number: str,
    name: str,
    qbo_id: str,
    posting_type: QuickBooksPostingType,
    amount: Decimal = Decimal("100.00"),
) -> QuickBooksJournalLine:
    """Create one journal line."""

    return QuickBooksJournalLine(
        account_number=number,
        account_name=name,
        qbo_account_id=qbo_id,
        posting_type=posting_type,
        amount=amount,
        description="Finz challenge bank transaction",
    )


def plan(
    *,
    sources: tuple[
        QuickBooksSourceReference,
        ...,
    ]
    | None = None,
) -> QuickBooksJournalEntryPlan:
    """Create one balanced revenue posting plan."""

    selected_sources = sources or (source(),)

    return QuickBooksJournalEntryPlan(
        request_id=build_quickbooks_request_id(
            tuple(item.normalized_transaction_id for item in selected_sources)
        ),
        sources=selected_sources,
        transaction_date=date(2026, 4, 1),
        currency="usd",
        private_note=("Finz source transaction BF-202604-0001"),
        lines=(
            line(
                number="1000",
                name="Operating Checking",
                qbo_id="qbo-bank-1000",
                posting_type=(QuickBooksPostingType.DEBIT),
            ),
            line(
                number="4000",
                name="Repair Service Revenue",
                qbo_id="qbo-income-4000",
                posting_type=(QuickBooksPostingType.CREDIT),
            ),
        ),
    )


def test_request_id_is_stable_order_independent_and_short() -> None:
    """A grouped retry always uses the same Intuit request ID."""

    first = build_quickbooks_request_id(
        (
            SOURCE_ID,
            SECOND_SOURCE_ID,
        )
    )
    reordered = build_quickbooks_request_id(
        (
            SECOND_SOURCE_ID,
            SOURCE_ID,
        )
    )

    assert first == reordered
    assert first.startswith("finz-je-")
    assert len(first) <= 50


def test_request_id_rejects_duplicate_sources() -> None:
    """One posting group cannot repeat the same source row."""

    with pytest.raises(
        ValueError,
        match="must be unique",
    ):
        build_quickbooks_request_id(
            (
                SOURCE_ID,
                SOURCE_ID,
            )
        )


def test_journal_line_rejects_float_money() -> None:
    """Floating-point values never enter QBO posting plans."""

    with pytest.raises(
        ValidationError,
        match="floating-point",
    ):
        line(
            number="1000",
            name="Operating Checking",
            qbo_id="qbo-bank",
            posting_type=QuickBooksPostingType.DEBIT,
            amount=100.0,
        )


def test_balanced_plan_is_accepted_and_normalizes_currency() -> None:
    """A two-line debit and credit plan is immutable and balanced."""

    posting_plan = plan()

    assert posting_plan.currency == "USD"
    assert posting_plan.lines[0].amount == Decimal("100.00")
    assert posting_plan.request_id == (build_quickbooks_request_id((SOURCE_ID,)))


def test_two_source_transfer_plan_is_supported() -> None:
    """Paired bank rows can share one QBO posting identity."""

    posting_plan = plan(
        sources=(
            source(SOURCE_ID),
            source(
                SECOND_SOURCE_ID,
                version=2,
            ),
        )
    )

    assert len(posting_plan.sources) == 2
    assert posting_plan.request_id == (
        build_quickbooks_request_id(
            (
                SOURCE_ID,
                SECOND_SOURCE_ID,
            )
        )
    )


def test_plan_rejects_nondeterministic_request_id() -> None:
    """A caller cannot substitute a new ID during a retry."""

    with pytest.raises(
        ValidationError,
        match="must be derived",
    ):
        QuickBooksJournalEntryPlan(
            request_id="different-request-id",
            sources=(source(),),
            transaction_date=date(2026, 4, 1),
            currency="USD",
            private_note="Invalid request identity",
            lines=plan().lines,
        )


def test_plan_rejects_unbalanced_entry() -> None:
    """Every journal entry must have equal debit and credit totals."""

    with pytest.raises(
        ValidationError,
        match="not balanced",
    ):
        QuickBooksJournalEntryPlan(
            request_id=build_quickbooks_request_id((SOURCE_ID,)),
            sources=(source(),),
            transaction_date=date(2026, 4, 1),
            currency="USD",
            private_note="Unbalanced posting",
            lines=(
                line(
                    number="1000",
                    name="Operating Checking",
                    qbo_id="qbo-bank",
                    posting_type=(QuickBooksPostingType.DEBIT),
                    amount=Decimal("100.00"),
                ),
                line(
                    number="4000",
                    name="Repair Service Revenue",
                    qbo_id="qbo-income",
                    posting_type=(QuickBooksPostingType.CREDIT),
                    amount=Decimal("99.99"),
                ),
            ),
        )


def test_pending_record_has_no_attempt_or_qbo_result() -> None:
    """A newly planned posting has not contacted QuickBooks."""

    record = QuickBooksSyncRecord(
        environment=QuickBooksEnvironment.SANDBOX,
        realm_id=REALM_ID,
        plan=plan(),
        created_at=NOW,
        updated_at=NOW,
    )

    assert record.status is QuickBooksSyncStatus.PENDING
    assert record.attempt_count == 0
    assert record.qbo_transaction_id is None
    assert record.normalized_transaction_ids == (SOURCE_ID,)


def test_success_requires_qbo_identity() -> None:
    """A record cannot claim success without QBO evidence."""

    with pytest.raises(
        ValidationError,
        match="transaction ID and sync token",
    ):
        QuickBooksSyncRecord(
            environment=QuickBooksEnvironment.SANDBOX,
            realm_id=REALM_ID,
            plan=plan(),
            status=QuickBooksSyncStatus.SUCCEEDED,
            attempt_count=1,
            created_at=NOW,
            updated_at=NOW,
        )


def test_retryable_error_and_success_states_are_valid() -> None:
    """Retryable failures remain safe and success clears errors."""

    retryable = QuickBooksSyncRecord(
        environment=QuickBooksEnvironment.SANDBOX,
        realm_id=REALM_ID,
        plan=plan(),
        status=QuickBooksSyncStatus.RETRYABLE_ERROR,
        attempt_count=1,
        last_error=QuickBooksSyncError(
            code="http_429",
            message="QuickBooks temporarily throttled the request.",
            retryable=True,
            occurred_at=NOW,
        ),
        created_at=NOW,
        updated_at=NOW,
    )

    succeeded = QuickBooksSyncRecord(
        environment=QuickBooksEnvironment.SANDBOX,
        realm_id=REALM_ID,
        id=uuid4(),
        plan=plan(),
        status=QuickBooksSyncStatus.SUCCEEDED,
        attempt_count=2,
        qbo_transaction_id="qbo-journal-entry-123",
        qbo_sync_token="0",
        created_at=NOW,
        updated_at=NOW,
    )

    assert retryable.last_error is not None
    assert retryable.last_error.retryable is True
    assert succeeded.qbo_transaction_id == ("qbo-journal-entry-123")
    assert succeeded.last_error is None
