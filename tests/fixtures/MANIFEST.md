# Test fixtures manifest

Generated from /mnt/user-data/outputs/Two_Commas_Accounting_2026.xlsx (cached values, openpyxl data_only=True) and the Wise CSVs. Row counts are data rows (header excluded).

## Files

| File | Rows | Source |
|---|---|---|
| transactions.csv | 235 | Transactions tab, all rows |
| invoices.csv | 18 | Invoice Register data rows (footer totals and the note row excluded) |
| statement_balances.csv | 28 | Bank Tie-out, "Statement balance" rows: Wise Jan-Oct, Chase *5200 Jan-Sep, Chase card *2110 Jan-Sep (28th) |
| vendor_rules.csv | 34 | Vendor Rules tab |
| golden_pnl.csv | 12 | Profit & Loss tab, numeric lines only (cached values) |
| raw/wise_full.csv | 392 | Upload statement_35739144_USD_2022-07-26_2026-09-11__1_.csv (copied) |
| raw/wise_2026-10.csv | 8 | Google Drive 1UkvzWyYVLgP59768cCojYMSk1vGFYiyr, statement_35739144_USD_2026-10-01_2026-10-31.csv (see note 1) |

## Chase PDFs (downloaded 15 Nov by the planner after H1 could not)

raw/chase5200/: 2026-01.pdf ... 2026-09.pdf (9 files). NOTE: Chase statement periods are NOT calendar months (e.g. Jan 1-Jan 30, Jan 31-Feb 27, Feb 28-Mar 31, May 1-May 29, May 30-Jun 30). Importers must tie to statement-period ending balances; the workbook compares the calendar month-end ledger to the statement ending balance.
raw/card2110/: 2026-01-28, 02-28, 03-28, 04-28, 05-28, 06-28, 07-28, 09-28 (.pdf) named by cycle closing date (8 files).
 - 2026-05-28.pdf was filed in Drive as "June 2026"; 2026-06-28.pdf was filed in Drive as "July 2026 - cc statement - 2210" (typo, it is card 2110).

## MISSING
- Card *2110 cycle 07/29-08/28 (Aug 2026): not on Drive. Joshua to supply -> raw/card2110/2026-08-28.pdf
- Oct 2026 Chase *5200 and card *2110: not issued/uploaded yet (workbook shows pending).
- Not used: none.

## Notes
1. The Oct Wise CSV came back from Drive as a text rendering. Markdown-style escapes were removed (\* became *, \_ became _) and trailing spaces were trimmed. Field content and row order are otherwise as returned. Final running balance 13344.65 matches the Bank Tie-out.
2. Card 2110 balances use as_of = the 28th of each month, per the Bank Tie-out.
