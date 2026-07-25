"""Deterministic duplicate detection for normalized transactions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from app.models.ingestion import (
    IssueSeverity,
    NormalizedTransaction,
    RecordStatus,
    ValidationIssue,
)


class DuplicateMatchType(StrEnum):
    """How a duplicate matched its canonical transaction."""

    SOURCE_TRANSACTION_ID = "source_transaction_id"
    NORMALIZED_FINGERPRINT = "normalized_fingerprint"


@dataclass(frozen=True, slots=True)
class DuplicateDetectionResult:
    """Transactions and summary counts after duplicate detection."""

    transactions: tuple[NormalizedTransaction, ...]
    within_upload_count: int
    cross_upload_count: int
    conflict_count: int


class DuplicateDetector:
    """Detect duplicate and conflicting normalized transactions."""

    def detect(
        self,
        transactions: Iterable[NormalizedTransaction],
        *,
        existing_transactions: Iterable[NormalizedTransaction] = (),
    ) -> DuplicateDetectionResult:
        """Return transactions with deterministic duplicate states."""

        source_index: dict[
            tuple[str, str],
            NormalizedTransaction,
        ] = {}
        fingerprint_index: dict[str, NormalizedTransaction] = {}

        for existing in existing_transactions:
            if existing.status == RecordStatus.VALID:
                self._register(
                    existing,
                    source_index=source_index,
                    fingerprint_index=fingerprint_index,
                )

        detected: list[NormalizedTransaction] = []
        within_upload_count = 0
        cross_upload_count = 0
        conflict_count = 0

        for transaction in transactions:
            if transaction.status != RecordStatus.VALID:
                detected.append(transaction)
                continue

            source_match = self._source_match(
                transaction,
                source_index,
            )

            if source_match is not None:
                if self._content_identity(transaction) != self._content_identity(source_match):
                    detected.append(
                        self._mark_source_conflict(
                            transaction,
                            source_match,
                        )
                    )
                    conflict_count += 1
                    continue

                detected.append(
                    self._mark_duplicate(
                        transaction,
                        source_match,
                        DuplicateMatchType.SOURCE_TRANSACTION_ID,
                    )
                )

                if transaction.upload_id == source_match.upload_id:
                    within_upload_count += 1
                else:
                    cross_upload_count += 1
                continue

            fingerprint_match = self._fingerprint_match(
                transaction,
                fingerprint_index,
            )

            if fingerprint_match is not None:
                detected.append(
                    self._mark_duplicate(
                        transaction,
                        fingerprint_match,
                        DuplicateMatchType.NORMALIZED_FINGERPRINT,
                    )
                )

                if transaction.upload_id == fingerprint_match.upload_id:
                    within_upload_count += 1
                else:
                    cross_upload_count += 1
                continue

            detected.append(transaction)
            self._register(
                transaction,
                source_index=source_index,
                fingerprint_index=fingerprint_index,
            )

        return DuplicateDetectionResult(
            transactions=tuple(detected),
            within_upload_count=within_upload_count,
            cross_upload_count=cross_upload_count,
            conflict_count=conflict_count,
        )

    @staticmethod
    def _source_key(
        transaction: NormalizedTransaction,
    ) -> tuple[str, str] | None:
        if transaction.source_transaction_id is None or transaction.bank_account is None:
            return None

        return (
            transaction.bank_account.casefold(),
            transaction.source_transaction_id.casefold(),
        )

    def _source_match(
        self,
        transaction: NormalizedTransaction,
        source_index: dict[tuple[str, str], NormalizedTransaction],
    ) -> NormalizedTransaction | None:
        key = self._source_key(transaction)
        if key is None:
            return None

        canonical = source_index.get(key)
        if canonical is not None and canonical.id == transaction.id:
            return None
        return canonical

    @staticmethod
    def _fingerprint_match(
        transaction: NormalizedTransaction,
        fingerprint_index: dict[str, NormalizedTransaction],
    ) -> NormalizedTransaction | None:
        if transaction.fingerprint is None:
            return None

        canonical = fingerprint_index.get(transaction.fingerprint)
        if canonical is not None and canonical.id == transaction.id:
            return None
        return canonical

    def _register(
        self,
        transaction: NormalizedTransaction,
        *,
        source_index: dict[tuple[str, str], NormalizedTransaction],
        fingerprint_index: dict[str, NormalizedTransaction],
    ) -> None:
        source_key = self._source_key(transaction)
        if source_key is not None:
            source_index.setdefault(source_key, transaction)

        if transaction.fingerprint is not None:
            fingerprint_index.setdefault(
                transaction.fingerprint,
                transaction,
            )

    @staticmethod
    def _content_identity(
        transaction: NormalizedTransaction,
    ) -> tuple[object, ...]:
        return (
            transaction.transaction_date,
            transaction.amount,
            transaction.currency,
            (transaction.bank_account.casefold() if transaction.bank_account else None),
            transaction.description_normalized,
        )

    @staticmethod
    def _mark_duplicate(
        transaction: NormalizedTransaction,
        canonical: NormalizedTransaction,
        match_type: DuplicateMatchType,
    ) -> NormalizedTransaction:
        issue = ValidationIssue(
            code=f"duplicate_{match_type.value}",
            field="_record",
            message=(
                f"Transaction matches canonical transaction {canonical.id} by {match_type.value}"
            ),
            severity=IssueSeverity.WARNING,
            raw_value=str(canonical.id),
        )

        values = transaction.model_dump()
        values.update(
            status=RecordStatus.DUPLICATE,
            duplicate_of=canonical.id,
            validation_issues=[
                *transaction.validation_issues,
                issue,
            ],
        )
        return NormalizedTransaction.model_validate(values)

    @staticmethod
    def _mark_source_conflict(
        transaction: NormalizedTransaction,
        canonical: NormalizedTransaction,
    ) -> NormalizedTransaction:
        issue = ValidationIssue(
            code="source_transaction_id_conflict",
            field="source_transaction_id",
            message=("The same bank transaction ID has different normalized content"),
            severity=IssueSeverity.ERROR,
            raw_value={
                "source_transaction_id": transaction.source_transaction_id,
                "canonical_transaction_id": str(canonical.id),
            },
        )

        values = transaction.model_dump()
        values.update(
            status=RecordStatus.INVALID,
            duplicate_of=None,
            validation_issues=[
                *transaction.validation_issues,
                issue,
            ],
        )
        return NormalizedTransaction.model_validate(values)
