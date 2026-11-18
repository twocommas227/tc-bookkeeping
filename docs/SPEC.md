# Two Commas LLC bookkeeping tool - SPEC (v1)

Local, single-user replacement for Firstbase accounting. Two Commas LLC is a single-member Wyoming LLC
(disregarded entity) earning a $0.50/box commission from WA Rubbermate on ProtectMate gloves sold on Amazon US.

**Answer key:** `/mnt/user-data/outputs/Two_Commas_Accounting_2026.xlsx` (tabs: Transactions, Invoice Register,
Bank Tie-out, Profit & Loss, Balance Sheet, Vendor Rules, Commission Tracker, Oct 2026 Close, Sep 2026 Close).
Everything in sections 6-8 was read from that workbook (openpyxl, `data_only=True`) on 2026-11-15.

## 1. Stack and scope

- Python 3.12, SQLite (`src/tc/schema.sql`), Streamlit UI, pytest.
- Money is **INTEGER cents**, never floats. Dates are ISO `YYYY-MM-DD` strings; months are `YYYY-MM`.
- Single user, local files, no bank API in v1 (CSV / statement imports only).
- Cash basis.
- **Non-goals v1:** payroll, multi-entity, receipts/OCR, tax filing.

Sign convention: `amount_cents` is signed from the account's point of view (+ in, - out; on the card a charge
is - and a payment is +). Balances are signed asset values, so the card *2110 liability is stored negative
(statement balance $1,456.41 -> `-145641`). Ledger balance = `accounts.opening_balance_cents` + sum of
`transactions.amount_cents` up to the date.

## 2. Entities (tables in `src/tc/schema.sql`)

| Table | Purpose | Key columns |
|---|---|---|
| `accounts` | Bank / card / jar accounts | `source_label` (exact workbook "Source" text), `kind`, `opening_date`, `opening_balance_cents`, `opening_inferred`, `tie_required`, `statement_day`, `monthly_fee_cents`, `fee_waiver_min_balance_cents` |
| `categories` | Chart of categories | `name`, `statement` (pnl / balance_sheet / equity / none), `section`, `default_type`, `pnl_line` |
| `transactions` | The ledger, one row per bank line (plus split fee rows) | `date`, `account_id`, `description`, `amount_cents`, `category_id` (nullable), `type`, `source_ref` with `UNIQUE(account_id, source_ref)`, `parent_id` (fee -> parent), `fee_cents` (fee kept inside the row), `payee`, `raw_type`, `running_balance_cents`, `notes`, `review_status` |
| `rules` | Vendor rules for auto-categorisation | `priority`, `label`, `pattern`, `match_type`, `account_id`, `amount_cents`, `action` (categorize / follow_parent / needs_invoice), `category_id`, `type` |
| `invoices` | Invoice register | `number`, `source_label`, `UNIQUE(source_label, number)`, `issue_date`, `due_date`, `commission_month`, `type` (Commission / Reimbursable / Voided), `boxes`, `rate_cents`, `amount_cents`, `status` (Pending / Paid / Voided), `replaces_invoice_id` |
| `invoice_payments` | Which transaction paid which invoice (many invoices per transaction) | `transaction_id`, `invoice_id`, `amount_cents` (gross) |
| `statement_balances` | Bank / card statement balances typed in for tie-out | `account_id`, `as_of`, `balance_cents`, `UNIQUE(account_id, as_of)` |
| `adjustments` | Manual non-bank entries (e.g. AR write-off) | `date`, `label`, `section`, `amount_cents`, `non_cash`, `reason` |
| `month_locks` | Closed months | `yyyy_mm`, `locked_at`, `reason` |

Views: `v_tie_out`, `v_fee_parent_mismatch`, `v_revenue_unbacked`, `v_transfer_net`, `v_pnl_monthly`, `v_invoice_labels`.

Transaction `type` values (exactly as in the workbook, row counts in brackets): `Expense` (106), `Transfer` (56),
`Personal` (38), `Income` (20), `Pass-through` (8), `Reimbursable` (7) in the workbook. The tool uses one label for
reimbursables: after loading, `Reimbursable` (12) and `Pass-through` (3, Payable to WA Rubbermate only).

Invoice numbering: Wise INV-010..014 overlap the old Firstbase INV-0000xx series. `invoices.source_label` is
NOT NULL and every display uses `v_invoice_labels.label` ("Wise INV-014" vs "Firstbase INV-000014").
Test: `test_invoice_label_always_has_source`.

