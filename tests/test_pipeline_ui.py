import pytest

from tc.db import insert_transaction
from tc.errors import LockedMonthError
from tc.pipeline_ui import (
    add_adjustment,
    confirm,
    list_accounts,
    list_adjustments,
    list_categories,
    mark_duplicate,
    review_queue,
    search_transactions,
    set_category,
    set_date,
)


def cat(conn, name):
    return conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]


def lock(conn, month):
    # bypass the tie-out trigger: tests only need the lock row itself
    conn.execute("DROP TRIGGER trg_month_lock_requires_tie_out")
    conn.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES (?, 'test')", (month,))
    conn.commit()


@pytest.fixture
def db(empty_db):
    c = empty_db
    ids = {}
    ids["new"] = insert_transaction(
        c, date="2026-10-05", account_id=1, description="NEW VENDOR LLC", amount_cents=-5000,
        source_ref="N1", payee="New Vendor", notes="[rule:none][review:no_rule,unknown_payee] call them",
        review_status="needs_review",
    )
    ids["fee"] = insert_transaction(
        c, date="2026-10-05", account_id=1, description="Wise Charges for: N1", amount_cents=-113,
        source_ref="FEE-N1", parent_id=ids["new"], notes="[review:parent_uncategorised]",
        review_status="needs_review",
    )
    ids["dup"] = insert_transaction(
        c, date="2026-10-06", account_id=1, description="Software Co", amount_cents=-1200, source_ref="D2",
        category_id=cat(c, "Software & Web Services"), type="Expense",
        notes="[review:possible_duplicate]", review_status="needs_review",
    )
    ids["orig"] = insert_transaction(
        c, date="2026-10-06", account_id=1, description="Software Co", amount_cents=-1200, source_ref="D1",
        category_id=cat(c, "Software & Web Services"), type="Expense", review_status="auto",
    )
    ids["proposed"] = insert_transaction(
        c, date="2026-10-07", account_id=2, description="HTGROWTH", amount_cents=-90000, source_ref="P1",
        category_id=cat(c, "Other Professional Services"), type="Expense",
        notes="[rule:htgrowth][review:confirm_rule]", review_status="needs_review",
    )
    c.commit()
    return c, ids


def test_review_queue_lists_reasons(db):
    c, ids = db
    q = review_queue(c, month="2026-10")
    assert [r["id"] for r in q] == [ids["new"], ids["fee"], ids["dup"], ids["proposed"]]
    new = q[0]
    assert new["reasons"] == ["no_rule", "unknown_payee"]
    assert new["category"] is None and new["type"] is None
    assert new["account"] == "USD account 1430" and new["amount_cents"] == -5000
    assert q[1]["parent_id"] == ids["new"] and q[1]["reasons"] == ["parent_uncategorised"]
    assert q[3]["category"] == "Other Professional Services" and q[3]["reasons"] == ["confirm_rule"]
    assert review_queue(c, month="2026-09") == []
    assert len(review_queue(c)) == 4
    with pytest.raises(ValueError):
        review_queue(c, month="2026-13")


def test_set_category_moves_fee_child_and_leaves_queue(db):
    c, ids = db
    set_category(c, ids["new"], cat(c, "Software & Web Services"), "Expense", notes="checked")
    row = c.execute("SELECT * FROM transactions WHERE id = ?", (ids["new"],)).fetchone()
    assert (row["category_id"], row["type"], row["review_status"]) == (cat(c, "Software & Web Services"), "Expense", "reviewed")
    assert row["notes"] == "[rule:none] checked"
    fee = c.execute("SELECT * FROM transactions WHERE id = ?", (ids["fee"],)).fetchone()
    assert fee["category_id"] == cat(c, "Bank Charges") and fee["type"] == "Expense"
    assert fee["review_status"] == "auto" and fee["notes"] is None
    assert ids["new"] not in [r["id"] for r in review_queue(c)]
    assert ids["fee"] not in [r["id"] for r in review_queue(c)]
    assert c.execute("SELECT COUNT(*) FROM v_fee_parent_mismatch").fetchone()[0] == 0

    # owner's draw: the fee child follows into Owner's Draw (Personal), still no mismatch
    set_category(c, ids["new"], cat(c, "Owner's Draw"), "Personal")
    fee = c.execute("SELECT * FROM transactions WHERE id = ?", (ids["fee"],)).fetchone()
    assert fee["category_id"] == cat(c, "Owner's Draw") and fee["type"] == "Personal"
    assert c.execute("SELECT COUNT(*) FROM v_fee_parent_mismatch").fetchone()[0] == 0
    # free text is kept when notes is None
    assert c.execute("SELECT notes FROM transactions WHERE id = ?", (ids["new"],)).fetchone()[0] == "[rule:none] checked"


