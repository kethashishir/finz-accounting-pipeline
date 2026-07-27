"""QuickBooks JournalEntry payload construction."""

from __future__ import annotations

import math
from decimal import Decimal

from app.models.quickbooks_sync import (
    QuickBooksJournalEntryPlan,
)


class QuickBooksJournalEntryPayloadError(ValueError):
    """A posting plan cannot be represented safely in QBO."""


def build_quickbooks_journal_entry_payload(
    plan: QuickBooksJournalEntryPlan,
) -> dict[str, object]:
    """Convert one validated plan to QBO JournalEntry JSON."""

    if plan.currency != "USD":
        raise QuickBooksJournalEntryPayloadError(
            f"Foreign-currency posting is not implemented; received {plan.currency}"
        )

    lines: list[dict[str, object]] = []

    for line in plan.lines:
        payload_line: dict[str, object] = {
            "Amount": _json_amount(line.amount),
            "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": {
                "PostingType": line.posting_type.value,
                "AccountRef": {
                    "value": line.qbo_account_id,
                },
            },
        }

        if line.description is not None:
            payload_line["Description"] = line.description

        lines.append(payload_line)

    return {
        "TxnDate": plan.transaction_date.isoformat(),
        "PrivateNote": plan.private_note,
        "Line": lines,
    }


def _json_amount(
    amount: Decimal,
) -> int | float:
    """Convert cent-precise Decimal money to a JSON number."""

    if amount == amount.to_integral_value():
        return int(amount)

    rendered = format(amount, "f")
    numeric = float(rendered)

    if not math.isfinite(numeric):
        raise QuickBooksJournalEntryPayloadError("QuickBooks amount exceeds JSON numeric range")

    recovered = Decimal(str(numeric)).quantize(Decimal("0.01"))

    if recovered != amount:
        raise QuickBooksJournalEntryPayloadError(
            "QuickBooks amount cannot be represented without changing its cent value"
        )

    return numeric
