"""Invoices: commission check, unpaid list, register, add / void, match payment (UI.md page 5)."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # app/  (Home.py: parents[0])

from datetime import date

import pandas as pd
import streamlit as st

import ui_common
from tc import close, db, invoices

st.set_page_config(page_title="Invoices", layout="wide")

conn = ui_common.get_conn()
month = ui_common.sidebar(conn)
money = ui_common.money
year = int(month[:4])

st.title("Invoices")
st.caption(f"Working month {month}" + (" (LOCKED)" if close.is_locked(conn, month) else ""))

RATE_CENTS = invoices.COMMISSION_RATE_CENTS
NEW_KEYS = [
    "inv_new_source", "inv_new_source_other", "inv_new_number", "inv_new_issue_date", "inv_new_due_date", "inv_new_type",
    "inv_new_month", "inv_new_boxes", "inv_new_amount", "inv_new_replaces", "inv_new_notes",
]


def locked_notice(exc: Exception) -> None:
    st.warning(f"{exc}. Nothing was changed. Re-open the month on Reports first.")


# ------------------------------------------------------------------ (A) check
st.subheader(f"Check: commission paid vs revenue, {year}")
check = invoices.commission_check(conn, year)
c1, c2, c3 = st.columns(3)
c1.metric("Commission invoices paid (gross)", money(check["invoice_paid_cents"]))
c2.metric("Service Revenue in ledger", money(check["revenue_cents"]))
c3.metric("Difference", money(check["diff_cents"]))
if check["diff_cents"] == 0 and not check["unbacked"]:
    st.success("OK: revenue is fully backed by commission invoices.")
else:
    st.error("Revenue is not fully backed by commission invoices")
if check["unbacked"]:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Date": u["date"],
                    "Amount": money(u["amount_cents"]),
                    "Backed by commission invoices": money(u["commission_backed_cents"]),
                }
                for u in check["unbacked"]
            ]
        ),
        hide_index=True,
        use_container_width=True,
        key="inv_unbacked",
    )
missing = invoices.missing_commission_months(conn, through_month=month)
if missing:
    st.warning(
        f"Commission invoice missing more than {invoices.MISSING_COMMISSION_GRACE_DAYS} days after month-end: "
        + ", ".join(missing)
    )
else:
    st.caption(f"Commission invoice missing more than {invoices.MISSING_COMMISSION_GRACE_DAYS} days after month-end: none")

# ------------------------------------------------------------------ (B) unpaid
st.subheader("Unpaid")
pending = invoices.unpaid(conn)
if pending:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Invoice": u["label"],
                    "Type": u["type"],
                    "Issued": u["issue_date"],
                    "Due": u["due_date"] or "",
                    "Amount": money(u["amount_cents"]),
                    "Days open": u["days_open"],
                }
                for u in pending
            ]
        ),
        hide_index=True,
        use_container_width=True,
        key="inv_unpaid",
    )
    comm = sum(u["amount_cents"] for u in pending if u["type"] == "Commission")
    reimb = sum(u["amount_cents"] for u in pending if u["type"] == "Reimbursable")
    st.write(f"Total unpaid: commission {money(comm)}   reimbursable {money(reimb)}")
else:
    st.write("Nothing unpaid.")

# ------------------------------------------------------------------ (C) register
st.subheader("Register")
f1, f2 = st.columns(2)
status_f = f1.selectbox("Status", ["all", "Pending", "Paid", "Voided"], key="inv_filter_status")
type_f = f2.selectbox("Type", ["all", "Commission", "Reimbursable", "Voided"], key="inv_filter_type")
register = invoices.list_invoices(
    conn,
    status=None if status_f == "all" else status_f,
    type=None if type_f == "all" else type_f,
)
if register:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Invoice": r["label"],
                    "Issued": r["issue_date"],
                    "Due": r["due_date"] or "",
                    "Comm. month": r["commission_month"] or "",
                    "Type": r["type"],
                    "Boxes": f"{r['boxes']:,}" if r["boxes"] else "",
                    "Rate": money(r["rate_cents"]) if r["rate_cents"] else "",
                    "Amount": money(r["amount_cents"]),
                    "Status": r["status"],
                    "Paid on": r["paid_on"] or "",
                    "Paid by transaction": r["paid_by_transaction_id"] or "",
                    "Replaces": r["replaces_label"] or "",
                    "Notes": r["notes"] or "",
                }
                for r in register
            ]
        ),
        hide_index=True,
        use_container_width=True,
        key="inv_register",
    )
else:
    st.write("No invoices yet." if not invoices.list_invoices(conn) else "No invoices match the filters.")

st.markdown("**Void invoice** (Pending invoices only)")
if pending:
    v1, v2, v3 = st.columns([2, 3, 1])
    void_id = v1.selectbox(
        "Invoice", [u["id"] for u in pending], format_func=lambda i: next(u["label"] for u in pending if u["id"] == i),
        key="inv_void_sel",
    )
    void_reason = v2.text_input("Reason", key="inv_void_reason")
    v3.write("")
    if v3.button("Void", key="inv_void_btn", disabled=not void_reason.strip()):
        try:
            invoices.void_invoice(conn, void_id, void_reason)
        except close.LockedMonthError as exc:
            locked_notice(exc)
        except ValueError as exc:
            st.error(str(exc))
        else:
            label = next(u["label"] for u in pending if u["id"] == void_id)
            ui_common.flash(f"Voided {label}.")
            st.session_state.pop("inv_void_reason", None)
            st.rerun()
else:
    st.caption("No Pending invoice to void.")

# ------------------------------------------------------------------ (D) add invoice
st.subheader("Add invoice")
all_invoices = invoices.list_invoices(conn)
sources = []
for r in all_invoices:
    s = r["label"].rsplit(" ", 1)[0]
    if s not in sources:
        sources.append(s)
if "Wise" in sources:
    sources.remove("Wise")
sources.insert(0, "Wise")
voided = [r for r in all_invoices if r["type"] == "Voided"]

d1, d2, d3, d4 = st.columns(4)
OTHER_SOURCE = "Other..."
source_pick = d1.selectbox("Source", sources + [OTHER_SOURCE], key="inv_new_source")
source = source_pick
if source_pick == OTHER_SOURCE:  # free text: e.g. the old Firstbase series on a database that has none yet
    source = d1.text_input("Source name", key="inv_new_source_other", placeholder="Firstbase").strip()
number = d2.text_input("Number", key="inv_new_number", placeholder="INV-015")
issue = d3.date_input("Issue date", value=date.today(), key="inv_new_issue_date")
due = d4.date_input("Due date (optional)", value=None, key="inv_new_due_date")
inv_type = st.radio("Type", ["Commission", "Reimbursable"], horizontal=True, key="inv_new_type")

amount_cents = 0
boxes = 0
comm_month = ""
valid = True
problem = ""
if inv_type == "Commission":
    m1, m2 = st.columns(2)
    comm_month = m1.text_input("Commission month (YYYY-MM)", key="inv_new_month", placeholder="2026-10").strip()
    boxes = int(m2.number_input("Boxes", min_value=0, step=1, value=0, key="inv_new_boxes"))
    amount_cents = boxes * RATE_CENTS
    st.write(f"Rate {money(RATE_CENTS)} per box (fixed). Amount = {boxes:,} x {money(RATE_CENTS)} = **{money(amount_cents)}**")
    try:
        if not (len(comm_month) == 7 and comm_month[4] == "-" and 1 <= int(comm_month[5:]) <= 12 and int(comm_month[:4]) > 2000):
            raise ValueError
    except ValueError:
        valid, problem = False, "Enter the commission month as YYYY-MM."
    if valid and boxes <= 0:
        valid, problem = False, "Boxes must be more than 0."
else:
    raw = st.text_input("Amount", key="inv_new_amount", placeholder="5,645.14")
    try:
        amount_cents = db.to_cents(raw) if raw.strip() else 0
    except ValueError:
        amount_cents = 0
        problem = f"Amount {raw!r} is not a number."
    if amount_cents <= 0:
        valid = False
        problem = problem or "Enter an amount above 0."

replaces = st.selectbox(
    "Replaces voided invoice",
    [None] + [r["id"] for r in voided],
    format_func=lambda i: "none" if i is None else next(r["label"] for r in voided if r["id"] == i),
    key="inv_new_replaces",
)
notes = st.text_input("Notes", key="inv_new_notes")
if not source:
    valid = False
    problem = problem or "Type the source name."
if not number.strip():
    valid = False
    problem = problem or "Enter the invoice number."
if issue is None:
    valid = False
    problem = problem or "Enter the issue date."
if problem and not valid:
    st.caption(problem)

if st.button("Add invoice", key="inv_add_btn", disabled=not valid):
    try:
        invoices.add_invoice(
            conn,
            number=number,
            source_label=source,
            issue_date=issue.isoformat(),
            type=inv_type,
            amount_cents=amount_cents,
            boxes=boxes if inv_type == "Commission" else 0,
            rate_cents=RATE_CENTS if inv_type == "Commission" else 0,
            commission_month=comm_month if inv_type == "Commission" else None,
            due_date=due.isoformat() if due else None,
            replaces_invoice_id=replaces,
            notes=notes.strip() or None,
        )
    except close.LockedMonthError as exc:
        locked_notice(exc)
    except ValueError as exc:
        st.error(str(exc))
    else:
        ui_common.flash(f"Added {source} {number.strip()} ({inv_type}, {money(amount_cents)}).")
        for k in NEW_KEYS:
            st.session_state.pop(k, None)
        st.rerun()

# ------------------------------------------------------------------ (E) match payment
st.subheader("Match payment")
receipts = invoices.unmatched_receipts(conn)
by_id = {r["id"]: r for r in receipts}
pre = st.session_state.get("match_tx_id")
if pre is not None:
    if pre in by_id:
        st.session_state.setdefault("match_receipt", pre)
        if st.session_state.get("match_receipt") not in by_id:
            st.session_state["match_receipt"] = pre
    else:
        st.info("The receipt chosen on Review queue is not available here (already matched, or its month is locked).")
        st.session_state.pop("match_tx_id", None)

if not receipts:
    st.write("No WA receipts waiting for an invoice.")
elif not pending:
    st.write("No Pending invoices to match.")
else:
    rid = st.selectbox(
        "Receipt",
        list(by_id),
        format_func=lambda i: (
            f"{by_id[i]['date']}  {by_id[i]['account']}  {by_id[i]['description']}  {money(by_id[i]['amount_cents'])}"
        ),
        key="match_receipt",
    )
    receipt = by_id[rid]
    st.write("Invoices paid by this receipt (gross amounts)")
    ticked = []
    cols = st.columns(3)
    for n, u in enumerate(pending):
        if cols[n % 3].checkbox(f"{u['label']}  {money(u['amount_cents'])}  ({u['type']})", key=f"match_inv_{u['id']}"):
            ticked.append(u)
    allocated = sum(u["amount_cents"] for u in ticked)
    left = receipt["amount_cents"] - allocated
    st.write(f"Receipt {money(receipt['amount_cents'])}   allocated {money(allocated)}   left {money(left)}")

    types = {u["type"] for u in ticked}
    mixed = "Commission" in types and len(types) > 1
    ready = bool(ticked) and left == 0
    if mixed:
        st.info("This wire mixes Commission and Reimbursable invoices. It is split into one row per type, then matched.")
    label = "Split receipt and match" if mixed else "Match"
    if st.button(label, key="match_btn", disabled=not ready):
        try:
            if mixed:
                groups = [
                    [(u["id"], u["amount_cents"]) for u in ticked if u["type"] == "Commission"],
                    [(u["id"], u["amount_cents"]) for u in ticked if u["type"] != "Commission"],
                ]
                invoices.split_receipt(conn, rid, groups)
            else:
                invoices.match_payment(conn, rid, [(u["id"], u["amount_cents"]) for u in ticked])
        except close.LockedMonthError as exc:
            locked_notice(exc)
        except ValueError as exc:
            st.error(str(exc))
        else:
            ui_common.flash(
                f"{'Split and matched' if mixed else 'Matched'} receipt {money(receipt['amount_cents'])} to "
                + ", ".join(u["label"] for u in ticked)
                + "."
            )
            st.session_state.pop("match_tx_id", None)
            for u in ticked:
                st.session_state.pop(f"match_inv_{u['id']}", None)
            st.session_state.pop("match_receipt", None)
            st.rerun()
