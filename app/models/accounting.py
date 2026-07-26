"""Typed accounting-domain models."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FinancialStatement(StrEnum):
    """Financial statement on which an account appears."""

    BALANCE_SHEET = "balance_sheet"
    PROFIT_AND_LOSS = "profit_and_loss"


class QBOAccountType(StrEnum):
    """QuickBooks account types required by the supplied workbook."""

    BANK = "Bank"
    FIXED_ASSETS = "Fixed Assets"
    EQUITY = "Equity"
    INCOME = "Income"
    COST_OF_GOODS_SOLD = "Cost of Goods Sold"
    EXPENSES = "Expenses"


BALANCE_SHEET_ACCOUNT_TYPES = frozenset(
    {
        QBOAccountType.BANK,
        QBOAccountType.FIXED_ASSETS,
        QBOAccountType.EQUITY,
    }
)

PROFIT_AND_LOSS_ACCOUNT_TYPES = frozenset(
    {
        QBOAccountType.INCOME,
        QBOAccountType.COST_OF_GOODS_SOLD,
        QBOAccountType.EXPENSES,
    }
)


class ChartOfAccount(BaseModel):
    """One approved account from the challenge chart of accounts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: str = Field(pattern=r"^\d{4}$")
    name: str = Field(min_length=1, max_length=100)
    qbo_account_type: QBOAccountType
    suggested_detail_type: str = Field(min_length=1, max_length=100)
    statement: FinancialStatement
    purpose: str = Field(min_length=1, max_length=300)
    active: bool = True

    @field_validator(
        "number",
        "name",
        "suggested_detail_type",
        "purpose",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: object) -> object:
        """Strip configuration text before applying constraints."""

        if isinstance(value, str):
            return value.strip()

        return value

    @model_validator(mode="after")
    def validate_statement_assignment(self) -> Self:
        """Prevent balance-sheet and P&L account-type mismatches."""

        if self.qbo_account_type in BALANCE_SHEET_ACCOUNT_TYPES:
            expected_statement = FinancialStatement.BALANCE_SHEET
        else:
            expected_statement = FinancialStatement.PROFIT_AND_LOSS

        if self.statement != expected_statement:
            raise ValueError(
                f"{self.qbo_account_type.value} accounts must use {expected_statement.value}"
            )

        return self


class ChartOfAccountsConfig(BaseModel):
    """Validated, immutable chart-of-accounts configuration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(min_length=1, max_length=20)
    company_name: str = Field(min_length=1, max_length=200)
    accounts: tuple[ChartOfAccount, ...] = Field(min_length=1)

    @field_validator("schema_version", "company_name", mode="before")
    @classmethod
    def strip_metadata(cls, value: object) -> object:
        """Normalize catalog metadata."""

        if isinstance(value, str):
            return value.strip()

        return value

    @model_validator(mode="after")
    def validate_unique_accounts(self) -> Self:
        """Require unique account numbers and case-insensitive names."""

        numbers = [account.number for account in self.accounts]
        if len(numbers) != len(set(numbers)):
            raise ValueError("Chart of account numbers must be unique")

        names = [account.name.casefold() for account in self.accounts]
        if len(names) != len(set(names)):
            raise ValueError("Chart of account names must be unique")

        return self

    def get(self, account_number: str) -> ChartOfAccount | None:
        """Return an account by number, or None when it is unknown."""

        normalized_number = account_number.strip()

        return next(
            (account for account in self.accounts if account.number == normalized_number),
            None,
        )

    def require(self, account_number: str) -> ChartOfAccount:
        """Return an account or reject an unsafe mapping."""

        account = self.get(account_number)
        if account is None:
            raise KeyError(f"Unknown chart-of-accounts number: {account_number}")

        if not account.active:
            raise ValueError(
                f"Inactive account cannot receive transactions: {account.number} {account.name}"
            )

        return account

    @property
    def balance_sheet_accounts(self) -> tuple[ChartOfAccount, ...]:
        """Return configured balance-sheet accounts."""

        return tuple(
            account
            for account in self.accounts
            if account.statement == FinancialStatement.BALANCE_SHEET
        )

    @property
    def profit_and_loss_accounts(self) -> tuple[ChartOfAccount, ...]:
        """Return configured P&L accounts."""

        return tuple(
            account
            for account in self.accounts
            if account.statement == FinancialStatement.PROFIT_AND_LOSS
        )
