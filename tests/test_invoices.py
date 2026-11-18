"""Invoices and commission matching (SPEC rules 1 and 2, RULES.md Stage 7, UI.md 4.4)."""

import pytest

from tc import invoices as inv
from tc.db import insert_transaction
from tc.errors import LockedMonthError

WISE = 1  # accounts.id of 'USD account 1430'


def _cat(conn, name):
    return conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]


def _invoice_id(conn, source, number):
    return conn.execute(
        "SELECT id FROM invoices WHERE source_label = ? AND number = ?", (source, number)
    ).fetchone()[0]


def _tx(conn, date, amount, description, ref, category=None, type=None):
    return insert_transaction(
        conn, date=date, account_id=WISE, description=description, amount_cents=amount, source_ref=ref,
        category_id=_cat(conn, category) if category else None, type=type,
    )


def _golden_tx(conn, date, amount):
    return conn.execute(
        "SELECT id FROM transactions WHERE date = ? AND amount_cents = ? AND parent_id IS NULL", (date, amount)
    ).fetchone()[0]


# ------------------------------------------------------------ parse refs
@pytest.mark.parametrize(
    "text, expected",
    [
        ("INV-013 PO-26-10-2950 USD.3,016 COMMISSION FOR AUG 2026 INV-014 PO-26-10-2948 USD.2,690", ["INV-013", "INV-014"]),
        ("INV-000005 Dec 2025", ["INV-000005"]),
        ("INV.-000016", ["INV-000016"]),
        ("INV-0000157 PO-26-06", ["INV-0000157"]),            # typo kept as-is
        ("W.A. Rubbermate - Wise INV-013 Aug + INV-014 Sep", ["INV-013", "INV-014"]),
        ("inv - 12 and INV-12 again", ["INV-12"]),            # unique, in order
        ("/ROC/PO-26-08-2306", []),
        ("", []),
    ],
)
def test_parse_invoice_refs(text, expected):
    assert inv.parse_invoice_refs(text) == expected


# ------------------------------------------------------------ golden backfill
def test_backfill_golden_one_wire_many_invoices(golden_db):
    created = inv.backfill_payments(golden_db)
    assert created == 15  # 13 single-invoice receipts + the 2-invoice wire
    assert inv.backfill_payments(golden_db) == 0  # idempotent

    tx_id = _golden_tx(golden_db, "2026-10-19", 570600)
    rows = golden_db.execute(
        "SELECT v.label, ip.amount_cents FROM invoice_payments ip JOIN v_invoice_labels v ON v.id = ip.invoice_id "
        "WHERE ip.transaction_id = ? ORDER BY v.label", (tx_id,)
    ).fetchall()
    assert [(r["label"], r["amount_cents"]) for r in rows] == [("Wise INV-013", 301600), ("Wise INV-014", 269000)]
    assert sum(r["amount_cents"] for r in rows) == 570600
    cat = golden_db.execute(
        "SELECT c.name, t.type FROM transactions t JOIN categories c ON c.id = t.category_id WHERE t.id = ?", (tx_id,)
    ).fetchone()
    assert (cat["name"], cat["type"]) == ("Service Revenue", "Income")
    # Firstbase INV-000014 (Feb 2025 back-invoice) is a different invoice and was paid by the 30 Apr receipt
    fb = golden_db.execute(
        "SELECT t.date FROM invoice_payments ip JOIN invoices i ON i.id = ip.invoice_id "
        "JOIN transactions t ON t.id = ip.transaction_id WHERE i.source_label = 'Firstbase' AND i.number = 'INV-000014'"
    ).fetchone()
    assert fb["date"] == "2026-04-30"


