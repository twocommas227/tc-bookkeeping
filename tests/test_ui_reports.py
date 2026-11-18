import sys
from datetime import date
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from tc import close, db, fixtures, invoices

APP = Path(__file__).resolve().parents[1] / "app"
PAGE = str(APP / "pages" / "4_Reports.py")
sys.path.insert(0, str(APP))


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


def _run(month="2026-09"):
    at = AppTest.from_file(PAGE, default_timeout=30)
    at.session_state["month"] = month
    return at.run()


_MARK = {
    "rep_pnl": "Line", "rep_tie": "Diff", "rep_bs": "Section", "rep_checklist": "Check",
}


def _df(at, key):
    """Find a table by a column it carries (AppTest does not expose st.dataframe keys)."""
    for d in at.dataframe:
        cols = list(d.value.columns)
        if key == "rep_draws" and cols[:1] == ["Line"] and len(d.value) == 1:
            return d.value
        if key == "rep_pnl" and cols[:1] == ["Line"] and len(d.value) > 1:
            return d.value
        if key not in ("rep_draws", "rep_pnl") and _MARK[key] in cols:
            return d.value
    raise AssertionError(f"no dataframe {key}")


def _tie_by_account(at):
    df = _df(at, "rep_tie")
    return {r["Account"]: r for _, r in df.iterrows()}


def test_pnl_goldens(golden_path):
    at = _run("2026-09")
    assert not at.exception
    pnl = _df(at, "rep_pnl").set_index("Line")
    assert pnl.loc["NET INCOME", "YTD"] == "18,618.29"
    assert pnl.loc["Total Revenue", "YTD"] == "32,186.00"
    assert "Oct (open)" in pnl.columns
    draws = _df(at, "rep_draws")
    assert draws.iloc[0]["YTD"] == "8,048.00"


def test_tie_out_sep_all_zero(golden_path):
    at = _run("2026-09")
    assert not at.exception
    rows = _tie_by_account(at)
    assert len(rows) == 3
    assert all(r["Diff"] == "0.00" for r in rows.values())
    assert any("ALL TIE: yes" in m.value for m in at.markdown)


def test_tie_out_oct_pending(golden_path):
    at = _run("2026-10")
    assert not at.exception
    rows = _tie_by_account(at)
    assert rows["Wise *1430"]["Diff"] == "0.00"
    assert rows["Chase *5200"]["Diff"] == "pending"
    assert rows["card *2110"]["Diff"] == "pending"
    assert any("ALL TIE: no" in m.value for m in at.markdown)


def test_close_oct_refused(golden_path):
    at = _run("2026-10")
    at.text_input(key="close_note").set_value("try it")
    at.button(key="close_btn").click().run()
    assert not at.exception
    errors = " ".join(e.value for e in at.error)
    assert "Chase *5200" in errors
    conn = db.connect(golden_path)
    try:
        assert not close.is_locked(conn, "2026-10")
    finally:
        conn.close()


def test_close_oct_refused_even_without_note(golden_path):
    at = _run("2026-10")
    at.button(key="close_btn").click().run()
    assert not at.exception
    assert "Chase *5200" in " ".join(e.value for e in at.error)


def test_close_jan_locks_and_disables_form(golden_path):
    at = _run("2026-01")
    assert not at.exception
    at.text_input(key="close_note").set_value("Jan done")
    if at.get("checkbox"):
        at.checkbox(key="close_tail_ack").check()
    at.button(key="close_btn").click().run()
    assert not at.exception, at.exception
    assert not at.error or all("LOCKED" in e.value for e in at.error)
    conn = db.connect(golden_path)
    try:
        assert close.is_locked(conn, "2026-01")
    finally:
        conn.close()
    at = _run("2026-01")
    assert not at.exception
    assert any("is locked" in w.value for w in at.warning)
    assert at.button(key="bal_save").disabled
    assert at.text_input(key="bal_amount").disabled
    assert at.button(key="reopen_btn") is not None


def test_enter_chase_balance_writes_row(golden_path):
    at = _run("2026-10")
    assert at.date_input(key="bal_as_of").value == date(2026, 10, 31)
    at.selectbox(key="bal_account").select("Chase *5200")
    at.run()
    at.text_input(key="bal_amount").set_value("1,234.56")
    at.date_input(key="bal_as_of").set_value(date(2026, 10, 30))
    at.button(key="bal_save").click().run()
    assert not at.exception
    conn = db.connect(golden_path)
    try:
        row = conn.execute(
            """SELECT sb.balance_cents FROM statement_balances sb JOIN accounts a ON a.id = sb.account_id
               WHERE a.code = 'CHASE_5200' AND sb.as_of = '2026-10-30'"""
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == 123456


def test_card_balance_stored_negative(golden_path):
    at = _run("2026-10")
    at.selectbox(key="bal_account").select("card *2110")
    at.run()
    assert at.date_input(key="bal_as_of").value == date(2026, 10, 28)
    at.text_input(key="bal_amount").set_value("100.00")
    at.button(key="bal_save").click().run()
    assert not at.exception
    conn = db.connect(golden_path)
    try:
        row = conn.execute(
            """SELECT sb.balance_cents FROM statement_balances sb JOIN accounts a ON a.id = sb.account_id
               WHERE a.code = 'CARD_2110' AND sb.as_of = '2026-10-28'"""
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == -10000


def test_bad_balance_shows_error(golden_path):
    at = _run("2026-10")
    at.text_input(key="bal_amount").set_value("abc")
    at.button(key="bal_save").click().run()
    assert not at.exception
    assert any("Enter the balance as 1,234.56" in e.value for e in at.error)


def test_oct_page_explains_the_28_nov_card_statement(golden_path):
    at = _run("2026-10")
    assert not at.exception
    assert any("28 Nov card statement" in i.value for i in at.info)
    assert not any("28 Nov card statement" in i.value for i in _run("2026-09").info)
