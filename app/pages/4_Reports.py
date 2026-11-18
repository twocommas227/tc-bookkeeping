"""Reports: P&L, statement balances and tie-out, balance sheet, owner's draws memo, month close (UI.md page 4)."""

import calendar
import pathlib
import re
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # app/

import pandas as pd
import streamlit as st

import ui_common
from tc import close, db, invoices, reports

st.set_page_config(page_title="Reports", layout="wide")

conn = ui_common.get_conn()
month = ui_common.sidebar(conn)

st.title("Reports")


# ------------------------------------------------------------------ helpers
def _short(code: str, name: str) -> str:
    """'WISE_1430' -> 'Wise *1430', 'CARD_2110' -> 'card *2110' (same wording as the checklist)."""
    m = re.match(r"^([A-Z]+)_(\d{4})$", code or "")
    if not m:
        return name
    return f"{'card' if m.group(1) == 'CARD' else m.group(1).capitalize()} *{m.group(2)}"


def _month_label(yyyy_mm: str) -> str:
    label = calendar.month_abbr[int(yyyy_mm[5:7])]
    return label if close.is_locked(conn, yyyy_mm) else f"{label} (open)"


def _last_day(yyyy_mm: str) -> date:
    return date(int(yyyy_mm[:4]), int(yyyy_mm[5:7]), calendar.monthrange(int(yyyy_mm[:4]), int(yyyy_mm[5:7]))[1])


# ------------------------------------------------------------------ year selector
this_year = date.today().year
year_options = sorted({int(month[:4]), this_year})
if st.session_state.get("_rep_month_seen") != month or st.session_state.get("rep_year") not in year_options:
    st.session_state["rep_year"] = int(month[:4])
    st.session_state["_rep_month_seen"] = month
year = st.selectbox("Year", year_options, key="rep_year")

lock = close.lock_info(conn, month)
st.caption(f"Working month {month}: " + (f"LOCKED {lock['locked_at']}" if lock else "OPEN"))
unlocked = ui_common.require_unlocked(conn, month)

# ------------------------------------------------------------------ (A) P&L
st.header("A. Profit & Loss (cash basis)")
pnl = reports.pnl_monthly(conn, year)
draws = reports.owner_draws(conn, year)
all_months = sorted(pnl[0]["months"]) if pnl else []
data_months = [
    m for m in all_months
    if any(r["months"].get(m, 0) for r in pnl) or draws.get(m, 0)
]
if not data_months:
    st.info(f"No transactions for {year}.")
    st.stop()

pnl_rows = []
for r in pnl:
    row = {"Line": r["line"]}
    for m in data_months:
        row[_month_label(m)] = ui_common.money(r["months"][m])
    row["YTD"] = ui_common.money(r["ytd"])
    pnl_rows.append(row)
st.dataframe(pd.DataFrame(pnl_rows), hide_index=True, width="stretch", key="rep_pnl")
st.caption("Months not locked are marked (open). Expenses are shown as positive numbers.")

cc = invoices.commission_check(conn, year)
st.caption(
    f"Commission invoices paid {ui_common.money(cc['invoice_paid_cents'])} vs Service Revenue "
    f"{ui_common.money(cc['revenue_cents'])} (difference {ui_common.money(cc['diff_cents'])})."
)

# ------------------------------------------------------------------ (B) statement balances and tie-out
st.header(f"B. Statement balances and tie-out - {month}")

expected = close.expected_statement_dates(conn, month)
by_label = {_short(e["code"], e["name"]): e for e in expected}
by_code = {e["code"]: e for e in expected}

if by_label:
    st.subheader("Enter / correct a statement balance")
    labels = list(by_label)
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        acct_label = st.selectbox("Account", labels, key="bal_account", disabled=not unlocked)
    acct = by_label[acct_label]
    default_key = (acct["account_id"], month)
    if st.session_state.get("_bal_defaults_for") != default_key:
        st.session_state["bal_as_of"] = date.fromisoformat(acct["default_as_of"])
        st.session_state["_bal_defaults_for"] = default_key
    with c2:
        as_of = st.date_input(
            "Statement closing date", key="bal_as_of", disabled=not unlocked,
            min_value=date(2000, 1, 1), max_value=date(2100, 12, 31),
        )
    is_card = acct["kind"] == "card"
    with c3:
        amount_text = st.text_input(
            "Ending balance" + (" (positive: amount owed)" if is_card else ""), key="bal_amount", disabled=not unlocked
        )
    with c4:
        source = st.text_input("Source", value="Manual entry", key="bal_source", disabled=not unlocked)
    st.caption("Use the date printed on the statement (not necessarily month-end). Card: type the New Balance as a positive number.")
    if as_of.isoformat()[:7] != month:
        st.warning(f"This date is in {as_of.isoformat()[:7]}, not the working month {month}.")
    if st.button("Save balance", key="bal_save", disabled=not unlocked):
        try:
            cents = db.to_cents(amount_text)
        except (ValueError, ArithmeticError):
            cents = None
            st.error("Enter the balance as 1,234.56")
        if cents is not None:
            try:
                close.set_statement_balance(
                    conn, acct["account_id"], as_of.isoformat(), -cents if is_card else cents,
                    source=source.strip() or None,
                )
            except ValueError as exc:  # includes LockedMonthError
                st.error(str(exc))
            else:
                ui_common.flash(f"Saved {acct_label} balance {ui_common.money(cents)} at {as_of.isoformat()}.")
                st.rerun()

