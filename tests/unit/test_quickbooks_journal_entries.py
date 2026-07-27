"""Tests for QBO JournalEntry payloads and transport."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from uuid import UUID

import httpx2
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.models.quickbooks_sync import (
    QuickBooksJournalEntryPlan,
    QuickBooksJournalLine,
    QuickBooksPostingType,
    QuickBooksSourceReference,
    build_quickbooks_request_id,
)
from app.services.quickbooks.api_client import (
    QuickBooksApiClient,
)
from app.services.quickbooks.journal_entries import (
    QuickBooksJournalEntryPayloadError,
    build_quickbooks_journal_entry_payload,
)
from app.services.quickbooks.oauth_config import (
    build_quickbooks_oauth_configuration,
)

REALM_ID = "9341456789012345"
SOURCE_ID = UUID("11111111-1111-4111-8111-111111111111")
ACCESS_TOKEN = SecretStr("test-access-token")


def configuration():
    """Create test-only QBO configuration."""

    return build_quickbooks_oauth_configuration(
        Settings(
            _env_file=None,
            qbo_environment="sandbox",
            qbo_client_id="client-id",
            qbo_client_secret="client-secret",
            qbo_redirect_uri=("http://localhost:8000/api/v1/quickbooks/callback"),
            token_encryption_key=("token-encryption-key-0123456789abcdef"),
            session_secret=("session-secret-key-0123456789abcdef"),
        )
    )


def posting_plan(
    *,
    currency: str = "USD",
) -> QuickBooksJournalEntryPlan:
    """Create one balanced cash-receipt plan."""

    request_id = build_quickbooks_request_id((SOURCE_ID,))

    return QuickBooksJournalEntryPlan(
        request_id=request_id,
        sources=(
            QuickBooksSourceReference(
                normalized_transaction_id=SOURCE_ID,
                classification_version=3,
                source_transaction_id=("BF-202604-0001"),
            ),
        ),
        transaction_date=date(2026, 4, 1),
        currency=currency,
        private_note=("Finz source transaction BF-202604-0001"),
        lines=(
            QuickBooksJournalLine(
                account_number="1000",
                account_name="Operating Checking",
                qbo_account_id="qbo-bank-1000",
                posting_type=(QuickBooksPostingType.DEBIT),
                amount=Decimal("100.25"),
                description="Customer receipt",
            ),
            QuickBooksJournalLine(
                account_number="4000",
                account_name="Repair Service Revenue",
                qbo_account_id="qbo-income-4000",
                posting_type=(QuickBooksPostingType.CREDIT),
                amount=Decimal("100.25"),
                description="Repair service revenue",
            ),
        ),
    )


def test_payload_maps_balanced_lines_exactly() -> None:
    """The builder emits the QBO JournalEntry line shape."""

    payload = build_quickbooks_journal_entry_payload(posting_plan())

    assert payload["TxnDate"] == "2026-04-01"
    assert payload["PrivateNote"] == ("Finz source transaction BF-202604-0001")

    lines = payload["Line"]

    assert isinstance(lines, list)
    assert len(lines) == 2

    debit = lines[0]
    credit = lines[1]

    assert debit["Amount"] == 100.25
    assert debit["DetailType"] == ("JournalEntryLineDetail")
    assert debit["JournalEntryLineDetail"] == {
        "PostingType": "Debit",
        "AccountRef": {
            "value": "qbo-bank-1000",
        },
    }
    assert credit["JournalEntryLineDetail"] == {
        "PostingType": "Credit",
        "AccountRef": {
            "value": "qbo-income-4000",
        },
    }

    assert Decimal(str(debit["Amount"])) == Decimal("100.25")
    assert Decimal(str(credit["Amount"])) == Decimal("100.25")


def test_whole_dollar_amount_uses_json_integer() -> None:
    """Whole-dollar Decimal values need no floating conversion."""

    plan = posting_plan()
    updated_lines = tuple(
        line.model_copy(
            update={
                "amount": Decimal("100.00"),
            }
        )
        for line in plan.lines
    )
    whole_dollar_plan = plan.model_copy(
        update={
            "lines": updated_lines,
        }
    )

    payload = build_quickbooks_journal_entry_payload(whole_dollar_plan)
    lines = payload["Line"]

    assert isinstance(lines, list)
    assert lines[0]["Amount"] == 100
    assert isinstance(lines[0]["Amount"], int)


def test_foreign_currency_is_rejected() -> None:
    """Amounts cannot silently post in the wrong home currency."""

    with pytest.raises(
        QuickBooksJournalEntryPayloadError,
        match="Foreign-currency",
    ):
        build_quickbooks_journal_entry_payload(posting_plan(currency="EUR"))


async def test_api_client_creates_idempotent_journal_entry() -> None:
    """The transport sends requestid and validates QBO evidence."""

    requests: list[httpx2.Request] = []
    plan = posting_plan()
    payload = build_quickbooks_journal_entry_payload(plan)

    async def handler(
        request: httpx2.Request,
    ) -> httpx2.Response:
        requests.append(request)

        return httpx2.Response(
            200,
            json={
                "JournalEntry": {
                    "Id": "qbo-je-123",
                    "SyncToken": "0",
                    "TxnDate": "2026-04-01",
                    "PrivateNote": ("Finz source transaction BF-202604-0001"),
                }
            },
        )

    client = QuickBooksApiClient(
        configuration=configuration(),
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        owns_client=True,
    )

    async with client:
        result = await client.create_journal_entry(
            access_token=ACCESS_TOKEN,
            realm_id=REALM_ID,
            request_id=plan.request_id,
            payload=payload,
        )

    assert result.id == "qbo-je-123"
    assert result.sync_token == "0"
    assert result.transaction_date == date(
        2026,
        4,
        1,
    )
    assert len(requests) == 1

    request = requests[0]

    assert request.method == "POST"
    assert request.url.path.endswith(f"/v3/company/{REALM_ID}/journalentry")
    assert request.url.params["minorversion"] == "75"
    assert request.url.params["requestid"] == plan.request_id
    assert request.headers["authorization"] == ("Bearer test-access-token")

    sent_payload = json.loads(request.content.decode("utf-8"))

    assert sent_payload == payload
