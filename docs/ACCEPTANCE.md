# Two Commas bookkeeping tool - ACCEPTANCE REVIEW (F4)

Reviewed 2026-11-16 against `docs/SPEC.md`, `docs/RULES.md`, `docs/UI.md`, `README.md`. Review only: no code was
changed. Money below is USD; the tool stores integer cents.

## 0. Verdict

**NOT READY for Joshua's November close as delivered; ready after H1 + H2 (one small seeding task) and the October
Chase / card statements.** The engine is sound: all 8 rules pass, the close gate and lock hold, and the clean-room
import reconciles to the workbook to the cent. What is missing is a supported way to get January-October 2026 into
the production database (`make init` gives an empty one), and the raw-statement route cannot tie Aug-Oct until two
missing statements are supplied.

| Area | PASS | FAIL |
|---|---|---|
| Test suite | 350 passed, 7 skipped (expected) | 0 |
| Golden numbers G1-G11, golden-loader path (what the tests assert) | 11 | 0 |
| Golden numbers G1-G11, clean-room raw import | 4 exact | 7 (all reconciled exactly, section 3) |
| Rules 1-8 | 8 (rule 5 with a data caveat) | 0 |
| Code-review checks (section 5) | 5 | 3 gaps (backup path, lock scope at SQL level, data-loss edges) |

## 1. Test suite

`TC_BACKUP_DIR=<tmp> python3 -m pytest -q -rs` -> **350 passed, 7 skipped in 33.6 s**, 0 failed.

All 7 skips are `tests/test_chase_card.py` (lines 95, 100, 114, 120, 136, 150, 180), reason: `2026-08-28.pdf not
available (card *2110 cycle closing 2026-08-28; Joshua to supply -> tests/fixtures/raw/card2110/)`.

Note: 19 of the 24 test names in SPEC sections 2, 3, 5 and 8 (`tests/test_golden.py`, `test_rule1_...`,
`test_backup_roundtrip`, ...) do not exist. The same assertions exist under other names
(`tests/test_reports.py::test_pnl_golden_headlines`, `test_pnl_matches_golden_csv_cell_by_cell`, `test_owner_draws`,
`test_tie_out_jan_to_sep_all_accounts`, `tests/test_db.py::test_golden_transaction_count`,
`tests/test_invoices.py` line 100, `tests/test_close.py`). Those golden tests run on `fixtures.load_golden` (the
answer-key loader), not on a raw import. There is no backup round-trip test of the golden numbers.

## 2. Clean-room run

Fresh file DB -> `db.init_db` -> `rules.sync_rules_table` -> 18 invoices through `invoices.add_invoice` (as
`tests/test_pipeline.py::load_invoices`) -> `pipeline.run_import` on 19 files (2 Wise CSV, 9 Chase *5200 PDF, 8 card
*2110 PDF) -> 3 adjustments through `pipeline.add_adjustment` with the definitions of `fixtures.load_golden`
(AR 2026-09-30 other_income +1,562.86 non-cash; opening "Reimbursable Expenses Recoverable (net)" asset plug, same
formula; "Loan from Shareholder" liability 4,081.90) -> `reports`.

Import result: 201 bank lines inserted, 211 ledger rows (98 Wise, 32 Chase, 75 card, 6 jar), 306 Wise rows skipped
as before the opening date, 0 errors. 7 rows in the review queue: 4 `confirm_rule` (HTGROWTH 16 Jan, 27 Feb, 16 Apr;
WA tariff wire 8 Jul), 2 `invoice_matched_by_amount` (18 Jun, 31 Aug), 1 `unpaired_transfer` (Chase 3 Aug -1,492.97,
its card leg is on the missing August statement). Re-importing all 19 files: 0 inserted, 201 duplicates, table
fingerprint unchanged. `run_rules` on everything: fingerprint unchanged.

### 2.1 Golden numbers

