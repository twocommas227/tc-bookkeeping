"""Home: working-month selector and the monthly close checklist (UI.md section 2)."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[0]))  # app/

import pandas as pd
import streamlit as st

import ui_common
from tc import close, invoices

st.set_page_config(page_title="Two Commas bookkeeping", layout="wide")

conn = ui_common.get_conn()
month = ui_common.sidebar(conn)

st.title("Two Commas bookkeeping")
st.subheader(f"Close checklist {month}")


def _page_name(page: str) -> str:
    stem = pathlib.PurePosixPath(page).stem  # '4_Reports'
    return stem.split("_", 1)[-1].replace("_", " ") if page else ""


items = close.checklist(conn, month)
table = pd.DataFrame(
    [
        {
            "Step": it["step"],
            "Check": it["label"],
            "Status": ui_common.status_label(it),
            "Detail": it["detail"],
            "Fix on": "" if it["ok"] else _page_name(it["page"]),
        }
        for it in items
    ]
)
blocking = [it for it in items if not it["ok"] and it["blocking"]]
if blocking:
    st.write(f"{len(blocking)} step{'s' if len(blocking) != 1 else ''} still block closing {month}: "
             + ", ".join(it["label"].lower() for it in blocking) + ".")
else:
    st.write(f"Nothing blocks closing {month}.")
st.dataframe(
    table, hide_index=True, use_container_width=True, key="close_checklist",
    column_config={
        "Status": st.column_config.TextColumn("Status", width=120),
        "Fix on": st.column_config.TextColumn("Fix on", width=100),
    },
)
st.caption(ui_common.STATUS_LEGEND)

unpaid = invoices.unpaid(conn)
if unpaid:
    total = sum(u["amount_cents"] for u in unpaid)
    names = ", ".join(f"{u['label']} {ui_common.money(u['amount_cents'])}" for u in unpaid)
    st.write(f"Unpaid WA invoices: {len(unpaid)}, total {ui_common.money(total)} ({names}).")
else:
    st.write("Unpaid WA invoices: none.")
