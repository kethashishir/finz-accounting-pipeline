"""Tests for QBO API access and chart-of-accounts setup."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs

import httpx2
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.services.accounting.chart_of_accounts import (
    load_chart_of_accounts,
)
from app.services.quickbooks.account_setup import (
    QuickBooksAccountSetupError,
    setup_quickbooks_chart_of_accounts,
)
from app.services.quickbooks.api_client import (
    QuickBooksApiAccount,
    QuickBooksApiClient,
    QuickBooksApiProviderError,
)
from app.services.quickbooks.oauth_config import (
    build_quickbooks_oauth_configuration,
)

CATALOG_PATH = Path("sample_config/chart_of_accounts.json")
REALM_ID = "9341456789012345"
ACCESS_TOKEN = SecretStr("test-access-token")


def configuration():
    """Create test-only OAuth configuration."""

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


def account(
    *,
    identifier: str,
    name: str,
    account_type: str,
    account_number: str | None = None,
    account_sub_type: str | None = None,
    active: bool = True,
) -> QuickBooksApiAccount:
    """Create one fake QBO account."""

    return QuickBooksApiAccount.model_validate(
        {
            "Id": identifier,
            "SyncToken": "0",
            "Name": name,
            "AcctNum": account_number,
            "AccountType": account_type,
            "AccountSubType": account_sub_type,
            "Active": active,
        }
    )


class FakeAccountClient:
    """Store QBO account operations in memory."""

    def __init__(
        self,
        accounts: list[QuickBooksApiAccount] | None = None,
    ) -> None:
        self.accounts = list(accounts or [])
        self.create_calls = 0
        self.update_calls = 0
        self.create_payloads: list[dict[str, object]] = []

    async def query_accounts(
        self,
        *,
        access_token: SecretStr,
        realm_id: str,
    ) -> tuple[QuickBooksApiAccount, ...]:
        return tuple(self.accounts)

    async def create_account(
        self,
        *,
        access_token: SecretStr,
        realm_id: str,
        payload: dict[str, object],
    ) -> QuickBooksApiAccount:
        self.create_calls += 1
        self.create_payloads.append(payload)
        created = QuickBooksApiAccount.model_validate(
            {
                "Id": str(1000 + self.create_calls),
                "SyncToken": "0",
                **payload,
            }
        )
        self.accounts.append(created)
        return created

    async def update_account(
        self,
        *,
        access_token: SecretStr,
        realm_id: str,
        payload: dict[str, object],
    ) -> QuickBooksApiAccount:
        self.update_calls += 1
        identifier = str(payload["Id"])
        current = next(item for item in self.accounts if item.id == identifier)
        values = current.model_dump(by_alias=True)
        values.update(
            {
                key: value
                for key, value in payload.items()
                if key
                not in {
                    "Id",
                    "SyncToken",
                    "sparse",
                }
            }
        )
        values["SyncToken"] = str(int(current.sync_token) + 1)
        updated = QuickBooksApiAccount.model_validate(values)
        self.accounts = [updated if item.id == identifier else item for item in self.accounts]
        return updated


async def test_api_client_queries_company_and_accounts() -> None:
    """The API adapter uses bearer auth and minor version 75."""

    requests: list[httpx2.Request] = []

    async def handler(
        request: httpx2.Request,
    ) -> httpx2.Response:
        requests.append(request)

        if "/companyinfo/" in str(request.url):
            return httpx2.Response(
                200,
                json={
                    "CompanyInfo": {
                        "Id": REALM_ID,
                        "SyncToken": "1",
                        "CompanyName": ("BrightFix Home Services LLC"),
                        "Country": "US",
                    }
                },
            )

        return httpx2.Response(
            200,
            json={
                "QueryResponse": {
                    "Account": [
                        {
                            "Id": "10",
                            "SyncToken": "0",
                            "Name": "Operating Checking",
                            "AcctNum": "1000",
                            "AccountType": "Bank",
                            "AccountSubType": "Checking",
                            "Active": True,
                        }
                    ]
                }
            },
        )

    client = QuickBooksApiClient(
        configuration=configuration(),
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        owns_client=True,
    )

    async with client:
        company = await client.get_company_info(
            access_token=ACCESS_TOKEN,
            realm_id=REALM_ID,
        )
        accounts = await client.query_accounts(
            access_token=ACCESS_TOKEN,
            realm_id=REALM_ID,
        )

    assert company.company_name == ("BrightFix Home Services LLC")
    assert accounts[0].account_number == "1000"
    assert len(requests) == 2

    for request in requests:
        assert request.headers["authorization"] == ("Bearer test-access-token")
        assert request.url.params["minorversion"] == "75"

    query = parse_qs(requests[1].url.query.decode())["query"][0]

    assert "FROM Account" in query


async def test_api_provider_error_is_secret_safe() -> None:
    """QBO failures omit tokens and response bodies."""

    async def handler(
        request: httpx2.Request,
    ) -> httpx2.Response:
        return httpx2.Response(
            401,
            headers={
                "intuit_tid": "test-tid",
            },
            json={
                "token": "do-not-leak",
            },
        )

    client = QuickBooksApiClient(
        configuration=configuration(),
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        owns_client=True,
    )

    async with client:
        with pytest.raises(
            QuickBooksApiProviderError,
            match="HTTP 401",
        ) as error:
            await client.query_accounts(
                access_token=ACCESS_TOKEN,
                realm_id=REALM_ID,
            )

    message = str(error.value)

    assert "test-tid" in message
    assert "test-access-token" not in message
    assert "do-not-leak" not in message


async def test_empty_sandbox_creates_all_required_accounts() -> None:
    """All 21 workbook accounts are created serially."""

    client = FakeAccountClient()
    catalog = load_chart_of_accounts(CATALOG_PATH)

    result = await setup_quickbooks_chart_of_accounts(
        client=client,
        access_token=ACCESS_TOKEN,
        realm_id=REALM_ID,
        catalog=catalog,
    )

    assert result.configured_count == 21
    assert len(result.created) == 21
    assert client.create_calls == 21
    assert client.update_calls == 0

    subcontractor_payload = next(
        payload for payload in client.create_payloads if payload["AcctNum"] == "5010"
    )

    assert subcontractor_payload["AccountType"] == ("Cost of Goods Sold")
    assert subcontractor_payload["AccountSubType"] == ("CostOfLaborCos")


async def test_second_setup_run_is_idempotent() -> None:
    """A repeated setup does not duplicate QBO accounts."""

    client = FakeAccountClient()
    catalog = load_chart_of_accounts(CATALOG_PATH)

    await setup_quickbooks_chart_of_accounts(
        client=client,
        access_token=ACCESS_TOKEN,
        realm_id=REALM_ID,
        catalog=catalog,
    )
    retry = await setup_quickbooks_chart_of_accounts(
        client=client,
        access_token=ACCESS_TOKEN,
        realm_id=REALM_ID,
        catalog=catalog,
    )

    assert retry.configured_count == 21
    assert len(retry.created) == 0
    assert len(retry.updated) == 0
    assert len(retry.reused) == 21
    assert client.create_calls == 21


async def test_existing_account_is_numbered_and_difference_recorded() -> None:
    """Compatible default accounts are reused safely."""

    existing = account(
        identifier="existing-utilities",
        name="Utilities",
        account_type="Expense",
        account_sub_type="OtherMiscellaneousExpense",
    )
    client = FakeAccountClient([existing])
    catalog = load_chart_of_accounts(CATALOG_PATH)

    result = await setup_quickbooks_chart_of_accounts(
        client=client,
        access_token=ACCESS_TOKEN,
        realm_id=REALM_ID,
        catalog=catalog,
    )

    assert "6060" in result.updated
    assert client.update_calls == 1
    assert any(
        difference.startswith("6060 Utilities:") for difference in result.detail_type_differences
    )


async def test_incompatible_existing_account_is_rejected() -> None:
    """An exact name with the wrong broad type is unsafe."""

    existing = account(
        identifier="wrong-utilities",
        name="Utilities",
        account_type="Income",
        account_sub_type="ServiceFeeIncome",
    )
    client = FakeAccountClient([existing])
    catalog = load_chart_of_accounts(CATALOG_PATH)

    with pytest.raises(
        QuickBooksAccountSetupError,
        match="expected 'Expense'",
    ):
        await setup_quickbooks_chart_of_accounts(
            client=client,
            access_token=ACCESS_TOKEN,
            realm_id=REALM_ID,
            catalog=catalog,
        )