"Loader" = the test suite on `load_golden`. "Clean-room" = raw import, before any review decision.

| # | Golden | Loader | Clean-room value | Clean-room |
|---|---|---|---|---|
| G1 | 235 rows | PASS | 211 | FAIL, reconciled: 235 - 10 (Aug card cycle) - 6 (Wise 12-30 Sep) - 8 (jar draws) = 211 |
| G2 | Wise month-end = statement, Jan-Oct | PASS | Jan-Aug equal to the cent (11,001.78 ... 17,455.08); 30 Sep 17,002.01; 31 Oct 14,700.76 | FAIL Sep, Oct: both +1,356.11 = the 6 missing Wise rows |
| G3 | Chase *5200 Jan-Sep | PASS | 2,195.78; 2,095.78; 2,083.45; 4,937.29; 1,932.58; 3,499.92; 3,505.93; 2,504.65; 3,603.52, all diff 0 | PASS |
| G4 | Card *2110 28th, Jan-Sep | PASS | Jan-Jul diff 0; 28 Aug no statement; 28 Sep ledger 1,057.20 vs 1,456.41 | FAIL Aug, Sep: 399.21 = 1,892.18 charges - 1,492.97 payment on the missing cycle |
| G5 | YTD revenue 32,186.00 | PASS | 32,186.00 | PASS |
| G6 | YTD net income 18,618.29 | PASS | 19,564.71 | FAIL, reconciled exactly (3.1) |
| G7 | YTD before AR adjustment 17,055.43 | PASS | 18,001.85 | FAIL, same 946.42 as G6 |
| G8 | YTD owner's draws 8,048.00 | PASS | 6,176.00 | FAIL, reconciled (3.3) |
| G9 | Oct revenue / bank charges / interest / net 5,706.00 / 9.50 / 45.13 / 5,741.63 | PASS | 5,706.00 / 9.50 / 45.13 / 5,741.63 | PASS |
| G10 | Sep net before AR adjustment -1,316.72 | PASS | -1,309.48 | FAIL: 7.24 = two Wise fees in the missing export |
| G11 | Commission paid vs revenue, diff 0 | PASS | 32,186.00 vs 32,186.00, 0 unbacked | PASS |

## 3. Reconciliation of clean-room to golden (exact)

Method: multiset difference on (date, account, amount, category, type) between `tests/fixtures/transactions.csv`
and the clean-room ledger. 38 key rows have no identical clean-room row, 14 clean-room rows have no identical key
row. 24 of the 38 are genuinely missing; the other 14 pair one-to-one with the 14 (date / type / category
deviations, listed in 3.4).

### 3.1 YTD net income

| Step | Item | Effect on net income | Running |
|---|---|---|---|
| | Clean-room YTD net income | | 19,564.71 |
| a | Card cycle 29 Jul-28 Aug missing: Anthropic 30 Jul 80.54 + 20.00; Google 1 Aug 16.80 + 12.84; Sellerboard 3 Aug 29.00; Firstbase 2 Aug 149.00, 12 Aug 299.00, 15 Aug 35.00; HTGROWTH 23 Aug 1,250.00 | -1,892.18 | 17,672.53 |
| b | Wise 12-30 Sep missing: fee 15 Sep 6.11 (on the INV-000020 receipt), fee 30 Sep 1.13 (on the transfer to Chase) | -7.24 | 17,665.29 |
| c | HTGROWTH 16 Apr 1,000.00: the rule proposes Other Professional Services and queues it (`confirm_rule`); the workbook has it as Reimbursable (INV-000011). A review decision, not a missing row | +1,000.00 | 18,665.29 |
| d | MYREALPROFIT 47.00: statement date 2025-12-31, workbook books it 2026-01-01. The importer keeps the statement date, so it is outside FY2026 | -47.00 | **18,618.29** |
| | Golden | | **18,618.29** |