def test_backfill_keeps_ledger_and_views_clean(golden_db):
    before = golden_db.execute(
        "SELECT id, category_id, type, review_status, amount_cents FROM transactions ORDER BY id"
    ).fetchall()
    inv.backfill_payments(golden_db)
    after = golden_db.execute(
        "SELECT id, category_id, type, review_status, amount_cents FROM transactions ORDER BY id"
    ).fetchall()
    assert [tuple(r) for r in before] == [tuple(r) for r in after]
    assert golden_db.execute("SELECT COUNT(*) FROM v_revenue_unbacked").fetchone()[0] == 0
    # every WA receipt is now fully allocated (rule 2: payments == gross wire)
    assert inv.unmatched_receipts(golden_db) == []
    for r in golden_db.execute(
        "SELECT t.amount_cents, SUM(ip.amount_cents) s FROM transactions t JOIN invoice_payments ip "
        "ON ip.transaction_id = t.id GROUP BY t.id"
    ):
        assert r["amount_cents"] == r["s"]


def test_commission_check_2026(golden_db):
    inv.backfill_payments(golden_db)
    chk = inv.commission_check(golden_db, 2026)
    assert chk == {"invoice_paid_cents": 3218600, "revenue_cents": 3218600, "diff_cents": 0, "unbacked": []}


def test_commission_check_lists_unbacked_revenue(golden_db):
    # before any payment rows exist all 10 Service Revenue rows are unbacked
    chk = inv.commission_check(golden_db, 2026)
    assert chk["invoice_paid_cents"] == 0 and chk["revenue_cents"] == 3218600 and chk["diff_cents"] == -3218600
    assert len(chk["unbacked"]) == 10
    assert set(chk["unbacked"][0]) == {"transaction_id", "date", "amount_cents", "commission_backed_cents"}


def test_unpaid_lists_wise_inv_010(golden_db):
    inv.backfill_payments(golden_db)
    rows = inv.unpaid(golden_db, as_of="2026-11-15")
    assert len(rows) == 1
    r = rows[0]
    assert (r["label"], r["type"], r["amount_cents"], r["issue_date"], r["due_date"], r["days_open"]) == (
        "Wise INV-010", "Reimbursable", 564514, "2026-10-02", None, 44)
    assert set(r) == {"id", "label", "type", "issue_date", "due_date", "amount_cents", "days_open"}


def test_commission_rows_obey_boxes_times_rate(golden_db):
    assert inv.flag_commission_mismatches(golden_db) == []
    rows = golden_db.execute("SELECT boxes, rate_cents, amount_cents FROM invoices WHERE type = 'Commission'").fetchall()
    assert len(rows) == 11 and all(r["boxes"] * r["rate_cents"] == r["amount_cents"] for r in rows)
    # a voided invoice is exempt from the CHECK, so a bad one can exist: it must be listed, not crash
    golden_db.execute(
        "INSERT INTO invoices (number, source_label, issue_date, commission_month, type, boxes, rate_cents, "
        "amount_cents, status) VALUES ('INV-099','Wise','2026-10-20','2026-10','Voided',100,50,6000,'Voided')"
    )
    flagged = inv.flag_commission_mismatches(golden_db)
    assert [(f["label"], f["expected_cents"], f["diff_cents"]) for f in flagged] == [("Wise INV-099", 5000, 1000)]


def test_register_and_filters(golden_db):
    inv.backfill_payments(golden_db)
    allrows = inv.list_invoices(golden_db)
    assert len(allrows) == 18
    assert set(allrows[0]) == {
        "id", "label", "issue_date", "due_date", "commission_month", "type", "boxes", "rate_cents", "amount_cents",
        "status", "paid_on", "paid_by_transaction_id", "replaces_label", "notes"}
    by_label = {r["label"]: r for r in allrows}
    assert by_label["Wise INV-014"]["replaces_label"] == "Wise INV-011"
    assert by_label["Wise INV-013"]["replaces_label"] == "Wise INV-012"
    assert by_label["Wise INV-014"]["paid_on"] == "2026-10-19"
    assert by_label["Firstbase INV-000016"]["paid_on"] == "2026-06-02"  # ledger date, not register 4 May
    assert by_label["Wise INV-010"]["paid_on"] is None and by_label["Wise INV-010"]["status"] == "Pending"
    # numbers keep their source label
    assert "Firstbase INV-000014" in by_label and "Wise INV-014" in by_label
    assert [r["label"] for r in inv.list_invoices(golden_db, status="Voided")] == ["Wise INV-012", "Wise INV-011"]
    assert {r["type"] for r in inv.list_invoices(golden_db, type="Reimbursable")} == {"Reimbursable"}
    assert len(inv.list_invoices(golden_db, type="Commission", status="Paid")) == 11
    assert allrows[0]["issue_date"] >= allrows[-1]["issue_date"]


