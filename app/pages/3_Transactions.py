"""Transactions: filter, search, edit one row's category, adjustments, CSV download (UI.md page 3). No SQL here."""

import calendar
import csv
import io
import pathlib
import re
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # app/

import pandas as pd
import streamlit as st

import ui_common
from tc import close, db, pipeline
from tc.pipeline_ui import ADJUSTMENT_SECTIONS, REVIEW_STATUSES, TYPES

st.set_page_config(page_title="Transactions", layout="wide")

conn = ui_common.get_conn()
month = ui_common.sidebar(conn)

st.title("Transactions")

_LEADING_TAGS = re.compile(r"^\s*(?:\[(?:rule|review):[^\]]*\]\s*)+")
ALL = "all"


def _free_text(notes: str | None) -> str:
    return _LEADING_TAGS.sub("", notes or "").strip()


def _plain(cents: int) -> str:
    """CSV amount: '-1234.56', no thousands separator."""
    return f"{'-' if cents < 0 else ''}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def _last_day(yyyy_mm: str) -> date:
    y, m = int(yyyy_mm[:4]), int(yyyy_mm[5:7])
    return date(y, m, calendar.monthrange(y, m)[1])


all_rows = pipeline.search_transactions(conn)
if not all_rows:
    st.info("No transactions yet - start on Import.")
    st.stop()

accounts = pipeline.list_accounts(conn)
categories = pipeline.list_categories(conn)
acct_by_label = {a["source_label"]: a for a in accounts}
acct_label_by_id = {a["id"]: a["source_label"] for a in accounts}
cat_by_name = {c["name"]: c for c in categories}
cat_name_by_id = {c["id"]: c["name"] for c in categories}
by_id = {r["id"]: r for r in all_rows}
month_options = [ALL] + sorted({r["date"][:7] for r in all_rows} | {month})

# ------------------------------------------------------------------ filters
# Reports' "Show ... transactions" button hands over st.session_state["tx_filters"]; it is consumed here once.
handoff = st.session_state.pop("tx_filters", None)
if handoff:
    st.session_state["tx_f_month"] = handoff.get("month") or ALL
    st.session_state["tx_f_account"] = acct_label_by_id.get(handoff.get("account_id"), ALL)
    st.session_state["tx_f_category"] = cat_name_by_id.get(handoff.get("category_id"), ALL)
    st.session_state["tx_f_type"] = handoff.get("type") or ALL
    st.session_state["tx_f_status"] = handoff.get("review_status") or ALL
    st.session_state["tx_f_search"] = handoff.get("search") or ""
    st.session_state["_tx_month_seen"] = month
elif "tx_f_month" not in st.session_state or st.session_state.get("_tx_month_seen") != month:
    st.session_state["tx_f_month"] = month  # filter follows the working month
    st.session_state["_tx_month_seen"] = month
if st.session_state["tx_f_month"] not in month_options:
    st.session_state["tx_f_month"] = month

c1, c2, c3, c4, c5, c6 = st.columns(6)
with c1:
    f_month = st.selectbox("Month", month_options, key="tx_f_month")
with c2:
    f_account = st.selectbox("Account", [ALL, *acct_by_label], key="tx_f_account")
with c3:
    f_category = st.selectbox("Category", [ALL, *cat_by_name], key="tx_f_category")
with c4:
    f_type = st.selectbox("Type", [ALL, *TYPES], key="tx_f_type")
with c5:
    f_status = st.selectbox("Status", [ALL, *REVIEW_STATUSES], key="tx_f_status")
with c6:
    f_search = st.text_input("Search", key="tx_f_search")

rows = pipeline.search_transactions(
    conn,
    month=None if f_month == ALL else f_month,
    account_id=None if f_account == ALL else acct_by_label[f_account]["id"],
    category_id=None if f_category == ALL else cat_by_name[f_category]["id"],
    type=None if f_type == ALL else f_type,
    review_status=None if f_status == ALL else f_status,
    search=f_search.strip() or None,
)

# ------------------------------------------------------------------ totals and table
money_in = sum(r["amount_cents"] for r in rows if r["amount_cents"] > 0)
money_out = sum(r["amount_cents"] for r in rows if r["amount_cents"] < 0)
st.text(
    f"{len(rows)} rows   in {ui_common.money(money_in)}   out {ui_common.money(money_out)}   "
    f"net {ui_common.money(money_in + money_out)}"
)