Items c and d are not in the "known missing rows" list; they are needed for the tie. c was applied through
`pipeline.set_category` (net income moved 19,564.71 -> 20,564.71, as computed). d needs an owner decision (M5).

By month (clean-room minus golden): Jan +47.00 (d); Apr -1,000.00 (c); Jul +100.54 (a: Anthropic); Aug +1,791.64
(a: 1,698.00 + 35.00 + 58.64); Sep +7.24 (b). Total +946.42 = 19,564.71 - 18,618.29. All other months equal.

### 3.2 Revenue and October

- YTD revenue: 32,186.00 = 32,186.00. No reconciling item (the only receipt in the missing periods, 15 Sep +550.00,
  is Reimbursable).
- October net income: 5,741.63 = 5,741.63. No reconciling item.

### 3.3 Other figures

| Figure | Clean-room | Missing rows | Golden |
|---|---|---|---|
| Wise ledger 30 Sep | 17,002.01 | +550.00 - 6.11 - 500.00 - 1,198.87 - 200.00 - 1.13 = -1,356.11 | 15,645.90 |
| Wise ledger 31 Oct | 14,700.76 | -1,356.11 | 13,344.65 |
| Card liability 28 Sep | 1,057.20 | +1,892.18 - 1,492.97 = +399.21 | 1,456.41 |
| Owner's draws YTD | 6,176.00 | Wise 25 Sep 500.00 + 30 Sep 200.00; jar draws 26 Jan, 27 Feb, 25 Mar, 17 Apr, 4 x 293.00 = 1,172.00 | 8,048.00 |
| Transfers cumulative to Sep | -294.10 | card payment-in 31 Jul +1,492.97; Wise-out 30 Sep -1,198.87 | 0.00 |
| Transfers cumulative to Oct | -1,791.84 | same +294.10 | -1,497.74 |
| Equity 30 Sep | 14,740.00 | Wise -1,356.11, jars -1,172.00, card -399.21 = -2,927.32 | 11,812.68 |

The reimbursable balance-sheet line equals -1,898.43 by construction (the plug formula of `load_golden`).

### 3.4 Row deviations that do not change any total