def test_missing_commission_months(golden_db):
    assert inv.missing_commission_months(golden_db, through_month="2026-10", today="2026-11-15") == []
    # Oct 2026 ends 31 Oct; 45 days later is 15 Dec -> flagged from 16 Dec on
    assert inv.missing_commission_months(golden_db, through_month="2026-10", today="2026-12-15") == []
    assert inv.missing_commission_months(golden_db, through_month="2026-10", today="2026-12-16") == ["2026-10"]
    assert inv.missing_commission_months(golden_db, through_month="2026-11", today="2027-01-01") == ["2026-10"]


# ------------------------------------------------------------ propose_match
def test_propose_commission_and_reimbursable_by_reference(golden_db):
    p = inv.propose_match(golden_db, _golden_tx(golden_db, "2026-10-19", 570600))
    assert p["kind"] == "commission" and p["category"] == "Service Revenue" and p["type"] == "Income"
    assert p["review_reasons"] == []
    assert sorted(a[1] for a in p["allocations"]) == [269000, 301600]
    r = inv.propose_match(golden_db, _golden_tx(golden_db, "2026-05-05", 876408))
    assert r["kind"] == "reimbursable" and r["category"] == "Reimbursable Expenses Recoverable"
    assert r["type"] == "Reimbursable" and r["review_reasons"] == []
    # propose_match never writes
    assert golden_db.execute("SELECT COUNT(*) FROM invoice_payments").fetchone()[0] == 0


def test_propose_mixed_commission_and_reimbursable(empty_db):
    c = inv.add_invoice(empty_db, number="INV-050", source_label="Wise", issue_date="2026-11-01", type="Commission",
                        boxes=2000, amount_cents=100000, commission_month="2026-10")
    r = inv.add_invoice(empty_db, number="INV-051", source_label="Wise", issue_date="2026-11-01",
                        type="Reimbursable", amount_cents=25000)
    tx = _tx(empty_db, "2026-11-05", 125000, "Received from W.A. RUBBERMATE ref INV-050 INV-051", "T-MIX")
    p = inv.propose_match(empty_db, tx)
    assert p["kind"] == "mixed"
    assert p["allocations"] == [(c, 100000), (r, 25000)]
    assert p["category"] == "Reimbursable Expenses Recoverable" and p["type"] == "Reimbursable"
    assert p["review_reasons"] == ["mixed_invoice_wire"]


def test_propose_amount_only_for_typo_references(golden_db):
    # RULES.md Stage 7: 18 Jun 'INV-0000157' (typo for INV-000017) and 31 Aug '/ROC/PO-26-08-2306'
    t1 = _tx(golden_db, "2026-06-18", 320100, "Received from W.A. RUBBERMATE with reference INV-0000157 PO-26-06", "T-TYPO")
    p1 = inv.propose_match(golden_db, t1)
    assert p1["kind"] == "amount_only"
    assert p1["allocations"] == [(_invoice_id(golden_db, "Firstbase", "INV-000017"), 320100)]
    assert (p1["category"], p1["type"]) == ("Service Revenue", "Income")
    assert p1["review_reasons"] == ["invoice_matched_by_amount"]
    t2 = _tx(golden_db, "2026-08-31", 281250, "Received from W.A. RUBBERMATE with reference /ROC/PO-26-08-2306", "T-ROC")
    p2 = inv.propose_match(golden_db, t2)
    assert p2["kind"] == "amount_only"
    assert p2["allocations"] == [(_invoice_id(golden_db, "Firstbase", "INV-000019"), 281250)]
    assert p2["review_reasons"] == ["invoice_matched_by_amount"]


