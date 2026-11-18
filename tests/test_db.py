import csv
import os
import re
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from tc import db
from tc.db import (cents_to_str, connect, init_db, insert_transaction, list_transactions, to_cents)
from tc.fixtures import FIXTURES_DIR, load_golden

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = (ROOT / "src" / "tc" / "schema.sql").read_text(encoding="utf-8")


# ------------------------------------------------------------------ money
@pytest.mark.parametrize(
    "value, expected",
    [
        ("1234.56", 123456),
        ("1,234.56", 123456),
        ("-149.00", -14900),
        ("0.5", 50),
        ("0", 0),
        (Decimal("2.675"), 268),      # half-up (float math would give 267)
        (2.675, 268),
        (0.1 + 0.2, 30),
        ("1.005", 101),
        ("-1.005", -101),             # ties round away from zero
        ("1.004", 100),
        (Decimal("-0.5"), -50),
        (5, 500),
    ],
)
def test_to_cents(value, expected):
    assert to_cents(value) == expected
    assert isinstance(to_cents(value), int)


@pytest.mark.parametrize("bad", ["abc", "", "NaN", "Infinity"])
def test_to_cents_rejects_garbage(bad):
    with pytest.raises(ValueError):
        to_cents(bad)


def test_cents_to_str():
    assert cents_to_str(123456) == "1,234.56"
    assert cents_to_str(0) == "0.00"
    assert cents_to_str(5) == "0.05"
    assert cents_to_str(-145641) == "-1,456.41"
    assert cents_to_str(1334465) == "13,344.65"
    assert cents_to_str(100000000) == "1,000,000.00"


# --------------------------------------------------------------- schema/CLI
def _schema_objects():
    return set(re.findall(r"^CREATE (?:TABLE|VIEW|TRIGGER) (\w+)", SCHEMA, flags=re.M))


def test_connect_enables_foreign_keys(tmp_path):
    for target in (":memory:", tmp_path / "sub" / "t.db"):
        conn = connect(target)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        conn.close()


