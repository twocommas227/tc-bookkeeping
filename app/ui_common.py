"""Shared Streamlit helpers (UI.md sections 0, 4.1, 4.2, 4.4). Pages contain no SQL; they call these helpers.

Every page starts with::

    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # app/  (Home.py: parents[0])
    import ui_common
    conn = ui_common.get_conn()
    month = ui_common.sidebar(conn)
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date
from pathlib import Path

import streamlit as st

from tc import close, db

DB_ENV = "TC_DB_PATH"
_FLASH_KINDS = ("success", "error", "warning", "info")


@st.cache_resource(show_spinner=False)
def _open(path: str) -> sqlite3.Connection:
    """One cached connection per database path (cache key = path, so changing TC_DB_PATH reopens)."""
    conn = db.connect(path, check_same_thread=False)
    has_tables = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone()
    if not has_tables:
        db.init_db(conn)
    return conn


def get_conn() -> sqlite3.Connection:
    """Cached connection to env TC_DB_PATH (default data/tc.db); an empty database is initialised."""
    return _open(os.environ.get(DB_ENV) or str(db.DEFAULT_DB_PATH))


def money(cents: int | None, *, flip: bool = False) -> str:
    """Format integer cents as '1,456.41' ('-435.77' for negatives); None -> 'pending'.

    flip=True negates first (card *2110 is stored as a negative asset, shown as a positive liability).
    """
    if cents is None:
        return "pending"
    return db.cents_to_str(-cents if flip else cents)


def flash(msg: str, kind: str = "success") -> None:
    """Queue a one-shot message (kind: success / error / warning / info) shown by the next sidebar() call.

    Call it before st.rerun() after a write. Session key 'flash' = (kind, text).
    """
    st.session_state["flash"] = (kind if kind in _FLASH_KINDS else "info", msg)


def require_unlocked(conn: sqlite3.Connection, month: str) -> bool:
    """True when `month` is open. When locked, shows an st.warning and returns False.

    Pages use the result to render write controls with disabled=True.
    """
    if close.is_locked(conn, month):
        st.warning(f"Month {month} is locked. Re-open it on Reports to change anything.")
        return False
    return True


def _month_options(conn: sqlite3.Connection) -> list[str]:
    months = {r[0] for r in conn.execute("SELECT DISTINCT substr(date, 1, 7) FROM transactions")}
    months.add(date.today().isoformat()[:7])
    return sorted(months)


def _default_month(conn: sqlite3.Connection, options: list[str]) -> str:
    """Earliest month with data that is not locked; the last option when all are locked."""
    locked = {r[0] for r in conn.execute("SELECT yyyy_mm FROM month_locks")}
    for m in options:
        if m not in locked:
            return m
    return options[-1]


def sidebar(conn: sqlite3.Connection) -> str:
    """Draw the working-month selectbox, the lock badge and any queued flash message; return 'YYYY-MM'.

    The selectbox is keyed 'month' (shared by all pages). Options = months that have transactions plus the
    current month. Default = earliest option not in month_locks. A one-shot 'flash' message is shown, then cleared.
    """
    options = _month_options(conn)
    if st.session_state.get("month") not in options:
        st.session_state["month"] = _default_month(conn, options)
    with st.sidebar:
        month = st.selectbox("Working month", options, key="month")
        info = close.lock_info(conn, month)
        if info:
            st.error(f"LOCKED {info['locked_at']}")
        else:
            st.success("OPEN")
    msg = st.session_state.pop("flash", None)
    if msg:
        kind, text = msg
        getattr(st, kind if kind in _FLASH_KINDS else "info")(text)
    return month
