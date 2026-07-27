# QuickBooks Cash-Basis P&L and Reconciliation Evidence

This is a sanitized capture of the completed live QuickBooks Online sandbox
validation. The application retrieved four cash-basis Profit and Loss reports
through the QBO Reports API and compared the 17 controlled BrightFix P&L
accounts and all required totals at exact cents.

OAuth tokens, realm IDs, QBO transaction IDs, request IDs, and unrelated
Intuit sample-company activity are intentionally omitted.

## Account-level comparison

`I` is the internally generated amount and `QBO` is the amount retrieved from
QuickBooks.

| Account | Apr I | Apr QBO | May I | May QBO | Jun I | Jun QBO | Quarter I | Quarter QBO |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 4000 Repair Service Revenue | $42,775.00 | $42,775.00 | $45,150.00 | $45,150.00 | $43,525.00 | $43,525.00 | $131,450.00 | $131,450.00 |
| 4010 Installation Revenue | $53,000.00 | $53,000.00 | $59,450.00 | $59,450.00 | $50,450.00 | $50,450.00 | $162,900.00 | $162,900.00 |
| 4020 Maintenance Plan Revenue | $3,650.00 | $3,650.00 | $3,650.00 | $3,650.00 | $3,650.00 | $3,650.00 | $10,950.00 | $10,950.00 |
| 4100 Customer Refunds | ($1,250.00) | ($1,250.00) | ($1,675.00) | ($1,675.00) | ($2,100.00) | ($2,100.00) | ($5,025.00) | ($5,025.00) |
| 5000 Materials & Supplies | $15,025.00 | $15,025.00 | $17,550.00 | $17,550.00 | $18,475.00 | $18,475.00 | $51,050.00 | $51,050.00 |
| 5010 Subcontractor Costs | $16,300.00 | $16,300.00 | $14,500.00 | $14,500.00 | $12,000.00 | $12,000.00 | $42,800.00 | $42,800.00 |
| 6000 Payroll Expense | $25,800.00 | $25,800.00 | $26,750.00 | $26,750.00 | $27,700.00 | $27,700.00 | $80,250.00 | $80,250.00 |
| 6010 Rent Expense | $8,200.00 | $8,200.00 | $8,200.00 | $8,200.00 | $8,200.00 | $8,200.00 | $24,600.00 | $24,600.00 |
| 6020 Vehicle & Fuel | $1,485.00 | $1,485.00 | $1,545.00 | $1,545.00 | $1,305.00 | $1,305.00 | $4,335.00 | $4,335.00 |
| 6030 Software & Subscriptions | $1,445.00 | $1,445.00 | $1,445.00 | $1,445.00 | $1,445.00 | $1,445.00 | $4,335.00 | $4,335.00 |
| 6040 Marketing & Advertising | $2,800.00 | $2,800.00 | $2,950.00 | $2,950.00 | $3,100.00 | $3,100.00 | $8,850.00 | $8,850.00 |
| 6050 Insurance Expense | $1,225.00 | $1,225.00 | $1,225.00 | $1,225.00 | $1,225.00 | $1,225.00 | $3,675.00 | $3,675.00 |
| 6060 Utilities | $1,170.00 | $1,170.00 | $1,215.00 | $1,215.00 | $1,260.00 | $1,260.00 | $3,645.00 | $3,645.00 |
| 6070 Professional Fees | $1,650.00 | $1,650.00 | $1,650.00 | $1,650.00 | $1,650.00 | $1,650.00 | $4,950.00 | $4,950.00 |
| 6080 Bank Fees | $35.00 | $35.00 | $35.00 | $35.00 | $35.00 | $35.00 | $105.00 | $105.00 |
| 6090 Office & General | $310.00 | $310.00 | $310.00 | $310.00 | $310.00 | $310.00 | $930.00 | $930.00 |
| 6100 Repairs & Maintenance | $740.00 | $740.00 | $915.00 | $915.00 | $915.00 | $915.00 | $2,570.00 | $2,570.00 |

## Required-total comparison

| Total | Apr I | Apr QBO | May I | May QBO | Jun I | Jun QBO | Quarter I | Quarter QBO |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Total revenue | $98,175.00 | $98,175.00 | $106,575.00 | $106,575.00 | $95,525.00 | $95,525.00 | $300,275.00 | $300,275.00 |
| Total COGS | $31,325.00 | $31,325.00 | $32,050.00 | $32,050.00 | $30,475.00 | $30,475.00 | $93,850.00 | $93,850.00 |
| Gross profit | $66,850.00 | $66,850.00 | $74,525.00 | $74,525.00 | $65,050.00 | $65,050.00 | $206,425.00 | $206,425.00 |
| Total operating expenses | $44,860.00 | $44,860.00 | $46,240.00 | $46,240.00 | $47,145.00 | $47,145.00 | $138,245.00 | $138,245.00 |
| Net profit | $21,990.00 | $21,990.00 | $28,285.00 | $28,285.00 | $17,905.00 | $17,905.00 | $68,180.00 | $68,180.00 |

Every account and total above had a `$0.00` difference and `PASS` status.
Because no mismatch remained, the explanation for every line was: “Internal
and QuickBooks amounts match.” If a future difference exists, the live command
prints both amounts, the exact difference, `FAIL`, and a mismatch explanation,
then exits unsuccessfully.

## Live-run summary

```text
Connected company: BrightFix Home Services LLC
Controlled P&L accounts: 17
Periods compared: 4
April 2026: PASS
May 2026: PASS
June 2026: PASS
April 1–June 30, 2026: PASS
QuickBooks cash-basis P&L reconciliation: PASS
All monthly and consolidated accounts and totals reconcile exactly.
```

The guarded synchronization planned 189 JournalEntries. A repeated live run
created zero new entries and reused all 189 successful records, demonstrating
idempotency.
