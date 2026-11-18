# Two Commas LLC bookkeeping tool - UI and monthly-close flow (v1)

Companion to `docs/SPEC.md`, `docs/RULES.md` (pipeline and review-queue design) and `src/tc/schema.sql`. Streamlit multipage app: `app/Home.py` + five pages in
`app/pages/`. Tables only: no dashboards, no charts, no metrics tiles.

Sources read for this document (15 Nov 2026): `docs/SPEC.md`, `src/tc/schema.sql`, `tests/fixtures/`,
`Two_Commas_Monthly_Accounting_SOP.docx` (v1.0, effective Sep 2026), and the "Oct 2026 Close", "Sep 2026 Close",
"Bank Tie-out", "Profit & Loss" and "Balance Sheet" tabs of `Two_Commas_Accounting_2026.xlsx`. Every number in a
wireframe below is a real fixture / workbook value, shown as an example of what the screen prints.

## 0. Conventions used on every page

| Item | Rule |
|---|---|
| Files | `app/Home.py`, `app/pages/1_Import.py`, `app/pages/2_Review_queue.py`, `app/pages/3_Transactions.py`, `app/pages/4_Reports.py`, `app/pages/5_Invoices.py`, shared helpers in `app/ui_common.py` |
| Working month | One sidebar selectbox (`st.session_state["month"]`, `YYYY-MM`), shared by all pages. Options = months that have transactions, plus the month after the last one. Default = earliest month that is not in `month_locks` |
| Lock badge | Sidebar shows `OPEN` or `LOCKED <locked_at>` for the working month (`month_locks`) |
| Money | Stored as INTEGER cents; shown as `1,456.41`; negatives as `-435.77`. Formatting is `db.cents_to_str`; typed amounts are parsed with `db.to_cents` (`Decimal`, never float arithmetic) |
| Card *2110 | Stored negative (signed asset). Every screen shows and accepts it as a **positive liability**, as the workbook does ("Chase card *2110 liability 1,456.41"); the page flips the sign for `accounts.kind = 'card'` |
| Invoice names | Always `v_invoice_labels.label` ("Wise INV-014" vs "Firstbase INV-000014"), never the bare number |
| Tables | `st.dataframe` (read-only). Edits go through small forms with keyed widgets, not `st.data_editor` (AppTest cannot drive it) |
| Errors | Module exceptions and trigger aborts (`sqlite3.IntegrityError`, e.g. `Month is closed and locked`) are caught and shown with `st.error`; the page never shows a traceback |
| Locked month | Write controls are rendered `disabled=True` with the caption "Month YYYY-MM is locked. Re-open it on Reports to change anything." The triggers `trg_locked_tx_*`, `trg_locked_adj_*`, `trg_locked_stmt_*` are the backstop |

Home page (`app/Home.py`) = the working-month selector and the close checklist of section 2. Nothing else.

---

## 1. Pages

### Page 1 - Import (`app/pages/1_Import.py`)

The three importers already exist in `src/tc/importers/` and are path-based; the page saves each upload to a
temporary file and calls `pipeline.run_import`, which is `run_import(file)` of `docs/RULES.md` section 2: backup,
parse, insert, rules (stages 3-8, including the fee split and invoice matching), statement balance.

```
+------------------------------------------------------------------------------------------------+
| IMPORT                                                        month: 2026-10 [OPEN]            |
+------------------------------------------------------------------------------------------------+
| (A) Files  [ Browse... ]  Wise CSV, Chase *5200 PDF, card *2110 PDF (several at once)          |
|     or folder [ inbox/                    ] [Load folder]                                      |
+------------------------------------------------------------------------------------------------+
| (B) Preview - nothing is written yet                                                           |
|  File                 | Detected as  | Account          | Lines | Period          | Locked?    |
|  wise_2026-10.csv     | Wise CSV     | USD account 1430 |     8 | 1 Oct - 29 Oct  | no         |
|                                                                                                |
|  Date       | Description                       | Amount    | Fee  | Source ref                |
|  2026-10-29 | Sent money to Two Commas LLC      | -1,200.00 | 1.13 | TRANSFER-2460784094       |
|  2026-10-29 | Sent money to Joshua F Decker     |   -300.00 | 0.00 | TRANSFER-2459989965       |
|  ...                                                                                           |
+------------------------------------------------------------------------------------------------+
| (C) Statement balance found in file                                                            |
|  Account          | Closing date   | Ending balance | Saved how                                |
|  USD account 1430 | [ 2026-10-31 ] |      13,344.65 | [x] save as statement balance            |
|  (Chase PDFs: saved automatically by the importer at the statement's own closing date)         |
+------------------------------------------------------------------------------------------------+
| (D) [ Import 1 file ]   (backup is taken first)                                                |
+------------------------------------------------------------------------------------------------+
| (E) Result: wise_2026-10.csv - 8 lines inserted, 0 duplicates skipped, 0 before opening date,  |
|     0 rejected (locked month). 12 ledger rows after fee split. Needs review: 0.                |
|     [ Go to Review queue ]                                                                     |
+------------------------------------------------------------------------------------------------+
```

| Element | Fields shown | User action | Table / view / query behind it |
|---|---|---|---|
| (A) Files | file uploader (`.csv`, `.pdf`, multiple); folder path text box | choose files, or type a folder and press Load folder | none. Each file is written to a temp file and parsed by `pipeline.parse_statement` (which calls `parse_wise_csv`, `parse_chase_checking_pdf` or `parse_chase_card_pdf`) |
| (B) Preview, per file | file name, detected kind, account (`accounts.source_label`), line count, first and last row date, locked? | read only | parsed dict only (no DB write). Locked? = `close.is_locked` for each month the rows fall in (`SELECT 1 FROM month_locks WHERE yyyy_mm = ?`) |
| (B) Preview, per line | `date`, `description`, `amount_cents`, `fee_cents`, `source_ref` | read only | the normalized rows of SPEC section 4 (not yet in the DB) |
| (C) Statement balance | account, closing date, ending balance, how it is saved | Wise: closing date editable, untick to skip. Chase PDFs: read only | Chase *5200 / card *2110: the importer itself inserts `statement_balances` (`account_id`, `as_of` = statement period end / cycle end, `balance_cents`, `source` = 'Chase PDF') unless a row for that date exists. Wise: ending balance = `running_balance_cents` of the last row; saved by `close.set_statement_balance` |
| (D) Import button | number of files | click | per file `pipeline.run_import`: backup (`close.backup(conn, "import")`), `INSERT INTO transactions` (skips existing `UNIQUE(account_id, source_ref)`; one file is one DB transaction), then the rules stages: fee split (parent + `FEE-` child), `UPDATE transactions SET category_id, type, review_status`, `INSERT INTO invoice_payments` for WA receipts that match an invoice, and the `statement_balances` row |
| (E) Result | per file: inserted, skipped duplicates, skipped before the account opening date, rejected (locked), ledger rows written, needs-review count by reason | button to Review queue | the report dict returned by `pipeline.run_import` (kept in `st.session_state["import_result"]`) |

Empty / error states

| State | What the page shows |
|---|---|
| Nothing uploaded | Only (A) and one line: "Upload the Wise CSV and the two Chase statements for the month." |
| File type not recognised / parse failure (`ValueError` from `pipeline.parse_statement`, e.g. "not a Wise CSV, missing column(s)", "statement period not found") | `st.error` with the file name and the message; other files still preview |
| Chase statement does not reconcile (`ValueError` from the importer: opening + rows <> printed ending balance) | `st.error`; nothing from that file is written |
| Every line is a duplicate | Result line "0 inserted, N duplicates - already imported." |
| Folder not found / empty | `st.warning("No .csv or .pdf files in <folder>")` |
| Wise ending balance disagrees with an existing `statement_balances` row for the same `account_id, as_of` | Warning with both values; checkbox unticked by default (nothing overwritten silently) |
| Wise export does not run to month-end (last row 29 Oct) | Closing date defaults to the period end in the file name when it has one (`..._2026-10-01_2026-10-31.csv`), else the last row date; caption "check the export covers the whole month" |

Locked month: a file with any row dated in a locked month is marked `locked` in the preview and is not imported
(the importers are all-or-nothing per file; `trg_locked_tx_ins` aborts the insert). The Import button is disabled
when every file is locked. The Wise statement-balance checkbox is disabled when its date is in a locked month
(`trg_locked_stmt_ins`).

### Page 2 - Review queue (`app/pages/2_Review_queue.py`)

```
+------------------------------------------------------------------------------------------------+
| REVIEW QUEUE                                                  month: 2026-10 [OPEN]            |
| 2 rows need a decision      ( ) this month  ( ) all open months          [ Re-run rules ]      |
+------------------------------------------------------------------------------------------------+
| Date       | Account          | Description        | Amount    | Why it is here                |
| 2026-10-xx | J. DECKER 2110   | <new vendor text>  |   -xx.xx  | New vendor - no rule          |
|   Category [ choose...               v ]  Type [ Expense v ]  Note [____________]              |
|   [ ] Always use this for "<pattern>"   [ Save ]                                               |
|------------------------------------------------------------------------------------------------|
| 2026-10-xx | USD account 1430 | W.A. Rubbermate... | x,xxx.xx  | WA receipt - needs an invoice |
|   [ Match to invoice ]  (opens Invoices > Match payment with this receipt selected)            |
|------------------------------------------------------------------------------------------------|
| (when empty)  Nothing to review for 2026-10.                                                   |
+------------------------------------------------------------------------------------------------+
```