tie = reports.tie_out(conn, month)
kind_of = {e["code"]: e["kind"] for e in expected}


def _amt(row: dict, key: str) -> str:
    if row[key] is None:
        return "pending"
    return ui_common.money(row[key], flip=kind_of.get(row["account_code"]) == "card")


saved = [t for t in tie if t["statement_cents"] is not None]
if saved:
    st.caption("Saved statement balances this month")
    st.dataframe(
        pd.DataFrame([
            {
                "Account": _short(t["account_code"], t["account_name"]),
                "Statement date": t["as_of"],
                "Balance": _amt(t, "statement_cents"),
                "Source": t["source"] or "",
            }
            for t in saved
        ]),
        hide_index=True, width="stretch", key="rep_saved_balances",
    )

tie_rows = []
for t in tie:
    pending = t["statement_cents"] is None
    if pending:
        cells = {k: "pending" for k in ("Opening", "Activity", "Closing (ledger)", "Statement", "Diff")}
    else:
        if t["diff_cents"] == 0:
            diff = ui_common.money(0)
        else:
            diff = f"{ui_common.money(t['diff_cents'], flip=kind_of.get(t['account_code']) == 'card')}  <<< OFF"
        cells = {
            "Opening": _amt(t, "opening_cents"),
            "Activity": ui_common.money(
                t["activity_cents"], flip=kind_of.get(t["account_code"]) == "card"
            ),
            "Closing (ledger)": _amt(t, "ledger_cents"),
            "Statement": _amt(t, "statement_cents"),
            "Diff": diff,
        }
    tie_rows.append({"Account": _short(t["account_code"], t["account_name"]), "As of": t["as_of"], **cells})
if tie_rows:
    st.dataframe(pd.DataFrame(tie_rows), hide_index=True, width="stretch", key="rep_tie")
else:
    st.info("No tie-required accounts.")

all_tie = bool(tie) and all(t["diff_cents"] == 0 for t in tie)
in_transit = reports.transfers_in_transit(conn, month)
st.write(f"ALL TIE: {'yes' if all_tie else 'no'}.  Own-account transfers in transit at {month}: {ui_common.money(in_transit)}")

for t in tie:
    if t["diff_cents"]:
        label = _short(t["account_code"], t["account_name"])
        is_card_row = kind_of.get(t["account_code"]) == "card"
        st.error(
            f"{label} is off by {ui_common.money(t['diff_cents'], flip=is_card_row)} - look for a missing or "
            f"duplicated line up to {t['as_of']}."
        )
        if st.button(f"Show {label} transactions", key=f"tie_goto_{t['account_code']}_{t['as_of']}"):
            st.session_state["tx_filters"] = {
                "month": month, "account_id": by_code.get(t["account_code"], {}).get("account_id"),
            }
            st.switch_page("pages/3_Transactions.py")

# ------------------------------------------------------------------ (C) balance sheet
st.header("C. Balance sheet")
if st.session_state.get("_bs_month_seen") != month or "rep_bs_as_of" not in st.session_state:
    st.session_state["rep_bs_as_of"] = _last_day(month)
    st.session_state["_bs_month_seen"] = month
bs_date = st.date_input("As of", key="rep_bs_as_of", min_value=date(2000, 1, 1), max_value=date(2100, 12, 31))
bs = reports.balance_sheet(conn, bs_date.isoformat())
st.dataframe(
    pd.DataFrame([
        {
            "Section": b["section"].capitalize(),
            "Line": b["line"],
            "Amount": ui_common.money(b["amount_cents"]),
        }
        for b in bs
    ]),
    hide_index=True, width="stretch", key="rep_bs",
)
_base_line = re.compile(r"(\*\d{4}$)|^(Total |TOTAL |EQUITY)|Reimbursable Expenses Recoverable|Payable to WA|Wise jars")
if not any(not _base_line.search(b["line"]) for b in bs):
    st.caption("Opening balance-sheet items not loaded")

# ------------------------------------------------------------------ (D) owner's draws memo
st.header("D. Memo - owner's draws (not in P&L)")
draw_row = {"Line": "Owner's draws"}
for m in data_months:
    draw_row[_month_label(m)] = ui_common.money(draws.get(m, 0))
