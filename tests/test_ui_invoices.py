from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from tc import close, db, fixtures, invoices

APP = Path(__file__).resolve().parents[1] / "app"
PAGE = str(APP / "pages" / "5_Invoices.py")


@pytest.fixture(autouse=True)
def _clear_cache():
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


@pytest.fixture
def golden_path(tmp_path, monkeypatch):
    path = tmp_path / "tc.db"
    conn = db.connect(path)
    db.init_db(conn)
    fixtures.load_golden(conn)
    invoices.backfill_payments(conn)
    conn.commit()
    conn.close()
    monkeypatch.setenv("TC_DB_PATH", str(path))
    monkeypatch.setenv("TC_BACKUP_DIR", str(tmp_path / "backups"))
    return path


def _run(month="2026-10"):
    at = AppTest.from_file(PAGE, default_timeout=30)
    at.session_state["month"] = month
    return at.run()


def _query(path, sql, params=()):
    conn = db.connect(path)
    try:
        return [tuple(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _df(at, marker):
    """The dataframe that has column `marker` (AppTest exposes no key for dataframes)."""
    found = [d.value for d in at.dataframe if marker in d.value.columns]
    assert len(found) == 1, f"expected one dataframe with {marker!r}, got {len(found)}"
    return found[0]


def _text(at):
    parts = [m.value for m in at.markdown] + [m.value for m in at.success] + [m.value for m in at.error]
    parts += [m.value for m in at.warning] + [m.value for m in at.info] + [m.value for m in at.caption]
    return "\n".join(parts)


def test_page_register_unpaid_and_check(golden_path):
    at = _run()
    assert not at.exception
    register = _df(at, "Paid on")
    assert len(register) == 18
    assert {"Invoice", "Issued", "Type", "Boxes", "Amount", "Status", "Paid on"} <= set(register.columns)
    unpaid = _df(at, "Days open")
    assert list(unpaid["Invoice"]) == ["Wise INV-010"]
    assert list(unpaid["Amount"]) == ["5,645.14"]
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Commission invoices paid (gross)"] == "32,186.00"
    assert metrics["Service Revenue in ledger"] == "32,186.00"
    assert metrics["Difference"] == "0.00"
    assert not at.error
    assert "Total unpaid: commission 0.00   reimbursable 5,645.14" in _text(at)


def test_register_filters(golden_path):
    at = _run()
    at.selectbox(key="inv_filter_status").set_value("Voided").run()
    assert not at.exception
    voided = _df(at, "Paid on")
    assert len(voided) > 0 and set(voided["Status"]) == {"Voided"}
    at.selectbox(key="inv_filter_status").set_value("all")
    at.selectbox(key="inv_filter_type").set_value("Reimbursable").run()
    assert set(_df(at, "Paid on")["Type"]) == {"Reimbursable"}


def test_add_commission_invoice_computes_amount(golden_path):
    at = _run()
    assert at.button(key="inv_add_btn").disabled  # nothing entered yet
    at.text_input(key="inv_new_number").set_value("INV-015")
    at.text_input(key="inv_new_month").set_value("2026-10")
    at.number_input(key="inv_new_boxes").set_value(1000).run()
    assert not at.exception
    assert "500.00" in _text(at)
    assert not at.number_input(key="inv_new_boxes").disabled
    assert not at.button(key="inv_add_btn").disabled
    at.button(key="inv_add_btn").click().run()
    assert not at.exception
    rows = _query(
        golden_path,
        "SELECT type, boxes, rate_cents, amount_cents, status, commission_month FROM invoices "
        "WHERE source_label = 'Wise' AND number = 'INV-015'",
    )
    assert rows == [("Commission", 1000, 50, 50000, "Pending", "2026-10")]
    assert any("Added Wise INV-015" in s.value for s in at.success)
    assert len(_df(at, "Paid on")) == 19
    assert at.text_input(key="inv_new_number").value == ""  # form cleared


def test_commission_amount_cannot_mismatch(golden_path):
    at = _run()
    # UI: a Commission invoice has no amount field and Add is off without boxes / month
    with pytest.raises(KeyError):
        at.text_input(key="inv_new_amount")
    at.text_input(key="inv_new_number").set_value("INV-016").run()
    assert at.button(key="inv_add_btn").disabled
    # module: a typed amount that differs from boxes x 0.50 is refused and nothing is written
    conn = db.connect(golden_path)
    try:
        with pytest.raises(ValueError, match="boxes x rate"):
            invoices.add_invoice(
                conn, number="INV-016", source_label="Wise", issue_date="2026-11-01", type="Commission",
                amount_cents=49999, boxes=1000, commission_month="2026-10",
            )
    finally:
        conn.close()
    assert _query(golden_path, "SELECT COUNT(*) FROM invoices WHERE number = 'INV-016'") == [(0,)]


def test_add_reimbursable_and_duplicate_error(golden_path):
    at = _run()
    at.radio(key="inv_new_type").set_value("Reimbursable").run()
    at.text_input(key="inv_new_number").set_value("INV-010")  # already exists for Wise
    at.text_input(key="inv_new_amount").set_value("100.00").run()
    at.button(key="inv_add_btn").click().run()
    assert not at.exception
    assert any("Wise INV-010 already exists" in e.value for e in at.error)
    assert _query(golden_path, "SELECT COUNT(*) FROM invoices") == [(18,)]
    at.text_input(key="inv_new_number").set_value("INV-017").run()
    at.button(key="inv_add_btn").click().run()
    assert _query(
        golden_path, "SELECT type, amount_cents, status FROM invoices WHERE number = 'INV-017'"
    ) == [("Reimbursable", 10000, "Pending")]


def test_void_invoice(golden_path):
    at = _run()
    assert at.button(key="inv_void_btn").disabled  # reason required
    sel = at.selectbox(key="inv_void_sel")
    inv_id = sel.value
    at.text_input(key="inv_void_reason").set_value("issued in error").run()
    at.button(key="inv_void_btn").click().run()
    assert not at.exception
    rows = _query(golden_path, "SELECT type, status, notes FROM invoices WHERE id = ?", (inv_id,))
    assert rows[0][:2] == ("Voided", "Voided") and "issued in error" in rows[0][2]
    assert any("Voided Wise INV-010" in s.value for s in at.success)
    assert _df(at, "Paid on")["Status"].tolist().count("Voided") >= 1
    assert "Nothing unpaid." in _text(at)


def _wa_receipt(path, cents, date="2026-10-28", ref="t1"):
    conn = db.connect(path)
    try:
        acct = conn.execute("SELECT id FROM accounts WHERE code = 'WISE_1430'").fetchone()[0]
        tid = db.insert_transaction(
            conn, date=date, account_id=acct, description="W.A. Rubbermate wire", amount_cents=cents, source_ref=ref
        )
        conn.commit()
        return tid
    finally:
        conn.close()


def _add(path, **kw):
    conn = db.connect(path)
    try:
        return invoices.add_invoice(conn, source_label="Wise", issue_date="2026-10-20", **kw)
    finally:
        conn.close()


def test_match_payment_commission(golden_path):
    _add(golden_path, number="INV-020", type="Commission", boxes=1000, rate_cents=50, amount_cents=50000,
         commission_month="2026-10")
    tid = _wa_receipt(golden_path, 50000)
    at = _run()
    assert at.selectbox(key="match_receipt").value == tid
    assert at.button(key="match_btn").disabled  # nothing ticked
    inv_id = _query(golden_path, "SELECT id FROM invoices WHERE number = 'INV-020'")[0][0]
    at.checkbox(key=f"match_inv_{inv_id}").check().run()
    assert "left 0.00" in _text(at)
    at.button(key="match_btn").click().run()
    assert not at.exception
    assert _query(golden_path, "SELECT status FROM invoices WHERE id = ?", (inv_id,)) == [("Paid",)]
    assert _query(
        golden_path,
        "SELECT c.name FROM transactions t JOIN categories c ON c.id = t.category_id WHERE t.id = ?", (tid,),
    ) == [("Service Revenue",)]
    assert any("Matched receipt 500.00" in s.value for s in at.success)


def test_match_blocked_until_allocation_equals_receipt(golden_path):
    _wa_receipt(golden_path, 100000)
    at = _run()
    inv10 = _query(golden_path, "SELECT id FROM invoices WHERE number = 'INV-010'")[0][0]
    at.checkbox(key=f"match_inv_{inv10}").check().run()
    assert at.button(key="match_btn").disabled
    assert "left -4,645.14" in _text(at)


def test_match_mixed_wire_uses_split(golden_path):
    _add(golden_path, number="INV-021", type="Commission", boxes=1000, rate_cents=50, amount_cents=50000,
         commission_month="2026-10")
    _add(golden_path, number="INV-022", type="Reimbursable", amount_cents=25000)
    tid = _wa_receipt(golden_path, 75000)
    at = _run()
    ids = {n: _query(golden_path, "SELECT id FROM invoices WHERE number = ?", (n,))[0][0] for n in ("INV-021", "INV-022")}
    for i in ids.values():
        at.checkbox(key=f"match_inv_{i}").check()
    at.run()
    assert at.button(key="match_btn").label == "Split receipt and match"
    at.button(key="match_btn").click().run()
    assert not at.exception
    assert _query(golden_path, "SELECT COUNT(*) FROM invoices WHERE number IN ('INV-021','INV-022') AND status = 'Paid'") == [(2,)]
    cats = _query(
        golden_path,
        "SELECT c.name, t.amount_cents FROM transactions t JOIN categories c ON c.id = t.category_id "
        "WHERE t.id = ? OR t.source_ref = (SELECT source_ref || '#2' FROM transactions WHERE id = ?) ORDER BY t.amount_cents",
        (tid, tid),
    )
    assert cats == [("Reimbursable Expenses Recoverable", 25000), ("Service Revenue", 50000)]


def test_no_unmatched_receipts_message(golden_path):
    at = _run()
    assert "No WA receipts waiting for an invoice." in _text(at)


def test_locked_month_receipt_not_offered(golden_path):
    tid = _wa_receipt(golden_path, 50000, date="2026-09-28", ref="t9")
    conn = db.connect(golden_path)
    try:
        # the receipt breaks the Sep tie-out, so lock past the guard trigger (temp DB only)
        conn.execute("DROP TRIGGER trg_month_lock_requires_tie_out")
        conn.execute("INSERT INTO month_locks (yyyy_mm, locked_at, reason) VALUES ('2026-09', '2026-11-01', 't')")
        conn.commit()
    finally:
        conn.close()
    at = _run("2026-09")
    assert not at.exception
    assert "No WA receipts waiting for an invoice." in _text(at)
    assert "(LOCKED)" in _text(at)
    # invoices stay addable while the month is locked
    assert not at.radio(key="inv_new_type").disabled
    conn = db.connect(golden_path)
    try:
        with pytest.raises(close.LockedMonthError):
            invoices.match_payment(conn, tid, [(1, 1)])
    finally:
        conn.close()


# ------------------------------------------------------------------ free-text source (ACCEPTANCE H1)
def test_source_is_free_text_with_existing_sources_as_suggestions(golden_path):
    at = _run()
    assert at.selectbox(key="inv_new_source").options == ["Wise", "Firstbase", "Other..."]
    with pytest.raises(KeyError):
        at.text_input(key="inv_new_source_other")  # shown only for "Other..."
    at.selectbox(key="inv_new_source").set_value("Other...").run()
    assert at.text_input(key="inv_new_source_other").value == ""
    at.radio(key="inv_new_type").set_value("Reimbursable").run()
    at.text_input(key="inv_new_number").set_value("INV-000099")
    at.text_input(key="inv_new_amount").set_value("100.00").run()
    assert at.button(key="inv_add_btn").disabled  # "Other..." without a name
    at.text_input(key="inv_new_source_other").set_value("  Acme Consulting ").run()
    assert not at.button(key="inv_add_btn").disabled
    at.button(key="inv_add_btn").click().run()
    assert not at.exception
    assert _query(golden_path, "SELECT source_label, number, type, amount_cents FROM invoices WHERE number = 'INV-000099'") == [
        ("Acme Consulting", "INV-000099", "Reimbursable", 10000)]
    assert any("Added Acme Consulting INV-000099" in s.value for s in at.success)
    assert at.selectbox(key="inv_new_source").value == "Wise"  # form cleared


def test_firstbase_series_can_be_entered_on_a_fresh_database(tmp_path, monkeypatch):
    path = tmp_path / "fresh.db"
    conn = db.connect(path)
    db.init_db(conn)
    conn.close()
    monkeypatch.setenv("TC_DB_PATH", str(path))
    monkeypatch.setenv("TC_BACKUP_DIR", str(tmp_path / "backups"))
    at = _run()
    assert at.selectbox(key="inv_new_source").options == ["Wise", "Other..."]  # no Firstbase invoice exists yet
    at.selectbox(key="inv_new_source").set_value("Other...").run()
    at.text_input(key="inv_new_source_other").set_value("Firstbase")
    at.radio(key="inv_new_type").set_value("Commission").run()
    at.text_input(key="inv_new_number").set_value("INV-000001")
    at.text_input(key="inv_new_month").set_value("2026-01")
    at.number_input(key="inv_new_boxes").set_value(100).run()
    at.button(key="inv_add_btn").click().run()
    assert not at.exception
    assert _query(path, "SELECT l.label, i.amount_cents FROM v_invoice_labels l JOIN invoices i ON i.id = l.id") == [
        ("Firstbase INV-000001", 5000)]
    assert at.selectbox(key="inv_new_source").options == ["Wise", "Firstbase", "Other..."]  # now a suggestion too


def test_unmatch_button_reopens_invoices(golden_path):
    at = _run("2026-10")
    assert not at.exception
    wire = _query(golden_path, "SELECT transaction_id FROM invoice_payments ip JOIN transactions t ON t.id = ip.transaction_id "
                               "WHERE t.date = '2026-10-19' LIMIT 1")
    assert wire, "golden data has the 19 Oct wire matched to INV-013 + INV-014"
    at.selectbox(key="unmatch_receipt").set_value(wire[0][0])
    at.button(key="unmatch_btn").click().run()
    assert not at.exception
    assert _query(golden_path, "SELECT COUNT(*) FROM invoice_payments WHERE transaction_id = ?", wire[0]) == [(0,)]
    assert _query(golden_path, "SELECT status FROM invoices WHERE number IN ('INV-013','INV-014') AND source_label = 'Wise'") == [("Pending",), ("Pending",)]
