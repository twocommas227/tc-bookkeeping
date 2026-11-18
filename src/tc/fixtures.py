"""Golden fixture loader: tests/fixtures/*.csv -> database (no month is locked).

Also the seed command that loads Joshua's real 2026 January-October ledger (the workbook, as exported to
tests/fixtures) into a production database:

    python -m tc.fixtures seed [db_path]        # default data/tc.db; same as: make seed
"""

from __future__ import annotations

import csv
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from .db import DEFAULT_DB_PATH, connect, cents_to_str, init_db, insert_transaction, to_cents

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures"

# statement_balances.csv "account" text -> accounts.code (the CSV does not use accounts.source_label)
STATEMENT_ACCOUNT_CODES = {
    "Wise USD *1430": "WISE_1430",
    "Chase *5200": "CHASE_5200",
    "Chase card *2110": "CARD_2110",
}
# Card *2110 is a liability: statement "balance 1,456.41" is stored as -145641 (SPEC section 1).
LIABILITY_ACCOUNT_CODES = {"CARD_2110"}

# Opening balances per SPEC section 8 (seeded by schema.sql; verified, never rewritten here).
EXPECTED_OPENINGS = {
    "WISE_1430": ("2026-01-01", 1044457),
    "CHASE_5200": ("2026-01-01", 252956),
    "CARD_2110": ("2025-12-28", -148992),
    "WISE_JAR_RENT": ("2026-01-01", 29300),
    "WISE_JAR_TAX": ("2026-01-01", 95000),
}

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

# SPEC section 8: the one manual adjustment (label/reason from golden_pnl.csv's P&L line).
AR_ADJUSTMENT = {
    "date": "2026-09-30",
    "label": "Prior-period AR adjustment",
    "section": "other_income",
    "amount_cents": 156286,
    "non_cash": 1,
    "reason": "Non-cash write-off, booked at Sep close",
}

# UI.md section 5 point 1: workbook 'Balance Sheet' tab at 30 Sep 2026 (opening items not in the bank ledger).
BS_DATE = "2026-09-30"
BS_REIMBURSABLE_NET_CENTS = -189843   # asset: Reimbursable Expenses Recoverable (net), -1,898.43
BS_LOAN_FROM_SHAREHOLDER_CENTS = 408190  # liability: Loan from Shareholder, 4,081.90
BS_OPENING_DATE = "2026-01-01"