- 9 card rows and 1 Chase row dated one day apart (posting vs purchase date), same month except MYREALPROFIT (3.1 d).
- Google 1 Sep 28.80: one statement line, two workbook rows (16.80 + 12.00).
- Draw 3 Sep 500.00: one workbook row, two ledger rows (-495.22 and fee -4.78, both Owner's Draw).
- Maersk 21 Apr -8,204.37: type `Reimbursable` in the tool, `Pass-through` or `Reimbursable` in the workbook (L2, resolved: one label).

## 4. Rules 1-8

Evidence is from the clean-room DB unless marked synthetic.

| # | Rule | Result | Evidence |
|---|---|---|---|
| 1 | ONE RULE (receipt vs invoice type) | PASS | `SELECT COUNT(*) FROM v_revenue_unbacked` -> 0. All 10 Service Revenue rows are backed only by Commission invoices (join `invoice_payments` / `invoices`). The 3 receipts settling Reimbursable invoices (5 Mar 900.00, 29 Apr 1,000.00, 5 May 8,764.08) are Reimbursable Expenses Recoverable, `statement = 'balance_sheet'`. `SELECT DISTINCT category FROM v_pnl_monthly` -> only the 6 P&L categories. Refused: `set_category(receipt -> Service Revenue)` ("only set by matching Commission invoices"); `INSERT invoice_payments` Service Revenue -> Reimbursable invoice (trigger); Commission invoice with amount <> boxes x 50 (API and CHECK). No invoices loaded -> revenue 0.00 and 13 receipts queued `no_invoice_match` |
| 2 | Cash basis | PASS | 19 Oct wire 5,706.00 = Wise INV-013 3,016.00 + Wise INV-014 2,690.00 (both issued 16 Oct, for Aug and Sep), fee row -6.11 Bank Charges, revenue in 2026-10. Receipts where allocations <> gross: 0 rows. INV-000016 issued 4 May, received 2 Jun -> June revenue; `v_pnl_monthly` revenue months = receipt months (Jan, Mar, Apr, Jun, Jul, Aug, Oct) |
| 3 | Owner draws | PASS | Owner's Draw rows: Betterment 9 (-900.00), IRS 1 (-1,411.00), Joshua 11 (-3,837.91), fees on draws 8 (-27.09). Betterment / Joshua / IRS rows in any other category: 0. Category `statement = 'equity'`; rows in `v_pnl_monthly`: 0 |
| 4 | Fee follows parent | PASS | `v_fee_parent_mismatch` -> 0. Parent -> fee category: Bank Transfer -> Bank Charges 13; Owner's Draw -> Owner's Draw 8; Payable to WA -> Payable to WA 1 (-36.13); Reimbursable -> Bank Charges 6; Service Revenue -> Bank Charges 10. Orphan fee rows: 0. Every Wise source id: ledger parent + synthetic child = CSV amount, 0 mismatches (e.g. CSV -300.00 fee 1.13 -> -298.87 and -1.13; WA wire CSV +5,699.89 fee 6.11 -> +5,706.00 and -6.11; draw -600.00 kept whole, `fee_cents` 113). Trigger refuses a fee row in the wrong category; `set_category` refuses a fee row |
| 5 | Own-account transfers net zero | PASS, data caveat | Bank Transfer not in the P&L (0 rows in `v_pnl_monthly`). `v_transfer_net` cumulative: Jan-Jul 0 (May -2,400.00 / Jun +2,400.00 straddle). To Sep -294.10 and to Oct -1,791.84 instead of 0 and -1,497.74: exactly the two missing legs (3.3). The leg whose partner should exist is flagged (`unpaired_transfer`, Chase 3 Aug). Known gap: net zero is a displayed figure and a review flag, not a close condition, and pairs are recomputed, not stored |
| 6 | Reimbursable never in P&L | PASS | CBP direct debit -5,510.14; Maersk on Wise 3 rows -8,889.37; Maersk, Pierpass, Portcheck on the card: all Reimbursable Expenses Recoverable (balance sheet). CBP tariff refund +18,375.57 -> Payable to WA. Fees on the Maersk payments -> Bank Charges (3 x -1.13) |
| 7 | Firstbase $35, Chase fee waiver | PASS | Firstbase -35.00 -> Postage & Delivery, 8 of 8; -149.00 / -68.48 / +149.00 -> Other Professional Services. Fee rows: 29 May and 31 Aug only, both `auto`. Printed tests per statement (`parse_statement`): fee expected when lowest balance < 2,000 and Ink activity < 2,000 -> expected = charged for all 9; April: low 1,983.45, Ink 3,142.79, waived, no fee, no flag. Unexpected / second fee -> `unexpected_bank_fee` (code, covered by tests) |
| 8 | Tie-out gate and month lock | PASS | Gate: `close_month('2026-09')` -> CloseBlocked (step 4 Wise balance missing, step 5 card off); raw `INSERT month_locks` -> trigger abort; a `needs_review` row blocks January. After review and Wise balances: Jan-Jul close in order, Aug-Oct blocked with the right reasons. Lock: raw insert / update / delete of transactions, adjustments and statement balances in a locked month, and moving a February row into January -> "Month is closed and locked"; `set_category`, `confirm`, `add_adjustment`, `set_statement_balance`, `match_payment`, `split_receipt` -> LockedMonthError; `run_rules` leaves locked rows untouched; re-import rejects locked rows (inserted 0). Re-open needs a reason and takes a backup. Caveats: section 5.3 |

## 5. Code review

### 5.1 Integer cents - PASS
No float arithmetic on money in `src/tc` or `app` (grep for `float(`, `/ 100`, `round(`). Parsing is `Decimal`
(`db.to_cents`), formatting is `divmod`. One exception, output only: `export_xlsx._dollars` writes `cents / 100` into
Excel cells.

### 5.2 SQL injection - PASS
Every value is a bound parameter. f-strings only build `?` placeholder lists, fixed WHERE fragments, and column
names checked against the `_TX_COLUMNS` whitelist (`db.insert_transaction`). Search text is LIKE-escaped.

### 5.3 Month-lock bypass - PASS for the UI, gaps at SQL level
- No UI path writes transactions, adjustments or statement balances of a locked month: every writing function checks
  the lock first and the triggers back it up. Add / void invoice stay available by design.
- Not covered by triggers (raw SQL only, all four succeeded on a locked January): `DELETE FROM invoice_payments`,
  `UPDATE invoices`, `UPDATE accounts SET opening_balance_cents`, `UPDATE categories SET statement`. Each changes a
  locked month's revenue backing, tie-out or P&L (L4).
- A month can be closed while earlier months are open, and re-opening an early month does not re-check later locked
  months (L5).
- Card cycle vs lock: with January locked, `card2110/2026-02-28.pdf` -> `rejected_locked: 2`; the Import page then
  refuses the whole file. Documented (`tail_covered`), but it shapes the close routine (M2).

### 5.4 Idempotent re-import - PASS, with one hole
Same files twice: 0 inserted, fingerprint unchanged. Hole: rows loaded by `load_golden` have a different
`source_ref` from importer rows, so a statement imported on top of golden rows is duplicated (H2). Also, Chase refs
are `period_end|index|sha1` not the SPEC key `date|amount|description|n`, so a Chase CSV download and the PDF of
the same period do not dedupe (PDF after CSV is refused by the tie check; CSV after PDF doubles the rows, flagged
`possible_duplicate`) (L3).

### 5.5 Backups - works, two defects
- Confirmed: `pipeline._backup` writes to `backups/` relative to the current directory; `close.backup` writes to
  `<repo>/backups`. With `TC_BACKUP_DIR` unset and another working directory the import backup went to
  `<cwd>/backups/`. How bad: nothing is lost, but import backups end up outside the folder Joshua copies off the
  machine. The default database path `data/tc.db` is relative too, so starting the app from another directory opens
  a new empty database (M6).
- Pruning: import backups are pruned to the last 30 (34 written -> 30 kept); close backups are kept. Not built:
  re-open backups are never pruned, and every blocked close attempt leaves a permanent close backup, because the
  backup is taken before the checks. No test covers pruning (L1).
- No backup before `run_rules` (RULES.md 4.2 says there is one) or before `mark_duplicate`, which deletes a row.

### 5.6 Data-loss risks
- `mark_duplicate`: deletes without a backup; limited to rows flagged `possible_duplicate` with no fee rows or
  invoice links.
- No unmatch: a receipt matched to the wrong invoice becomes `reviewed`; Transactions says "change it on Invoices",
  Invoices has no unmatch, Re-run rules skips reviewed rows. Only SQL can undo it (M4).
- A Wise export with a hole imports without any warning: `wise_2026-10.csv` opens at 15,645.90 while the ledger at
  30 Sep is 17,002.01; `warnings: []`. Any Wise CSV, including a jar export, is booked to *1430. Both are caught
  only at tie-out (M1).
- Uploaded statements are saved to a temp folder and not archived.
- `rules.add_rule` rewrites `src/tc/rules.yaml` (restores it on failure); the file is the only copy of the rules.

### 5.7 Pages containing SQL - PASS
`app/pages/*.py`, `app/Home.py`, `app/import_helpers.py`: none. `app/ui_common.py` has three read-only SELECTs
(table check, month list, lock list).

## 6. Known gaps - verified

| Gap | Status |
|---|---|
| Wise export 12-30 Sep 2026 missing | Confirmed. `wise_full.csv` last row 3 Sep; `wise_2026-10.csv` starts 1 Oct. 6 key rows, net -1,356.11 |
| Aug 2026 card statement missing | Confirmed. No `card2110/2026-08-28.pdf`; 10 key rows; 7 tests skipped |
| Invoice Register CSV not written on close | Confirmed. A close wrote `tc_..._close.db` and `..._close_2026-01_transactions.csv` only |
| Excel "Wise Fee" / "Net Received" blank | Confirmed. 18 of 18 rows blank. The export has 4 tabs (no Balance Sheet) and no button in the app |
| Golden-loader `source_ref` differs from importer | Confirmed with consequence: `wise_2026-10.csv` on top of golden rows -> 8 inserted, 12 ledger rows, 4 flagged `possible_duplicate`, Wise 31 Oct ledger 11,043.40 vs 13,344.65 |
| Reimbursable vs Pass-through labels | Resolved. One label, `Reimbursable`, for every `Reimbursable Expenses Recoverable` row (category default, rules, invoices, loader). `Pass-through` is only for Payable to WA Rubbermate. No effect on any total |
| Backup pruning | Partly built (5.5): import yes, re-open no, untested |
| RULES.md 4.3 merge / split on an Owner's Draw flip | Implemented and verified on both paths. `set_category` on the 2 Oct transfer: (-298.87, -1.13) -> draw (-300.00, `fee_cents` 113, child deleted) -> transfer (-298.87 + new -1.13 child); ledger sum unchanged, mismatch view 0. Re-run path: `_Engine._reset` / `_stage5`, tested at `tests/test_pipeline.py` line 617. Not done: a reviewed child blocks the merge silently, no conflict is reported |

## 7. Prioritised gap list

### HIGH

**H1. No way to load January-October 2026 into the production database.**
`make init` gives 0 transactions, 0 invoices, 0 `rules` rows. Tie-out is cumulative from the 1 Jan openings, so no
month can tie without the full year. `load_golden` and `backfill_payments` are not reachable from the app, a CLI or
the README. The UI route fails too: Add invoice offers only the sources already in the database plus "Wise", so the
13 Firstbase-series invoices cannot be entered.
Fix: add `python -m tc.fixtures seed [db]` and `make seed`: refuse a non-empty database, then `load_golden`,
`invoices.backfill_payments`, `rules.sync_rules_table`. Add a README section "Start from the 2026 workbook". Let the
Add invoice source be free text.
Files: `src/tc/fixtures.py`, `Makefile`, `README.md`, `app/pages/5_Invoices.py`.
Test: `tests/test_db.py::test_cli_seed_reproduces_golden`: after the CLI, YTD net income 1861829, revenue 3218600,
`v_revenue_unbacked` empty, every blocking item of `close.checklist(conn, '2026-09')` ok, `rules` not empty.
Verified by hand: `load_golden` + `backfill_payments` (15 links) gives 18,618.29 / 32,186.00 / 5,741.63 and September
passes every blocking check.

**H2. After a golden seed, importing a statement that covers seeded dates duplicates rows.** Evidence in section 6.
Only flagged rows can be removed (`mark_duplicate`); the rest have no delete path.
Fix: record a per-account seed cutoff (Wise 2026-10-31, Chase 2026-09-30, card 2026-09-28) and have
`pipeline._normalize` skip rows dated on or before it, counted like `skipped_before_opening` and shown on Import.
Files: `src/tc/fixtures.py`, `src/tc/schema.sql` (cutoff column or meta table), `src/tc/pipeline.py`,
`app/import_helpers.py`.
Test: `tests/test_pipeline.py::test_import_over_seed_inserts_nothing`: seed, `run_import(wise_2026-10.csv)` ->
inserted 0, Wise ledger at 31 Oct 1334465.
Note for H1: seeded transfer legs carry no `[rule:]` tag, so the October Chase credits cannot pair with them; a
synthetic test queued 1 `unpaired_transfer` to confirm by hand (in-transit total correctly returned to 0).

**H3. Two source statements are missing (owner: Joshua).** Wise export covering 12-30 Sep 2026 and the card
statement closing 28 Aug 2026. On the raw-import route August (card), September (Wise +1,356.11, card 399.21) and
October cannot tie. Not blocking if H1 + H2 are used, but the importers stay unproven on those periods.
Files: `tests/fixtures/raw/`, `tests/fixtures/MANIFEST.md`, `tests/test_pipeline.py` (drop the `GAP_START` /
`CARD_MISSING` exclusions).
Test: the 7 skips run; a new `test_clean_room_matches_golden_pnl` asserts raw-import net income by month equals
`golden_pnl.csv` after the documented review decisions (3.1 c, d).

### MEDIUM

**M1. Wise import has no continuity check.** Evidence 5.6.
Fix: in `pipeline._tie_checks`, for `wise_csv`, compare the ledger just before the first new row with that row's
running balance minus its amount; refuse on a mismatch and name the gap. This also stops a jar export being booked
to *1430. Files: `src/tc/pipeline.py`. Test: `tests/test_pipeline.py::test_wise_gap_is_refused` (needs the H3 Wise
file, or the fixtures import in tests must change).

**M2. Lock timing vs the card cycle.** A month locked before the card statement closing on the 28th of the next
month makes that statement un-importable until the month is re-opened. October can be locked for good only after
the 28 Nov card statement.
Fix: say so in README step 7 and in the tail-acknowledgement text on Reports (E). Files: `README.md`,
`app/pages/4_Reports.py`. Test: `tests/test_ui_reports.py` asserts the caption.

**M3. Invoice Register CSV is not written on close** (SPEC section 5, UI.md lines 272 and 474).
Fix: `close._export_csv` also writes `<backup stem>_<month>_invoices.csv` from `invoices.list_invoices`.
Files: `src/tc/close.py`, `README.md`. Test: extend `tests/test_close.py::test_close_writes_csv_next_to_backup`
(file exists, 18 rows, labels carry the source).

**M4. No way to unmatch a receipt.** Evidence 5.6.
Fix: `invoices.unmatch_payment(conn, transaction_id)` (lock check, delete the links, invoices back to Pending, row
back to `needs_review`) and a button on Invoices. Files: `src/tc/invoices.py`, `app/pages/5_Invoices.py`.
Test: `tests/test_invoices.py::test_unmatch_payment_restores_pending`.

**M5. Statement date vs workbook date (decision for Joshua).** The importer books the posting date. MYREALPROFIT
47.00 (31 Dec 2025) drops out of FY2026 on the raw route; any charge posted on the 1st for a purchase on the last
day shifts a month. There is no way to change a row's date.
Fix, if the workbook convention is wanted: `pipeline_ui.set_date` limited to open months plus a field on
Transactions. Files: `src/tc/pipeline_ui.py`, `app/pages/3_Transactions.py`.
Test: `tests/test_pipeline_ui.py::test_set_date_open_month_only`.

**M6. Backup and database paths depend on the working directory.** Evidence 5.5.
Fix: `pipeline._backup` calls `close.backup` and prunes there; anchor `db.DEFAULT_DB_PATH` to the repo root.
Files: `src/tc/pipeline.py`, `src/tc/close.py`, `src/tc/db.py`, `README.md`.
Test: `tests/test_pipeline.py::test_import_backup_dir_ignores_cwd` (chdir to tmp, env unset, backup dir patched).

**M7. Jar draws are not importable.** 8 key rows, 1,172.00: on the raw route the balance sheet shows Wise jars
1,172.00 instead of 0.00 and draws are short by the same amount. No P&L effect; the jars are closed. Solved by H1
for 2026; no code needed unless the jars are used again.

### LOW

- **L1. Pruning incomplete and untested** (5.5). Done for re-open backups: `close.prune_backups` keeps the last 30 import
  and the last 30 re-open backups; close backups are kept. Test: `tests/test_close.py::test_reopen_backups_are_pruned_to_the_last_30`.
  Still open: take the close backup after the checks pass.
- **L2. Reimbursable vs Pass-through.** Done: `Reimbursable` everywhere for that category (`rules.yaml`, schema default,
  golden loader, `upgrade.sql` for older databases). Tests: `tests/test_pipeline.py`, `tests/test_rules.py`, `tests/test_db.py`.
- **L3. Spec drift.** Chase `source_ref` format (SPEC section 4, RULES.md 4.1); SPEC test names; `backup("rerun")`
  and the `tc rules rerun` CLI of RULES.md 4.2. Update the docs or add the backup. Files: `docs/SPEC.md`,
  `docs/RULES.md`, `src/tc/pipeline.py`.
- **L4. Lock triggers for `invoice_payments`, `invoices`, `accounts`, `categories`** (5.3). Done for `invoice_payments` and
  `invoices` in `src/tc/upgrade.sql` (`db.upgrade` applies it to an existing database). Test:
  `tests/test_invoices.py::test_invoice_tables_follow_the_month_lock`. Still open: `accounts`, `categories`.
- **L5. Close order.** Refuse to close month M while an earlier month with rows is open; on re-open, list later
  locked months. Done for the refusal (`close.open_months_before`; test `test_cannot_close_a_month_while_an_earlier_one_is_open`).
  Still open: listing later locked months on re-open.
- **L6. Excel export.** Fill Wise Fee (fee rows of the paying receipt) and Net Received; add the Balance Sheet tab
  and a download button; strip `[rule:..]` tags from Notes. Files: `src/tc/export_xlsx.py`, `app/pages/4_Reports.py`.
  Wise Fee and Net Received are done (`invoices.invoice_fees`; test `test_invoice_register_has_wise_fee_and_net_received`,
  INV-013 fee 6.11). Still open: Balance Sheet tab, download button, `[rule:..]` tags in Notes.
- **L7. False warning on a clean file.** `chase5200/2026-01.pdf`: "ledger-computed lowest daily balance 219578 <>
  printed 221365". Fixed: the printed low leaves out the statement's last day, so the ledger-computed low does too
  (`_chase_fee_flags`). Test: `test_chase_january_has_no_false_lowest_balance_warning` (no warning on the 9 PDFs).
- **L8. Small items.** `rules` table empty until the first `add_rule` (no effect on categorisation; fix inside H1);
  uploads not archived; a reviewed fee child blocks merge / split without a message; README says Close is "enabled
  only when" the checks pass, the button is always enabled and refuses on click.

## 8. What a November close needs, in order

1. H1 + H2 delivered; seed `data/tc.db`; confirm 18,618.29 / 32,186.00 / 5,741.63 on Reports.
2. Import the October Chase *5200 PDF and the card PDF closing 28 Oct (its 29-30 Sep lines land in September, so
   September must still be open). Do not re-import any file dated before 1 Nov on Wise.
3. Clear the review queue, lock Jan-Sep, then October once the 28 Nov card statement is in (M2).

Scripts and outputs of this review (temporary, session scratchpad):
`/tmp/claude-0/-home-claude/94379fcb-e29e-551b-a6c7-66068ba8f0d4/scratchpad/f4/cleanroom.py`,
`/tmp/claude-0/-home-claude/94379fcb-e29e-551b-a6c7-66068ba8f0d4/scratchpad/f4/diff.py`,
`/tmp/claude-0/-home-claude/94379fcb-e29e-551b-a6c7-66068ba8f0d4/scratchpad/f4/rules_check.py`,
`/tmp/claude-0/-home-claude/94379fcb-e29e-551b-a6c7-66068ba8f0d4/scratchpad/f4/probe2.py`.