if rows:
    st.dataframe(
        pd.DataFrame([
            {
                "Id": r["id"],
                "Date": r["date"],
                "Account": r["account"],
                "Description": ("  -> " if r["parent_id"] is not None else "") + " ".join((r["description"] or "").split()),
                "Amount": ui_common.money(r["amount_cents"]),
                "Category": r["category"] or "",
                "Type": r["type"] or "",
                "Notes": _free_text(r["notes"]),
                "Status": r["review_status"],
                "Invoice": ", ".join(r["invoice_labels"]),
                "Source ref": r["source_ref"],
            }
            for r in rows
        ]),
        hide_index=True, width="stretch", key="tx_table",
    )
else:
    st.info("No transactions match.")

# ------------------------------------------------------------------ edit one row
st.subheader("Edit one row")
if "tx_edit_id" not in st.session_state:
    st.session_state["tx_edit_id"] = rows[0]["id"] if rows else 0
edit_id = int(st.number_input("Row id", min_value=0, step=1, key="tx_edit_id"))
target = by_id.get(edit_id)
if target is None:
    st.error(f"No transaction with id {edit_id}")
else:
    row_month = target["date"][:7]
    st.text(
        f"#{target['id']}   {target['date']}   {target['account']}   {ui_common.money(target['amount_cents'])}   "
        f"{' '.join((target['description'] or '').split())}"
    )
    unlocked = ui_common.require_unlocked(conn, row_month)
    is_fee = target["parent_id"] is not None
    invoice_backed = target["category"] == "Service Revenue" or bool(target["invoice_labels"])

    # Service Revenue is never offered; it only appears when the row already has it (control then disabled)
    edit_cats = [n for n in cat_by_name if n != "Service Revenue" or n == target["category"]]
    stamp = (edit_id, target["category_id"], target["type"], target["notes"])
    if st.session_state.get("_tx_edit_seen") != stamp or "tx_edit_cat" not in st.session_state:
        st.session_state["tx_edit_cat"] = target["category"]
        st.session_state["tx_edit_type"] = target["type"] if target["type"] in TYPES else None
        st.session_state["tx_edit_note"] = _free_text(target["notes"])
        st.session_state["_tx_edit_seen"] = stamp
        st.session_state["_tx_edit_cat_seen"] = target["category"]
    elif st.session_state.get("_tx_edit_cat_seen") != st.session_state["tx_edit_cat"]:
        chosen = st.session_state["tx_edit_cat"]  # a new category resets the type to its default
        st.session_state["tx_edit_type"] = cat_by_name[chosen]["default_type"] if chosen else None
        st.session_state["_tx_edit_cat_seen"] = chosen

    cat_locked = is_fee or invoice_backed
    e1, e2, e3 = st.columns([2, 1, 2])
    with e1:
        e_cat = st.selectbox(
            "Category", edit_cats, index=None, placeholder="choose...", key="tx_edit_cat",
            disabled=not unlocked or cat_locked,
        )
    with e2:
        e_type = st.selectbox(
            "Type", list(TYPES), index=None, placeholder="choose...", key="tx_edit_type",
            disabled=not unlocked or cat_locked,
        )
    with e3:
        e_note = st.text_input("Note", key="tx_edit_note", disabled=not unlocked or is_fee)
    if is_fee:
        st.caption("Fee row: follows the parent.")
    elif invoice_backed:
        st.caption("Set by invoice match - change it on Invoices.")

    save_disabled = not unlocked or is_fee or e_cat is None or e_type is None
    if st.button("Save", key="tx_save_btn", disabled=save_disabled):
        try:
            pipeline.set_category(conn, edit_id, cat_by_name[e_cat]["id"], e_type, notes=e_note)
        except ValueError as exc:  # LockedMonthError (a ValueError) and trigger messages: nothing was changed
            st.error(str(exc))
        else:
            ui_common.flash(f"Saved #{edit_id} as {e_cat} / {e_type}.")
            st.rerun()

    d1, d2 = st.columns([1, 3])
    with d1:
        e_date = st.date_input(
            "Date", value=date.fromisoformat(target["date"]), key=f"tx_edit_date_{edit_id}",
            disabled=not unlocked or is_fee,
        )
    with d2:
        st.caption("Change the date only to follow the workbook's date convention. Open months only; "
                   "the old date is kept in the note.")
    date_disabled = not unlocked or is_fee or e_date.isoformat() == target["date"]
    if st.button("Save date", key="tx_date_btn", disabled=date_disabled):
        try:
            pipeline.set_date(conn, edit_id, e_date.isoformat())
        except ValueError as exc:  # LockedMonthError is a ValueError: nothing was changed
            st.error(str(exc))
        else:
            ui_common.flash(f"Moved #{edit_id} to {e_date.isoformat()}.")
            st.rerun()