def _read(directory: Path, name: str) -> list[dict]:
    with open(directory / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _norm_desc(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _commission_month(text: str) -> str | None:
    text = text.strip()
    if not text or text.upper() == "N/A":
        return None
    mon, year = text.split()
    return f"{int(year):04d}-{_MONTHS[mon[:3].title()]:02d}"


# Invoice numbers without a "Wise " prefix are the old Firstbase series (SPEC section 2).
def _split_invoice_no(raw: str) -> tuple[str, str]:
    raw = raw.strip()
    if raw.startswith("Wise "):
        return "Wise", raw[len("Wise "):]
    return "Firstbase", raw


def load_golden(conn: sqlite3.Connection, fixtures_dir: str | Path | None = None) -> None:
    """Load the golden fixture into a freshly initialised DB. Raises if data is already loaded.

    transactions.source_ref uses the SPEC section 4 stable key
    ``date|amount_cents|normalised description|n`` (n = 1-based occurrence index of that key
    within the account), because transactions.csv carries no bank id column.
    Rows are loaded with review_status 'reviewed' (they are the verified workbook rows).
    """
    directory = Path(fixtures_dir) if fixtures_dir else FIXTURES_DIR
    if conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone():
        raise RuntimeError("golden fixture already loaded (transactions is not empty)")

    accounts = {r["code"]: r for r in conn.execute("SELECT * FROM accounts")}
    for code, (opening_date, opening_cents) in EXPECTED_OPENINGS.items():
        row = accounts.get(code)
        if row is None or (row["opening_date"], row["opening_balance_cents"]) != (opening_date, opening_cents):
            raise RuntimeError(f"seeded opening balance for {code} does not match SPEC")
    by_label = {r["source_label"]: r["id"] for r in accounts.values()}
    cat_ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}

    tx_rows = _read(directory, "transactions.csv")
    invoice_rows = _read(directory, "invoices.csv")
    stmt_rows = _read(directory, "statement_balances.csv")

    with conn:  # one transaction: all-or-nothing
        seen: Counter = Counter()
        for r in tx_rows:
            account_id = by_label[r["source"]]
            amount = to_cents(r["amount"])
            desc = r["description"].strip()
            key = (account_id, r["date"], amount, _norm_desc(desc))
            seen[key] += 1
            insert_transaction(
                conn,
                date=r["date"],
                account_id=account_id,
                description=desc,
                amount_cents=amount,
                category_id=cat_ids[r["category"]],
                type=r["type"],
                source_ref=f"{r['date']}|{amount}|{_norm_desc(desc)}|{seen[key]}",
                notes=r["notes"] or None,
                review_status="reviewed",
            )

        # Voided invoices first so reissues can point at them.
        invoice_ids: dict[tuple[str, str], int] = {}
        for r in sorted(invoice_rows, key=lambda x: x["Type"] != "Voided"):
            source_label, number = _split_invoice_no(r["Invoice No."])
            invoice_ids[(source_label, number)] = conn.execute(
                """INSERT INTO invoices (number, source_label, issue_date, commission_month, type,
                                         boxes, rate_cents, amount_cents, status, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (number, source_label, r["Date"], _commission_month(r["Commission Month"]), r["Type"],
                 int(r["Boxes"]), to_cents(r["Rate"]), to_cents(r["Gross Amount"]), r["Status"],
                 r["Notes"] or None),
            ).lastrowid
        # Reissues named in the register notes ("Replaces voided Wise INV-012" / "Replaces voided Wise INV-011").
        for new, old in (("INV-013", "INV-012"), ("INV-014", "INV-011")):
            conn.execute(
                "UPDATE invoices SET replaces_invoice_id = ? WHERE id = ?",
                (invoice_ids[("Wise", old)], invoice_ids[("Wise", new)]),
            )

        for r in stmt_rows:
            code = STATEMENT_ACCOUNT_CODES[r["account"]]
            cents = to_cents(r["balance"])
            if code in LIABILITY_ACCOUNT_CODES:
                cents = -cents
            conn.execute(
                "INSERT INTO statement_balances (account_id, as_of, balance_cents, source) VALUES (?,?,?,?)",
                (accounts[code]["id"], r["as_of"], cents, "golden fixture"),
            )

        conn.execute(
            """INSERT INTO adjustments (date, label, section, amount_cents, non_cash, reason)
               VALUES (:date, :label, :section, :amount_cents, :non_cash, :reason)""",
            AR_ADJUSTMENT,
        )

        # Balance-sheet opening items (non-cash, section asset / liability, dated at the opening date).
        # Reimbursable net = -(sum of ledger rows in category 'Reimbursable Expenses Recoverable' up to 30 Sep)
        # + opening adjustment (an out-flow fronted for WA is negative in the ledger and a positive asset).
        # The adjustment is the plug that makes the 30 Sep net equal the workbook's -1,898.43.
        reimb_sum = conn.execute(
            """SELECT COALESCE(SUM(t.amount_cents), 0) FROM transactions t
               JOIN categories c ON c.id = t.category_id
               WHERE c.name = 'Reimbursable Expenses Recoverable' AND t.date <= ?""",
            (BS_DATE,),
        ).fetchone()[0]
        opening_reimb = BS_REIMBURSABLE_NET_CENTS + reimb_sum
        conn.execute(
            """INSERT INTO adjustments (date, label, section, amount_cents, non_cash, reason)
               VALUES (?,?,?,?,1,?)""",
            (BS_OPENING_DATE, "Reimbursable Expenses Recoverable (net)", "asset", opening_reimb,
             "Opening balance (workbook Balance Sheet tab, 30 Sep 2026: -1,898.43, built from Firstbase 31 Jul "
             "-2,898.43 +550 -550 +1,000 HTGROWTH reclass). Derived as the plug: "
             f"{BS_REIMBURSABLE_NET_CENTS} - (-(ledger reimbursable rows to 30 Sep = {reimb_sum})) = "
             f"{opening_reimb} cents, so that net = {BS_REIMBURSABLE_NET_CENTS} at 30 Sep. "
             "Negative net = WA has paid more than was fronted."),
        )
        conn.execute(
            """INSERT INTO adjustments (date, label, section, amount_cents, non_cash, reason)
               VALUES (?,?,?,?,1,?)""",
            (BS_OPENING_DATE, "Loan from Shareholder", "liability", BS_LOAN_FROM_SHAREHOLDER_CENTS,
             "Workbook Balance Sheet tab, 30 Sep 2026: 2022 seed funding, no repayment found, parked. "
             "Not in the bank ledger."),
        )


# ====================================================================== seed command (ACCEPTANCE H1, H2)
class SeedError(RuntimeError):
    """The seed was refused (database not empty) or failed; the message is meant to be shown as is."""


def _ensure_seed_column(conn: sqlite3.Connection) -> None:
    """Databases created before accounts.seeded_through existed get the column (additive, nullable)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)")}
    if "seeded_through" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN seeded_through TEXT")
        conn.commit()


def set_seed_cutoffs(conn: sqlite3.Connection) -> dict[str, str]:
    """accounts.seeded_through = the latest statement_balances.as_of of each account (accounts without a
    statement balance keep NULL). Returns {account code: cutoff}. Import skips rows dated on or before it."""
    _ensure_seed_column(conn)
    out: dict[str, str] = {}
    with conn:
        for r in conn.execute(
            """SELECT a.id, a.code, MAX(sb.as_of) AS last_as_of FROM accounts a
               JOIN statement_balances sb ON sb.account_id = a.id GROUP BY a.id ORDER BY a.id"""
        ).fetchall():
            conn.execute("UPDATE accounts SET seeded_through = ? WHERE id = ?", (r["last_as_of"], r["id"]))
            out[r["code"]] = r["last_as_of"]
    return out


def seed(db_path: str | Path | None = None, fixtures_dir: str | Path | None = None) -> dict:
    """Load the 2026 workbook history into a real database. Refuses a database that has any transactions.

    Creates and initialises the file when it is missing or empty, then: load_golden, invoices.backfill_payments,
    rules.sync_rules_table(load_rules()), seed cutoffs. All-or-nothing: on a failure the file is put back as it was.
    Returns {'path', 'transactions', 'invoices', 'payment_links', 'rules', 'revenue_cents', 'net_income_cents',
    'cutoffs': {account code: date}}. Raises SeedError.
    """
    from . import invoices, reports, rules  # late: keeps `python -m tc.fixtures` light and avoids import cycles

    path = Path(DEFAULT_DB_PATH if db_path is None else db_path)
    before = path.read_bytes() if path.exists() else None
    conn = connect(path)
    try:
        has_accounts = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'").fetchone()
        if has_accounts and conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone():
            n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            raise SeedError(
                f"{path} already has {n} transactions. The seed only runs on an empty database; "
                "use another file (python -m tc.fixtures seed <path>) or move this one away first."
            )
        try:
            if not has_accounts:
                init_db(conn)
            _ensure_seed_column(conn)
            load_golden(conn, fixtures_dir)
            links = invoices.backfill_payments(conn)
            n_rules = rules.sync_rules_table(conn, rules.load_rules())
            cutoffs = set_seed_cutoffs(conn)
            conn.commit()
            pnl = {r["line"]: r["ytd"] for r in reports.pnl_monthly(conn, 2026)}
            out = {
                "path": str(path),
                "transactions": conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
                "invoices": conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],
                "payment_links": links,
                "rules": n_rules,
                "revenue_cents": pnl["Total Revenue"],
                "net_income_cents": pnl["NET INCOME"],
                "cutoffs": cutoffs,
            }
        except BaseException:
            conn.rollback()
            conn.close()
            if before is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(before)
            raise
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    return out


def _main(argv: list[str]) -> int:
    if len(argv) not in (2, 3) or argv[1] != "seed":
        print("usage: python -m tc.fixtures seed [db_path]", file=sys.stderr)
        return 2
    target = Path(argv[2]) if len(argv) == 3 else DEFAULT_DB_PATH
    try:
        res = seed(target)
    except SeedError as exc:
        print(f"refusing to seed: {exc}", file=sys.stderr)
        return 1
    print(f"Seeded {res['path']} from the 2026 workbook (tests/fixtures).")
    print(f"  rows: {res['transactions']} transactions, {res['invoices']} invoices "
          f"({res['payment_links']} payment links), {res['rules']} rules")
    print(f"  2026 YTD: revenue {cents_to_str(res['revenue_cents'])}, net income {cents_to_str(res['net_income_cents'])}")
    print("  seed cutoffs (statement lines dated on or before these are skipped on import):")
    for code, day in res["cutoffs"].items():
        print(f"    {code}: {day}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
