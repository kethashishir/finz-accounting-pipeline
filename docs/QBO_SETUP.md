# QuickBooks Online Sandbox Setup

## Developer and sandbox preparation

1. Create an Intuit Developer account, an app with the QuickBooks Online
   Accounting scope, and a sandbox company.
2. Set the sandbox company name to `BrightFix Home Services LLC`, country to
   United States, home currency to USD, and use cash-basis reporting.
3. Add this exact development redirect URI in the Intuit app:
   `http://localhost:8000/api/v1/quickbooks/callback`.
4. Copy `.env.example` to `.env` and set `QBO_CLIENT_ID` and
   `QBO_CLIENT_SECRET`. Keep `.env` ignored.
5. Generate independent local secrets:

   ```bash
   python - <<'PY'
   import secrets
   from cryptography.fernet import Fernet

   print("TOKEN_ENCRYPTION_KEY=" + Fernet.generate_key().decode())
   print("SESSION_SECRET=" + secrets.token_urlsafe(48))
   PY
   ```

6. Set the generated values in `.env`, start MongoDB and the application, then
   open `http://localhost:8000/api/v1/quickbooks/connect`.
7. Sign in to the sandbox, select the BrightFix company, approve access, and
   allow the callback to store encrypted tokens locally in MongoDB.
8. Configure and validate the required chart of accounts:

   ```bash
   python scripts/setup_qbo_sandbox.py
   ```

9. Load the untouched challenge workbook into an otherwise empty accounting
   database. This guarded acceptance loader verifies its checksum, ingests and
   classifies the records, approves the validated deterministic results, and
   recalculates all four internal reports:

   ```bash
   python scripts/load_challenge_data.py \
     --workbook "/absolute/path/to/Finz Accounting Data Engineering Challenge Dataset.xlsx"
   ```

10. Preview, synchronize, repeat the sync to prove idempotency, and reconcile:

   ```bash
   python scripts/preview_qbo_sync.py
   python scripts/sync_qbo_sandbox.py \
     --confirm BRIGHTFIX-SANDBOX-LIVE-SYNC
   python scripts/sync_qbo_sandbox.py \
     --confirm BRIGHTFIX-SANDBOX-LIVE-SYNC
   python scripts/reconcile_qbo_profit_and_loss.py
   ```

## Account and detail-type choices

The workbook labels remain the source of truth. The setup adapter translates
human-readable detail types to Intuit API enum values—for example
`Machinery and Equipment` to `MachineryAndEquipment`,
`Service/Fee Income` to `ServiceFeeIncome`, and
`Supplies & Materials - COGS` to `SuppliesMaterialsCogs`.

QuickBooks uses singular API broad types (`Fixed Asset` and `Expense`) where
the workbook uses plural display labels (`Fixed Assets` and `Expenses`).
This is an API terminology difference, not an accounting change.

The setup is idempotent: it creates missing accounts, reuses compatible
accounts, restores the number or active status when safe, and rejects a
name/number collision or incompatible broad account type. If an existing
sandbox account retains a different supported detail subtype, the setup
command prints the requested and actual values for review. No broad-type
difference is silently accepted.

## Integration choices

- Approved canonical bank activity is posted as balanced JournalEntries.
- Each source transaction debits or credits its bank account and the selected
  chart-of-accounts target.
- The two sides of an internal transfer become one entry between bank
  accounts, never revenue or expense.
- Deterministic request IDs and stored sync inventory prevent duplicate
  posting and permit safe retry of failed records.
- QBO IDs, status, errors, and attempts are stored; tokens are encrypted.
- Reconciliation uses QBO cash-basis P&L reports filtered to the controlled
  accounts so unrelated default sandbox activity cannot contaminate results.
