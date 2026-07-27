# AI Usage and Independent Validation

AI development tools were used as accelerators, not as an accounting system of
record.

## Tools and generated assistance

- ChatGPT and Codex helped plan the phased architecture, draft implementation
  and test scaffolding, review errors, and improve documentation.
- Gemini is integrated in the application only as a structured classification
  fallback after learned patterns and deterministic rules cannot safely decide.
- AI did not calculate report totals, alter the supplied workbook, approve
  classifications, or directly write arbitrary accounting entries.

## Independently validated

- The supplied workbook remained unchanged and its SHA-256 checksum was
  verified.
- Raw preservation, normalization, configurable mapping, duplicates, transfer
  pairing, accounting classification, correction history, P&L construction,
  OAuth security, posting plans, retries, and idempotency were covered by 408
  automated tests.
- The real workbook produced 200 preserved rows, 195 canonical transactions,
  five duplicates, and zero invalid normalized rows.
- Every deterministic rule target was validated against the supplied chart of
  accounts, and all 195 canonical classifications were reviewed in the
  acceptance workflow.
- Monthly and consolidated totals were recalculated from transaction-level
  `Decimal` values; no AI model performs financial arithmetic.
- Live QBO sandbox synchronization was repeated to verify that no duplicate
  JournalEntries were created.
- Four cash-basis QBO reports were fetched through the API and every controlled
  account and required total reconciled at a `$0.00` difference.

The final responsibility for architecture, code review, tests, accounting
treatment, live QuickBooks validation, and submission evidence remained with
the developer.