def test_backfill_skips_amount_only_and_mixed(empty_db):
    inv.add_invoice(empty_db, number="INV-000017", source_label="Firstbase", issue_date="2026-06-04",
                    type="Commission", boxes=6402, amount_cents=320100, commission_month="2026-05")
    _tx(empty_db, "2026-06-18", 320100, "W.A. RUBBERMATE ref INV-0000157", "T1")
    assert inv.backfill_payments(empty_db) == 0
    assert empty_db.execute("SELECT status FROM invoices").fetchone()[0] == "Pending"


def test_propose_none_cases(golden_db):
    inv.backfill_payments(golden_db)  # every invoice but INV-010 is now consumed
    t = _tx(golden_db, "2026-11-02", 123456, "W.A. RUBBERMATE ref INV-012", "T-NONE1")
    p = inv.propose_match(golden_db, t)
    assert p["kind"] == "none" and p["allocations"] == []
    assert p["category"] == "Reimbursable Expenses Recoverable"   # never revenue by default
    assert p["review_reasons"] == ["no_invoice_match", "voided_invoice_referenced"]
    # referenced invoice exists but the wire is short: amount mismatch
    t2 = _tx(golden_db, "2026-11-03", 500000, "W.A. RUBBERMATE ref INV-010", "T-NONE2")
    p2 = inv.propose_match(golden_db, t2)
    assert p2["kind"] == "none" and p2["review_reasons"] == ["invoice_amount_mismatch"]
    # a wire cannot pay an invoice that did not exist yet
    t3 = _tx(golden_db, "2026-09-30", 564514, "W.A. RUBBERMATE ref INV-010", "T-NONE3")
    assert inv.propose_match(golden_db, t3)["kind"] == "none"


# ------------------------------------------------------------ match_payment
def test_match_payment_one_wire_two_invoices(golden_db):
    tx = _golden_tx(golden_db, "2026-10-19", 570600)
    a, b = _invoice_id(golden_db, "Wise", "INV-013"), _invoice_id(golden_db, "Wise", "INV-014")
    with pytest.raises(ValueError, match="must equal the receipt"):
        inv.match_payment(golden_db, tx, [(a, 301600)])
    with pytest.raises(ValueError, match="gross invoice amount"):
        inv.match_payment(golden_db, tx, [(a, 300000), (b, 270600)])
    assert golden_db.execute("SELECT COUNT(*) FROM invoice_payments").fetchone()[0] == 0
    inv.match_payment(golden_db, tx, [(a, 301600), (b, 269000)])
    assert golden_db.execute("SELECT COUNT(*) FROM invoice_payments WHERE transaction_id = ?", (tx,)).fetchone()[0] == 2
    row = golden_db.execute(
        "SELECT c.name, t.type, t.review_status FROM transactions t JOIN categories c ON c.id = t.category_id WHERE t.id = ?",
        (tx,)).fetchone()
    assert tuple(row) == ("Service Revenue", "Income", "reviewed")
    with pytest.raises(ValueError, match="already matched"):
        inv.match_payment(golden_db, tx, [(a, 301600), (b, 269000)])


def test_match_payment_sets_reimbursable_and_pays_invoice(empty_db):
    r = inv.add_invoice(empty_db, number="INV-060", source_label="Wise", issue_date="2026-11-01",
                        type="Reimbursable", amount_cents=70000)
    tx = _tx(empty_db, "2026-11-04", 70000, "W.A. RUBBERMATE", "T-R")  # uncategorised
    assert [u["id"] for u in inv.unmatched_receipts(empty_db)] == [tx]
    inv.match_payment(empty_db, tx, [(r, 70000)])
    row = empty_db.execute(
        "SELECT c.name, t.type FROM transactions t JOIN categories c ON c.id = t.category_id WHERE t.id = ?", (tx,)
    ).fetchone()
    assert tuple(row) == ("Reimbursable Expenses Recoverable", "Reimbursable")
    assert empty_db.execute("SELECT status FROM invoices WHERE id = ?", (r,)).fetchone()[0] == "Paid"
    assert inv.unmatched_receipts(empty_db) == []
    assert inv.unpaid(empty_db, as_of="2026-11-15") == []
    # an invoice can be paid once
    tx2 = _tx(empty_db, "2026-11-06", 70000, "W.A. RUBBERMATE", "T-R2")
    with pytest.raises(ValueError, match="already paid"):
        inv.match_payment(empty_db, tx2, [(r, 70000)])


