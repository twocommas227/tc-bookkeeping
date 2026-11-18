import os
import sys
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from tc import close, db, fixtures, invoices

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
import ui_common  # noqa: E402


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
    conn.commit()
    conn.close()
    monkeypatch.setenv("TC_DB_PATH", str(path))
    monkeypatch.setenv("TC_BACKUP_DIR", str(tmp_path / "backups"))
    return path


def test_money():
    assert ui_common.money(145641) == "1,456.41"
    assert ui_common.money(-43577) == "-435.77"
    assert ui_common.money(None) == "pending"
    assert ui_common.money(None, flip=True) == "pending"
    assert ui_common.money(-145641, flip=True) == "1,456.41"
    assert ui_common.money(0, flip=True) == "0.00"


def test_connect_check_same_thread_flag():
    conn = db.connect(":memory:", check_same_thread=False)
    conn.close()


def test_get_conn_initialises_empty_db(tmp_path, monkeypatch):
    path = tmp_path / "sub" / "new.db"
    monkeypatch.setenv("TC_DB_PATH", str(path))
    conn = ui_common.get_conn()
    assert path.exists()
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] > 0
    assert ui_common.get_conn() is conn  # cached


def test_get_conn_opens_existing_db_and_follows_env(golden_path, tmp_path, monkeypatch):
    conn = ui_common.get_conn()
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] > 0
    monkeypatch.setenv("TC_DB_PATH", str(tmp_path / "other.db"))
    other = ui_common.get_conn()
    assert other is not conn
    assert other.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def _home(month=None):
    at = AppTest.from_file(str(APP / "Home.py"), default_timeout=30)
    if month:
        at.session_state["month"] = month
    return at.run()


def test_home_checklist_golden(golden_path):
    at = _home("2026-10")
    assert not at.exception
    sel = at.sidebar.selectbox(key="month")
    assert sel.value == "2026-10"
    assert "2026-01" in sel.options and "2026-10" in sel.options
    assert [s.value for s in at.sidebar.success] == ["OPEN"]
    df = at.dataframe[0].value
    assert list(df.columns) == ["Step", "Check", "Status", "Detail", "Fix on"]
    assert len(df) == 10
    by_check = {r["Check"]: r for _, r in df.iterrows()}
    assert by_check["Auto-categorise"]["Status"] == "\u2713 Done"
    assert by_check["Enter statement ending balances"]["Status"] == "\u25cf Blocks"
    assert by_check["Enter statement ending balances"]["Fix on"] == "Reports"
    text = " ".join(m.value for m in at.markdown)
    assert "Unpaid WA invoices: 1" in text and "Wise INV-010 5,645.14" in text


def test_home_default_month_is_earliest_open(golden_path):
    at = _home()
    assert not at.exception
    assert at.sidebar.selectbox(key="month").value == "2026-01"


def test_home_locked_month_shows_badge(golden_path):
    conn = db.connect(golden_path)
    try:
        invoices.backfill_payments(conn)  # Service Revenue must be backed by invoices before months can close
        conn.commit()
        for m in range(1, 10):  # months close oldest first
            close.close_month(conn, f"2026-{m:02d}", "test lock", acknowledge_tail=True)
    finally:
        conn.close()
    at = _home("2026-09")
    assert not at.exception
    badges = [e.value for e in at.sidebar.error]
    assert len(badges) == 1 and badges[0].startswith("LOCKED ")
    assert not at.sidebar.success
    at = _home("2026-10")
    assert [s.value for s in at.sidebar.success] == ["OPEN"]


def test_default_month_is_earliest_with_data(tmp_path, monkeypatch):
    path = tmp_path / "tc.db"
    conn = db.connect(path)
    db.init_db(conn)
    cid = conn.execute("SELECT id FROM accounts WHERE tie_required = 1 ORDER BY id").fetchone()[0]
    db.insert_transaction(conn, date="2026-03-05", account_id=cid, description="x", amount_cents=100, source_ref="a")
    db.insert_transaction(conn, date="2026-04-05", account_id=cid, description="y", amount_cents=100, source_ref="b")
    conn.commit()
    conn.close()
    monkeypatch.setenv("TC_DB_PATH", str(path))
    assert _home().sidebar.selectbox(key="month").value == "2026-03"


def test_flash_shown_once(golden_path):
    at = AppTest.from_file(str(APP / "Home.py"), default_timeout=30)
    at.session_state["flash"] = ("success", "Saved it")
    at.run()
    assert "Saved it" in [s.value for s in at.success]
    assert "flash" not in at.session_state
    at.run()
    assert "Saved it" not in [s.value for s in at.success]