## 3. The 8 accounting rules as testable statements

| # | Rule (testable statement) | Enforced by (schema) | Test name |
|---|---|---|---|
| 1 | **ONE RULE.** A transaction is `Service Revenue` only if it is fully settled by `Commission` invoices; a Commission invoice is one line: boxes x $0.50 for one named month ("Commission for units (box) sold during [Month Year]"). Any other receipt from WA is `Reimbursable Expenses Recoverable` (balance sheet), never revenue. | `invoices.type`; CHECK `ck_invoice_commission_single_line` (`rate_cents = 50`, `amount_cents = boxes * rate_cents`, `commission_month` set); trigger `trg_one_rule_invoice_payment`; view `v_revenue_unbacked` (must be empty); `categories.statement = 'balance_sheet'` for Reimbursable | `test_rule1_service_revenue_only_from_commission_invoices`, `test_rule1_reimbursable_never_in_pnl` |
| 2 | **Cash basis.** Commission is revenue on the date WA's wire lands (`transactions.date`), not the invoice date. One wire may pay several invoices; the sum of its `invoice_payments.amount_cents` equals the wire's gross `amount_cents`. | `invoice_payments` (`UNIQUE(transaction_id, invoice_id)`); `v_pnl_monthly` groups by `transactions.date`; `v_revenue_unbacked` | `test_rule2_one_wire_many_invoices` (19 Oct 2026: Wise INV-013 301600 + Wise INV-014 269000 = 570600, fee row -611), `test_rule2_revenue_month_is_payment_month` |
| 3 | **Disregarded LLC.** Transfers to Joshua, Betterment, and IRS federal income tax are `Owner's Draw` (type `Personal`), never an expense. | `categories` row `Owner's Draw` with `statement = 'equity'` (excluded from `v_pnl_monthly`); `rules` rows for Betterment / Joshua F Decker / IRS | `test_rule3_owner_draws_not_in_pnl`, `test_rule3_draw_vendors_map_to_owner_draw` |
| 4 | **Wise fee follows the parent.** Fee on a WA receipt, a Wise->Chase transfer or a Maersk payment = `Bank Charges`; fee on an owner draw = `Owner's Draw`; fee on the WA tariff wire = `Payable to WA Rubbermate`. A Wise debit in the CSV already includes the fee, so the importer splits it into transfer row + fee row (`amount = debit - fee`, fee row = `-fee`). WA receipts are booked gross with the fee as a separate `Bank Charges` row. | `transactions.parent_id`; triggers `trg_fee_follows_parent_ins` / `_upd`; view `v_fee_parent_mismatch` (must be empty); `transactions.fee_cents`; `rules.action = 'follow_parent'` | `test_rule4_fee_follows_parent`, `test_rule4_wise_debit_split_sums_to_csv_amount`, `test_rule4_wa_receipt_booked_gross` |
| 5 | **Own-account transfers net to zero.** Wise->Chase, card payments and jar moves are `Bank Transfer`; both legs are booked, each on its own bank date; the cumulative total is 0 except for legs whose other side is not yet imported. | `categories` row `Bank Transfer` (`statement = 'none'`); view `v_transfer_net` | `test_rule5_own_account_transfers_net_zero` (Jan-Sep 2026 cumulative = 0; YTD 31 Oct = -149774 = the 2 Oct and 29 Oct Wise->Chase legs awaiting the Chase Oct statement) |
| 6 | CBP direct debits, Maersk entry fees / bond and demurrage are `Reimbursable Expenses Recoverable`, never an expense. | `rules` rows (Maersk, CBP, Paycargo, Pierpass, Portcheck); `categories.statement = 'balance_sheet'` | `test_rule6_customs_and_freight_are_reimbursable` |
| 7 | Firstbase $35 = `Postage & Delivery` (not Other Professional Services). The Chase *5200 $15 monthly fee is expected only in months where the daily ledger balance fell below $2,000 AND the waiver by Ink card activity (>= $2,000 in the statement period) did not apply (see RULES.md 5.4; April 2026 balance $1,983.45 was still waived by card activity). | `rules.amount_cents = -3500` for the Firstbase rule; `accounts.monthly_fee_cents = 1500`, `accounts.fee_waiver_min_balance_cents = 200000` | `test_rule7_firstbase_35_is_postage`, `test_rule7_chase_fee_only_when_balance_below_2000` (2026: fee in May and Aug only) |
| 8 | **Month-end tie-out and lock.** A month can be locked only if every `tie_required` account has a statement balance in that month and ledger = statement (diff 0) for each. A locked month rejects insert / update / delete of transactions, adjustments and statement balances. | `statement_balances`; view `v_tie_out` (`diff_cents`); trigger `trg_month_lock_requires_tie_out`; `month_locks`; triggers `trg_locked_tx_*`, `trg_locked_adj_*`, `trg_locked_stmt_*` | `test_rule8_cannot_lock_untied_month`, `test_rule8_locked_month_rejects_changes`, `test_golden_tie_outs` |