def test_one_rule_violation_is_refused(empty_db):
    c = inv.add_invoice(empty_db, number="INV-070", source_label="Wise", issue_date="2026-11-01", type="Commission",
                        boxes=1000, amount_cents=50000, commission_month="2026-10")
    r = inv.add_invoice(empty_db, number="INV-071", source_label="Wise", issue_date="2026-11-01",
                        type="Reimbursable", amount_cents=50000)
    # (a) a receipt booked as Service Revenue cannot settle a Reimbursable invoice
    rev = _tx(empty_db, "2026-11-05", 50000, "W.A. RUBBERMATE", "T-REV", "Service Revenue", "Income")
    with pytest.raises(ValueError, match="ONE RULE"):
        inv.match_payment(empty_db, rev, [(r, 50000)])
    # (b) one receipt cannot settle both kinds
    both = _tx(empty_db, "2026-11-06", 100000, "W.A. RUBBERMATE", "T-BOTH")
    with pytest.raises(ValueError, match="ONE RULE"):
        inv.match_payment(empty_db, both, [(c, 50000), (r, 50000)])
    # nothing was written
    assert empty_db.execute("SELECT COUNT(*) FROM invoice_payments").fetchone()[0] == 0
    assert empty_db.execute("SELECT status FROM invoices WHERE id = ?", (r,)).fetchone()[0] == "Pending"
    cat = empty_db.execute("SELECT c.name FROM transactions t JOIN categories c ON c.id = t.category_id WHERE t.id = ?", (rev,)).fetchone()[0]
    assert cat == "Service Revenue"
    # (c) the schema trigger is the backstop: a raw insert is refused too
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError, match="ONE RULE"):
        empty_db.execute("INSERT INTO invoice_payments (transaction_id, invoice_id, amount_cents) VALUES (?,?,?)", (rev, r, 50000))
    # a voided invoice cannot be paid
    inv.void_invoice(empty_db, r, "wrong amount")
    with pytest.raises(ValueError, match="voided"):
        inv.match_payment(empty_db, both, [(r, 100000)])


def test_match_payment_on_locked_month_raises(golden_db):
    golden_db.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES ('2026-01', 'test close')")
    golden_db.commit()
    tx = _golden_tx(golden_db, "2026-01-30", 378650)
    inv_id = _invoice_id(golden_db, "Firstbase", "INV-000005")
    with pytest.raises(LockedMonthError) as ei:
        inv.match_payment(golden_db, tx, [(inv_id, 378650)])
    assert ei.value.month == "2026-01"
    with pytest.raises(LockedMonthError):
        inv.split_receipt(golden_db, tx, [[(inv_id, 378650)]])
    assert golden_db.execute("SELECT COUNT(*) FROM invoice_payments").fetchone()[0] == 0
    assert tx not in [u["id"] for u in inv.unmatched_receipts(golden_db)]
    assert {u["date"][:7] for u in inv.unmatched_receipts(golden_db)} >= {"2026-03", "2026-10"}
    assert inv.unmatched_receipts(golden_db, month="2026-01") == []


