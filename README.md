# Two Commas bookkeeping tool

Local bookkeeping for Two Commas LLC: imports the Wise, Chase *5200 and Chase card *2110 statements, categorises
transactions with rules, ties each account to its statement balance, and locks a month once it ties.
It is a single-user Streamlit app on a local SQLite file, with no bank feeds and no login.
Design docs: `docs/SPEC.md` (data model, close rules, backups), `docs/UI.md` (pages and close checklist), `docs/RULES.md`.

## Install

Python 3.11 or newer. Run everything from the project directory.

```
pip install -e .
pip install -e ".[dev]"      # adds pytest, needed only to run the tests
```

On this machine (Python 3.13, root user) `pip install -e .` ran without `--break-system-packages`.

## First run

Create the database (once; the command refuses to overwrite a non-empty file):

```
make init                    # same as: python3 -m tc.db init
```

This creates `data/tc.db`. To use another file, set `TC_DB_PATH` before starting the app.

Start the app:

```
streamlit run app/Home.py    # opens at http://localhost:8501
```

## Start from the 2026 workbook

The month-end tie-out runs from the 1 Jan 2026 opening balances, so no month ties until the whole year is in the
database. The seed loads Joshua's real January to October 2026 ledger (the workbook, exported to `tests/fixtures/`):
235 transactions, the 18 invoices with their payments, the statement balances, the adjustments and the rules table.

First-time sequence (run from the project directory; `make init` is not needed, the seed creates and initialises
`data/tc.db`):

1. `make seed` (same as `python3 -m tc.fixtures seed`). It prints the row counts, the 2026 year to date and the
   cutoffs. Expected: revenue 32,186.00, net income 18,618.29.
2. `streamlit run app/Home.py`. On Reports the year to date shows net income 18,618.29 and revenue 32,186.00, and
   October net income 5,741.63.
3. Import only statements for the period after the cutoffs: the October Chase *5200 PDF, the card *2110 PDF closing
   28 Oct (its 29 and 30 Sep lines belong to September, so keep September open until it is in), and Wise exports that
   start on or after 1 Nov 2026.

The seed refuses a database that already has any transaction, and prints why. To seed another file:
`python3 -m tc.fixtures seed path/to/other.db`.

**Cutoffs.** The seed stores the latest statement balance date of each account in `accounts.seeded_through`:
Wise *1430 2026-10-31, Chase *5200 2026-09-30, card *2110 2026-09-28. An import skips every statement line dated on
or before the cutoff of its account, so re-importing a statement that the workbook already covers inserts nothing.
The Import page says how many ("N lines already in your seeded 2026 history were skipped"); the import report has the
count as `skipped_seeded`. An account without a cutoff is not affected.

**Invoices.** Add invoice (Invoices page) accepts any source name: pick "Other..." and type it, for example
`Firstbase` for the old Firstbase series.

**Seeded transfers.** Seeded transfer rows carry no `[rule:]` tag, so a later Chase credit that matches a seeded Wise
transfer can come up in the Review queue as `unpaired_transfer`; confirm it by hand.

## Monthly close

The working month is chosen in the sidebar. The Home page shows the close checklist for it. Steps 1 to 7 are the
checklist of `docs/UI.md` section 2.

1. **Import** (Import page). Upload the Wise CSV, the Chase *5200 PDF and the card *2110 PDF. A backup is taken
   before each import. Chase PDFs save their own statement balance; for Wise, tick the balance checkbox.
2. **Auto-categorise** (runs during import). Check the count on Home or the Review queue.
3. **Review unknowns** (Review queue). Clear every row marked needs review.
4. **Statement balances** (Reports, section B). Every tie-required account needs a balance dated in the month.
   Card *2110 is dated the 28th.
5. **Tie-out = 0.00** (Reports, section B). Ledger balance minus statement balance must be 0.00 for every account.
6. **Invoice check** (Invoices page). Service Revenue must be backed by Commission invoices, and fee rows must follow
   their parent. Commission paid vs revenue and the unpaid list are warnings only.
7. **Lock month** (Reports, section E). Type a close note and click "Close month". If a statement does not reach
   month-end, tick the warning before closing. The close takes a backup and writes a CSV of that month's
   transactions to `backups/`.

Every blocking line must be OK before "Close month" is enabled. Step 1 and the commission and unpaid lines of step 6
are warnings only.

**October 2026 and the card *2110.** The card cycle closes on the 28th, so October can only be locked for good after
the 28 Nov card statement is imported: its 29-31 Oct lines arrive on that statement. The 29 Sep-28 Oct statement also
carries 29-30 Sep lines, which belong to September, so keep September open until that statement is in. Reports shows
the same note when October is the working month.

**Statement periods:** Chase *5200 statements are not calendar months. Periods run from the day after the previous
statement closes, for example 1 Jan to 30 Jan, then 31 Jan to 27 Feb. The balance is stored at the period end, and
the month-end tie-out uses the balance dated in that month. The card *2110 closes on the 28th, so its balance is
dated the 28th. The app shows the card as a positive liability.

To re-open a closed month, use Reports, section E: type a reason and click "Re-open month". A backup is taken first.

## Files

| Path | What it is |
|---|---|
| `data/tc.db` | The database (default; override with `TC_DB_PATH`). Not in git. |
| `backups/` | Backups (override folder with `TC_BACKUP_DIR`). Written before each import, close and re-open as `tc_YYYYMMDD_HHMMSS_<reason>.db`. A close also writes `tc_..._close_<YYYY-MM>_transactions.csv`. Backup files are not in git. |
| `src/tc/rules.yaml` | Categorisation rules (source of truth; the database `rules` table mirrors it). |
| `src/tc/schema.sql` | Database schema, views, triggers and seed data. |
| `tests/fixtures/` | Golden fixtures: CSVs and the raw statements. `tests/fixtures/MANIFEST.md` lists the sources. |

**Restore a backup:** stop the app (Ctrl+C where `streamlit run` is running), copy a backup over the database, and
start the app again:

```
cp backups/tc_<YYYYMMDD_HHMMSS>_<reason>.db data/tc.db
```

Keep a copy of the current `data/tc.db` first if you may need it.

## Excel export for the accountant

Run from the project directory. The export is values only (no formulas) and writes four tabs: Transactions,
Invoice Register, Bank Tie-out and Profit & Loss.

```python
from tc import db
from tc.export_xlsx import export_workbook

export_workbook(db.connect(), "Two_Commas_Accounting_2026.xlsx", year=2026)
```

`db.connect()` opens `data/tc.db`; pass a path to export another database. `year` defaults to 2026.

## Tests

```
python3 -m pytest -q         # or: make test
```

## Known limits

- The fixtures have no card *2110 statement for the cycle 29 Jul to 28 Aug 2026, and no Wise statement for 12 to 30
  Sep 2026 (the Wise export in the fixtures ends 11 Sep 2026). The October 2026 Chase and card statements are not
  issued yet, so October cannot close (`docs/SPEC.md` section 5).
- No bank feeds: every transaction comes from an uploaded file.
- Single user, local files only. Copying `backups/` off the machine is manual.
- The close CSV covers transactions only. The Invoice Register is in the Excel export, not in a CSV.
- Import backups go to `backups` relative to the current directory, while close and re-open backups go to the
  project's `backups/`. Start the app from the project directory so both land in the same place.