Notes on rule 4 as the workbook actually applies it:
- Draw fees: Jan-Aug rows carry the draw fee as a separate row categorised `Owner's Draw`; from Sep the draw is a
  single row with the fee inside ("Incl. Wise fee $1.13"). Both keep the fee inside the draw. **The importer
  does not split owner-draw fees**: it keeps one row and records the fee in `transactions.fee_cents`.
  Jan-Aug split rows are loaded as they stand (needed for the 235-row count).
- Wise->Chase: Jan-Aug the fee was charged on top (e.g. 8 Jan: transfer -1,500.00 and fee -1.13); from Sep it is
  taken out of the debit (e.g. 29 Oct: Wise debit $1,200.00 = -1,198.87 transfer + -1.13 fee).

## 4. Import contract

Every importer (Wise CSV, Chase *5200 statement, Chase card *2110 statement) emits a list of normalized rows.
Importers do no categorisation; rules run afterwards.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `date` | `YYYY-MM-DD` | yes | Bank posting date |
| `account` | text | yes | Must equal an `accounts.source_label` |
| `description` | text | yes | Bank description, trimmed, otherwise unchanged |
| `amount_cents` | int | yes | Signed, as the bank shows it. For a Wise debit this is the full debit **including** the fee |
| `source_ref` | text | yes | Bank's own id (`TRANSFER-2441134368`, `DIRECT_DEBIT-40502598`, `BALANCE-...`). Where the bank gives none (Chase PDFs), a stable key: `<statement period end>|<line index in the statement>|<first 8 hex of sha1(date\|cents\|description)>`, e.g. `2026-01-30|0|a6898dd9`. The card *2110 uses its cycle end the same way. The seeded workbook rows use `date|amount_cents|normalised description|n` |
| `fee_cents` | int >= 0 | yes (0 if none) | Fee contained in / attached to this line |
| `running_balance_cents` | int | optional | Balance after the line, as printed |
| `payee` | text | optional | Payee / payer name |
| `raw_type` | text | optional | Bank's type code (TRANSFER, DIRECT_DEBIT, CARD, INTEREST, ...) |

Loader behaviour:
1. Insert is idempotent through `UNIQUE(account_id, source_ref)`; a duplicate is skipped and counted.
2. Fee split (rule 4): when `fee_cents > 0` and the parent is not an owner draw, write the parent with
   `amount_cents + fee_cents` (i.e. net of fee for a debit; gross for a WA receipt) and a child row with
   `amount_cents = -fee_cents`, `source_ref = 'FEE-' || parent source_ref`, `parent_id = parent.id`.
   Parent + child must sum to the CSV cash movement.
3. Uncategorised rows land with `category_id NULL`, `review_status = 'needs_review'`.
4. Rows dated in a locked month are rejected (rule 8).
5. All arithmetic is in integer cents; parse decimals with `Decimal`, never `float`.

## 5. Month close and backups

**Month close** for month M succeeds only when all of these hold (`trg_month_lock_requires_tie_out`):
1. Every active `tie_required` account (Wise *1430, Chase *5200, card *2110) has a `statement_balances` row dated
   in M (card: the 28th; banks: month-end).
2. `v_tie_out.diff_cents = 0` for every statement row in M.
3. No transaction in M has `category_id IS NULL`.
4. Application checks before inserting the lock (tests, not triggers): `v_revenue_unbacked` and
   `v_fee_parent_mismatch` are empty; no row in M is `needs_review`.

Closing inserts `month_locks(yyyy_mm, locked_at, reason)`. Re-opening = deleting the lock row, done only through
the UI with a typed reason, after a backup. Wise jars are not tied (`tie_required = 0`): no jar statements exist.
As of this spec, Jan-Sep 2026 can close; Oct 2026 cannot (Chase *5200 and card *2110 October statements pending).