draw_row["YTD"] = ui_common.money(draws["ytd"])
st.dataframe(pd.DataFrame([draw_row]), hide_index=True, width="stretch", key="rep_draws")

# ------------------------------------------------------------------ (E) close month
st.header(f"E. Close month {month}")
items = close.checklist(conn, month)


def _status(it: dict) -> str:
    if it["ok"]:
        return "[ok]"
    return "[!!]" if it["blocking"] else "[!]"


def _page_name(page: str) -> str:
    stem = pathlib.PurePosixPath(page).stem
    return stem.split("_", 1)[-1].replace("_", " ") if page else ""


st.dataframe(
    pd.DataFrame([
        {
            "Step": it["step"], "Check": it["label"], "Status": _status(it), "Detail": it["detail"],
            "Fix on": "" if it["ok"] else _page_name(it["page"]),
        }
        for it in items
    ]),
    hide_index=True, width="stretch", key="rep_checklist",
)
st.caption("[ok] done   [!!] blocks closing the month   [!] warning only")
if month == "2026-10":
    st.info(
        "October can only be locked for good after the 28 Nov card statement is imported: the card *2110 cycle closes "
        "on the 28th, so its 29-31 Oct lines arrive on that statement. The 29-30 Sep lines on the 29 Sep-28 Oct "
        "statement belong to September, so keep September open until that statement is in."
    )

if lock:
    st.write(f'LOCKED {lock["locked_at"]} "{lock["reason"] or ""}"')
    later = close.locked_months_after(conn, month)
    if later:
        st.warning(
            f"Later months are still locked: {', '.join(later)}. Re-opening {month} leaves them locked; they were "
            f"closed with the current {month} numbers, so close {month} again before changing anything in it."
        )
    reopen_reason = st.text_input("Re-open reason", key="reopen_reason")
    if st.button(f"Re-open month {month}", key="reopen_btn"):
        if not reopen_reason.strip():
            st.error("Enter a reason to re-open the month.")
        else:
            try:
                close.reopen_month(conn, month, reopen_reason)
            except ValueError as exc:
                st.error(str(exc))
            else:
                ui_common.flash(
                    f"Month {month} re-opened."
                    + (f" Later months still locked: {', '.join(later)}." if later else "")
                )
                st.rerun()
else:
    for it in items:
        if not it["ok"] and it["blocking"] and not it["page"].endswith("4_Reports.py"):
            if st.button(f"Fix: {it['label']} ({_page_name(it['page'])})", key=f"fix_{it['key']}"):
                st.switch_page(it["page"])

    tail_ok = next((it["ok"] for it in items if it["key"] == "tail_covered"), True)
    ack = True
    if not tail_ok:
        ack = st.checkbox(
            "I have imported the statement that covers the last days of the month, or there are none",
            key="close_tail_ack",
        )
    note = st.text_input("Close note (required)", key="close_note")

    def _refusal(failed: list) -> None:
        lines = "\n".join(f"- step {i['step']} {i['label']}: {i['detail']}" for i in failed)
        st.error(f"Cannot close {month}:\n\n{lines}")

    if st.button(f"Close month {month}", key="close_btn"):
        blocking_failed = [i for i in items if not i["ok"] and i["blocking"]]
        if not note.strip() and blocking_failed:
            _refusal(blocking_failed)
        elif not note.strip():
            st.error("Enter a close note.")
        else:
            try:
                close.close_month(conn, month, note, acknowledge_tail=ack)
            except close.CloseBlocked as exc:
                _refusal(exc.items) if exc.items else st.error(str(exc))
            except ValueError as exc:  # includes LockedMonthError
                st.error(str(exc))
            else:
                ui_common.flash(f"Month {month} closed and locked.")
                st.rerun()

# ------------------------------------------------------------------ (F) export for the accountant
st.header(f"F. Excel export {year}")
import tempfile  # noqa: E402

from tc import export_xlsx  # noqa: E402

with tempfile.TemporaryDirectory() as _tmp:
    _xlsx = pathlib.Path(_tmp) / f"Two_Commas_Accounting_{year}.xlsx"
    export_xlsx.export_workbook(conn, _xlsx, year, as_of=bs_date.isoformat() if bs_date.year == year else None)
    _xlsx_bytes = _xlsx.read_bytes()
st.download_button(
    f"Download Excel {year}", data=_xlsx_bytes, file_name=f"Two_Commas_Accounting_{year}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="rep_xlsx_btn",
)
st.caption("Tabs: Transactions, Invoice Register, Bank Tie-out, Profit & Loss, Balance Sheet (as of the date in C "
           "when it falls in this year). Values only, no formulas.")
