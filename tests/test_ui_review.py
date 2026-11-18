"""AppTest smoke tests for the Review queue and Transactions pages (docs/UI.md pages 2 and 3, section 4.5).

The DB is built once from the October Wise CSV and the nine Chase *5200 / eight card *2110 statements through
pipeline.run_import (invoices loaded first, as tests/test_pipeline.py does), so the queue holds real rows:
2026-01 HTGROWTH 182.87, 2026-02 HTGROWTH 1,041.02, 2026-04 HTGROWTH 1,000.00 (all confirm_rule) and
2026-08 Chase -> card payment (unpaired_transfer). Every test works on a copy of that file.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from tc import db, pipeline, rules

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
REVIEW = str(APP / "pages" / "2_Review_queue.py")
TX = str(APP / "pages" / "3_Transactions.py")
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pipeline import CARD_PDFS, CHASE_PDFS, RAW, load_invoices  # noqa: E402

WISE = "USD account 1430"


# ---------------------------------------------------------------------- fixtures
@pytest.fixture(autouse=True)
def _clear_cache():
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    d = tmp_path_factory.mktemp("ui_review")
    mp = pytest.MonkeyPatch()
    mp.setenv("TC_BACKUP_DIR", str(d / "backups"))
    path = d / "template.db"
    conn = db.connect(path)
    db.init_db(conn)
    rules.sync_rules_table(conn, rules.load_rules())
    load_invoices(conn)
    for f in [RAW / "wise_2026-10.csv", *CHASE_PDFS, *CARD_PDFS]:
        pipeline.run_import(conn, f)
    conn.commit()
    conn.close()
    yield path
    mp.undo()


@pytest.fixture
def db_path(template_db, tmp_path, monkeypatch):
    path = tmp_path / "tc.db"
    shutil.copy(template_db, path)
    monkeypatch.setenv("TC_DB_PATH", str(path))
    monkeypatch.setenv("TC_BACKUP_DIR", str(tmp_path / "backups"))
    return path


@pytest.fixture
def rules_copy(tmp_path, monkeypatch):
    """A copy of rules.yaml that add_rule appends to; the packaged file must stay untouched."""
    copy = tmp_path / "rules.yaml"
    shutil.copy(rules.RULES_PATH, copy)
    monkeypatch.setattr(rules, "RULES_PATH", copy)
    return copy


# ---------------------------------------------------------------------- helpers
def q(path, sql, *params):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def queue_for(path, month=None):
    conn = db.connect(path)
    try:
        return pipeline.review_queue(conn, month=month)
    finally:
        conn.close()


def lock_month(path, yyyy_mm):
    """Lock a month in the fixture DB. The tie-out trigger is dropped: this DB has no Wise statement balances."""
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER trg_month_lock_requires_tie_out")
    conn.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES (?, 'test lock')", (yyyy_mm,))
    conn.commit()
    conn.close()


def run(page, month):
    at = AppTest.from_file(page, default_timeout=60)
    at.session_state["month"] = month
    return at.run()


def df_with(at, column):
    for d in at.dataframe:
        if column in d.value.columns:
            return d.value
    raise AssertionError(f"no table with column {column!r}")


def texts(at):
    return " | ".join(
        x.value for group in (at.subheader, at.text, at.markdown, at.caption, at.info, at.warning, at.error, at.success) for x in group
    )


def queue_id(path, month):
    rows = q(path, "SELECT id FROM transactions WHERE review_status = 'needs_review' AND substr(date,1,7) = ?", month)
    assert len(rows) == 1, rows
    return rows[0]["id"]


# ====================================================================== Review queue
def test_fixture_queue_has_real_rows(db_path):
    assert [r["reasons"] for r in queue_for(db_path)] == [
        ["confirm_rule"], ["confirm_rule"], ["confirm_rule"], ["unpaired_transfer"],
    ]


def test_review_page_lists_every_queue_row_for_the_month(db_path):
    for month in ("2026-01", "2026-02", "2026-04", "2026-08"):
        at = run(REVIEW, month)
        assert not at.exception, at.exception
        expected = queue_for(db_path, month)
        assert len(expected) == 1
        table = df_with(at, "Why it is here")
        assert len(table) == len(expected)
        assert list(table["Id"]) == [r["id"] for r in expected]
    at = run(REVIEW, "2026-04")
    row = df_with(at, "Why it is here").iloc[0]
    assert row["Why it is here"] == "Check this one"
    assert row["Proposed category"] == "Other Professional Services"
    assert row["Amount"] == "-1,000.00"
    assert "1 row needs a decision" in texts(at)
    assert "Check this one" in texts(at)  # plain words, not the reason code
    assert "confirm_rule" not in texts(at)


def test_review_page_all_open_months_matches_queue(db_path):
    at = AppTest.from_file(REVIEW, default_timeout=60)
    at.session_state["month"] = "2026-04"
    at.run()
    at.radio(key="rq_scope").set_value("all").run()
    assert not at.exception, at.exception
    expected = queue_for(db_path)
    assert len(expected) == 4
    assert len(df_with(at, "Why it is here")) == len(expected)
    assert "4 rows need a decision" in texts(at)
    assert "Transfer - other leg not found" in texts(at)


def test_review_page_empty_month(db_path):
    at = run(REVIEW, "2026-10")
    assert not at.exception, at.exception
    assert "Nothing to review for 2026-10." in texts(at)
    assert not at.dataframe


def test_review_page_empty_database(tmp_path, monkeypatch):
    monkeypatch.setenv("TC_DB_PATH", str(tmp_path / "empty.db"))
    at = run(REVIEW, "2026-10")
    assert not at.exception, at.exception
    current = at.sidebar.selectbox(key="month").value  # an empty DB offers only the current month
    assert f"Nothing to review for {current}." in texts(at)


def test_review_set_category_removes_row_and_marks_reviewed(db_path):
    tid = queue_id(db_path, "2026-04")
    at = run(REVIEW, "2026-04")
    assert at.selectbox(key=f"rq_cat_{tid}").value == "Other Professional Services"  # the rule's proposal
    at.selectbox(key=f"rq_cat_{tid}").select("Reimbursable Expenses Recoverable").run()
    default_type = q(db_path, "SELECT default_type FROM categories WHERE name = 'Reimbursable Expenses Recoverable'")[0]["default_type"]
    assert at.selectbox(key=f"rq_type_{tid}").value == default_type  # type follows the category default
    at.selectbox(key=f"rq_type_{tid}").select("Reimbursable").run()  # ... and can be overridden
    at.text_input(key=f"rq_note_{tid}").set_value("WA reimbursable per INV-000011")
    at.button(key=f"rq_save_{tid}").click().run()
    assert not at.exception, at.exception
    row = q(db_path, "SELECT t.review_status, c.name AS cat, t.type, t.notes FROM transactions t "
                     "JOIN categories c ON c.id = t.category_id WHERE t.id = ?", tid)[0]
    assert (row["review_status"], row["cat"], row["type"]) == ("reviewed", "Reimbursable Expenses Recoverable", "Reimbursable")
    assert "WA reimbursable per INV-000011" in row["notes"]
    assert queue_for(db_path, "2026-04") == []
    assert "Nothing to review for 2026-04." in texts(at)
    assert f"Saved #{tid} as Reimbursable Expenses Recoverable / Reimbursable." in texts(at)
    assert len(queue_for(db_path)) == 3  # the other open months are untouched


def test_review_service_revenue_not_offered(db_path):
    tid = queue_id(db_path, "2026-04")
    at = run(REVIEW, "2026-04")
    assert "Service Revenue" not in at.selectbox(key=f"rq_cat_{tid}").options
    assert "Reimbursable Expenses Recoverable" in at.selectbox(key=f"rq_cat_{tid}").options


def test_review_confirm_proposed(db_path):
    tid = queue_id(db_path, "2026-01")
    before = q(db_path, "SELECT category_id, type FROM transactions WHERE id = ?", tid)[0]
    at = run(REVIEW, "2026-01")
    at.button(key=f"rq_confirm_{tid}").click().run()
    assert not at.exception, at.exception
    after = q(db_path, "SELECT category_id, type, review_status FROM transactions WHERE id = ?", tid)[0]
    assert after["review_status"] == "reviewed"
    assert (after["category_id"], after["type"]) == (before["category_id"], before["type"])
    assert queue_for(db_path, "2026-01") == []
    assert "Nothing to review for 2026-01." in texts(at)


def test_review_create_rule_previews_count_and_appends_to_rules_copy(db_path, rules_copy):
    packaged = (Path(rules.__file__).with_name("rules.yaml")).read_bytes()
    tid = queue_id(db_path, "2026-04")
    description = " ".join(q(db_path, "SELECT description FROM transactions WHERE id = ?", tid)[0]["description"].split())
    at = run(REVIEW, "2026-04")
    assert not at.checkbox(key=f"rq_rule_{tid}").value
    at.checkbox(key=f"rq_rule_{tid}").check().run()
    assert at.text_input(key=f"rq_pattern_{tid}").value == description  # pre-filled from the description

    # preview = existing rows whose description contains the pattern, counted independently of the page
    rx = re.compile(re.escape(description), re.IGNORECASE)
    expected = sum(1 for r in q(db_path, "SELECT description FROM transactions") if rx.search(" ".join(r["description"].split())))
    assert expected >= 3  # the three HTGROWTH card charges
    assert f"This rule would match {expected} existing rows (1 in the review queue)." in texts(at)

    # narrowing the pattern changes the preview
    at.text_input(key=f"rq_pattern_{tid}").set_value("HTGROWTH").run()
    wide = sum(1 for r in q(db_path, "SELECT description FROM transactions") if "htgrowth" in r["description"].lower())
    assert f"This rule would match {wide} existing rows" in texts(at)
    at.text_input(key=f"rq_pattern_{tid}").set_value(description).run()

    at.selectbox(key=f"rq_cat_{tid}").select("Reimbursable Expenses Recoverable").run()
    at.selectbox(key=f"rq_type_{tid}").select("Reimbursable").run()
    at.button(key=f"rq_save_{tid}").click().run()
    assert not at.exception, at.exception
    assert "Rule added." in texts(at)

    loaded = rules.load_rules(rules_copy)
    new = loaded[-1]
    assert new.category == "Reimbursable Expenses Recoverable" and new.type == "Reimbursable"
    assert new.priority == 50 and new.action == "categorize"
    assert re.search(new.match["description"], description, re.IGNORECASE)
    assert len(loaded) == len(rules.load_rules(Path(rules.__file__).with_name("rules.yaml"))) + 1
    assert Path(rules.__file__).with_name("rules.yaml").read_bytes() == packaged  # real rule file untouched
    assert q(db_path, "SELECT review_status FROM transactions WHERE id = ?", tid)[0]["review_status"] == "reviewed"
    assert q(db_path, "SELECT COUNT(*) AS n FROM rules WHERE label LIKE ?", f"%{new.id}%")[0]["n"] == 1


def test_review_create_rule_without_category_choice_blocks_save(db_path, rules_copy):
    tid = queue_id(db_path, "2026-08")  # unpaired_transfer: proposed Bank Transfer
    at = run(REVIEW, "2026-08")
    at.checkbox(key=f"rq_rule_{tid}").check().run()
    at.text_input(key=f"rq_pattern_{tid}").set_value("   ").run()
    assert "Enter a pattern to see how many rows it would match." in texts(at)
    before = rules_copy.read_text()
    at.button(key=f"rq_save_{tid}").click().run()
    assert not at.exception, at.exception
    assert "Enter a pattern for the rule" in texts(at)
    assert rules_copy.read_text() == before
    assert q(db_path, "SELECT review_status FROM transactions WHERE id = ?", tid)[0]["review_status"] == "needs_review"


def test_review_duplicate_rule_is_reported_not_raised(db_path, rules_copy):
    tid = queue_id(db_path, "2026-04")
    at = run(REVIEW, "2026-04")
    at.checkbox(key=f"rq_rule_{tid}").check().run()
    at.selectbox(key=f"rq_cat_{tid}").select("Reimbursable Expenses Recoverable").run()
    at.button(key=f"rq_save_{tid}").click().run()
    n = len(rules.load_rules(rules_copy))
    # same pattern again on another row of the same vendor: the rule exists, the page says so and does not crash
    other = queue_id(db_path, "2026-01")
    at2 = run(REVIEW, "2026-01")
    at2.checkbox(key=f"rq_rule_{other}").check().run()
    at2.text_input(key=f"rq_pattern_{other}").set_value(
        " ".join(q(db_path, "SELECT description FROM transactions WHERE id = ?", tid)[0]["description"].split())
    ).run()
    at2.selectbox(key=f"rq_cat_{other}").select("Reimbursable Expenses Recoverable").run()
    at2.button(key=f"rq_save_{other}").click().run()
    assert not at2.exception, at2.exception
    assert "Rule not added" in texts(at2)
    assert len(rules.load_rules(rules_copy)) == n


def test_review_mark_duplicate_deletes_row(db_path):
    wise = q(db_path, "SELECT id, date, amount_cents, category_id, type FROM transactions "
                      "WHERE substr(date,1,7) = '2026-10' AND parent_id IS NULL AND category_id IS NOT NULL LIMIT 1")[0]
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO transactions (date, account_id, description, amount_cents, category_id, type, source_ref, notes, review_status)"
        " VALUES (?, 1, 'copy of an existing line', ?, ?, ?, 'DUP-TEST-1', '[review:possible_duplicate]', 'needs_review')",
        (wise["date"], wise["amount_cents"], wise["category_id"], wise["type"]),
    )
    dup_id = cur.lastrowid
    conn.commit()
    conn.close()
    at = run(REVIEW, "2026-10")
    assert not at.exception, at.exception
    assert "Possible duplicate" in texts(at)
    assert f"Duplicates existing row #{wise['id']}" in texts(at)
    assert not [b for b in at.button if b.key == f"rq_save_{dup_id}"]  # keep / mark duplicate only
    at.button(key=f"rq_dup_{dup_id}").click().run()
    assert not at.exception, at.exception
    assert q(db_path, "SELECT COUNT(*) AS n FROM transactions WHERE id = ?", dup_id)[0]["n"] == 0
    assert q(db_path, "SELECT COUNT(*) AS n FROM transactions WHERE id = ?", wise["id"])[0]["n"] == 1
    assert "Nothing to review for 2026-10." in texts(at)


def test_review_locked_month_is_read_only(db_path):
    tid = queue_id(db_path, "2026-04")
    lock_month(db_path, "2026-04")
    at = run(REVIEW, "2026-04")
    assert not at.exception, at.exception
    assert any("Month 2026-04 is locked" in w.value for w in at.warning)
    assert at.button(key="rq_rerun_btn").disabled
    assert at.button(key=f"rq_confirm_{tid}").disabled
    assert at.button(key=f"rq_save_{tid}").disabled
    assert at.selectbox(key=f"rq_cat_{tid}").disabled
    assert at.checkbox(key=f"rq_rule_{tid}").disabled
    assert at.text_input(key=f"rq_note_{tid}").disabled
    assert q(db_path, "SELECT review_status FROM transactions WHERE id = ?", tid)[0]["review_status"] == "needs_review"


def test_review_locked_month_without_rows_shows_notice(db_path):
    lock_month(db_path, "2026-10")
    at = run(REVIEW, "2026-10")
    assert not at.exception, at.exception
    assert any("Month 2026-10 is locked" in w.value for w in at.warning)
    assert "Nothing to review for 2026-10." in texts(at)
    assert at.button(key="rq_rerun_btn").disabled


def test_review_rerun_rules_is_a_noop_on_unchanged_rules(db_path):
    before = q(db_path, "SELECT id, category_id, type, review_status FROM transactions ORDER BY id")
    at = run(REVIEW, "2026-04")
    at.button(key="rq_rerun_btn").click().run()
    assert not at.exception, at.exception
    assert q(db_path, "SELECT id, category_id, type, review_status FROM transactions ORDER BY id") == before
    assert "Rules re-run on" in texts(at)


# ====================================================================== Transactions
def test_transactions_oct_wise_filter_shows_12_rows(db_path):
    at = run(TX, "2026-10")
    assert not at.exception, at.exception
    assert at.selectbox(key="tx_f_month").value == "2026-10"  # filter follows the working month
    at.selectbox(key="tx_f_account").select(WISE).run()
    assert not at.exception, at.exception
    table = df_with(at, "Source ref")
    assert len(table) == 12
    assert set(table["Account"]) == {WISE}
    assert set(table["Date"].str[:7]) == {"2026-10"}
    sums = q(db_path, "SELECT SUM(CASE WHEN amount_cents > 0 THEN amount_cents END) AS i, "
                      "SUM(CASE WHEN amount_cents < 0 THEN amount_cents END) AS o, COUNT(*) AS n FROM transactions "
                      "WHERE substr(date,1,7) = '2026-10' AND account_id = 1")[0]
    assert sums["n"] == 12
    line = f"12 rows   in {db.cents_to_str(sums['i'])}   out {db.cents_to_str(sums['o'])}   net {db.cents_to_str(sums['i'] + sums['o'])}"
    assert line in texts(at)
    fee = table[table["Description"].str.startswith("  -> ")]
    assert len(fee) == q(db_path, "SELECT COUNT(*) AS n FROM transactions WHERE parent_id IS NOT NULL "
                                  "AND substr(date,1,7) = '2026-10'")[0]["n"] > 0  # fee rows are indented


def test_transactions_all_months_and_other_filters(db_path):
    at = run(TX, "2026-10")
    at.selectbox(key="tx_f_month").select("all").run()
    total = q(db_path, "SELECT COUNT(*) AS n FROM transactions")[0]["n"]
    assert len(df_with(at, "Source ref")) == total
    at.selectbox(key="tx_f_status").select("needs_review").run()
    assert len(df_with(at, "Source ref")) == 4
    at.selectbox(key="tx_f_status").select("all").run()
    at.text_input(key="tx_f_search").set_value("htgrowth").run()
    n = q(db_path, "SELECT COUNT(*) AS n FROM transactions WHERE description LIKE '%htgrowth%' OR payee LIKE '%htgrowth%' "
                   "OR notes LIKE '%htgrowth%'")[0]["n"]
    assert n >= 3 and len(df_with(at, "Source ref")) == n
    at.text_input(key="tx_f_search").set_value("zzz-no-such-text").run()
    assert "No transactions match." in texts(at)
    assert at.get("download_button") is not None


def test_transactions_type_and_category_filters(db_path):
    at = run(TX, "2026-10")
    at.selectbox(key="tx_f_type").select("Income").run()
    inc = q(db_path, "SELECT COUNT(*) AS n FROM transactions WHERE substr(date,1,7) = '2026-10' AND type = 'Income'")[0]["n"]
    assert len(df_with(at, "Source ref")) == inc >= 1
    at.selectbox(key="tx_f_type").select("all").run()
    at.selectbox(key="tx_f_category").select("Interest Income").run()
    n = q(db_path, "SELECT COUNT(*) AS n FROM transactions t JOIN categories c ON c.id = t.category_id "
                   "WHERE substr(t.date,1,7) = '2026-10' AND c.name = 'Interest Income'")[0]["n"]
    assert len(df_with(at, "Source ref")) == n >= 1


def test_transactions_empty_database(tmp_path, monkeypatch):
    monkeypatch.setenv("TC_DB_PATH", str(tmp_path / "empty.db"))
    at = run(TX, "2026-10")
    assert not at.exception, at.exception
    assert "No transactions yet - start on Import." in texts(at)


def test_transactions_edit_category_through_set_category(db_path):
    row = q(db_path, "SELECT t.id FROM transactions t JOIN categories c ON c.id = t.category_id "
                     "WHERE substr(t.date,1,7) = '2026-10' AND c.name = 'Interest Income' AND t.parent_id IS NULL")[0]
    at = run(TX, "2026-10")
    at.number_input(key="tx_edit_id").set_value(row["id"]).run()
    assert at.selectbox(key="tx_edit_cat").value == "Interest Income"
    at.selectbox(key="tx_edit_cat").select("Other Professional Services").run()
    assert at.selectbox(key="tx_edit_type").value == "Expense"  # type follows the category default
    at.text_input(key="tx_edit_note").set_value("moved by hand")
    at.button(key="tx_save_btn").click().run()
    assert not at.exception, at.exception
    after = q(db_path, "SELECT c.name AS cat, t.type, t.review_status, t.notes, t.amount_cents FROM transactions t "
                       "JOIN categories c ON c.id = t.category_id WHERE t.id = ?", row["id"])[0]
    assert (after["cat"], after["type"], after["review_status"]) == ("Other Professional Services", "Expense", "reviewed")
    assert "moved by hand" in after["notes"]
    shown = df_with(at, "Source ref")
    assert shown[shown["Id"] == row["id"]].iloc[0]["Status"] == "reviewed"


def test_transactions_unknown_id_and_fee_row(db_path):
    at = run(TX, "2026-10")
    at.number_input(key="tx_edit_id").set_value(999999).run()
    assert not at.exception, at.exception
    assert "No transaction with id 999999" in texts(at)
    fee = q(db_path, "SELECT id FROM transactions WHERE parent_id IS NOT NULL AND substr(date,1,7) = '2026-10' LIMIT 1")[0]["id"]
    at.number_input(key="tx_edit_id").set_value(fee).run()
    assert not at.exception, at.exception
    assert at.selectbox(key="tx_edit_cat").disabled
    assert at.button(key="tx_save_btn").disabled
    assert "follows the parent" in texts(at)


def test_transactions_locked_month_blocks_edit_and_changes_nothing(db_path):
    row = q(db_path, "SELECT id FROM transactions WHERE substr(date,1,7) = '2026-09' AND parent_id IS NULL "
                     "AND category_id IS NOT NULL LIMIT 1")[0]["id"]
    snapshot = "SELECT id, category_id, type, notes, review_status FROM transactions ORDER BY id"
    at = run(TX, "2026-09")
    at.number_input(key="tx_edit_id").set_value(row).run()
    assert not at.button(key="tx_save_btn").disabled  # open month: editable
    at.selectbox(key="tx_edit_cat").select("Other Professional Services").run()
    at.text_input(key="tx_edit_note").set_value("should not be saved")
    lock_month(db_path, "2026-09")  # the month is locked behind the open page: the save must fail cleanly
    before = q(db_path, snapshot)
    at.button(key="tx_save_btn").click().run()
    assert not at.exception, at.exception
    assert q(db_path, snapshot) == before
    assert "locked" in texts(at).lower()
    # a fresh page on the locked month: notice shown, controls disabled, still nothing changes
    at = run(TX, "2026-09")
    at.number_input(key="tx_edit_id").set_value(row).run()
    assert any("Month 2026-09 is locked" in w.value for w in at.warning)
    assert at.button(key="tx_save_btn").disabled
    assert at.selectbox(key="tx_edit_cat").disabled
    assert q(db_path, snapshot) == before  # a browser user cannot even click: AppTest refuses disabled widgets
    assert df_with(at, "Source ref") is not None  # filters and table stay available


def test_transactions_adjustments_add_and_list(db_path):
    at = run(TX, "2026-10")
    assert "No adjustments for this selection." in texts(at)
    assert at.button(key="adj_add_btn").disabled  # no label / reason yet
    at.text_input(key="adj_label").set_value("Test accrual")
    at.text_input(key="adj_amount").set_value("1,562.86")
    at.selectbox(key="adj_section").select("other_income")
    at.text_input(key="adj_reason").set_value("test reason").run()
    assert not at.button(key="adj_add_btn").disabled
    assert str(at.date_input(key="adj_date").value) == "2026-10-31"
    at.button(key="adj_add_btn").click().run()
    assert not at.exception, at.exception
    adj = q(db_path, "SELECT date, label, section, amount_cents, non_cash, reason FROM adjustments")
    assert adj == [{"date": "2026-10-31", "label": "Test accrual", "section": "other_income",
                    "amount_cents": 156286, "non_cash": 1, "reason": "test reason"}]
    shown = df_with(at, "Reason")
    assert list(shown["Amount"]) == ["1,562.86"] and list(shown["Non-cash"]) == ["yes"]
    assert at.text_input(key="adj_label").value == ""  # form cleared after a successful add


def test_transactions_adjustment_bad_amount_and_locked_month(db_path):
    at = run(TX, "2026-10")
    at.text_input(key="adj_label").set_value("Bad amount")
    at.text_input(key="adj_amount").set_value("abc")
    at.text_input(key="adj_reason").set_value("r").run()
    at.button(key="adj_add_btn").click().run()
    assert not at.exception, at.exception
    assert "Enter the amount as 1,234.56" in texts(at)
    assert q(db_path, "SELECT COUNT(*) AS n FROM adjustments")[0]["n"] == 0

    lock_month(db_path, "2026-09")
    at = run(TX, "2026-09")
    at.text_input(key="adj_label").set_value("Locked month")
    at.text_input(key="adj_amount").set_value("10.00")
    at.text_input(key="adj_reason").set_value("r").run()
    assert any("Month 2026-09 is locked" in w.value for w in at.warning)
    assert at.button(key="adj_add_btn").disabled
    assert q(db_path, "SELECT COUNT(*) AS n FROM adjustments")[0]["n"] == 0


def test_transactions_handoff_filters_from_reports(db_path):
    at = AppTest.from_file(TX, default_timeout=60)
    at.session_state["month"] = "2026-10"
    at.session_state["tx_filters"] = {"month": "2026-09", "account_id": 2}
    at.run()
    assert not at.exception, at.exception
    assert at.selectbox(key="tx_f_month").value == "2026-09"
    assert at.selectbox(key="tx_f_account").value == "BUS COMPLETE CHK 5200"
    n = q(db_path, "SELECT COUNT(*) AS n FROM transactions WHERE substr(date,1,7) = '2026-09' AND account_id = 2")[0]["n"]
    assert len(df_with(at, "Source ref")) == n >= 1
