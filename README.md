# Finz Accounting Pipeline

A local accounting-data application for importing bank exports, preserving and
normalizing raw records, detecting duplicates, reviewing transaction
classifications, generating cash-basis Profit and Loss statements, syncing
approved transactions to a QuickBooks Online sandbox, and reconciling the
internal P&L with QuickBooks.

## Status

In active development for the Finz Data Engineering Internship technical
challenge.

## Planned workflow

1. Upload and map CSV or Excel bank data.
2. Preserve raw records and create normalized transactions.
3. Detect duplicates and internal transfers.
4. Classify, review, correct, and approve transactions.
5. Generate monthly and consolidated cash-basis P&Ls.
6. Sync approved transactions to QuickBooks Online.
7. Pull QuickBooks reports and reconcile every account.

## Technology

- Python 3.12
- FastAPI
- MongoDB
- Gemini API
- QuickBooks Online API
- Jinja2 and HTMX

## Security

Secrets and OAuth tokens are never committed to Git. The supplied financial
workbook remains outside the repository and is uploaded only during local
testing.
