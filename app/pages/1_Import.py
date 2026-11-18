"""Import: upload statements, preview, confirm, import, result report (UI.md page 1)."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # app/  (Home.py: parents[0])

import tempfile
from datetime import date

import pandas as pd
import streamlit as st

import import_helpers as ih
import ui_common
from tc import close, pipeline

st.set_page_config(page_title="Import", layout="wide")

conn = ui_common.get_conn()
month = ui_common.sidebar(conn)
money = ui_common.money

st.title("Import")
st.caption(f"Working month {month}" + (" (LOCKED)" if close.is_locked(conn, month) else " (OPEN)"))

ss = st.session_state
ss.setdefault("import_dir", "inbox/")
ss.setdefault("import_parsed", [])
ss.setdefault("import_result", [])


def _load(items: list[dict], source: str, sig=None) -> None:
    """Replace the files awaiting confirmation; drop stale widget state and the previous result."""
    for k in [k for k in ss if str(k).startswith("import_wise_")]:
        del ss[k]
    ss["import_parsed"] = items
    ss["import_source"] = source
    ss["import_sig"] = sig
    ss["import_result"] = []


# ------------------------------------------------------------------ (A) files
st.subheader("Files")
uploads = st.file_uploader(
    "Wise CSV, Chase *5200 PDF or CSV, card *2110 PDF (several at once)",
    type=["csv", "pdf"], accept_multiple_files=True, key="import_files",
)
if uploads:
    sig = tuple((u.name, u.size) for u in uploads)
    if sig != ss.get("import_sig"):
        tmp = tempfile.mkdtemp(prefix="tc_import_")
        entries = [(u.name, ih.save_upload(u.name, u.getvalue(), tmp)) for u in uploads]
        _load(ih.preview_files(conn, entries), "upload", sig)
elif ss.get("import_source") == "upload" and ss.get("import_sig") is not None:
    _load([], "upload", None)  # the files were removed from the uploader

c1, c2 = st.columns([4, 1], vertical_alignment="bottom")
folder = c1.text_input("or folder", key="import_dir")
if c2.button("Load folder", key="import_load_dir_btn"):
    found = ih.list_folder(folder)
    if found:
        _load(ih.preview_files(conn, [(f.name, f) for f in found]), "folder")
    else:
        _load([], "folder")
        st.warning(f"No .csv or .pdf files in {folder}")

items: list[dict] = ss["import_parsed"]
results: list[dict] = ss["import_result"]

if not items and not results:
    st.info("Upload the Wise CSV and the two Chase statements for the month.")

# ------------------------------------------------------------------ (B) preview
wise_plan: dict[str, tuple[str, str]] = {}  # path -> (checkbox key, date key)
if items:
    st.subheader("Preview - nothing is written yet")
    for it in items:
        if it["error"]:
            st.error(it["error"])
    st.dataframe(pd.DataFrame([ih.summary_row(i) for i in items]), hide_index=True, width="stretch",
                 key="import_preview")
    for it in items:
        if it["parsed"] is None:
            continue
        if it["locked_months"]:
            st.warning(
                f"{it['filename']}: {len(it['locked_rows'])} line(s) are dated in locked month(s) "
                f"{', '.join(it['locked_months'])}. The whole file is refused; re-open the month on Reports first."
            )
            st.dataframe(pd.DataFrame(ih.lines_table(it["locked_rows"])), hide_index=True, width="stretch")
        if it["seeded_rows"]:
            st.info(f"{it['filename']}: {ih.seeded_text(it['seeded_rows'], future=True)} "
                    f"(this account is seeded through {it['seed_cutoff']}).")
        with st.expander(f"{it['filename']}: {len(it['parsed']['rows'])} lines"):
            st.dataframe(pd.DataFrame(ih.lines_table(it["parsed"]["rows"])), hide_index=True, width="stretch")

    # -------------------------------------------------------------- (C) statement balance found in file
    pdfs = [i for i in items if i["parsed"] and i["parsed"]["kind"] in ("chase5200_pdf", "card2110_pdf")]
    wises = ih.balance_candidates(items)
    csvs = [i for i in items if i["parsed"] and i["parsed"]["kind"] == "chase5200_csv"]
    if pdfs or wises or csvs:
        st.subheader("Statement balance found in file")
    for it in pdfs:
        p = it["parsed"]
        st.write(
            f"{p['account']}: {ih.signed(it, p['ending_cents'])} at {p['period_end']} "
            "(saved automatically by the importer at the statement's own closing date)."
        )
    for it in csvs:
        st.caption(f"{it['filename']}: a Chase CSV carries no printed statement balance; enter it below if you need one.")
    for n, it in enumerate(wises):
        p = it["parsed"]
        suffix = "" if n == 0 else f"_{n}"
        dkey, ckey = f"import_wise_as_of{suffix}", f"import_wise_save{suffix}"
        default_as_of = ih.wise_default_as_of(p)
        if dkey not in ss:
            ss[dkey] = date.fromisoformat(default_as_of)
        as_of = ss[dkey].isoformat() if isinstance(ss[dkey], date) else default_as_of
        state = ih.wise_balance_state(conn, it, as_of)
        if ckey not in ss:
            ss[ckey] = state["default_save"]
        if state["locked"]:
            ss[ckey] = False
        st.write(f"{p['account']} - ending balance in {it['filename']}: {money(p['ending_cents'])}")
        d1, d2 = st.columns(2)
        d1.date_input("Closing date", key=dkey)
        d2.checkbox("save as statement balance", key=ckey, disabled=state["locked"])
        st.caption("Check the export covers the whole month.")
        if state["conflict"]:
            st.warning(
                f"A statement balance of {money(state['existing_cents'])} is already saved for {as_of}; "
                f"this file says {money(p['ending_cents'])}. Nothing is overwritten unless you tick the box."
            )
        if state["locked"]:
            st.warning(f"{as_of[:7]} is locked: the balance cannot be saved.")
        if ss.get(ckey) and not state["locked"]:
            wise_plan[it["path"]] = (ckey, dkey)

    # -------------------------------------------------------------- (D) import button
    todo = [i for i in items if i["importable"]]
    if st.button(f"Import {len(todo)} file{'s' if len(todo) != 1 else ''}", key="import_btn", disabled=not todo):
        saves = {}
        for path, (ckey, dkey) in wise_plan.items():
            if ss.get(ckey) and isinstance(ss.get(dkey), date):
                saves[path] = ss[dkey].isoformat()
        out = ih.import_all(conn, todo, saves)
        # files that cannot be imported (unreadable, locked) are listed in the result as well
        skipped = [i for i in items if not i["importable"]]
        out += ih.import_all(conn, skipped)
        ss["import_result"] = out
        ss["import_parsed"] = []
        for k in [k for k in ss if str(k).startswith("import_wise_")]:
            del ss[k]
        st.rerun()
    st.caption("A backup is taken before each file is imported.")

# ------------------------------------------------------------------ (E) result
if results:
    st.subheader("Result")
    for res in results:
        getattr(st, ih.result_level(res))(ih.format_report(res))
        if res["report"]:
            for w in res["report"].get("warnings") or []:
                st.warning(w)
        if res["balance_msg"]:
            st.info(res["balance_msg"])
        if res["balance_error"]:
            st.warning(res["balance_error"])
        if res["locked_rows"]:
            st.dataframe(pd.DataFrame(ih.lines_table(res["locked_rows"])), hide_index=True, width="stretch")
    if st.button("Go to Review queue", key="import_goto_review_btn"):
        st.switch_page("pages/2_Review_queue.py")

# ------------------------------------------------------------------ manual statement balance
st.divider()
with st.expander("Enter a statement balance by hand"):
    accounts = pipeline.list_accounts(conn, tie_required_only=True)
    if not accounts:
        st.info("No account needs a statement balance.")
    else:
        by_id = {a["id"]: a for a in accounts}
        acc_id = st.selectbox("Account", list(by_id), format_func=lambda i: by_id[i]["name"], key="import_bal_account")
        defaults = {d["account_id"]: d["default_as_of"] for d in close.expected_statement_dates(conn, month)}
        default_as_of = defaults.get(acc_id, f"{month}-01")
        ctx = (acc_id, month)
        if ss.get("import_bal_ctx") != ctx:
            ss["import_bal_ctx"] = ctx
            ss["import_bal_as_of"] = default_as_of
        is_card = by_id[acc_id]["kind"] == "card"
        b1, b2 = st.columns(2)
        as_of_text = b1.text_input("Statement closing date (YYYY-MM-DD)", key="import_bal_as_of")
        amount_text = b2.text_input(
            "New Balance owed (positive)" if is_card else "Statement ending balance", key="import_bal_amount"
        )
        st.caption("Check the date printed on the statement: use its own closing date, not the month-end. "
                   + ("The card is typed as a positive liability and stored as a negative." if is_card else ""))
        if st.button("Save statement balance", key="import_bal_btn"):
            try:
                msg = ih.save_manual_balance(conn, acc_id, as_of_text, amount_text)
            except ValueError as exc:
                st.error(str(exc))
            else:
                ui_common.flash(msg)
                st.rerun()