"Why it is here" = the reason codes of `docs/RULES.md` section 3, returned by `pipeline.review_queue` in the
`reasons` list of each row (stored as tags in `transactions.notes`, RULES.md decision 6). The page only
translates the code to a label and decides which controls to show:

| Reason code | Shown as | Controls on the row |
|---|---|---|
| `no_rule`, `unknown_payee` | New vendor - no rule | Category / Type / Save, "Always use this" |
| `ambiguous_rules` | Two rules disagree | Category / Type / Save (and fix the rule file) |
| `confirm_rule` | Check this one (e.g. HTGROWTH charge that is not the $1,250 retainer; any wire to WA) | Confirm, or Category / Type / Save |
| `unexpected_fee`, `unexpected_bank_fee` | Unexpected fee | Confirm, or Category / Type / Save |
| `orphan_fee`, `parent_uncategorised` | Fee waiting for its parent | none (follows the parent) |
| `no_invoice_match`, `invoice_amount_mismatch`, `voided_invoice_referenced` | WA receipt - needs an invoice | Match to invoice |
| `invoice_matched_by_amount` | WA receipt matched on amount only | Confirm, or Match to invoice |
| `mixed_invoice_wire` | One wire pays commission and reimbursable invoices | Match to invoice (split on Invoices) |
| `unpaired_transfer` | Transfer - other leg not found | Confirm, or Category / Type / Save |
| `possible_duplicate` | Possible duplicate | Confirm (keep), or Mark as duplicate |

| Element | Fields shown | User action | Table / view / query behind it |
|---|---|---|---|
| Header count | number of rows | - | `SELECT COUNT(*) FROM transactions WHERE review_status = 'needs_review' AND substr(date,1,7) = :month` |
| Scope radio | this month / all open months | choose | same query without the month filter, excluding months in `month_locks` |
| Queue rows | `date`, `accounts.source_label`, `description`, `amount_cents`, `payee`, reason label(s), proposed category (if any) | - | `pipeline.review_queue`: `transactions` JOIN `accounts` LEFT JOIN `categories` WHERE `review_status = 'needs_review'` ORDER BY `date`, `id` |
| Category / Type / Note | selectbox of `categories.name` (ORDER BY `sort_order`); type defaults to `categories.default_type`; free-text note | choose, press Save | `pipeline.set_category` -> `UPDATE transactions SET category_id, type, notes, review_status = 'reviewed'`; child fee rows (`parent_id`) are re-pointed in the same call so `v_fee_parent_mismatch` stays empty |
| Always use this | checkbox + editable pattern (default = the description) | tick before Save | `pipeline.add_rule` -> appends the rule to `src/tc/rules.yaml` and re-syncs the `rules` table (`label`, `pattern`, `match_type`, `category_id`, `type`, `priority`), RULES.md 1.4 |
| Confirm (rows with a proposed category) | category as proposed by the rule | press Confirm, or change category then Save | `pipeline.confirm` -> `UPDATE transactions SET review_status = 'reviewed'` |
| Mark as duplicate (`possible_duplicate` only) | the existing row it duplicates | click | `pipeline.mark_duplicate` -> `DELETE FROM transactions WHERE id = ?` (unlocked month only; `trg_locked_tx_del`) |
| Match to invoice | - | click | sets `st.session_state["match_tx_id"]`, `st.switch_page("pages/5_Invoices.py")` |
| Re-run rules | - | click | `pipeline.run_rules(conn, month=...)`: RULES.md stages 3-8 on rows of the month with `review_status IN ('auto','needs_review')`; never touches `reviewed` rows |

Empty / error states

| State | What the page shows |
|---|---|
| No rows | "Nothing to review for YYYY-MM." |
| Fee row in the queue (`parent_id IS NOT NULL`) | Shown indented under its parent, no category control: "follows the parent" (triggers `trg_fee_follows_parent_ins` / `_upd`) |
| Save rejected by a trigger | `st.error` with the trigger message (e.g. `Fee follows the parent: wrong category for this fee row`) |
| Service Revenue chosen by hand for a WA receipt | Not offered: `Service Revenue` is absent from the selectbox here; revenue is only set by Invoices > Match payment (ONE RULE, `v_revenue_unbacked`) |

Locked month: a locked month cannot contain `needs_review` rows (close requires none), so its queue is empty and
the page shows "Month YYYY-MM is locked." Re-run rules is disabled.

### Page 3 - Transactions (`app/pages/3_Transactions.py`)

```
+------------------------------------------------------------------------------------------------+
| TRANSACTIONS                                                  month: 2026-10 [OPEN]            |
| Month [2026-10 v] Account [all v] Category [all v] Type [all v] Status [all v] Search [______] |
+------------------------------------------------------------------------------------------------+
| 12 rows   in 5,751.13   out -8,052.38   net -2,301.25                                          |
|  id | Date       | Account          | Description                  |   Amount | Category | Type|
| 224 | 2026-10-01 | USD account 1430 | Interest (Received)          |    45.13 | Interest | Inc |
| 225 | 2026-10-02 | USD account 1430 | Two Commas LLC (Sent) - Wise |  -298.87 | Bank Tra | Tra |
| 226 | 2026-10-02 | USD account 1430 |   Wise Charges for: TRANSFER |    -1.13 | Bank Cha | Exp |
| ...   (also: Notes, Status, Invoice label(s), Source ref)                                      |
+------------------------------------------------------------------------------------------------+
| Edit one row   id [ 225 ]  Category [ Bank Transfer v ] Type [ Transfer v ] Note [________]    |
|                [ Save ]                                                                        |
+------------------------------------------------------------------------------------------------+
| > Adjustments (non-bank entries)                                                               |
|  Date       | Label                      | Section      |   Amount | Non-cash | Reason         |
|  2026-09-30 | Prior-period AR adjustment | other_income | 1,562.86 | yes      | ...            |
|  Add: Date [____] Label [____] Section [v] Amount [____] [x] non-cash Reason [____] [ Add ]    |
+------------------------------------------------------------------------------------------------+
| [ Download CSV ]                                                                               |
+------------------------------------------------------------------------------------------------+
```

(Row ids in the wireframe are illustrative positions; the amounts are the Oct 2026 fixture rows.)