**Backup policy**
- Before every import, every month close and every re-open: copy the SQLite file with the SQLite online backup
  API to `backups/tc_YYYYMMDD_HHMMSS_<reason>.db`.
- Keep all month-close backups permanently; keep the last 30 other backups.
- After each close, also export Transactions and the Invoice Register to CSV in `backups/` (plain-text copy).
- `backups/` is local; copying it off the machine is the owner's job (documented in the UI, not automated in v1).
- A restore is a file copy; `test_backup_roundtrip` asserts the restored DB reproduces the golden numbers.

## 6. Category list (full list on the Transactions tab)

| Category | Statement | Section | Type(s) used | Rows | Sum (USD) |
|---|---|---|---|---|---|
| Service Revenue | P&L | Revenue | Income | 10 | 32,186.00 |
| Other Professional Services | P&L | SG&A | Expense | 23 | -14,033.37 |
| Postage & Delivery | P&L | SG&A | Expense | 9 | -315.00 |
| Software & Web Services | P&L | SG&A | Expense | 40 | -988.70 |
| Bank Charges | P&L | SG&A | Expense | 34 | -165.98 |
| Interest Income | P&L | Other income | Income | 10 | 372.48 |
| Owner's Draw | Equity (memo, not P&L) | Owner draw | Personal | 38 | -8,048.00 |
| Bank Transfer | none | Transfer | Transfer | 56 | -1,497.74 (in transit to Chase, Oct) |
| Reimbursable Expenses Recoverable | Balance sheet | Asset | Reimbursable (12; the workbook has 7 + 5 Pass-through) | 12 | -4,745.14 |
| Payable to WA Rubbermate | Balance sheet | Liability | Pass-through | 3 | 0.00 |
| **Total** | | | | **235** | |

Not seeded: `Taxes & Licenses` is named once in a Vendor Rules note (Wyoming/state fees) but has no
Transactions row. Add it when the first such charge appears.

Sources (accounts) on the Transactions tab: `USD account 1430` (103 rows), `J. DECKER 2110` (86),
`BUS COMPLETE CHK 5200` (32), `Wise USD - Rent & Utilities 0000` (13), `Wise USD - Taxes jar` (1).

## 7. Vendor Rules (every row of the Vendor Rules tab, in workbook order)

The workbook's Type column here uses a different vocabulary from the Transactions tab. Mapping used when the
rows are loaded into `rules.type`: Revenue -> Income; Balance Sheet -> Reimbursable (vendor payments on the card /
WA receipts, Wise payments; Pass-through only for Payable to WA Rubbermate); No P&L -> Transfer; Rule -> `action = 'follow_parent'`.