def test_set_category_validation(db):
    c, ids = db
    with pytest.raises(ValueError):
        set_category(c, ids["fee"], cat(c, "Bank Charges"), "Expense")  # fee row follows its parent
    with pytest.raises(ValueError):
        set_category(c, ids["new"], 999, "Expense")
    with pytest.raises(ValueError):
        set_category(c, ids["new"], cat(c, "Bank Charges"), "Nonsense")
    with pytest.raises(ValueError):
        set_category(c, 9999, cat(c, "Bank Charges"), "Expense")
    with pytest.raises(ValueError):
        set_category(c, ids["new"], cat(c, "Service Revenue"), "Income")  # not backed by invoices
    assert c.execute("SELECT category_id, review_status FROM transactions WHERE id = ?", (ids["new"],)).fetchone()[:] == (None, "needs_review")


def test_confirm(db):
    c, ids = db
    confirm(c, ids["proposed"])
    row = c.execute("SELECT * FROM transactions WHERE id = ?", (ids["proposed"],)).fetchone()
    assert row["review_status"] == "reviewed" and row["notes"] == "[rule:htgrowth]"
    assert row["category_id"] == cat(c, "Other Professional Services")
    assert ids["proposed"] not in [r["id"] for r in review_queue(c)]
    with pytest.raises(ValueError):
        confirm(c, ids["new"])  # nothing proposed
    with pytest.raises(ValueError):
        confirm(c, 9999)


def test_mark_duplicate_deletes_only_flagged_row(db):
    c, ids = db
    with pytest.raises(ValueError):
        mark_duplicate(c, ids["orig"])  # not flagged
    with pytest.raises(ValueError):
        mark_duplicate(c, ids["new"])
    n = c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    mark_duplicate(c, ids["dup"])
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n - 1
    assert c.execute("SELECT 1 FROM transactions WHERE id = ?", (ids["dup"],)).fetchone() is None
    assert c.execute("SELECT 1 FROM transactions WHERE id = ?", (ids["orig"],)).fetchone() is not None


def test_locked_month_changes_nothing(db):
    c, ids = db
    lock(c, "2026-10")
    before = [tuple(r) for r in c.execute("SELECT * FROM transactions ORDER BY id")]
    with pytest.raises(LockedMonthError):
        set_category(c, ids["new"], cat(c, "Software & Web Services"), "Expense")
    with pytest.raises(LockedMonthError):
        confirm(c, ids["proposed"])
    with pytest.raises(LockedMonthError):
        mark_duplicate(c, ids["dup"])
    with pytest.raises(LockedMonthError) as ei:
        add_adjustment(c, date="2026-10-31", label="x", section="sga", amount_cents=-100, reason="r")
    assert ei.value.month == "2026-10"
    assert [tuple(r) for r in c.execute("SELECT * FROM transactions ORDER BY id")] == before
    assert list_adjustments(c) == []
    assert review_queue(c) == []  # locked months are excluded from "all open months"


def test_search_filters(db):
    c, ids = db
    assert len(search_transactions(c)) == 5
    assert [r["id"] for r in search_transactions(c, account_id=2)] == [ids["proposed"]]
    assert [r["id"] for r in search_transactions(c, category_id=cat(c, "Software & Web Services"))] == [ids["dup"], ids["orig"]]
    assert {r["id"] for r in search_transactions(c, type="Expense")} == {ids["dup"], ids["orig"], ids["proposed"]}
    assert [r["id"] for r in search_transactions(c, review_status="auto")] == [ids["orig"]]
    assert [r["id"] for r in search_transactions(c, search="new vendor")] == [ids["new"]]
    assert [r["id"] for r in search_transactions(c, search="htgrowth")] == [ids["proposed"]]  # description/notes
    assert search_transactions(c, search="100%") == []  # % is literal
    assert search_transactions(c, month="2026-09") == []
    r = search_transactions(c, month="2026-10", review_status="needs_review", account_id=1)[0]
    assert set(r) >= {"id", "date", "account", "description", "amount_cents", "category", "type", "notes",
                      "review_status", "parent_id", "source_ref", "invoice_labels"}
    assert r["invoice_labels"] == []
    with pytest.raises(ValueError):
        search_transactions(c, type="Bogus")
    with pytest.raises(ValueError):
        search_transactions(c, review_status="Bogus")


