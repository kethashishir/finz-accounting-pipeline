"""Idempotent QuickBooks sandbox chart-of-accounts setup."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr

from app.models.accounting import (
    ChartOfAccount,
    ChartOfAccountsConfig,
    QBOAccountType,
)
from app.services.quickbooks.api_client import (
    QuickBooksApiAccount,
    QuickBooksApiClient,
    QuickBooksApiError,
)

ACCOUNT_TYPE_MAP = {
    QBOAccountType.BANK: "Bank",
    QBOAccountType.FIXED_ASSETS: "Fixed Asset",
    QBOAccountType.EQUITY: "Equity",
    QBOAccountType.INCOME: "Income",
    QBOAccountType.COST_OF_GOODS_SOLD: ("Cost of Goods Sold"),
    QBOAccountType.EXPENSES: "Expense",
}

DETAIL_TYPE_MAP = {
    "Checking": "Checking",
    "Savings": "Savings",
    "Machinery and Equipment": ("MachineryAndEquipment"),
    "Owner's Equity": "OwnersEquity",
    "Service/Fee Income": "ServiceFeeIncome",
    "Discounts/Refunds Given": ("DiscountsRefundsGiven"),
    "Supplies & Materials - COGS": ("SuppliesMaterialsCogs"),
    "Cost of Labor": "CostOfLaborCos",
    "Payroll Expenses": "PayrollExpenses",
    "Rent or Lease of Buildings": ("RentOrLeaseOfBuildings"),
    "Auto": "Auto",
    "Dues & Subscriptions": "DuesSubscriptions",
    "Advertising/Promotional": ("AdvertisingPromotional"),
    "Insurance": "Insurance",
    "Utilities": "Utilities",
    "Legal & Professional Fees": ("LegalProfessionalFees"),
    "Bank Charges": "BankCharges",
    "Office/General Administrative Expenses": ("OfficeGeneralAdministrativeExpenses"),
    "Repair & Maintenance": "RepairMaintenance",
}


class QuickBooksAccountSetupError(RuntimeError):
    """The QBO chart of accounts cannot be configured safely."""


@dataclass(frozen=True, slots=True)
class QuickBooksAccountSetupResult:
    """Summary of one idempotent account-setup run."""

    created: tuple[str, ...]
    updated: tuple[str, ...]
    reused: tuple[str, ...]
    detail_type_differences: tuple[str, ...]
    accounts: tuple[QuickBooksApiAccount, ...]

    @property
    def configured_count(self) -> int:
        """Return the number of validated required accounts."""

        return len(self.accounts)


async def setup_quickbooks_chart_of_accounts(
    *,
    client: QuickBooksApiClient,
    access_token: SecretStr,
    realm_id: str,
    catalog: ChartOfAccountsConfig,
) -> QuickBooksAccountSetupResult:
    """Create, update, or reuse every active catalog account."""

    existing = list(
        await client.query_accounts(
            access_token=access_token,
            realm_id=realm_id,
        )
    )
    created: list[str] = []
    updated: list[str] = []
    reused: list[str] = []
    differences: list[str] = []

    for desired in catalog.accounts:
        if not desired.active:
            continue

        by_number = _find_by_number(
            existing,
            desired.number,
        )
        by_name = _find_by_name(
            existing,
            desired.name,
        )

        if by_number is not None and by_name is not None and by_number.id != by_name.id:
            raise QuickBooksAccountSetupError(
                "QuickBooks account number "
                f"{desired.number} and name "
                f"{desired.name!r} belong to different "
                "existing accounts"
            )

        current = by_number or by_name

        if current is None:
            try:
                created_account = await client.create_account(
                    access_token=access_token,
                    realm_id=realm_id,
                    payload=_create_payload(desired),
                )
            except QuickBooksApiError as exc:
                raise QuickBooksAccountSetupError(
                    f"Failed to create QBO account {desired.number} {desired.name!r}: {exc}"
                ) from exc

            existing.append(created_account)
            created.append(desired.number)
            continue

        _validate_compatible_account(
            current=current,
            desired=desired,
        )

        expected_subtype = _detail_type(desired)

        if current.account_sub_type != expected_subtype:
            differences.append(
                f"{desired.number} {desired.name}: "
                f"requested {expected_subtype}, "
                "QBO uses "
                f"{current.account_sub_type or 'unspecified'}"
            )

        needs_update = current.account_number != desired.number or not current.active

        if needs_update:
            try:
                changed = await client.update_account(
                    access_token=access_token,
                    realm_id=realm_id,
                    payload={
                        "Id": current.id,
                        "SyncToken": current.sync_token,
                        "sparse": True,
                        "AcctNum": desired.number,
                        "Active": True,
                        "Description": desired.purpose,
                    },
                )
            except QuickBooksApiError as exc:
                raise QuickBooksAccountSetupError(
                    f"Failed to update QBO account {desired.number} {desired.name!r}: {exc}"
                ) from exc

            existing = [changed if item.id == changed.id else item for item in existing]
            updated.append(desired.number)
        else:
            reused.append(desired.number)

    final_accounts = await client.query_accounts(
        access_token=access_token,
        realm_id=realm_id,
    )
    configured = tuple(
        _require_final_account(
            final_accounts,
            desired=desired,
        )
        for desired in catalog.accounts
        if desired.active
    )

    return QuickBooksAccountSetupResult(
        created=tuple(created),
        updated=tuple(updated),
        reused=tuple(reused),
        detail_type_differences=tuple(differences),
        accounts=configured,
    )


def _create_payload(
    account: ChartOfAccount,
) -> dict[str, object]:
    """Build one QBO account creation payload."""

    return {
        "Name": account.name,
        "AcctNum": account.number,
        "AccountType": _account_type(account),
        "AccountSubType": _detail_type(account),
        "Description": account.purpose,
        "Active": True,
    }


def _account_type(
    account: ChartOfAccount,
) -> str:
    """Map workbook account type to QBO terminology."""

    try:
        return ACCOUNT_TYPE_MAP[account.qbo_account_type]
    except KeyError as exc:
        raise QuickBooksAccountSetupError(
            f"Unsupported QBO account type for {account.number}"
        ) from exc


def _detail_type(
    account: ChartOfAccount,
) -> str:
    """Map workbook detail type to a QBO subtype."""

    try:
        return DETAIL_TYPE_MAP[account.suggested_detail_type]
    except KeyError as exc:
        raise QuickBooksAccountSetupError(
            "Unsupported QBO detail type "
            f"{account.suggested_detail_type!r} "
            f"for account {account.number}"
        ) from exc


def _validate_compatible_account(
    *,
    current: QuickBooksApiAccount,
    desired: ChartOfAccount,
) -> None:
    """Reject unsafe reuse of an incompatible account."""

    expected_type = _account_type(desired)

    if current.account_type != expected_type:
        raise QuickBooksAccountSetupError(
            f"Existing QBO account {current.name!r} uses "
            f"type {current.account_type!r}; expected "
            f"{expected_type!r}"
        )

    if current.name.casefold() != desired.name.casefold():
        raise QuickBooksAccountSetupError(
            f"QBO account number {desired.number} is already assigned to {current.name!r}"
        )


def _find_by_number(
    accounts: list[QuickBooksApiAccount],
    number: str,
) -> QuickBooksApiAccount | None:
    """Find one account by configured number."""

    matches = [account for account in accounts if account.account_number == number]

    if len(matches) > 1:
        raise QuickBooksAccountSetupError(f"Multiple QBO accounts use number {number}")

    return matches[0] if matches else None


def _find_by_name(
    accounts: list[QuickBooksApiAccount],
    name: str,
) -> QuickBooksApiAccount | None:
    """Find one account by case-insensitive exact name."""

    matches = [account for account in accounts if account.name.casefold() == name.casefold()]

    if len(matches) > 1:
        raise QuickBooksAccountSetupError(f"Multiple QBO accounts use name {name!r}")

    return matches[0] if matches else None


def _require_final_account(
    accounts: tuple[QuickBooksApiAccount, ...],
    *,
    desired: ChartOfAccount,
) -> QuickBooksApiAccount:
    """Require final name, number, type, and active state."""

    account = _find_by_number(
        list(accounts),
        desired.number,
    )

    if account is None:
        raise QuickBooksAccountSetupError(f"QBO account {desired.number} was not created")

    _validate_compatible_account(
        current=account,
        desired=desired,
    )

    if not account.active:
        raise QuickBooksAccountSetupError(f"QBO account {desired.number} is inactive")

    return account