| # | Vendor / Pattern | Correct Category | Type | Notes |
|---|---|---|---|---|
| 1 | WA Rubbermate (commission invoice) | Service Revenue | Revenue | Only single-line $0.50/box commission invoices |
| 2 | WA Rubbermate (all other invoices) | Reimbursable Expenses Recoverable | Balance Sheet | Duties, customs, freight, creative, A+, campaigns |
| 3 | Maersk / Maersk Customs Services | Reimbursable Expenses Recoverable | Balance Sheet | Always WA pass-through |
| 4 | Paycargo | Reimbursable Expenses Recoverable | Balance Sheet | Ocean freight — WA pass-through |
| 5 | Pierpass LLC | Reimbursable Expenses Recoverable | Balance Sheet | Port fee — WA pass-through |
| 6 | Portcheck LLC | Reimbursable Expenses Recoverable | Balance Sheet | Port inspection — WA pass-through |
| 7 | HTGROWTH | Other Professional Services | Expense | $1,250/mo retainer. Extra charges may be WA reimbursable — confirm |
| 8 | Firstbase.io ($149) | Other Professional Services | Expense | Accounting/admin - CANCELLED. Sep 2026 charge refunded (correct) |
| 9 | Firstbase.io ($35) | Postage & Delivery | Expense | Monthly mailbox / registered-agent address - KEEP (Firstbase remains the registered agent) |
| 10 | Firstbase.io ($299) | Other Professional Services | Expense | Annual registered agent or similar |
| 11 | Sellerboard Pro | Software & Web Services | Expense | Amazon analytics SaaS |
| 12 | Google Workspace | Software & Web Services | Expense | Business Standard, 1 seat, $16.80/mo. Annual domain renewal $12 (twocommas.info) each Aug |
| 13 | MyRealProfit | Software & Web Services | Expense | Amazon profit tracker |
| 14 | Anthropic / Claude | Software & Web Services | Expense | AI tools |
| 15 | OpenAI | Software & Web Services | Expense | AI tools |
| 16 | Betterment Securities | Owner's Draw | Personal | Personal investment — NEVER software |
| 17 | Joshua F Decker / Joshua Decker (any amount, incl. $293 monthly) | Owner's Draw | Personal | ALL transfers to Joshua are draws. Rent/home-office deduction withdrawn (Thai permanent-establishment risk). Oct 2026 decision. |
| 18 | (merged into row above) | Owner's Draw | Personal | Superseded |
| 19 | IRS / US Treasury (federal income tax) | Owner's Draw | Personal | Disregarded single-member LLC: income tax is the owner's, not the business's. Wyoming/state fees still = Taxes & Licenses. |
| 20 | Wise → Chase transfers | Bank Transfer | No P&L | Internal transfer between own accounts |
| 21 | Wise jar moves | Bank Transfer | No P&L | Internal jar allocation |
| 22 | Interest (Received) | Interest Income | Income | Wise/Chase interest |
| 23 | Wise fee lines ("Wise Charges for...") | Bank Charges | Expense | Fees on WA receipts ($6.11) and on Wise->Chase transfers ($1.13). Fees on draws stay inside the draw. |
| 24 | Chase Monthly Service Fee ($15) | Bank Charges | Expense | Waived if daily balance >= $2,000 - keep Chase *5200 above that. |
| 25 | Chase card payment / Payment Thank You | Bank Transfer | No P&L | Both legs (checking out, card in). |
| 26 | Google Voice Starter (g.co/HelpPay SVCS) | Software & Web Services | Expense | VoIP line ~$13/mo incl. telecom taxes ($9.97 licence + fees) |
| 27 | WA Rubbermate receipt - reference INV-0000xx PO-.. + customs/bond text | Reimbursable Expenses Recoverable | Balance Sheet | Never revenue unless single-line $0.50/box commission |
| 28 | Maersk Customs Services (bond, entry fee), CBP ACH, PortCheck/PierPass, detention | Reimbursable Expenses Recoverable | Balance Sheet | Billed to WA on Wise invoice |
| 29 | Cloudflare (twocommas.us domain / website hosting) | Software & Web Services | Expense | Business website domain, bought Oct 2026. Annual renewal. If paid on a personal card, record as an owner contribution, not a business expense |
| 30 | Wise fee line (Wise Charges for: TRANSFER-xxxx) | Follows the parent transfer | Rule | Fee on a WA receipt, Wise->Chase transfer or Maersk payment = Bank Charges; fee on an owner draw = Owner's Draw (inside the draw); fee on the WA tariff-refund wire = Payable to WA |
| 31 | Firstbase $35 (mailbox / registered agent) | Postage & Delivery | Expense | All months Jan-Sep (was Other Professional Services in the Firstbase export) |
| 32 | Wise internal "Moved ... to/from Rent & Utilities / Taxes" | Bank Transfer | Transfer | Net zero; jar side kept on its own source line |
| 33 | CBP TREAS 310 (tariff refund) / payment to WA | Payable to WA Rubbermate | Pass-through | Refund received 29 Jun $18,375.57; sent to WA 8 Jul |
| 34 | US CBP direct debit ("Paid to US CBP", DIRECT_DEBIT) | Reimbursable Expenses Recoverable | Reimbursable | Monthly CBP PMS debit for shipment duty; match to CBP PMS report. Billed to WA on Wise invoice. |

Loading notes: row 18 is a superseded placeholder (load inactive); rows 9/31, 23/30 and 3/28 overlap - row 30
(follow the parent) takes priority over row 23.

## 8. Golden numbers (the tool must reproduce these; `tests/test_golden.py`)

All verified against the workbook on 2026-11-15: **no mismatch**.