def test_search_golden_wise_october(golden_db):
    wise = next(a["id"] for a in list_accounts(golden_db) if a["source_label"] == "USD account 1430")
    rows = search_transactions(golden_db, month="2026-10", account_id=wise)
    assert len(rows) == 12
    assert all(r["account"] == "USD account 1430" and r["date"].startswith("2026-10") for r in rows)
    assert rows == sorted(rows, key=lambda r: (r["date"], r["id"]))
    assert len(search_transactions(golden_db)) == 235


def test_search_invoice_labels_and_service_revenue(empty_db):
    c = empty_db
    tid = insert_transaction(
        c, date="2026-10-10", account_id=1, description="WA wire", amount_cents=500, source_ref="W1",
        category_id=cat(c, "Service Revenue"), type="Income", review_status="needs_review",
    )
    c.execute(
        "INSERT INTO invoices (id, number, source_label, issue_date, commission_month, type, boxes, rate_cents,"
        " amount_cents, status) VALUES (1, 'INV-014', 'Wise', '2026-10-01', '2026-09', 'Commission', 10, 50,"
        " 500, 'Paid')"
    )
    c.execute("INSERT INTO invoice_payments (transaction_id, invoice_id, amount_cents) VALUES (?, 1, 500)", (tid,))
    c.commit()
    assert search_transactions(c)[0]["invoice_labels"] == ["Wise INV-014"]
    # fully backed by a Commission invoice: Service Revenue may be (re)set
    set_category(c, tid, cat(c, "Service Revenue"), "Income")
    assert c.execute("SELECT review_status FROM transactions WHERE id = ?", (tid,)).fetchone()[0] == "reviewed"


def test_lists(golden_db):
    accts = list_accounts(golden_db)
    assert [a["code"] for a in accts] == ["WISE_1430", "CHASE_5200", "CARD_2110", "WISE_JAR_RENT", "WISE_JAR_TAX"]
    assert set(accts[0]) == {"id", "code", "source_label", "name", "kind", "statement_day"}
    assert [a["code"] for a in list_accounts(golden_db, tie_required_only=True)] == ["WISE_1430", "CHASE_5200", "CARD_2110"]
    cats = list_categories(golden_db)
    assert len(cats) == 10 and cats[0]["name"] == "Service Revenue" and cats[-1]["name"] == "Payable to WA Rubbermate"
    assert set(cats[0]) == {"id", "name", "statement", "section", "default_type"}


def test_adjustments_round_trip(empty_db):
    c = empty_db
    new_id = add_adjustment(c, date="2026-11-03", label="Accrual", section="sga", amount_cents=-2500,
                            reason="Test accrual", non_cash=False)
    other = add_adjustment(c, date="2026-12-01", label="AR", section="other_income", amount_cents=100,
                           reason="later")
    assert isinstance(new_id, int) and new_id != other
    rows = list_adjustments(c, month="2026-11")
    assert len(rows) == 1
    r = rows[0]
    assert (r["id"], r["date"], r["label"], r["section"], r["amount_cents"], r["non_cash"], r["reason"]) == (
        new_id, "2026-11-03", "Accrual", "sga", -2500, False, "Test accrual")
    assert [x["id"] for x in list_adjustments(c)] == [new_id, other]
    assert list_adjustments(c, month="2026-10") == []
    assert list_adjustments(c)[1]["non_cash"] is True  # default
    c.rollback()  # proves the inserts were committed
    assert len(list_adjustments(c)) == 2