# ------------------------------------------------------------ split_receipt
def test_split_receipt_mixed_wire(empty_db):
    c = inv.add_invoice(empty_db, number="INV-080", source_label="Wise", issue_date="2026-11-01", type="Commission",
                        boxes=2000, amount_cents=100000, commission_month="2026-10")
    r = inv.add_invoice(empty_db, number="INV-081", source_label="Wise", issue_date="2026-11-01",
                        type="Reimbursable", amount_cents=25000)
    tx = _tx(empty_db, "2026-11-05", 125000, "W.A. RUBBERMATE INV-080 INV-081", "TRANSFER-9")
    fee = insert_transaction(empty_db, date="2026-11-05", account_id=WISE, description="Wise Charges for: TRANSFER-9",
                             amount_cents=-611, category_id=_cat(empty_db, "Bank Charges"), type="Expense",
                             source_ref="FEE-TRANSFER-9", parent_id=tx)
    with pytest.raises(ValueError, match="ONE RULE"):
        inv.split_receipt(empty_db, tx, [[(c, 100000), (r, 25000)]])
    with pytest.raises(ValueError, match="add up"):
        inv.split_receipt(empty_db, tx, [[(c, 100000)], [(r, 20000)]])
    ids = inv.split_receipt(empty_db, tx, [[(c, 100000)], [(r, 25000)]])
    assert len(ids) == 2 and ids[0] == tx
    rows = empty_db.execute(
        "SELECT t.id, t.amount_cents, t.source_ref, t.date, c.name, t.type FROM transactions t "
        "JOIN categories c ON c.id = t.category_id WHERE t.id IN (?,?) ORDER BY t.id", ids).fetchall()
    assert [(x["amount_cents"], x["source_ref"], x["name"]) for x in rows] == [
        (100000, "TRANSFER-9", "Service Revenue"), (25000, "TRANSFER-9#2", "Reimbursable Expenses Recoverable")]
    assert {x["date"] for x in rows} == {"2026-11-05"}
    assert empty_db.execute("SELECT COUNT(*) FROM v_revenue_unbacked").fetchone()[0] == 0
    assert empty_db.execute("SELECT COUNT(*) FROM v_fee_parent_mismatch").fetchone()[0] == 0
    assert empty_db.execute("SELECT SUM(amount_cents) FROM transactions WHERE id IN (?,?)", ids).fetchone()[0] == 125000
    assert empty_db.execute("SELECT parent_id FROM transactions WHERE id = ?", (fee,)).fetchone()[0] == tx
    assert inv.commission_check(empty_db, 2026) == {
        "invoice_paid_cents": 100000, "revenue_cents": 100000, "diff_cents": 0, "unbacked": []}


# ------------------------------------------------------------ add / void
def test_add_invoice_rules(golden_db):
    new = inv.add_invoice(golden_db, number="INV-015", source_label="Wise", issue_date="2026-11-10", type="Commission",
                          boxes=5000, amount_cents=250000, commission_month="2026-10", due_date="2026-11-17")
    row = golden_db.execute("SELECT * FROM invoices WHERE id = ?", (new,)).fetchone()
    assert (row["status"], row["rate_cents"], row["amount_cents"]) == ("Pending", 50, 250000)
    with pytest.raises(ValueError, match="already exists"):
        inv.add_invoice(golden_db, number="INV-015", source_label="Wise", issue_date="2026-11-10", type="Commission",
                        boxes=5000, amount_cents=250000, commission_month="2026-10")
    # same number under another source is a different invoice
    inv.add_invoice(golden_db, number="INV-015", source_label="Firstbase", issue_date="2026-11-10",
                    type="Reimbursable", amount_cents=1000)
    assert sorted(r["label"] for r in inv.list_invoices(golden_db) if r["label"].endswith("INV-015")) == [
        "Firstbase INV-015", "Wise INV-015"]
    with pytest.raises(ValueError):   # boxes x rate != amount
        inv.add_invoice(golden_db, number="INV-016", source_label="Wise", issue_date="2026-11-10", type="Commission",
                        boxes=5000, amount_cents=250001, commission_month="2026-10")
    with pytest.raises(ValueError):   # no month
        inv.add_invoice(golden_db, number="INV-016", source_label="Wise", issue_date="2026-11-10", type="Commission",
                        boxes=5000, amount_cents=250000)
    with pytest.raises(ValueError):   # rate is fixed
        inv.add_invoice(golden_db, number="INV-016", source_label="Wise", issue_date="2026-11-10", type="Commission",
                        boxes=5000, rate_cents=60, amount_cents=300000, commission_month="2026-10")
    with pytest.raises(ValueError):   # bad date
        inv.add_invoice(golden_db, number="INV-016", source_label="Wise", issue_date="10/11/2026", type="Reimbursable",
                        amount_cents=100)
    with pytest.raises(ValueError):   # replaces must be a voided invoice
        inv.add_invoice(golden_db, number="INV-016", source_label="Wise", issue_date="2026-11-10", type="Reimbursable",
                        amount_cents=100, replaces_invoice_id=new)
    ok = inv.add_invoice(golden_db, number="INV-017", source_label="Wise", issue_date="2026-11-10", type="Reimbursable",
                         amount_cents=100, replaces_invoice_id=_invoice_id(golden_db, "Wise", "INV-012"))
    assert golden_db.execute("SELECT replaces_invoice_id FROM invoices WHERE id = ?", (ok,)).fetchone()[0] == \
        _invoice_id(golden_db, "Wise", "INV-012")