| # | Golden | Value | Workbook source | Test |
|---|---|---|---|---|
| G1 | Transactions data rows | 235 | Transactions tab | `test_golden_row_count` |
| G2 | Wise *1430 month-end ledger = statement, diff 0.00, Jan-Oct | 31 Jan 11,001.78; 28 Feb 10,732.76; 31 Mar 15,228.65; 30 Apr 7,816.32; 31 May 13,410.58; 30 Jun 36,359.52; 31 Jul 17,932.78; 31 Aug 17,455.08; **30 Sep 15,645.90**; **31 Oct 13,344.65** | Bank Tie-out | `test_golden_tie_outs` |
| G3 | Chase *5200, 30 Sep | 3,603.52 (Jan-Sep: 2,195.78; 2,095.78; 2,083.45; 4,937.29; 1,932.58; 3,499.92; 3,505.93; 2,504.65; 3,603.52; Oct pending) | Bank Tie-out | `test_golden_tie_outs` |
| G4 | Card *2110 liability, 28 Sep | 1,456.41 (28th of Jan-Sep: 1,467.87; 2,612.33; 1,493.08; 2,889.71; 1,611.53; 1,492.86; 1,492.97; 1,892.18; 1,456.41; Oct pending) | Bank Tie-out | `test_golden_tie_outs` |
| G5 | YTD 31 Oct revenue | 32,186.00 | Profit & Loss | `test_golden_ytd_pnl` |
| G6 | YTD 31 Oct net income | 18,618.29 | Profit & Loss | `test_golden_ytd_pnl` |
| G7 | YTD net income before the non-cash AR adjustment (1,562.86) | 17,055.43 | Profit & Loss | `test_golden_ytd_pnl` |
| G8 | YTD owner's draws | 8,048.00 | P&L memo | `test_golden_owner_draws` |
| G9 | Oct 2026 (Wise only): revenue / bank charges / interest / net | 5,706.00 / 9.50 / 45.13 / 5,741.63 | Profit & Loss | `test_golden_oct_2026` |
| G10 | Sep 2026 net income before AR adjustment | -1,316.72 | Profit & Loss | `test_golden_sep_2026` |
| G11 | Commissions paid in 2026 per Invoice Register vs revenue | 32,186.00 vs 32,186.00, diff 0 | P&L check row | `test_golden_commission_check` |

Supporting figures (same source, for building fixtures):
- Openings: Wise *1430 10,444.57 (1 Jan 2026); Chase *5200 2,529.56 (1 Jan 2026); card *2110 liability 1,489.92
  (28 Dec 2025). Jar openings are inferred (Rent & Utilities 293.00, Taxes 950.00; both jars now $0).
- Monthly revenue Jan-Oct: 3,786.50; 0; 6,582.50; 4,773.50; 0; 5,739.00; 2,786.00; 2,812.50; 0; 5,706.00.
- Monthly net income Jan-Oct: 2,059.35; -2,541.35; 5,050.81; 3,226.96; -1,603.35; 4,252.38; 1,228.19; 957.53;
  246.14 (after AR adj) / -1,316.72 (before); 5,741.63.
- YTD SG&A 15,503.05 = Professional Services 14,033.37 + Postage 315.00 + Software 988.70 + Bank Charges 165.98;
  YTD interest 372.48.
- Monthly owner's draws Jan-Oct: 393; 393; 393; 686; 893; 393; 2,304; 393; 1,300; 900.
- The AR adjustment is one `adjustments` row: 2026-09-30, "Prior-period AR adjustment", `other_income`,
  +156286, `non_cash = 1`.
- Oct 2026: 12 ledger rows, all on Wise *1430; Wise activity -2,301.25 (15,645.90 -> 13,344.65).
- Invoice Register: commission total 32,186.00 (64,372 boxes); reimbursable total 16,859.22; Wise INV-010
  (5,645.14) Pending; Wise INV-011 / INV-012 Voided, reissued as Wise INV-014 / INV-013.

## 9. Open points found in the workbook (not changed)

1. Invoice Register "Date Paid" differs from the ledger receipt date for three invoices: INV-000016 (register
   4 May, ledger 2 Jun), INV-000017 (4 Jun vs 18 Jun), INV-000018 (8 Jul vs 16 Jul). The P&L follows the ledger
   (May revenue 0, Jun 5,739.00). The tool takes the payment date from `transactions.date`.
2. `Reimbursable Expenses Recoverable` rows use two types in the workbook (`Reimbursable` 7 rows, `Pass-through` 5 rows)
   with no stated rule. Resolved in the tool: all 12 load as `Reimbursable`.
3. Commission Tracker, Jul 2026 "Date Paid" is stored as a raw serial number (46265 = 31 Aug 2026), not a date.
4. Jan-Aug vs Sep-Oct rows treat Wise fees on draws and Wise->Chase transfers differently (see section 3 notes).
