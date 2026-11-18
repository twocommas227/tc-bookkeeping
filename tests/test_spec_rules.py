"""The 8 accounting rules of docs/SPEC.md section 3 as testable statements, on the seeded workbook data."""

import sqlite3

import pytest

from tc import invoices, reports
from tc.db import insert_transaction


@pytest.fixture
def gdb(golden_db):
    invoices.backfill_payments(golden_db)
    golden_db.commit()
    return golden_db


def _cat(conn, name):
    return conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]


def test_rule1_service_revenue_only_from_commission_invoices(gdb):
    assert gdb.execute("SELECT COUNT(*) FROM v_revenue_unbacked").fetchone()[0] == 0
    wire = gdb.execute("SELECT id FROM transactions WHERE date = '2026-10-19' AND amount_cents = 570600").fetchone()[0]
    reimb = gdb.execute("SELECT id FROM invoices WHERE type = 'Reimbursable' LIMIT 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="ONE RULE"):  # the trigger is the backstop
        gdb.execute("INSERT INTO invoice_payments (transaction_id, invoice_id, amount_cents) VALUES (?, ?, 1)", (wire, reimb))


def test_rule1_reimbursable_never_in_pnl(gdb):
    assert gdb.execute("SELECT statement FROM categories WHERE name = 'Reimbursable Expenses Recoverable'").fetchone()[0] == "balance_sheet"
    assert gdb.execute("SELECT COUNT(*) FROM v_pnl_monthly WHERE category = 'Reimbursable Expenses Recoverable'").fetchone()[0] == 0
    cbp = gdb.execute("SELECT c.name FROM transactions t JOIN categories c ON c.id = t.category_id "
                      "WHERE t.amount_cents = -551014").fetchone()[0]
    assert cbp == "Reimbursable Expenses Recoverable"
    assert gdb.execute("SELECT c.name FROM transactions t JOIN categories c ON c.id = t.category_id "
                       "WHERE t.amount_cents = -13500 AND t.date = '2026-10-24'").fetchone()[0] == "Reimbursable Expenses Recoverable"


def test_rule2_revenue_month_is_payment_month(gdb):
    p = {r["line"]: r for r in reports.pnl_monthly(gdb, 2026)}["Commission Revenue (cash received)"]["months"]
    assert p["2026-01"] == 378650  # INV-000005 (Dec 2025 commission) is January revenue: it was paid on 30 Jan
    assert p["2026-09"] == 0 and p["2026-10"] == 570600  # Aug + Sep commissions, one wire on 19 Oct
    pays = gdb.execute("SELECT SUM(ip.amount_cents), COUNT(*) FROM invoice_payments ip JOIN transactions t "
                       "ON t.id = ip.transaction_id WHERE t.date = '2026-10-19'").fetchone()
    assert tuple(pays) == (570600, 2)  # one wire, two invoices, the sum equals the wire


def test_rule3_draw_vendors_map_to_owner_draw(gdb):
    for pattern in ("%BETTERMENT%", "Sent money to Joshua%", "Joshua F Decker (Sent)%"):
        cats = {r[0] for r in gdb.execute(
            "SELECT DISTINCT c.name FROM transactions t JOIN categories c ON c.id = t.category_id "
            "WHERE t.description LIKE ? AND t.description NOT LIKE 'Wise %'", (pattern,))}
        assert cats <= {"Owner's Draw", "Bank Transfer"} and "Owner's Draw" in cats, pattern
    assert gdb.execute("SELECT statement, default_type FROM categories WHERE name = \"Owner's Draw\"").fetchone()[:] == ("equity", "Personal")


def test_rule4_fee_follows_parent(empty_db):
    c = empty_db
    draw, bank = _cat(c, "Owner's Draw"), _cat(c, "Bank Charges")
    parent = insert_transaction(c, date="2026-10-21", account_id=1, description="Sent money to Joshua", amount_cents=-60000,
                                source_ref="P1", category_id=draw, type="Personal", review_status="auto")
    ok = insert_transaction(c, date="2026-10-21", account_id=1, description="Wise Charges for: P1", amount_cents=-113,
                            source_ref="FEE-P1", parent_id=parent, category_id=draw, type="Personal", review_status="auto")
    assert ok
    with pytest.raises(sqlite3.IntegrityError, match="Fee follows the parent"):  # a fee on a draw is not Bank Charges
        insert_transaction(c, date="2026-10-21", account_id=1, description="Wise Charges for: P1", amount_cents=-100,
                           source_ref="FEE-P1b", parent_id=parent, category_id=bank, type="Expense")
    assert c.execute("SELECT COUNT(*) FROM v_fee_parent_mismatch").fetchone()[0] == 0


def test_rule5_own_account_transfers_net_zero(gdb):
    net = gdb.execute("SELECT SUM(net_cents) FROM v_transfer_net").fetchone()[0]
    assert net == reports.transfers_in_transit(gdb, "2026-10-31")  # everything else nets to zero
    assert abs(net) == 149774  # the known legs whose other side posts later (SPEC section 3 notes)


def test_rule7_firstbase_35_is_postage(gdb):
    cats = {r[0] for r in gdb.execute(
        "SELECT DISTINCT c.name FROM transactions t JOIN categories c ON c.id = t.category_id "
        "WHERE t.description LIKE '%FIRSTBASE%' AND t.amount_cents = -3500")}
    assert cats == {"Postage & Delivery"}


def test_rule8_cannot_lock_untied_month(gdb):
    with pytest.raises(sqlite3.IntegrityError, match="Cannot close month"):
        gdb.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES ('2026-10', 'no chase / card balance yet')")
    assert gdb.execute("SELECT COUNT(*) FROM month_locks").fetchone()[0] == 0


def test_rule8_locked_month_rejects_changes(gdb):
    gdb.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES ('2026-01', 'tied')")  # January ties: the lock is accepted
    gdb.commit()
    with pytest.raises(sqlite3.IntegrityError, match="locked"):
        insert_transaction(gdb, date="2026-01-31", account_id=1, description="late", amount_cents=-1, source_ref="LATE")
    with pytest.raises(sqlite3.IntegrityError, match="locked"):
        gdb.execute("UPDATE transactions SET description = 'x' WHERE date = '2026-01-30'")
    with pytest.raises(sqlite3.IntegrityError, match="locked"):
        gdb.execute("DELETE FROM transactions WHERE date = '2026-01-30'")
    with pytest.raises(sqlite3.IntegrityError, match="locked"):
        gdb.execute("UPDATE statement_balances SET balance_cents = 1 WHERE substr(as_of,1,7) = '2026-01'")
