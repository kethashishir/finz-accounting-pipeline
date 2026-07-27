"""Typed configuration models for deterministic classification rules."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.models.classification import (
    AccountNumber,
    ImmutableAccountingModel,
    NonEmptyString,
    TransactionType,
)
from app.models.ingestion import TransactionDirection

RuleIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z][a-z0-9_]*$",
    ),
]

DeterministicConfidence = Annotated[
    Decimal,
    Field(
        ge=Decimal("0.900"),
        le=Decimal("1.000"),
    ),
]


class DeterministicRuleMatch(ImmutableAccountingModel):
    """Controlled transaction attributes required for one rule match."""

    description_exact: tuple[NonEmptyString, ...] = ()
    description_contains_all: tuple[NonEmptyString, ...] = ()
    description_contains_any: tuple[NonEmptyString, ...] = ()
    description_excludes: tuple[NonEmptyString, ...] = ()

    direction: TransactionDirection | None = None
    bank_account: NonEmptyString | None = None
    currency: str | None = Field(
        default=None,
        pattern=r"^[A-Z]{3}$",
    )

    @field_validator(
        "description_exact",
        "description_contains_all",
        "description_contains_any",
        "description_excludes",
        mode="before",
    )
    @classmethod
    def normalize_description_terms(
        cls,
        value: object,
    ) -> object:
        """Normalize and de-duplicate configured match phrases."""

        if not isinstance(value, (list, tuple)):
            return value

        normalized: list[object] = []

        for item in value:
            if isinstance(item, str):
                item = " ".join(item.strip().casefold().split())

            if item not in normalized:
                normalized.append(item)

        return tuple(normalized)

    @field_validator("bank_account", mode="before")
    @classmethod
    def normalize_bank_account(
        cls,
        value: object,
    ) -> object:
        """Normalize an optional bank-account match value."""

        if isinstance(value, str):
            return " ".join(value.strip().casefold().split())

        return value

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_currency(
        cls,
        value: object,
    ) -> object:
        """Normalize an optional currency constraint."""

        if isinstance(value, str):
            return value.strip().upper()

        return value

    @model_validator(mode="after")
    def validate_description_conditions(
        self,
    ) -> Self:
        """Require a positive description signal and consistent exclusions."""

        positive_terms = set(
            (
                *self.description_exact,
                *self.description_contains_all,
                *self.description_contains_any,
            )
        )

        if not positive_terms:
            raise ValueError(
                "deterministic rules require at least one positive description condition"
            )

        overlapping_exclusions = positive_terms.intersection(self.description_excludes)

        if overlapping_exclusions:
            raise ValueError("a description phrase cannot be both required and excluded")

        return self


class DeterministicRuleOutcome(ImmutableAccountingModel):
    """Accounting result produced when a deterministic rule matches."""

    transaction_type: TransactionType
    account_number: AccountNumber
    account_name: NonEmptyString
    counterparty_name: NonEmptyString | None = None
    confidence_score: DeterministicConfidence
    explanation: NonEmptyString
    review_required: bool

    @field_validator(
        "account_name",
        "counterparty_name",
        "explanation",
        mode="before",
    )
    @classmethod
    def normalize_output_text(
        cls,
        value: object,
    ) -> object:
        """Strip human-readable accounting output fields."""

        if isinstance(value, str):
            return value.strip()

        return value


class DeterministicClassificationRule(ImmutableAccountingModel):
    """One ordered and auditable deterministic classification rule."""

    id: RuleIdentifier
    priority: int = Field(ge=1, le=10_000)
    active: bool = True
    match: DeterministicRuleMatch
    outcome: DeterministicRuleOutcome


class DeterministicRuleSet(ImmutableAccountingModel):
    """Versioned deterministic-rule configuration."""

    schema_version: NonEmptyString
    rules: tuple[DeterministicClassificationRule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_rule_identity_and_order(
        self,
    ) -> Self:
        """Prevent nondeterministic rule identity or priority collisions."""

        identifiers = [rule.id for rule in self.rules]

        if len(identifiers) != len(set(identifiers)):
            raise ValueError("deterministic rule identifiers must be unique")

        priorities = [rule.priority for rule in self.rules]

        if len(priorities) != len(set(priorities)):
            raise ValueError("deterministic rule priorities must be unique")

        return self

    @property
    def active_rules(
        self,
    ) -> tuple[DeterministicClassificationRule, ...]:
        """Return active rules in ascending priority order."""

        return tuple(
            sorted(
                (rule for rule in self.rules if rule.active),
                key=lambda rule: rule.priority,
            )
        )