def test_void_invoice(golden_db):
    inv.backfill_payments(golden_db)
    i10 = _invoice_id(golden_db, "Wise", "INV-010")
    with pytest.raises(ValueError, match="reason"):
        inv.void_invoice(golden_db, i10, "  ")
    with pytest.raises(ValueError, match="Pending"):  # already paid
        inv.void_invoice(golden_db, _invoice_id(golden_db, "Wise", "INV-013"), "oops")
    inv.void_invoice(golden_db, i10, "issued to the wrong entity")
    row = golden_db.execute("SELECT type, status, notes FROM invoices WHERE id = ?", (i10,)).fetchone()
    assert (row["type"], row["status"]) == ("Voided", "Voided")
    assert row["notes"].endswith("Voided (was Reimbursable): issued to the wrong entity")
    assert row["notes"].startswith("Shipment 10 customs duty")
    assert inv.unpaid(golden_db, as_of="2026-11-15") == []
    with pytest.raises(ValueError, match="Pending"):
        inv.void_invoice(golden_db, i10, "again")


def test_unmatch_payment_reopens_invoices(golden_db):
    tx = _golden_tx(golden_db, "2026-10-19", 570600)
    a, b = _invoice_id(golden_db, "Wise", "INV-013"), _invoice_id(golden_db, "Wise", "INV-014")
    with pytest.raises(ValueError, match="not matched"):
        inv.unmatch_payment(golden_db, tx)
    inv.match_payment(golden_db, tx, [(a, 301600), (b, 269000)])
    assert [m["id"] for m in inv.matched_receipts(golden_db) if m["id"] == tx]
    assert sorted(inv.unmatch_payment(golden_db, tx)) == sorted([a, b])
    assert golden_db.execute("SELECT COUNT(*) FROM invoice_payments WHERE transaction_id = ?", (tx,)).fetchone()[0] == 0
    assert {r[0] for r in golden_db.execute("SELECT status FROM invoices WHERE id IN (?,?)", (a, b))} == {"Pending"}
    assert golden_db.execute("SELECT review_status FROM transactions WHERE id = ?", (tx,)).fetchone()[0] == "needs_review"
    inv.match_payment(golden_db, tx, [(a, 301600), (b, 269000)])  # can be matched again
    assert {r[0] for r in golden_db.execute("SELECT status FROM invoices WHERE id IN (?,?)", (a, b))} == {"Paid"}


def test_unmatch_payment_refused_in_locked_month(golden_db):
    tx = _golden_tx(golden_db, "2026-01-30", 378650)
    inv_id = _invoice_id(golden_db, "Firstbase", "INV-000005")
    inv.match_payment(golden_db, tx, [(inv_id, 378650)])
    golden_db.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES ('2026-01', 'test close')")
    golden_db.commit()
    with pytest.raises(LockedMonthError):
        inv.unmatch_payment(golden_db, tx)
    assert golden_db.execute("SELECT COUNT(*) FROM invoice_payments WHERE transaction_id = ?", (tx,)).fetchone()[0] == 1
    assert [m for m in inv.matched_receipts(golden_db) if m["id"] == tx][0]["locked"] is True