# ------------------------------------------------------------------ adjustments
with st.expander("Adjustments (non-bank entries)", expanded=True):
    adj_scope = None if f_month == ALL else f_month
    adjustments = pipeline.list_adjustments(conn, month=adj_scope)
    if adjustments:
        st.dataframe(
            pd.DataFrame([
                {
                    "Date": a["date"],
                    "Label": a["label"],
                    "Section": a["section"],
                    "Amount": ui_common.money(a["amount_cents"]),
                    "Non-cash": "yes" if a["non_cash"] else "no",
                    "Reason": a["reason"],
                }
                for a in adjustments
            ]),
            hide_index=True, width="stretch", key="tx_adjustments",
        )
    else:
        st.caption("No adjustments for this selection.")

    if st.session_state.pop("_adj_clear", False):
        for k, v in (("adj_label", ""), ("adj_amount", ""), ("adj_reason", ""), ("adj_noncash", True)):
            st.session_state[k] = v
    default_adj_month = month if f_month == ALL else f_month
    if st.session_state.get("_adj_date_for") != default_adj_month or "adj_date" not in st.session_state:
        st.session_state["adj_date"] = _last_day(default_adj_month)
        st.session_state["_adj_date_for"] = default_adj_month

    st.caption("Add an adjustment")
    a1, a2, a3, a4 = st.columns(4)
    with a1:
        adj_date = st.date_input("Date", key="adj_date", min_value=date(2000, 1, 1), max_value=date(2100, 12, 31))
    with a2:
        adj_label = st.text_input("Label", key="adj_label")
    with a3:
        adj_section = st.selectbox("Section", list(ADJUSTMENT_SECTIONS), key="adj_section")
    with a4:
        adj_amount = st.text_input("Amount (signed, e.g. 1,562.86)", key="adj_amount")
    a5, a6 = st.columns([1, 4])
    with a5:
        st.session_state.setdefault("adj_noncash", True)
        adj_noncash = st.checkbox("Non-cash", key="adj_noncash")
    with a6:
        adj_reason = st.text_input("Reason (required)", key="adj_reason")

    adj_open = ui_common.require_unlocked(conn, adj_date.isoformat()[:7])
    adj_disabled = not adj_open or not adj_reason.strip() or not adj_label.strip()
    if st.button("Add", key="adj_add_btn", disabled=adj_disabled):
        try:
            cents = db.to_cents(adj_amount)
        except (ValueError, TypeError, ArithmeticError):
            cents = None
            st.error("Enter the amount as 1,234.56")
        if cents is not None:
            try:
                pipeline.add_adjustment(
                    conn, date=adj_date.isoformat(), label=adj_label, section=adj_section,
                    amount_cents=cents, reason=adj_reason, non_cash=adj_noncash,
                )
            except ValueError as exc:  # includes LockedMonthError
                st.error(str(exc))
            else:
                st.session_state["_adj_clear"] = True
                ui_common.flash(f"Added adjustment '{adj_label.strip()}' {ui_common.money(cents)} on {adj_date.isoformat()}.")
                st.rerun()

# ------------------------------------------------------------------ download
buf = io.StringIO()
writer = csv.writer(buf)
writer.writerow(
    ["id", "date", "account", "description", "amount", "category", "type", "notes", "status", "invoices", "source_ref"]
)
for r in rows:
    writer.writerow([
        r["id"], r["date"], r["account"], r["description"], _plain(r["amount_cents"]), r["category"] or "",
        r["type"] or "", _free_text(r["notes"]), r["review_status"], "; ".join(r["invoice_labels"]), r["source_ref"],
    ])
st.download_button(
    "Download CSV", data=buf.getvalue(), file_name=f"transactions_{'all' if f_month == ALL else f_month}.csv",
    mime="text/csv", key="tx_download_btn", disabled=not rows,
)
