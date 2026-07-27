"""Tests for deterministic classification-rule configuration."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.classification import TransactionType
from app.models.classification_rule import (
    DeterministicClassificationRule,
    DeterministicRuleMatch,
    DeterministicRuleOutcome,
    DeterministicRuleSet,
)
from app.models.ingestion import TransactionDirection


def create_rule(
    *,
    rule_id: str = "vehicle_fuel",
    priority: int = 100,
    active: bool = True,
    match: DeterministicRuleMatch | None = None,
) -> DeterministicClassificationRule:
    """Create one valid fuel classification rule."""

    return DeterministicClassificationRule(
        id=rule_id,
        priority=priority,
        active=active,
        match=match
        or DeterministicRuleMatch(
            description_contains_any=(
                "fuel",
                "gas station",
            ),
            direction=TransactionDirection.OUTFLOW,
            currency="USD",
        ),
        outcome=DeterministicRuleOutcome(
            transaction_type=TransactionType.OPERATING_EXPENSE,
            account_number="6020",
            account_name="Vehicle & Fuel",
            counterparty_name="Fuel Vendor",
            confidence_score=Decimal("0.980"),
            explanation=("The normalized description contains a configured vehicle-fuel phrase."),
            review_required=False,
        ),
    )


def test_match_conditions_are_normalized_and_deduplicated() -> None:
    """Configured phrases become stable case-insensitive values."""

    match = DeterministicRuleMatch(
        description_contains_any=(
            "  GAS   Station ",
            "gas station",
            "FUEL",
        ),
        description_excludes=(" Personal Fuel ",),
        direction=TransactionDirection.OUTFLOW,
        bank_account=" Operating Checking ",
        currency=" usd ",
    )

    assert match.description_contains_any == (
        "gas station",
        "fuel",
    )
    assert match.description_excludes == ("personal fuel",)
    assert match.bank_account == "operating checking"
    assert match.currency == "USD"


def test_rule_requires_positive_description_condition() -> None:
    """Direction alone is too broad for deterministic accounting."""

    with pytest.raises(
        ValidationError,
        match="positive description condition",
    ):
        DeterministicRuleMatch(
            direction=TransactionDirection.INFLOW,
            currency="USD",
        )


def test_required_phrase_cannot_also_be_excluded() -> None:
    """Contradictory rule configuration is rejected."""

    with pytest.raises(
        ValidationError,
        match="both required and excluded",
    ):
        DeterministicRuleMatch(
            description_contains_any=("fuel",),
            description_excludes=("FUEL",),
        )


def test_deterministic_confidence_must_be_high() -> None:
    """Low-confidence cases must fall through to review or Gemini."""

    with pytest.raises(
        ValidationError,
        match="greater than or equal to 0.900",
    ):
        DeterministicRuleOutcome(
            transaction_type=TransactionType.OPERATING_EXPENSE,
            account_number="6020",
            account_name="Vehicle & Fuel",
            confidence_score=Decimal("0.700"),
            explanation="This result is not certain enough.",
            review_required=True,
        )


def test_rule_identifier_uses_stable_machine_format() -> None:
    """Rule IDs remain safe for logs, configuration, and tests."""

    with pytest.raises(
        ValidationError,
        match="string_pattern_mismatch",
    ):
        create_rule(rule_id="Vehicle Fuel")


def test_rule_set_rejects_duplicate_identifiers() -> None:
    """Two accounting rules cannot share one audit identifier."""

    with pytest.raises(
        ValidationError,
        match="identifiers must be unique",
    ):
        DeterministicRuleSet(
            schema_version="1.0",
            rules=(
                create_rule(priority=100),
                create_rule(priority=200),
            ),
        )


def test_rule_set_rejects_duplicate_priorities() -> None:
    """Priority ties would make first-match behavior ambiguous."""

    with pytest.raises(
        ValidationError,
        match="priorities must be unique",
    ):
        DeterministicRuleSet(
            schema_version="1.0",
            rules=(
                create_rule(
                    rule_id="vehicle_fuel",
                    priority=100,
                ),
                create_rule(
                    rule_id="software_subscription",
                    priority=100,
                ),
            ),
        )


def test_active_rules_are_filtered_and_ordered() -> None:
    """The matcher receives a deterministic active-rule sequence."""

    rule_set = DeterministicRuleSet(
        schema_version="1.0",
        rules=(
            create_rule(
                rule_id="late_rule",
                priority=300,
            ),
            create_rule(
                rule_id="disabled_rule",
                priority=50,
                active=False,
            ),
            create_rule(
                rule_id="early_rule",
                priority=100,
            ),
        ),
    )

    assert [rule.id for rule in rule_set.active_rules] == [
        "early_rule",
        "late_rule",
    ]