| Element | Fields shown | User action | Table / view / query behind it |
|---|---|---|---|
| Filters | month (default = working month, or "all"), account, category, type, review status, text search | choose | `pipeline.search_transactions`: `transactions t JOIN accounts a LEFT JOIN categories c` WHERE `substr(t.date,1,7) = ?` AND `t.account_id = ?` AND `t.category_id = ?` AND `t.type = ?` AND `t.review_status = ?` AND (`t.description LIKE ?` OR `t.payee LIKE ?` OR `t.notes LIKE ?`) ORDER BY `t.date, t.id` |
| Totals line | row count, sum of positive amounts, sum of negative amounts, net | - | computed from the filtered rows |
| Table | `id`, `date`, `accounts.source_label`, `description` (fee rows indented), `amount_cents`, `categories.name`, `type`, `notes`, `review_status`, invoice label(s), `source_ref` | sort, scroll | as above; invoice labels via `invoice_payments` JOIN `v_invoice_labels` |
| Edit one row | id, category, type, note | Save | `pipeline.set_category` (`UPDATE transactions`). Date, amount, account and `source_ref` are never editable (they are the bank's) |
| Adjustments | `date`, `label`, `section`, `amount_cents`, `non_cash`, `reason` | add a row | `SELECT * FROM adjustments WHERE substr(date,1,7) = ?`; `pipeline.add_adjustment` -> `INSERT INTO adjustments` |
| Download CSV | the filtered rows | click | `st.download_button` on the same list |

Empty / error states

| State | What the page shows |
|---|---|
| No rows for the filter | "No transactions match." |
| Empty database | "No transactions yet - start on Import." |
| Unknown row id in Edit | `st.error("No transaction with id N")` |
| Fee row chosen in Edit | Category control disabled: "follows the parent" |
| Row is `Service Revenue` or has `invoice_payments` | Category control disabled: "set by invoice match - change it on Invoices" |
| Adjustment without a reason | Add disabled (`adjustments.reason` is NOT NULL) |

Locked month: when the row's month is in `month_locks`, Edit one row and Add adjustment are disabled. Filters,
table and Download CSV stay available.

### Page 4 - Reports (`app/pages/4_Reports.py`)

One page, five stacked sections, in this order.

```
+------------------------------------------------------------------------------------------------+
| REPORTS                                        year: 2026     month: 2026-09 [OPEN]            |
+------------------------------------------------------------------------------------------------+
| (A) PROFIT & LOSS - cash basis                 | Jan .. Aug |     Sep |      Oct |  YTD 31 Oct |
|  Commission Revenue (cash received)            |        ... |    0.00 | 5,706.00 |   32,186.00 |
|  Total Revenue                                 |        ... |    0.00 | 5,706.00 |   32,186.00 |
|  Professional Services (HTGROWTH retainer...)  |        ... |1,250.00 |     0.00 |   14,033.37 |
|  Postage & Delivery (Firstbase mailbox...)     |        ... |   35.00 |     0.00 |      315.00 |
|  Software & Web Services                       |        ... |   71.41 |     0.00 |      988.70 |
|  Bank Charges (Wise fees, Chase MSF, FX fees)  |        ... |    7.24 |     9.50 |      165.98 |
|  Total SG&A                                    |        ... |1,363.65 |     9.50 |   15,503.05 |
|  OPERATING INCOME                              |        ... |-1,363.65| 5,696.50 |   16,682.95 |
|  Interest Income                               |        ... |   46.93 |    45.13 |      372.48 |
|  Prior-period AR adjustment (non-cash...)      |        ... |1,562.86 |     0.00 |    1,562.86 |
|  NET INCOME                                    |        ... |  246.14 | 5,741.63 |   18,618.29 |
|  Net income before the non-cash AR adjustment  |        ... |-1,316.72| 5,741.63 |   17,055.43 |
|  Months not locked are marked "open" in the column header (e.g. "Oct (open)").                 |
+------------------------------------------------------------------------------------------------+
| (B) STATEMENT BALANCES AND TIE-OUT - 2026-09                                                   |
|  Enter / correct a statement balance                                                           |
|   Account [ Chase *5200 v ]  Statement closing date [ 2026-09-30 ]  Ending balance [ 3,603.52 ]|
|   Source [ Chase PDF ]  [ Save balance ]          (see section 3 for non-calendar periods)     |
|                                                                                                |
|  Bank / card                    |  Opening | Activity | Closing (ledger) | Statement |   Diff  |
|  Wise USD *1430      30 Sep     |17,455.08 |-1,809.18 |        15,645.90 | 15,645.90 |   0.00  |
|  Chase *5200         30 Sep     | 2,504.65 | 1,098.87 |         3,603.52 |  3,603.52 |   0.00  |
|  Chase card *2110    28 Sep     | 1,892.18 |  -435.77 |         1,456.41 |  1,456.41 |   0.00  |
|  ALL TIE: yes           Own-account transfers in transit at 30 Sep: 0.00                       |
+------------------------------------------------------------------------------------------------+
| (C) BALANCE SHEET as of [ 2026-09-30 ]                                                         |
|  ASSETS       Wise US Checking *1430                      15,645.90                            |
|               Wise jars (Taxes / Rent)                         0.00                            |
|               Chase Checking *5200                         3,603.52                            |
|               Total Cash                                  19,249.42                            |
|               Reimbursable Expenses Recoverable (net)      (ledger + adjustments)              |
|               TOTAL ASSETS                                                                     |
|  LIABILITIES  Chase Credit Card *2110                      1,456.41                            |
|               Payable to WA Rubbermate                          0.00                           |
|               (other liability lines from adjustments)                                         |
|               Total Liabilities                                                                |
|  EQUITY (assets - liabilities)                                                                 |
+------------------------------------------------------------------------------------------------+
| (D) MEMO - OWNER'S DRAWS (not in P&L)          | Jan .. Aug |     Sep |      Oct |         YTD |
|  Owner's draws                                 |        ... |1,300.00 |   900.00 |    8,048.00 |
+------------------------------------------------------------------------------------------------+
| (E) CLOSE MONTH 2026-09                                                                        |
|  [ok] 1 Import            [ok] 2 Auto-categorise    [ok] 3 Review unknowns                     |
|  [ok] 4 Statement balances [ok] 5 Tie-out = 0.00    [ok] 6 Invoice check                       |
|  [!!] Card *2110 closes on the 28th: charges dated 29-30 Sep arrive on the next statement.     |
|       [ ] I have imported the statement that covers them, or there are none                    |
|  Close note [ ________________________________ ]            [ Close month 2026-09 ]            |
|  (locked month instead shows)  LOCKED 2026-10-18 "note"  Reason [______] [ Re-open month ]     |
+------------------------------------------------------------------------------------------------+
```

| Element | Fields shown | User action | Table / view / query behind it |
|---|---|---|---|
| (A) P&L monthly + YTD | the 12 lines of `tests/fixtures/golden_pnl.csv`, one column per month Jan..Dec with data, plus YTD | year selectbox | `reports.pnl_monthly`: `SELECT yyyy_mm, category, section, amount_cents FROM v_pnl_monthly WHERE substr(yyyy_mm,1,4) = :year`; line labels from `categories.pnl_line` ORDER BY `categories.sort_order`; expenses shown positive; adjustment lines from `SELECT substr(date,1,7), label, section, amount_cents FROM adjustments WHERE section IN ('revenue','sga','other_income')`; "before the non-cash adjustment" = net income minus `adjustments` with `non_cash = 1` |
| (B) Balance entry form | account (only `accounts.tie_required = 1 AND active = 1`), statement closing date, ending balance, source; under it the rows already saved for the month (date, balance, source) | Save balance | `close.set_statement_balance` -> upsert `statement_balances` on `UNIQUE(account_id, as_of)`. Defaults from `close.expected_statement_dates`; saved rows from `reports.tie_out` |
| (B) Tie-out table | account, statement date, opening, activity, closing (ledger), statement, diff | - | `reports.tie_out`: `SELECT account_code, as_of, ledger_cents, statement_cents, diff_cents FROM v_tie_out WHERE yyyy_mm = :month`; opening = `ledger_cents` of the previous `v_tie_out` row for that account, or `accounts.opening_balance_cents`; activity = ledger - opening. Accounts with no statement row print `pending` in all five cells, as the "Oct 2026 Close" tab does |
| (B) All-tie line | yes / no; transfers in transit | - | every `diff_cents = 0`; `reports.transfers_in_transit`: `SELECT SUM(net_cents) FROM v_transfer_net WHERE yyyy_mm <= :month` (at 31 Oct 2026: -1,497.74, the two Wise->Chase legs awaiting the Chase statement) |
| (C) Balance sheet | lines of the workbook "Balance Sheet" tab | as-of date (default = last day of working month) | `reports.balance_sheet`: cash and card lines = `accounts.opening_balance_cents + SUM(transactions.amount_cents WHERE date <= :as_of)` per account (jars summed into one line; card sign flipped); Reimbursable = minus `SUM(amount_cents)` of category `Reimbursable Expenses Recoverable`; Payable to WA = `SUM(amount_cents)` of category `Payable to WA Rubbermate`; plus `adjustments` rows with `section IN ('asset','liability','equity')` dated `<= :as_of`; equity = assets - liabilities |
| (D) Owner's draws memo | one row, monthly + YTD | - | `reports.owner_draws`: `SELECT substr(t.date,1,7), -SUM(t.amount_cents) FROM transactions t JOIN categories c ON c.id = t.category_id WHERE c.statement = 'equity' GROUP BY 1` |
| (E) Checklist | six check lines + warnings | - | `close.checklist` (section 2) |
| (E) Close month button | close note (required) | click | `close.close_month`: `close.backup(conn, "close")`, re-run checks, `INSERT INTO month_locks (yyyy_mm, reason)` (trigger `trg_month_lock_requires_tie_out`), CSV export of Transactions and Invoice Register to `backups/` |
| (E) Re-open month | reason (required) | click | `close.reopen_month`: `close.backup(conn, "reopen")`, `DELETE FROM month_locks WHERE yyyy_mm = ?` |

Empty / error states

| State | What the page shows |
|---|---|
| No transactions in the year | P&L: "No transactions for YYYY." Other sections hidden |
| Month has no statement balance for an account | Tie-out row prints `pending`; ALL TIE: no |
| `diff_cents <> 0` | Diff cell shows the amount; below the table: "Chase *5200 is off by 15.00 - look for a missing or duplicated line between <previous as_of> and <as_of>" with a button to Transactions filtered to that account and month |
| Balance typed is not a number | `st.error("Enter the balance as 1,234.56")`; nothing saved |
| Closing date outside the working month | Warning "This date is in YYYY-MM, not the working month"; Save still allowed |
| Balance sheet: no `adjustments` rows in `asset` / `liability` / `equity` | Caption: "Opening balance-sheet items not loaded" (see open point 1 in section 5) |
| Close blocked | Close button disabled; each failed check line names what to fix and links to the page that fixes it |
| Trigger abort on close (`Cannot close month: ...`) | `st.error` with the trigger message |

Locked month: balance entry form and Save balance disabled; Close month replaced by the LOCKED line and
Re-open month (typed reason required). P&L, tie-out, balance sheet and draws memo stay readable.

### Page 5 - Invoices (`app/pages/5_Invoices.py`)

```
+------------------------------------------------------------------------------------------------+
| INVOICES                                                      month: 2026-10 [OPEN]            |
+------------------------------------------------------------------------------------------------+
| (A) CHECK: commission paid vs revenue, 2026                                                    |
|  Commission invoices paid (gross) | Service Revenue in ledger | Difference                     |
|                         32,186.00 |                 32,186.00 |       0.00   OK                |
|  Commission invoice missing more than 45 days after month-end: none                            |
+------------------------------------------------------------------------------------------------+
| (B) UNPAID                                                                                     |
|  Invoice      | Type         | Issued     | Due        |   Amount | Days open                  |
|  Wise INV-010 | Reimbursable | 2026-10-02 | 2026-10-09 | 5,645.14 |        44                  |
|  Total unpaid: commission 0.00   reimbursable 5,645.14                                         |
+------------------------------------------------------------------------------------------------+
| (C) REGISTER      Status [all v]  Type [all v]                                                 |
|  Invoice             | Issued     | Comm. month | Type       | Boxes | Rate |   Amount | Status|
|  Wise INV-014        | 2026-10-16 | 2026-09     | Commission | 5,380 | 0.50 | 2,690.00 | Paid  |
|  Wise INV-013        | 2026-10-16 | 2026-08     | Commission | 6,032 | 0.50 | 3,016.00 | Paid  |
|  Wise INV-012        | 2026-10-09 | 2026-08     | Voided     | 6,032 | 0.50 | 3,016.00 | Voided|
|  ...  (also: Paid on, Paid by transaction, Replaces, Notes)                                    |
|  Void invoice [ Wise INV-0xx v ]  Reason [________]  [ Void ]                                  |
+------------------------------------------------------------------------------------------------+
| (D) ADD INVOICE                                                                                |
|  Source [ Wise v ]  Number [ INV-015 ]  Issue date [____]  Due date [____]                     |
|  Type ( ) Commission  ( ) Reimbursable                                                         |
|   Commission:   Month [ 2026-10 ]  Boxes [ ____ ]  Rate 0.50 (fixed)  Amount = boxes x 0.50    |
|   Reimbursable: Amount [ ____ ]                                                                |
|  Replaces voided invoice [ none v ]   Notes [______________]              [ Add invoice ]      |
+------------------------------------------------------------------------------------------------+
| (E) MATCH PAYMENT                                                                              |
|  Receipt [ 2026-10-19  USD account 1430  W.A. Rubbermate ...  5,706.00  v ]                    |
|  Invoices paid by this receipt (gross amounts)                                                 |
|   [x] Wise INV-013   3,016.00        [x] Wise INV-014   2,690.00        [ ] Wise INV-010       |
|  Receipt 5,706.00   allocated 5,706.00   left 0.00                        [ Match ]            |
+------------------------------------------------------------------------------------------------+
```

| Element | Fields shown | User action | Table / view / query behind it |
|---|---|---|---|
| (A) Commission check | commission invoices paid in the year (gross), Service Revenue in the ledger, difference | year follows the working month | `invoices.commission_check`: paid = `SELECT SUM(ip.amount_cents) FROM invoice_payments ip JOIN invoices i ON i.id = ip.invoice_id JOIN transactions t ON t.id = ip.transaction_id WHERE i.type = 'Commission' AND substr(t.date,1,4) = :year`; revenue = `SELECT SUM(amount_cents) FROM v_pnl_monthly WHERE category = 'Service Revenue' AND substr(yyyy_mm,1,4) = :year`. Any row in `v_revenue_unbacked` is listed under the check |
| (A) Missing commission invoice | months without a commission invoice | - | `invoices.missing_commission_months`: months from January of the year to the working month with no `invoices` row where `type = 'Commission' AND commission_month = :m`, and today more than 45 days past that month-end |
| (B) Unpaid list | `v_invoice_labels.label`, `type`, `issue_date`, `due_date`, `amount_cents`, days open; totals by type | - | `invoices.unpaid`: `SELECT ... FROM invoices i JOIN v_invoice_labels l ON l.id = i.id WHERE i.status = 'Pending' ORDER BY i.issue_date` |
| (C) Register | label, `issue_date`, `commission_month`, `type`, `boxes`, `rate_cents`, `amount_cents`, `status`, paid on, paying transaction, replaces, `notes` | filter by status / type | `invoices.list_invoices`: `invoices` JOIN `v_invoice_labels` LEFT JOIN `invoice_payments` LEFT JOIN `transactions`; "paid on" = `transactions.date` of the paying receipt (SPEC open point 1: the ledger date, not a typed date) |
| (C) Void | invoice (Pending only), reason | Void | `invoices.void_invoice` -> `UPDATE invoices SET type = 'Voided', status = 'Voided'`, reason appended to `notes` (`ck_invoice_voided_status`) |
| (D) Add invoice | `source_label`, `number`, `issue_date`, `due_date`, `type`, `commission_month`, `boxes`, `rate_cents` (fixed 50), `amount_cents`, `replaces_invoice_id`, `notes` | Add invoice | `invoices.add_invoice` -> `INSERT INTO invoices (..., status = 'Pending')`; `UNIQUE(source_label, number)`; `ck_invoice_commission_single_line` |
| (E) Receipt selectbox | unmatched WA receipts: `date`, account, `description`, `amount_cents` | choose (pre-selected when arriving from Review queue via `st.session_state["match_tx_id"]`) | `invoices.unmatched_receipts`: `transactions` with `amount_cents > 0`, flagged by a `rules.action = 'needs_invoice'` rule, where `SUM(invoice_payments.amount_cents)` is less than `amount_cents` |
| (E) Invoice checkboxes | Pending invoices with gross amounts | tick the invoices this wire paid | `invoices.unpaid` |
| (E) Match | receipt, allocated, left | Match (enabled only when left = 0.00) | `invoices.match_payment`: `INSERT INTO invoice_payments (transaction_id, invoice_id, amount_cents)` per invoice (trigger `trg_one_rule_invoice_payment`); `UPDATE transactions SET category_id, type, review_status = 'reviewed'` (Commission -> `Service Revenue` / `Income`; Reimbursable -> `Reimbursable Expenses Recoverable` / `Reimbursable`); `UPDATE invoices SET status = 'Paid'` |

Empty / error states

| State | What the page shows |
|---|---|
| No invoices | "No invoices yet." under Register; Unpaid: "Nothing unpaid." |
| Difference not 0.00 | Check line in red: "Revenue is not fully backed by commission invoices" + the `v_revenue_unbacked` rows (date, amount, backed amount) |
| Duplicate number for the same source | `st.error("Wise INV-015 already exists")` (`UNIQUE(source_label, number)`) |
| Commission with boxes = 0 or no month | Add disabled; amount is always computed, never typed |
| One wire mixing Commission and Reimbursable invoices | Match is replaced by "Split receipt and match": `invoices.split_receipt` replaces the bank line by one row per invoice type (`source_ref` = original, `original#2`; amounts add up to the original), then matches each (RULES.md section 3 action 4, decision 1) |
| Allocation does not equal the receipt | Match disabled; "left" shows the gap |
| No unmatched receipts | "No WA receipts waiting for an invoice." |
| Void an invoice that has `invoice_payments` | Not offered (only Pending invoices are in the Void selectbox) |

Locked month: receipts dated in a locked month are not offered in (E), and `invoices.match_payment` raises
`close.LockedMonthError` for them (there is no lock trigger on `invoice_payments`, so the module must check).
Add invoice and Void stay available: an unpaid invoice has no effect on a cash-basis month. Register, Unpaid and
the check stay readable.

---

## 2. Monthly close checklist (the on-screen flow)

Shown in full on Home and as the check lines of Reports (E). Each line is one check item (dict) from `close.checklist`;
a line that is not OK links to the page that fixes it. Steps 1-7 run top to bottom; the Close month button is
enabled only when every blocking line is OK.

```
+------------------------------------------------------------------------------------------------+
| CLOSE 2026-10                                                                    [OPEN]        |
+------------------------------------------------------------------------------------------------+
| [ok] 1 Import            Wise *1430: 12 rows.                              [ Import ]          |
| [!!]                     Chase *5200: 0 rows.  Card *2110: 0 rows.                             |
| [ok] 2 Auto-categorise   12 of 12 rows categorised by rules.               [ Review queue ]    |
| [ok] 3 Review unknowns   0 rows need review.                               [ Review queue ]    |
| [!!] 4 Statement balances Missing: Chase *5200, card *2110.                [ Reports ]         |
| [!!] 5 Tie-out = 0.00    Wise *1430 0.00. Chase *5200 pending. Card *2110 pending. [ Reports ] |
| [ok] 6 Invoice check     Commission paid 32,186.00 = revenue 32,186.00.    [ Invoices ]        |
|                          Unpaid: Wise INV-010 5,645.14 (due 9 Oct).                            |
| [  ] 7 Lock month        Blocked by steps 4 and 5.                         [ Reports ]         |
+------------------------------------------------------------------------------------------------+
```

| # | Step | check item `key` | OK when | Query | Blocks close? | Fixed on |
|---|---|---|---|---|---|---|
| 1 | Import | `imported` | every active `tie_required` account has at least one row in the month | `SELECT a.code, COUNT(t.id) FROM accounts a LEFT JOIN transactions t ON t.account_id = a.id AND substr(t.date,1,7) = :m WHERE a.tie_required = 1 AND a.active = 1 GROUP BY a.id` | No (warning: a month with no activity is possible; step 4 is the real gate) | Import |
| 2 | Auto-categorise | `categorized` | no row in the month has `category_id IS NULL` | `SELECT COUNT(*) FROM transactions WHERE substr(date,1,7) = :m AND category_id IS NULL` | Yes (`trg_month_lock_requires_tie_out`) | Review queue |
| 3 | Review unknowns | `reviewed` | no row in the month is `needs_review` | `SELECT COUNT(*) FROM transactions WHERE substr(date,1,7) = :m AND review_status = 'needs_review'` | Yes (application check, SPEC section 5 item 4) | Review queue |
| 4 | Enter statement ending balances | `balances_entered` | every active `tie_required` account has a `statement_balances` row dated in the month (Chase PDFs: written by the importer; Wise: the Import checkbox; any of them by hand) | `SELECT a.code FROM accounts a WHERE a.tie_required = 1 AND a.active = 1 AND NOT EXISTS (SELECT 1 FROM statement_balances sb WHERE sb.account_id = a.id AND substr(sb.as_of,1,7) = :m)` | Yes (trigger) | Reports (B), or the checkbox on Import |
| 5 | Tie-out, all accounts = 0.00 | `tie_out_zero` | every `v_tie_out` row of the month has `diff_cents = 0` | `SELECT account_code, as_of, diff_cents FROM v_tie_out WHERE yyyy_mm = :m` | Yes (trigger) | Reports (B) -> Transactions |
| 6 | Invoice check | `revenue_backed`, `fees_follow_parent`, `commission_equals_revenue`, `unpaid_listed` | `v_revenue_unbacked` empty; `v_fee_parent_mismatch` empty; `invoices.commission_check` difference = 0; unpaid list and missing-commission-invoice months are displayed | `SELECT COUNT(*) FROM v_revenue_unbacked`; `SELECT COUNT(*) FROM v_fee_parent_mismatch`; queries of Invoices (A) and (B) | First two: yes (SPEC section 5 item 4). Commission = revenue: warning only (not a close condition in SPEC). Unpaid list: information | Invoices |
| 7 | Lock month | `tail_covered` (warning) then the button | steps 2-6 blocking lines OK, close note typed, tail warning acknowledged | see below | - | Reports (E) |

`tail_covered` (warning, needs a tick, not a hard block): for each tie-required account, is there a
`statement_balances` row with `as_of >=` the last day of the month? If not, the statement in hand stops before
month-end (card *2110 always: it closes on the 28th; Chase *5200 in some months) and rows dated in the gap will
arrive on the next statement. Once the month is locked that whole statement file is refused (SPEC section 4
rule 4; the importers are all-or-nothing per file). See
section 3.

Auto-categorise is not a separate button press: `pipeline.run_import` runs the rules as part of the import. Step 2 only reports the result; "Re-run rules" on the Review queue repeats it.

### 2.1 SOP v1.0 -> tool mapping (every SOP step, in SOP order)

SOP text is quoted from `Two_Commas_Monthly_Accounting_SOP.docx`. "Checklist" = the step number in the table
above. Steps the tool cannot do (creating an invoice inside Wise, pulling a box count) stay manual and say so.

Monthly Workflow (1st of each month)

| SOP id | SOP step (verbatim) | Replaced by (page / button) | Checklist |
|---|---|---|---|
| W1 | Wise transaction export (CSV) - covers the prior month | Import > Files > Import (Wise CSV) | 1 |
| W2 | Chase CC statement (PDF) and Chase Checking statement (PDF) | Import > Files > Import (card *2110 PDF, Chase *5200 PDF) | 1 |
| W3 | Claude categorizes all transactions per the Vendor Rules tab in the accounting workbook | Automatic inside Import (`pipeline.run_import` applies the `rules`); Review queue > Re-run rules | 2 |
| W4 | Claude updates the Transactions tab, P&L, and Balance Sheet in the master workbook | Nothing to do: Transactions page, Reports (A) P&L and Reports (C) Balance sheet read the ledger live | - |
| W5 | Claude flags any transactions that don't match a known vendor for Joshua to classify | Review queue ("New vendor - no rule") > Save | 3 |
| W6 | Claude checks for missing commission invoices and reminds Joshua to create them | Invoices (A) "Commission invoice missing" line; repeated in checklist step 6 | 6 |
| W7 | Claude outputs the updated workbook and a brief monthly summary | Reports page (P&L, tie-out, balance sheet, draws memo); Reports > Close month writes the CSV exports and backup | 7 |

Commission Invoice Process

| SOP id | SOP step (verbatim) | Replaced by (page / button) | Checklist |
|---|---|---|---|
| C1 | After each month ends, Joshua pulls the box count from Amazon Seller Central or MyRealProfit and creates a commission invoice to WA Rubbermate. The invoice must follow this exact format: | Stays manual (Seller Central / MyRealProfit, then Wise). Then Invoices > Add invoice, type Commission | 6 |
| C2 | Single line item: "Commission for units (box) sold during [Month Year]" | Invoices > Add invoice: Commission type has one Month field (`invoices.commission_month`), no line items | 6 |
| C3 | Quantity: number of boxes sold that month | Invoices > Add invoice: Boxes (`invoices.boxes`) | 6 |
| C4 | Rate: $0.50 per box | Invoices > Add invoice: Rate fixed at 0.50, amount computed (`ck_invoice_commission_single_line`) | 6 |
| C5 | No other line items on the invoice | Enforced: a Commission invoice has no other amount field; anything else is a separate Reimbursable invoice | 6 |
| C6 | Create through Wise (not Firstbase) to avoid auto-routing issues | Stays manual in Wise. Invoices > Add invoice: Source defaults to Wise (`invoices.source_label`) | 6 |
| C7 | Claude tracks the commission for every month in the Commission Tracker tab. If a month is missing, Claude will flag it. | Invoices (C) Register filtered to Commission (one row per `commission_month`) + Invoices (A) "Commission invoice missing" line | 6 |

Reimbursable Invoice Process

| SOP id | SOP step (verbatim) | Replaced by (page / button) | Checklist |
|---|---|---|---|
| R1 | Record the outflow: Debit Reimbursable Expenses Recoverable, Credit Cash | Automatic after Import: Maersk / Paycargo / Pierpass / Portcheck / CBP rules set `Reimbursable Expenses Recoverable` | 2 |
| R2 | Create a reimbursement invoice to WA through Wise (NOT Firstbase) | Stays manual in Wise. Then Invoices > Add invoice, type Reimbursable | 6 |
| R3 | When WA pays: Debit Cash, Credit Reimbursable Expenses Recoverable | Invoices > Match payment (receipt -> the Reimbursable invoice) | 3, 6 |
| R4 | No P&L impact at any stage. Reimbursable invoices never appear as revenue. | Enforced by `trg_one_rule_invoice_payment` and `categories.statement = 'balance_sheet'`; shown on Reports (C), never on Reports (A) | 6 |

Transaction Classification Rules

| SOP id | SOP step (verbatim) | Replaced by (page / button) | Checklist |
|---|---|---|---|
| T0 | The single rule: if it's not a commission invoice, it's not revenue. | Invoices > Match payment is the only way a row becomes `Service Revenue`; checked by `v_revenue_unbacked` | 6 |
| T1 | WA commission invoice | Invoices > Match payment against a Commission invoice -> `Service Revenue` | 6 |
| T2 | WA all other invoices | Invoices > Match payment against a Reimbursable invoice -> `Reimbursable Expenses Recoverable` | 6 |
| T3 | Maersk / Paycargo / Pierpass / Portcheck | `rules` rows, applied automatically after Import | 2 |
| T4 | HTGROWTH ($1,250/mo) | `rules` row, automatic; charges that are not the 1,250.00 retainer go to the Review queue (`confirm_rule`) | 2, 3 |
| T5 | Firstbase ($149 + $35) | `rules` rows keyed on `rules.amount_cents` (-3500 -> `Postage & Delivery`), automatic | 2 |
| T6 | Sellerboard / Google / MyRealProfit / Claude / OpenAI | `rules` rows, automatic | 2 |
| T7 | Betterment Securities | `rules` row -> `Owner's Draw`, automatic; shown in Reports (D) | 2 |
| T8 | Joshua F Decker / Joshua Decker (any amount, incl. $293) | `rules` row -> `Owner's Draw`, automatic; shown in Reports (D) | 2 |
| T9 | Joshua Decker (other) | Same `rules` row as T8 (SPEC Vendor Rules row 18 is merged into row 17) | 2 |
| T10 | IRS / US Treasury (federal income tax) | `rules` row -> `Owner's Draw`, automatic; shown in Reports (D) | 2 |
| T11 | Wise -> Chase transfers | `rules` row -> `Bank Transfer`, automatic; in-transit total on Reports (B) | 2, 5 |
| T12 | Interest received | `rules` row -> `Interest Income`, automatic | 2 |

When to Flag for Joshua

| SOP id | SOP step (verbatim) | Replaced by (page / button) | Checklist |
|---|---|---|---|
| F1 | Any WA Rubbermate receipt that cannot be matched to a known invoice | Review queue "WA receipt - needs an invoice" > Match to invoice > Invoices (E) | 3 |
| F2 | Any HTGROWTH charge above the $1,250 monthly retainer (could be TC or WA reimbursable) | Review queue "Check this one" (`confirm_rule`, rule `htgrowth-other`) > Confirm or re-categorise | 3 |
| F3 | Any new vendor not in the Vendor Rules tab | Review queue "New vendor - no rule" > Save (+ "Always use this" adds the rule) | 3 |
| F4 | Any month where a commission invoice has not been created within 45 days of month-end | Invoices (A) "Commission invoice missing" line; checklist step 6 | 6 |
| F5 | Any foreign transaction fees (use no-FTF card for international purchases) | Not queued today: RULES.md rule `card-fx-fee` books it to `Bank Charges` automatically. Seen on Transactions (category Bank Charges). Queued only if that rule gets `review: true` (open point 4) | 2 |
| F6 | Any credit card interest charges (autopay should prevent this) | Review queue "New vendor - no rule": no rule exists for card interest, so the first one lands in the queue | 3 |

Monthly Output

| SOP id | SOP step (verbatim) | Replaced by (page / button) | Checklist |
|---|---|---|---|
| O1 | Updated master workbook (Transactions, Invoice Register, P&L, Balance Sheet, Commission Tracker) | Transactions page, Invoices (C) Register, Reports (A) and (C); Close month exports Transactions and Invoice Register CSVs to `backups/` | 7 |
| O2 | One-paragraph summary: revenue, expenses, net income, any flags | Reports (A) column for the month (Total Revenue, Total SG&A, NET INCOME) + the checklist lines. No generated paragraph in v1 | 7 |
| O3 | List of action items (missing invoices, unclassified transactions, WA amounts to follow up) | Checklist: step 6 (missing commission invoices, unpaid list), step 3 (review count) | 3, 6 |

Checklist steps with **no** SOP v1.0 counterpart (the SOP does not mention them; they come from SPEC rule 8 and
from what the "Bank Tie-out" / "Sep 2026 Close" / "Oct 2026 Close" tabs already do):

| Checklist step | Source |
|---|---|
| 4 Enter statement ending balances | SPEC rule 8; workbook "Bank Tie-out" row "Statement balance" |
| 5 Tie-out, all accounts = 0.00 | SPEC rule 8; workbook close tabs (Opening / Activity / Closing / Statement / Diff) |
| 7 Lock month | SPEC rule 8 and section 5 (`month_locks`) |

The free-text STATUS / FLAGS notes on the workbook close tabs (BOOKED / OPEN / WATCH / CARRIED OVER) have no
table in the schema. The computed checklist replaces the OPEN items; the close note typed at step 7 is stored in
`month_locks.reason`.

---

## 3. Balance entry when a statement period is not a calendar month

Applies to the balance-entry form on Reports (B) and the statement-balance checkbox on Import (C).

What the files actually show:

| Account | Statement period | Examples from `tests/fixtures/raw/` |
|---|---|---|
| Wise *1430 | calendar month (CSV export, running balance) | 1-31 Oct, ending 13,344.65 |
| Chase *5200 | **not** a calendar month | Jan 1 - Jan 30; Jan 31 - Feb 27; Feb 28 - Mar 31; May 1 - May 29; May 30 - Jun 30; Sep 1 - Sep 30 |
| Card *2110 | 29th to 28th (`accounts.statement_day = 28`) | 29 Aug - 28 Sep, New Balance 1,456.41 |

How `v_tie_out` works: for each `statement_balances` row it compares the statement balance with the ledger
summed **up to that row's `as_of` date**. The lock trigger only asks for a row whose `as_of` is inside the month.
So the date typed matters as much as the amount.

Rules for the screen:

1. **Use the statement's own closing date and its Ending Balance.** Chase May statement: `2026-05-29`,
   1,932.58 - not 2026-05-31. Card: the 28th and the "New Balance" (typed positive). Importing a Chase PDF does
   this by itself: `import_chase_checking` and `import_chase_card` save the balance at the period end / cycle end
   printed in the file, so the form is normally needed only for Wise or for a correction. When typing by hand the
   date defaults to `close.expected_statement_dates` (month-end for banks, the 28th for the card) and the caption
   says "check the date printed on the statement".
2. **Why not month-end:** the workbook and `tests/fixtures/statement_balances.csv` type Chase balances at the
   calendar month-end (e.g. 2026-05-31 = 1,932.58). That ties only because nothing posted on 30-31 May. A line
   posted in the gap would show as a false difference. Dating the balance at the real closing date always ties.
3. **Days after the closing date belong to the same P&L month but arrive on the next statement.** Chase
   30-31 May lines are printed on the "May 30 - Jun 30" statement; card charges dated 29-31 are on the next
   cycle (the fixtures hold 12 such card rows for Jan-Sep; the workbook notes "29-30 Sep charges (e.g. Anthropic)
   land on the Oct statement"). The P&L uses `transactions.date`, so they count in the earlier month.
   **Do not lock month M until the statement that contains its last days is imported** - otherwise that
   statement file is refused by the lock. This is the `tail_covered` warning at checklist step 7. In practice: lock M after
   the card statement closing on the 28th of M+1 is in.
4. **Optional second row at month-end.** Once the next Chase statement is imported, a second balance can be
   saved for the last day of M using that statement's "DAILY ENDING BALANCE" table (the last date listed on or
   before month-end; the balance carries forward to month-end). `UNIQUE(account_id, as_of)` allows several rows
   per month; every one of them must show diff 0.00. This also clears the `tail_covered` warning for Chase.
5. **If no Chase statement closes inside month M** (not seen in the Jan-Sep 2026 files read above, but possible
   with these drifting periods): use the "DAILY ENDING BALANCE" line for the last day of M from the statement
   that spans it, dated at that day.
6. **Balance sheet dates.** Reports (C) uses the ledger balance at the as-of date, so the card line at 30 Sep
   includes 29-30 Sep charges even though the 28 Sep statement does not.
7. **Chase $15 fee (SPEC rule 7).** The fee test uses the daily ledger balance over the statement period, not the
   calendar month; no screen in v1 depends on it.

---

## 4. Streamlit implementation notes

### 4.1 Page -> module map

Already in the repo (read 15 Nov 2026): `src/tc/db.py` (`connect`, `init_db`, `to_cents`, `cents_to_str`,
`insert_transaction`, `list_transactions`), `src/tc/importers/wise.py`, `chase_checking.py`, `chase_card.py`,
`src/tc/fixtures.py` (`load_golden`). To be written against this document: `src/tc/pipeline.py`,
`src/tc/reports.py`, `src/tc/invoices.py`, `src/tc/close.py`, `app/ui_common.py` and the pages.

| Page | Calls |
|---|---|
| `app/Home.py` | `ui_common`, `close` |
| `app/pages/1_Import.py` | `ui_common`, `pipeline` (`src/tc/pipeline.py`, which wraps `src/tc/importers/*`), `close` (backup, Wise statement balance, lock test), `db` (money) |
| `app/pages/2_Review_queue.py` | `ui_common`, `pipeline`, `close` (lock test) |
| `app/pages/3_Transactions.py` | `ui_common`, `pipeline`, `close` (lock test), `db` (money) |
| `app/pages/4_Reports.py` | `ui_common`, `reports` (`src/tc/reports.py`), `close` (`src/tc/close.py`), `invoices` (commission check line), `db` (money) |
| `app/pages/5_Invoices.py` | `ui_common`, `invoices` (`src/tc/invoices.py`), `close` (lock test), `db` (money) |

Pages contain no SQL. All queries listed in section 1 live in the modules. Imports are
`from tc import db, pipeline, reports, invoices, close`; the `tc` package must be importable
(`pip install -e .`; pytest already has `pythonpath = ["src"]`). Each page starts with:

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # app/  (Home.py: parents[0])
import ui_common
conn = ui_common.get_conn()
month = ui_common.sidebar(conn)
```

so that a page also runs on its own under `AppTest.from_file` (section 4.5).

### 4.2 Session-state keys

| Key | Type | Set by | Read by | Meaning |
|---|---|---|---|---|
| `month` | `str` `YYYY-MM` | `ui_common.sidebar` (selectbox key) | all pages | working month |
| `import_dir` | `str` | Import (text input key) | Import | folder to load from; default `inbox/` |
| `import_parsed` | `list[dict]` (parsed statements, each with its temp `path`) | Import, after parse | Import | files awaiting confirmation |
| `import_wise_as_of`, `import_wise_save` | widget keys | Import (C) | Import | Wise statement-balance date and tick |
| `import_result` | `list[dict]` (one `pipeline.run_import` report per file, plus `filename`) | Import, after the button | Import | result panel (E); cleared on a new upload |
| `rq_scope` | `"month"` or `"all"` | Review queue (radio key) | Review queue | queue scope |
| `rq_cat_<tx_id>`, `rq_type_<tx_id>`, `rq_note_<tx_id>`, `rq_rule_<tx_id>`, `rq_save_<tx_id>`, `rq_confirm_<tx_id>`, `rq_dup_<tx_id>`, `rq_match_<tx_id>` | widget keys | Review queue | Review queue | one set per queue row |
| `match_tx_id` | `int` or absent | Review queue "Match to invoice" | Invoices (E); deleted after a successful match | receipt to pre-select |
| `tx_filters` | `dict` (`month`, `account_id`, `category_id`, `type`, `review_status`, `search`) | Transactions; Reports "off by" button | Transactions | current filters |
| `tx_edit_id` | `int` | Transactions (number input key) | Transactions | row being edited |
| `rep_year` | `int` | Reports (selectbox key) | Reports | P&L / memo year |
| `rep_bs_as_of` | `datetime.date` | Reports (date input key) | Reports | balance sheet date |
| `bal_account`, `bal_as_of`, `bal_amount`, `bal_source`, `bal_save` | widget keys | Reports (B) | Reports | balance entry form |
| `close_note`, `close_tail_ack`, `close_btn` | widget keys | Reports (E) | Reports | close note, tail-warning tick, button |
| `reopen_reason`, `reopen_btn` | widget keys | Reports (E) | Reports | re-open |
| `inv_filter_status`, `inv_filter_type` | widget keys | Invoices (C) | Invoices | register filters |
| `inv_new_*` (`source`, `number`, `issue_date`, `due_date`, `type`, `month`, `boxes`, `amount`, `replaces`, `notes`), `inv_add_btn` | widget keys | Invoices (D) | Invoices | add-invoice form |
| `match_receipt`, `match_inv_<invoice_id>`, `match_btn` | widget keys | Invoices (E) | Invoices | match-payment form |
| `flash` | `tuple[str, str]` (`"success"` / `"error"`, text) | any page before `st.rerun()` | `ui_common.sidebar` (shows once, then deletes) | one-shot message |

Every button has a fixed `key` (listed above, plus `import_btn`, `import_load_dir_btn`, `rq_rerun_btn`,
`tx_save_btn`, `adj_add_btn`, `inv_void_btn`) so tests can address it.

Other notes: the connection is opened once with `@st.cache_resource` (`sqlite3.connect(...,
check_same_thread=False)` is needed, so `ui_common.get_conn` cannot use `db.connect` unchanged - see open point 3);
no `@st.cache_data` on report queries (the DB is tiny, and stale numbers after an edit are worse than a
re-query). After any write: set `flash`, call `st.rerun()`. Navigation between pages uses `st.button` +
`st.switch_page` (executed only on click); do not use `st.page_link`, it needs the multipage context that a
single-page AppTest run does not have. Database path: environment variable `TC_DB_PATH`, default
`db.DEFAULT_DB_PATH` (`data/tc.db`); `ui_common.get_conn` calls `db.init_db` when the file has no tables.

### 4.3 Data shapes returned by the modules

Plain dicts (as the existing importers return); cents as `int`; dates as ISO `str`. Pages format for display.

| Name | Defined in | Keys / fields |
|---|---|---|
| parsed statement | `pipeline.parse_statement` | `path`, `filename`, `kind` (`wise_csv` / `chase5200_pdf` / `chase5200_csv` / `card2110_pdf`), `account` (`accounts.source_label`), `rows` (SPEC section 4 fields), `period_start`, `period_end`, `opening_cents`, `ending_cents` (signed asset value; card negative; `None` when the file does not carry it) |
| import report | `pipeline.run_import` | `inserted`, `skipped_duplicate`, `skipped_before_opening`, `rejected_locked`, `ledger_rows`, `review_counts` (dict reason code -> count), as in RULES.md section 2 |
| rules report | `pipeline.run_rules` | `rows`, `categorized`, `review_counts` |
| check item | `close.checklist` | `key`, `step` (1-7), `label`, `ok` (bool), `blocking` (bool), `detail`, `page` (page file to fix it on) |
| `CloseBlocked` | `tc.close` | exception (subclass of `ValueError`); `.items` = the failed check items |
| `LockedMonthError` | `tc.close` | exception (subclass of `ValueError`); `.month` |

### 4.4 Function signatures (the one table - every function a page calls)

`conn` is always a `sqlite3.Connection` from `db.connect` (`row_factory = sqlite3.Row`, foreign keys ON).
`month` is `YYYY-MM`. Functions that write commit before returning. Status: "exists" = already in the repo with
this signature; "new" = to be written to this signature.

| Module | Signature | Returns / effect | Used by | Status |
|---|---|---|---|---|
| `db` | `db.connect(path: str or Path or None = None) -> sqlite3.Connection` | opens the DB (`None` -> `data/tc.db`) | `ui_common` | exists |
| `db` | `db.init_db(conn) -> None` | creates schema + seeds on an empty DB | `ui_common` | exists |
| `db` | `db.to_cents(value: str or Decimal or float or int) -> int` | `"1,456.41"` -> `145641`; raises `ValueError` | Import, Transactions, Reports, Invoices | exists |
| `db` | `db.cents_to_str(cents: int) -> str` | `"1,456.41"`, `"-435.77"` | `ui_common` | exists |
| `db` | `db.insert_transaction(conn, **fields) -> int` | inserts one `transactions` row, no commit | UI tests only (pages never insert directly) | exists |
| `fixtures` | `fixtures.load_golden(conn, fixtures_dir: str or Path or None = None) -> None` | loads `tests/fixtures/` into an initialised DB | UI tests only | exists |
| `ui_common` | `ui_common.get_conn() -> sqlite3.Connection` | cached connection to `TC_DB_PATH`; initialises an empty DB | all pages | new |
| `ui_common` | `ui_common.sidebar(conn) -> str` | draws month selectbox + lock badge + `flash`; returns the working month | all pages | new |
| `ui_common` | `ui_common.money(cents: int or None, *, flip: bool = False) -> str` | `db.cents_to_str`; `"pending"` for `None`; `flip=True` negates (card liability shown positive) | all pages | new |
| `importers` | `tc.importers.wise.parse_wise_csv(path: str or Path) -> list[dict]` | normalized rows, oldest first | via `pipeline` | exists |
| `importers` | `tc.importers.wise.import_wise(conn, path: str or Path) -> dict` | inserts raw rows; `{new, duplicates, total}` | via `pipeline` | exists |
| `importers` | `tc.importers.chase_checking.parse_chase_checking_pdf(path: str or Path) -> dict` | `{period_start, period_end, opening_cents, ending_cents, rows}` | via `pipeline` | exists |
| `importers` | `tc.importers.chase_checking.parse_chase_checking_csv(path: str or Path) -> dict` | same shape, from Chase's CSV download | via `pipeline` | exists |
| `importers` | `tc.importers.chase_checking.import_chase_checking(conn, path: str or Path) -> dict` | inserts raw rows; PDF also saves the statement balance at `period_end`; `{new, duplicates, total}` | via `pipeline` | exists |
| `importers` | `tc.importers.chase_card.parse_chase_card_pdf(path: str or Path) -> dict` | `{cycle_start, cycle_end, previous_balance_cents, new_balance_cents, summary, rows}` | via `pipeline` | exists |
| `importers` | `tc.importers.chase_card.import_chase_card(conn, path: str or Path) -> dict` | inserts raw rows + statement balance at `cycle_end`; `{new, duplicates, total}` | via `pipeline` | exists |
| `pipeline` | `pipeline.parse_statement(path: str or Path, *, filename: str or None = None) -> dict` | detects the kind (extension + content) and returns the "parsed statement" dict of 4.3; no DB access; raises `ValueError` | Import | new |
| `pipeline` | `pipeline.run_import(conn, path: str or Path, *, kind: str or None = None) -> dict` | RULES.md `run_import(file)`: backup, insert, rules, statement balance, one DB transaction per file; returns the "import report" dict; raises `ValueError` | Import | new |
| `pipeline` | `pipeline.run_rules(conn, row_ids: list[int] or None = None, *, month: str or None = None) -> dict` | RULES.md `run_rules(row_ids)` (stages 3-8); `row_ids=None` = every unreviewed row of `month` (or of all open months); returns the "rules report" dict | Review queue | new |
| `pipeline` | `pipeline.review_queue(conn, *, month: str or None = None) -> list[dict]` | rows with `id, date, account, description, amount_cents, payee, category, type, parent_id, reasons` (list of RULES.md reason codes) | Review queue | new |
| `pipeline` | `pipeline.confirm(conn, transaction_id: int) -> None` | accepts the proposed category; sets `reviewed` | Review queue | new |
| `pipeline` | `pipeline.mark_duplicate(conn, transaction_id: int) -> None` | deletes a `possible_duplicate` row in an unlocked month | Review queue | new |
| `pipeline` | `pipeline.search_transactions(conn, *, month: str or None = None, account_id: int or None = None, category_id: int or None = None, type: str or None = None, review_status: str or None = None, search: str or None = None) -> list[dict]` | rows with `id, date, account, description, amount_cents, category, type, notes, review_status, parent_id, source_ref, invoice_labels` | Transactions | new |
| `pipeline` | `pipeline.set_category(conn, transaction_id: int, category_id: int, type: str, *, notes: str or None = None) -> None` | updates the row and its fee children, sets `reviewed` | Review queue, Transactions | new |
| `pipeline` | `pipeline.add_rule(conn, *, label: str, pattern: str, category_id: int, type: str, match_type: str = "contains", account_id: int or None = None, amount_cents: int or None = None, priority: int = 100) -> int` | appends to `src/tc/rules.yaml`, re-syncs the `rules` table; returns the new `rules.id` | Review queue | new |
| `pipeline` | `pipeline.list_accounts(conn, *, tie_required_only: bool = False) -> list[dict]` | `id, code, source_label, name, kind, statement_day` | all pages | new |
| `pipeline` | `pipeline.list_categories(conn) -> list[dict]` | `id, name, statement, section, default_type` ordered by `sort_order` | Review queue, Transactions | new |
| `pipeline` | `pipeline.list_adjustments(conn, *, month: str or None = None) -> list[dict]` | `adjustments` rows | Transactions | new |
| `pipeline` | `pipeline.add_adjustment(conn, *, date: str, label: str, section: str, amount_cents: int, reason: str, non_cash: bool = True) -> int` | new `adjustments.id` | Transactions | new |
| `reports` | `reports.pnl_monthly(conn, year: int) -> list[dict]` | one dict per P&L line, in order: `line: str`, `kind: str` (`detail` / `total`), `months: dict[str, int]` (`YYYY-MM` -> cents), `ytd: int` | Reports | new |
| `reports` | `reports.tie_out(conn, month: str) -> list[dict]` | one dict per statement row of the month, plus one per tie-required account without a row: `account_code, account_name, as_of, opening_cents, activity_cents, ledger_cents, statement_cents, diff_cents, source` (`None` where pending) | Reports | new |
| `reports` | `reports.transfers_in_transit(conn, month: str) -> int` | cumulative `v_transfer_net` through the month, cents | Reports | new |
| `reports` | `reports.balance_sheet(conn, as_of: str) -> list[dict]` | ordered lines: `section` (`assets` / `liabilities` / `equity`), `line`, `amount_cents`, `kind` (`detail` / `total`) | Reports | new |
| `reports` | `reports.owner_draws(conn, year: int) -> dict[str, int]` | `YYYY-MM` -> cents (positive), plus key `"ytd"` | Reports | new |
| `invoices` | `invoices.list_invoices(conn, *, status: str or None = None, type: str or None = None) -> list[dict]` | `id, label, issue_date, due_date, commission_month, type, boxes, rate_cents, amount_cents, status, paid_on, paid_by_transaction_id, replaces_label, notes` | Invoices | new |
| `invoices` | `invoices.unpaid(conn, *, as_of: str or None = None) -> list[dict]` | Pending invoices: `id, label, type, issue_date, due_date, amount_cents, days_open` | Invoices, Home / Reports checklist | new |
| `invoices` | `invoices.add_invoice(conn, *, number: str, source_label: str, issue_date: str, type: str, amount_cents: int, boxes: int = 0, rate_cents: int = 0, commission_month: str or None = None, due_date: str or None = None, replaces_invoice_id: int or None = None, notes: str or None = None) -> int` | new `invoices.id`, status `Pending`; raises `ValueError` on a duplicate or a CHECK failure | Invoices | new |
| `invoices` | `invoices.void_invoice(conn, invoice_id: int, reason: str) -> None` | sets type and status `Voided` | Invoices | new |
| `invoices` | `invoices.unmatched_receipts(conn, *, month: str or None = None) -> list[dict]` | `id, date, account, description, amount_cents, unallocated_cents` | Invoices | new |
| `invoices` | `invoices.match_payment(conn, transaction_id: int, allocations: list[tuple[int, int]]) -> None` | `allocations` = `(invoice_id, amount_cents)`; sum must equal the receipt; sets category / type, marks invoices `Paid`; raises `ValueError`, `LockedMonthError` | Invoices | new |
| `invoices` | `invoices.split_receipt(conn, transaction_id: int, groups: list[list[tuple[int, int]]]) -> list[int]` | mixed wire: one new row per group of `(invoice_id, amount_cents)` allocations, each matched; returns the transaction ids; raises `ValueError`, `LockedMonthError` | Invoices | new |
| `invoices` | `invoices.commission_check(conn, year: int) -> dict` | `invoice_paid_cents, revenue_cents, diff_cents, unbacked: list[dict]` | Invoices, Reports, checklist | new |
| `invoices` | `invoices.missing_commission_months(conn, *, through_month: str, today: str or None = None) -> list[str]` | months with no Commission invoice more than 45 days after month-end | Invoices, checklist | new |
| `close` | `close.is_locked(conn, month: str) -> bool` | row exists in `month_locks` | all pages | new |
| `close` | `close.lock_info(conn, month: str) -> dict or None` | `yyyy_mm, locked_at, reason` | `ui_common`, Reports | new |
| `close` | `close.expected_statement_dates(conn, month: str) -> list[dict]` | per tie-required account: `account_id, code, name, kind, default_as_of` (month-end, or `statement_day`) | Reports | new |
| `close` | `close.set_statement_balance(conn, account_id: int, as_of: str, balance_cents: int, *, source: str or None = None) -> None` | upsert; `balance_cents` is the signed asset value (the page negates a card balance before calling) | Import, Reports | new |
| `close` | `close.checklist(conn, month: str) -> list[dict]` | the "check item" dicts of section 2, in step order | Home, Reports | new |
| `close` | `close.close_month(conn, month: str, reason: str, *, acknowledge_tail: bool = False) -> pathlib.Path` | backup, checks, lock, CSV export; returns the backup path; raises `CloseBlocked` | Reports | new |
| `close` | `close.reopen_month(conn, month: str, reason: str) -> pathlib.Path` | backup, delete the lock; returns the backup path | Reports | new |
| `close` | `close.backup(conn, reason: str) -> pathlib.Path` | SQLite online backup to `backups/tc_YYYYMMDD_HHMMSS_<reason>.db` | via `pipeline.run_import`, `close.close_month`, `close.reopen_month` | new |

### 4.5 Smoke tests with AppTest (`tests/test_ui_*.py`)

One test file per page: `test_ui_home.py`, `test_ui_import.py`, `test_ui_review_queue.py`,
`test_ui_transactions.py`, `test_ui_reports.py`, `test_ui_invoices.py`.

Approach:

1. `tests/conftest.py` has `empty_db` and `golden_db`, but both are `:memory:` connections and a page opens its
   own connection, so UI tests need a file. Add two fixtures (in `conftest.py` or a `tests/ui_fixtures.py`):
   `empty_db_path(tmp_path, monkeypatch)` and `golden_db_path(tmp_path, monkeypatch)`. Each does
   `conn = db.connect(tmp_path / "tc.db"); db.init_db(conn)`, the golden one also `fixtures.load_golden(conn)`,
   then `conn.commit(); conn.close()`, `monkeypatch.setenv("TC_DB_PATH", str(path))`,
   `st.cache_resource.clear()`, and returns the path.
2. Each page is run on its own: `at = AppTest.from_file("app/pages/4_Reports.py", default_timeout=30)`, then
   `at.session_state["month"] = "2026-09"`, `at.run()`.
3. Every test asserts `not at.exception` first, then one or two real values.
4. Widgets are addressed by key: `at.button(key="close_btn").click().run()`,
   `at.text_input(key="bal_amount").set_value("3,603.52")`, `at.selectbox(key="bal_account")`.
5. `st.file_uploader` cannot be driven by AppTest. The Import test uses the folder path instead:
   `at.text_input(key="import_dir").set_value(<tmp dir holding a copy of raw/wise_2026-10.csv>)`, click
   `import_load_dir_btn`, then `import_btn`. That is the reason the folder box exists.
6. Import tests start from `empty_db_path`, not the golden DB: `load_golden` keys rows by the SPEC stable key,
   the Wise importer by the Wise id, so re-importing a Wise file over golden rows would not be seen as duplicates.

Minimum cases (values are fixture goldens from SPEC section 8):

| Test | Page | Asserts |
|---|---|---|
| `test_ui_home_checklist_oct_blocked` | Home, month 2026-10 | no exception; step 4 and 5 lines not OK; step 7 says blocked |
| `test_ui_import_empty` | Import, empty DB | no exception; the "Upload the Wise CSV..." line is shown; `import_btn` absent or disabled |
| `test_ui_import_wise_oct_from_folder` | Import, empty DB | after import: result shows 8 inserted, 0 duplicates; running it again shows 0 inserted, 8 duplicates |
| `test_ui_review_queue_empty` | Review queue, 2026-09 | "Nothing to review for 2026-09." |
| `test_ui_review_queue_set_category` | Review queue, DB with one uncategorised row inserted by the test (`db.insert_transaction`) | choose category, click `rq_save_<id>`; row leaves the queue |
| `test_ui_transactions_oct` | Transactions, 2026-10 | 12 rows; net -2,301.25 |
| `test_ui_transactions_locked_disables_edit` | Transactions, 2026-09 after lock | `tx_save_btn` disabled |
| `test_ui_reports_pnl_goldens` | Reports, 2026 | page text contains 32,186.00 and 18,618.29; draws memo 8,048.00 |
| `test_ui_reports_tie_out_sep` | Reports, 2026-09 | three rows, every diff 0.00 |
| `test_ui_reports_close_blocked_oct` | Reports, 2026-10 | `close_btn` disabled |
| `test_ui_reports_close_and_reopen_sep` | Reports, 2026-09 | type note, tick `close_tail_ack`, click `close_btn` -> LOCKED shown; then re-open with a reason -> OPEN |
| `test_ui_invoices_check_and_unpaid` | Invoices | check shows 32,186.00 / 32,186.00 / 0.00; unpaid list has Wise INV-010 5,645.14 |
| `test_ui_invoices_add_commission_computes_amount` | Invoices | boxes 100 -> amount 50.00; duplicate number shows the error |

Not verified here: Streamlit is not installed in the environment this document was written in, so the AppTest
calls above are written from the `streamlit.testing.v1` API, not run. Pin a Streamlit version that has
`AppTest` (1.28 or later) and confirm `st.switch_page` is available in it.

---

## 5. Open points for the planner (found while writing this, nothing changed)

1. **Balance sheet opening items.** The workbook "Balance Sheet" tab at 30 Sep shows Reimbursable Expenses
   Recoverable (net) -1,898.43, Loan from Shareholder 4,081.90 and equity 11,812.68. The first is built from the
   Firstbase 31 Jul balance, and the loan is not in the ledger; neither is seeded in `schema.sql` or
   `tests/fixtures/`. Reports (C) can only reproduce the tab once they are loaded as `adjustments` rows
   (`section` `asset` / `liability`). The equity roll-forward block of that tab is not in v1.
2. **Locking with a statement that stops before month-end** (section 3 rule 3). SPEC says Jan-Sep 2026 can close
   now; that is true for the triggers, but the card statement for 29 Sep - 28 Oct is not imported yet, and its
   29-30 Sep lines would be rejected once September is locked. `tail_covered` is specified as a warning with a
   tick, not a trigger.
3. **Existing code vs this document.** (a) The existing `import_wise` / `import_chase_checking` /
   `import_chase_card` insert raw, unsplit rows and commit by themselves; RULES.md wants insert + rules in one
   transaction inside `pipeline.run_import`. Whether `run_import` reuses them or only the `parse_*` functions is
   the pipeline coder's call; the pages only see `pipeline.parse_statement` and `pipeline.run_import`. (b) `db.connect` has no `check_same_thread`
   option, which a cached Streamlit connection needs; `ui_common.get_conn` must open its own connection or
   `db.connect` gains a keyword. (c) `app/ui_common.py` and `pipeline.parse_statement` are additions; the task list named only importers / pipeline / reports /
   invoices / close. (d) The Chase importers save statement balances at the statement's own closing date, while
   `tests/fixtures/statement_balances.csv` dates Chase *5200 at calendar month-end: a month can then hold two
   rows for the account, and both must tie.
4. **SOP flags not queued by RULES.md.** The SOP says to flag foreign transaction fees and card interest.
   RULES.md books `FOREIGN TRANSACTION FEE` automatically (rule `card-fx-fee`, no review) and has no rule for
   card interest (it would arrive as `no_rule`). To follow the SOP literally, `card-fx-fee` needs `review: true`.
5. **Commission = revenue** is shown at step 6 as a warning, because SPEC section 5 lists only
   `v_revenue_unbacked` and `v_fee_parent_mismatch` as close conditions.