def test_init_db_creates_every_schema_object(empty_db):
    names = {r[0] for r in empty_db.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    expected = _schema_objects()
    assert len(expected) >= 9 + 6  # tables + views at least
    assert expected <= names
    assert empty_db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 5
    assert empty_db.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 10


def test_cli_init_creates_db(tmp_path):
    target = tmp_path / "x.db"
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    r = subprocess.run([sys.executable, "-m", "tc.db", "init", str(target)], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    conn = sqlite3.connect(target)
    names = {x[0] for x in conn.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}
    conn.close()
    assert _schema_objects() <= names
    again = subprocess.run([sys.executable, "-m", "tc.db", "init", str(target)], capture_output=True, text=True, env=env)
    assert again.returncode != 0  # never clobbers an existing DB


# ---------------------------------------------------------- insert / list
def _tx(account_id=1, **kw):
    base = dict(date="2026-01-05", account_id=account_id, description="x", amount_cents=-100, source_ref="r1")
    base.update(kw)
    return base


def test_insert_and_list_transactions(empty_db):
    a = insert_transaction(empty_db, **_tx(date="2026-01-05", source_ref="a"))
    b = insert_transaction(empty_db, **_tx(date="2026-02-05", source_ref="b", amount_cents=500))
    c = insert_transaction(empty_db, **_tx(account_id=2, date="2026-03-05", source_ref="c"))
    assert (a, b, c) == (1, 2, 3)
    assert [r["id"] for r in list_transactions(empty_db)] == [1, 2, 3]
    assert [r["id"] for r in list_transactions(empty_db, account=1)] == [1, 2]
    assert [r["id"] for r in list_transactions(empty_db, account="CHASE_5200")] == [3]
    assert [r["id"] for r in list_transactions(empty_db, account="USD account 1430")] == [1, 2]
    assert [r["id"] for r in list_transactions(empty_db, start="2026-02-05")] == [2, 3]
    assert [r["id"] for r in list_transactions(empty_db, end="2026-02-05")] == [1, 2]
    assert [r["id"] for r in list_transactions(empty_db, account=1, start="2026-02-01", end="2026-02-28")] == [2]
    assert list_transactions(empty_db)[0]["review_status"] == "needs_review"


def test_insert_rejects_unknown_field_duplicates_and_bad_fk(empty_db):
    with pytest.raises(ValueError):
        insert_transaction(empty_db, **_tx(bogus=1))
    insert_transaction(empty_db, **_tx())
    with pytest.raises(sqlite3.IntegrityError):
        insert_transaction(empty_db, **_tx())  # UNIQUE(account_id, source_ref)
    with pytest.raises(sqlite3.IntegrityError):
        insert_transaction(empty_db, **_tx(account_id=99, source_ref="z"))  # foreign key


# ------------------------------------------------------------ golden load
def _csv(name):
    with open(FIXTURES_DIR / name, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_golden_transaction_count(golden_db):
    assert golden_db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 235
    assert golden_db.execute("SELECT COUNT(*) FROM transactions WHERE category_id IS NULL").fetchone()[0] == 0


def test_golden_sum_per_account_matches_csv(golden_db):
    expected = {}
    for r in _csv("transactions.csv"):
        expected[r["source"]] = expected.get(r["source"], Decimal(0)) + Decimal(r["amount"])
    got = {
        r["source_label"]: r["s"]
        for r in golden_db.execute(
            "SELECT a.source_label, SUM(t.amount_cents) s FROM transactions t JOIN accounts a ON a.id = t.account_id GROUP BY a.id"
        )
    }
    assert set(got) == set(expected)
    for label, total in expected.items():
        assert got[label] == int(total * 100), label


def test_golden_preserves_type_and_notes(golden_db):
    counts = dict(golden_db.execute("SELECT type, COUNT(*) FROM transactions GROUP BY type").fetchall())
    assert counts == {"Expense": 106, "Transfer": 56, "Personal": 38, "Income": 20, "Pass-through": 3, "Reimbursable": 12}
    assert golden_db.execute("SELECT COUNT(*) FROM transactions WHERE notes IS NOT NULL").fetchone()[0] == 129


def test_golden_wise_ledger_at_oct_31(golden_db):
    (ledger,) = golden_db.execute(
        """SELECT a.opening_balance_cents + SUM(t.amount_cents) FROM accounts a
           JOIN transactions t ON t.account_id = a.id
           WHERE a.code = 'WISE_1430' AND t.date <= '2026-10-31'"""
    ).fetchone()
    assert ledger == 1334465


def test_golden_statement_balances_and_tie_out(golden_db):
    assert golden_db.execute("SELECT COUNT(*) FROM statement_balances").fetchone()[0] == 28
    (card,) = golden_db.execute(
        "SELECT balance_cents FROM statement_balances sb JOIN accounts a ON a.id = sb.account_id "
        "WHERE a.code = 'CARD_2110' AND as_of = '2026-09-28'"
    ).fetchone()
    assert card == -145641
    diffs = golden_db.execute("SELECT as_of, account_code, diff_cents FROM v_tie_out WHERE diff_cents <> 0").fetchall()
    assert [tuple(d) for d in diffs] == []


def test_golden_invoices(golden_db):
    assert golden_db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 18
    labels = {r[0] for r in golden_db.execute("SELECT label FROM v_invoice_labels")}
    assert {"Wise INV-014", "Firstbase INV-000014", "Wise INV-010"} <= labels
    assert golden_db.execute("SELECT SUM(amount_cents), SUM(boxes) FROM invoices WHERE type='Commission'").fetchone()[:] == (3218600, 64372)
    assert golden_db.execute("SELECT SUM(amount_cents) FROM invoices WHERE type='Reimbursable'").fetchone()[0] == 1685922
    assert golden_db.execute("SELECT status FROM invoices WHERE source_label='Wise' AND number='INV-010'").fetchone()[0] == "Pending"
    replaced = dict(golden_db.execute(
        "SELECT n.number, o.number FROM invoices n JOIN invoices o ON o.id = n.replaces_invoice_id").fetchall())
    assert replaced == {"INV-013": "INV-012", "INV-014": "INV-011"}


def test_golden_adjustment_and_no_locks(golden_db):
    rows = golden_db.execute(
        "SELECT date, label, section, amount_cents, non_cash FROM adjustments WHERE section = 'other_income'").fetchall()
    assert [tuple(r) for r in rows] == [("2026-09-30", "Prior-period AR adjustment", "other_income", 156286, 1)]
    # S5: two balance-sheet opening items (reimbursable plug, shareholder loan) are loaded as well
    assert golden_db.execute("SELECT COUNT(*) FROM adjustments").fetchone()[0] == 3
    assert golden_db.execute("SELECT COUNT(*) FROM month_locks").fetchone()[0] == 0


def test_golden_openings(golden_db):
    got = {r["code"]: (r["opening_date"], r["opening_balance_cents"]) for r in golden_db.execute("SELECT * FROM accounts")}
    assert got == {
        "WISE_1430": ("2026-01-01", 1044457),
        "CHASE_5200": ("2026-01-01", 252956),
        "CARD_2110": ("2025-12-28", -148992),
        "WISE_JAR_RENT": ("2026-01-01", 29300),
        "WISE_JAR_TAX": ("2026-01-01", 95000),
    }


def test_load_golden_twice_raises(golden_db):
    with pytest.raises(RuntimeError):
        load_golden(golden_db)
    assert golden_db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 235


# ------------------------------------------------------------ seed command (ACCEPTANCE H1)
def test_golden_loader_leaves_seed_cutoff_null(golden_db):
    # only the seed command sets accounts.seeded_through; the loader the other tests use does not
    assert golden_db.execute("SELECT COUNT(*) FROM accounts WHERE seeded_through IS NOT NULL").fetchone()[0] == 0


def test_cli_seed_reproduces_golden(tmp_path, monkeypatch, capsys):
    """python -m tc.fixtures seed <db> (the CLI function) builds the 2026 history from an empty file.

    Close gate: `close.checklist(conn, '2026-09')` is what close_month runs. Its blocking items must all be ok;
    the one non-blocking item that can fail for September, tail_covered, is the warning that close_month lets
    through with acknowledge_tail=True (as tests/test_close.py does), so it is irrelevant to this check.
    The final step runs close_month itself that way.
    """
    from tc import close, reports
    from tc.fixtures import _main

    monkeypatch.setenv("TC_BACKUP_DIR", str(tmp_path / "backups"))
    path = tmp_path / "seeded.db"
    assert not path.exists()  # `make init` is not needed: the seed creates and initialises the file
    assert _main(["tc.fixtures", "seed", str(path)]) == 0
    out = capsys.readouterr().out
    assert "net income 18,618.29" in out and "revenue 32,186.00" in out
    assert "WISE_1430: 2026-10-31" in out and "CHASE_5200: 2026-09-30" in out and "CARD_2110: 2026-09-28" in out

    conn = connect(path)
    try:
        pnl = {r["line"]: r["ytd"] for r in reports.pnl_monthly(conn, 2026)}
        assert pnl["NET INCOME"] == 1861829
        assert pnl["Total Revenue"] == 3218600
        assert conn.execute("SELECT COUNT(*) FROM v_revenue_unbacked").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 235
        assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 18
        assert conn.execute("SELECT COUNT(*) FROM invoice_payments").fetchone()[0] == 15
        assert conn.execute("SELECT COUNT(*) FROM rules").fetchone()[0] > 0

        items = close.checklist(conn, "2026-09")
        blocking = [i for i in items if i["blocking"]]
        assert blocking and all(i["ok"] for i in blocking), [(i["key"], i["detail"]) for i in blocking if not i["ok"]]
        assert [i["key"] for i in items if not i["ok"]] in ([], ["tail_covered"])  # the warning needs the tick only

        cutoffs = {r["code"]: r["seeded_through"] for r in conn.execute("SELECT code, seeded_through FROM accounts")}
        assert cutoffs == {"WISE_1430": "2026-10-31", "CHASE_5200": "2026-09-30", "CARD_2110": "2026-09-28",
                           "WISE_JAR_RENT": None, "WISE_JAR_TAX": None}
        latest = dict(conn.execute("SELECT a.code, MAX(sb.as_of) FROM statement_balances sb "
                                   "JOIN accounts a ON a.id = sb.account_id GROUP BY a.id").fetchall())
        assert latest == {k: v for k, v in cutoffs.items() if v}

        for m in range(1, 10):  # months close oldest first
            close.close_month(conn, f"2026-{m:02d}", "seed check", acknowledge_tail=True)
        assert close.is_locked(conn, "2026-09")
    finally:
        conn.close()

    # a second seed refuses, says why and changes nothing
    assert _main(["tc.fixtures", "seed", str(path)]) == 1
    captured = capsys.readouterr()
    assert "already has 235 transactions" in captured.err and captured.out == ""
    conn = connect(path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 235
    finally:
        conn.close()
    assert _main(["tc.fixtures"]) == 2  # usage


def test_seed_on_initialised_and_on_pre_cutoff_schema_databases(tmp_path):
    from tc.fixtures import SeedError, seed

    # `make init` first is allowed (an empty initialised file)
    a = tmp_path / "inited.db"
    conn = connect(a)
    init_db(conn)
    conn.close()
    assert seed(a)["net_income_cents"] == 1861829
    # a database made before accounts.seeded_through existed gets the column
    b = tmp_path / "old.db"
    conn = connect(b)
    init_db(conn)
    conn.execute("ALTER TABLE accounts DROP COLUMN seeded_through")
    conn.commit()
    conn.close()
    assert seed(b)["cutoffs"]["WISE_1430"] == "2026-10-31"
    # a refused seed leaves the file byte for byte as it was
    before = b.read_bytes()
    with pytest.raises(SeedError):
        seed(b)
    assert b.read_bytes() == before
    # a failed seed (bad fixtures directory) removes the file it created / restores the one it found
    c = tmp_path / "never.db"
    with pytest.raises(Exception):
        seed(c, tmp_path / "no_such_fixtures")
    assert not c.exists()
    d = tmp_path / "empty_inited.db"
    conn = connect(d)
    init_db(conn)
    conn.close()
    snap = d.read_bytes()
    with pytest.raises(Exception):
        seed(d, tmp_path / "no_such_fixtures")
    assert d.read_bytes() == snap


def test_upgrade_relabels_reimbursables_in_open_months_only(golden_db):
    """LOW: an older database with 'Pass-through' on reimbursable rows gets one label, locked months untouched."""
    from tc import db as dbmod

    cat = golden_db.execute("SELECT id FROM categories WHERE name = 'Reimbursable Expenses Recoverable'").fetchone()[0]
    golden_db.execute("UPDATE categories SET default_type = 'Pass-through' WHERE id = ?", (cat,))
    golden_db.execute("UPDATE transactions SET type = 'Pass-through' WHERE category_id = ?", (cat,))
    first = golden_db.execute("SELECT MIN(substr(date,1,7)) FROM transactions WHERE category_id = ?", (cat,)).fetchone()[0]
    golden_db.execute("DROP TRIGGER trg_month_lock_requires_tie_out")  # the test only needs the lock row
    golden_db.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES (?, 'test')", (first,))
    golden_db.commit()
    locked_rows = golden_db.execute(
        "SELECT COUNT(*) FROM transactions WHERE category_id = ? AND substr(date,1,7) = ?", (cat, first)).fetchone()[0]
    dbmod.upgrade(golden_db)
    dbmod.upgrade(golden_db)  # idempotent
    assert golden_db.execute("SELECT default_type FROM categories WHERE id = ?", (cat,)).fetchone()[0] == "Reimbursable"
    rows = dict(golden_db.execute(
        "SELECT type, COUNT(*) FROM transactions WHERE category_id = ? GROUP BY type", (cat,)).fetchall())
    assert rows == {"Pass-through": locked_rows, "Reimbursable": 12 - locked_rows}


def test_accounts_and_categories_freeze_once_a_month_is_locked(golden_db):
    """LOW L4: the settings behind a closed month's numbers cannot be changed or deleted."""
    import sqlite3

    golden_db.execute("UPDATE accounts SET opening_balance_cents = opening_balance_cents WHERE id = 1")  # no lock yet
    golden_db.execute("DROP TRIGGER trg_month_lock_requires_tie_out")  # the test only needs the lock row
    golden_db.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES ('2026-01', 'test')")
    golden_db.commit()
    for sql in ("UPDATE accounts SET opening_balance_cents = 1 WHERE id = 1",
                "UPDATE accounts SET source_label = 'x' WHERE id = 1",
                "UPDATE accounts SET tie_required = 0 WHERE id = 1",
                "DELETE FROM accounts WHERE id = 5",
                "UPDATE categories SET section = 'sga' WHERE name = 'Service Revenue'",
                "UPDATE categories SET name = 'x' WHERE name = 'Bank Charges'",
                "DELETE FROM categories WHERE name = 'Interest Income'",
                "INSERT INTO accounts (code, source_label, name, kind, institution, opening_date, opening_balance_cents) "
                "VALUES ('NEW', 'new', 'new', 'bank', 'x', '2026-01-15', 5)"):
        with pytest.raises(sqlite3.IntegrityError, match="locked"):
            golden_db.execute(sql)
    # still editable: notes, seeded cutoff, default type, and a new account that opens after the locked month
    golden_db.execute("UPDATE accounts SET notes = 'ok', seeded_through = '2026-10-31' WHERE id = 1")
    golden_db.execute("UPDATE categories SET default_type = default_type WHERE name = 'Bank Charges'")
    golden_db.execute("INSERT INTO accounts (code, source_label, name, kind, institution, opening_date, opening_balance_cents) "
                      "VALUES ('NEW', 'new', 'new', 'bank', 'x', '2026-02-01', 0)")