def test_adjustment_validation(empty_db):
    c = empty_db
    good = dict(date="2026-11-03", label="L", section="sga", amount_cents=-1, reason="r")
    for bad in (dict(date="2026-13-40"), dict(date="11/03/2026"), dict(label=" "), dict(reason=""),
                dict(section="nope"), dict(amount_cents=12.5), dict(amount_cents=True)):
        with pytest.raises(ValueError):
            add_adjustment(c, **{**good, **bad})
    assert list_adjustments(c) == []


def test_set_category_merges_and_splits_regime_b_fee(empty_db):
    """RULES.md 4.3/4.4: a regime B Wise parent keeps its fee inside an Owner's Draw and is split otherwise."""
    c = empty_db
    draw = cat(c, "Owner's Draw")
    parent = insert_transaction(
        c, date="2026-10-21", account_id=1, description="Sent money to Joshua Decker", amount_cents=-60000,
        source_ref="TRANSFER-1", fee_cents=113, raw_type="DEBIT/TRANSFER", category_id=draw, type="Personal",
        review_status="auto",
    )
    c.commit()

    def rows():
        p = c.execute("SELECT * FROM transactions WHERE id = ?", (parent,)).fetchone()
        ch = c.execute("SELECT * FROM transactions WHERE parent_id = ?", (parent,)).fetchall()
        return p, ch

    set_category(c, parent, cat(c, "Bank Transfer"), "Transfer")  # draw -> not a draw: split
    p, ch = rows()
    assert (p["amount_cents"], p["fee_cents"], p["review_status"]) == (-59887, 0, "reviewed")
    assert len(ch) == 1 and (ch[0]["amount_cents"], ch[0]["source_ref"]) == (-113, "FEE-TRANSFER-1")
    assert ch[0]["category_id"] == cat(c, "Bank Charges") and ch[0]["type"] == "Expense"
    assert p["amount_cents"] + ch[0]["amount_cents"] == -60000  # parent + child = the CSV cash movement

    set_category(c, parent, cat(c, "Reimbursable Expenses Recoverable"), "Pass-through")
    p, ch = rows()  # still split: nothing merged or split twice
    assert (p["amount_cents"], len(ch)) == (-59887, 1)

    set_category(c, parent, draw, "Personal")  # not a draw -> draw: merge, the fee goes back inside the row
    p, ch = rows()
    assert (p["amount_cents"], p["fee_cents"], ch) == (-60000, 113, [])
    set_category(c, parent, draw, "Personal")  # draw -> draw: nothing
    p, ch = rows()
    assert (p["amount_cents"], p["fee_cents"], ch) == (-60000, 113, [])
    assert c.execute("SELECT COUNT(*) FROM v_fee_parent_mismatch").fetchone()[0] == 0


def test_set_date_moves_row_and_fee_child_in_open_months(db):
    c, ids = db
    set_date(c, ids["new"], "2026-09-30")
    rows = {r["id"]: r for r in c.execute("SELECT * FROM transactions WHERE id IN (?, ?)", (ids["new"], ids["fee"]))}
    assert rows[ids["new"]]["date"] == rows[ids["fee"]]["date"] == "2026-09-30"
    assert "date was 2026-10-05" in rows[ids["new"]]["notes"]
    assert rows[ids["new"]]["notes"].startswith("[rule:none][review:no_rule,unknown_payee]")  # tags are kept
    assert rows[ids["new"]]["amount_cents"] == -5000
    set_date(c, ids["new"], "2026-09-30")  # same date: nothing happens


def test_set_date_validation_and_locks(db):
    c, ids = db
    with pytest.raises(ValueError):
        set_date(c, ids["fee"], "2026-10-06")  # fee row follows its parent
    with pytest.raises(ValueError):
        set_date(c, ids["new"], "10/06/2026")
    with pytest.raises(ValueError):
        set_date(c, ids["new"], "2026-02-30")
    with pytest.raises(ValueError):
        set_date(c, 9999, "2026-10-06")
    lock(c, "2026-09")
    with pytest.raises(LockedMonthError):
        set_date(c, ids["new"], "2026-09-30")  # target month locked
    c.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES ('2026-10', 'test')")  # trigger is already dropped
    c.commit()
    with pytest.raises(LockedMonthError):
        set_date(c, ids["new"], "2026-11-01")  # source month locked
    assert c.execute("SELECT date FROM transactions WHERE id = ?", (ids["new"],)).fetchone()[0] == "2026-10-05"
