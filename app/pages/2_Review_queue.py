"""Review queue: rows that need a decision (UI.md page 2, RULES.md section 3). No SQL here; tc modules only."""

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))  # app/

import pandas as pd
import streamlit as st

import ui_common
from tc import close, pipeline
from tc.pipeline_ui import TYPES

st.set_page_config(page_title="Review queue", layout="wide")

conn = ui_common.get_conn()
month = ui_common.sidebar(conn)

st.title("Review queue")

# ------------------------------------------------------------------ reason codes (RULES.md section 3)
REASON_LABELS = {
    "no_rule": "New vendor - no rule",
    "unknown_payee": "New vendor - no rule",
    "ambiguous_rules": "Two rules disagree",
    "confirm_rule": "Check this one",
    "unexpected_fee": "Unexpected fee",
    "unexpected_bank_fee": "Unexpected fee",
    "orphan_fee": "Fee waiting for its parent",
    "parent_uncategorised": "Fee waiting for its parent",
    "no_invoice_match": "WA receipt - needs an invoice",
    "invoice_amount_mismatch": "WA receipt - needs an invoice",
    "voided_invoice_referenced": "WA receipt - needs an invoice",
    "invoice_matched_by_amount": "WA receipt matched on amount only",
    "mixed_invoice_wire": "One wire pays commission and reimbursable invoices",
    "unpaired_transfer": "Transfer - other leg not found",
    "possible_duplicate": "Possible duplicate",
}
# which controls a row gets (UI.md page 2 table)
CATEGORY_REASONS = {
    "no_rule", "unknown_payee", "ambiguous_rules", "confirm_rule",
    "unexpected_fee", "unexpected_bank_fee", "unpaired_transfer",
}
CONFIRM_REASONS = {
    "confirm_rule", "unexpected_fee", "unexpected_bank_fee", "unpaired_transfer",
    "possible_duplicate", "invoice_matched_by_amount",
}
MATCH_REASONS = {
    "no_invoice_match", "invoice_amount_mismatch", "voided_invoice_referenced",
    "invoice_matched_by_amount", "mixed_invoice_wire",
}
WAIT_REASONS = {"orphan_fee", "parent_uncategorised"}
KNOWN_REASONS = CATEGORY_REASONS | CONFIRM_REASONS | MATCH_REASONS | WAIT_REASONS
RULE_PRIORITY = 50  # RULES.md 1.3 band 30-59: vendor / payee

_LEADING_TAGS = re.compile(r"^\s*(?:\[(?:rule|review):[^\]]*\]\s*)+")


def _why(reasons: list[str]) -> str:
    labels: list[str] = []
    for code in reasons:
        label = REASON_LABELS.get(code, code)
        if label not in labels:
            labels.append(label)
    return "; ".join(labels) or "Needs review"


def _free_text(notes: str | None) -> str:
    return _LEADING_TAGS.sub("", notes or "").strip()


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


_all_rows: list[dict] = []


def _ledger_rows() -> list[dict]:
    """Every transaction, fetched once per run (only needed for the rule preview)."""
    if not _all_rows:
        _all_rows.extend(pipeline.search_transactions(conn))
    return _all_rows


# ------------------------------------------------------------------ scope and header
col_scope, col_rerun = st.columns([3, 1])
with col_scope:
    scope = st.radio(
        "Show",
        ["month", "all"],
        format_func=lambda s: "this month" if s == "month" else "all open months",
        horizontal=True,
        key="rq_scope",
    )
scope_month = month if scope == "month" else None
month_open = ui_common.require_unlocked(conn, month) if scope == "month" else True

with col_rerun:
    st.write("")
    rerun_clicked = st.button("Re-run rules", key="rq_rerun_btn", disabled=not month_open)
if rerun_clicked:
    try:
        rep = pipeline.rerun_rules(conn, month=scope_month)
    except ValueError as exc:
        st.error(str(exc))
    else:
        ui_common.flash(f"Rules re-run on {_plural(rep['rows'], 'row', 'rows')}; {rep['categorized']} categorised.")
        st.rerun()

queue = pipeline.review_queue(conn, month=scope_month)
if not queue:
    st.info(f"Nothing to review for {month}." if scope == "month" else "Nothing to review in any open month.")
    st.stop()

st.subheader(f"{_plural(len(queue), 'row needs', 'rows need')} a decision")

# ------------------------------------------------------------------ overview table (fee rows under their parent)
by_id = {r["id"]: r for r in queue}
ordered: list[tuple[dict, bool]] = []
for r in queue:
    if r["parent_id"] in by_id:
        continue
    ordered.append((r, False))
    ordered.extend((c, True) for c in queue if c["parent_id"] == r["id"])

st.dataframe(
    pd.DataFrame([
        {
            "Id": r["id"],
            "Date": r["date"],
            "Account": r["account"],
            "Description": ("  -> " if child else "") + _norm(r["description"]),
            "Amount": ui_common.money(r["amount_cents"]),
            "Why it is here": _why(r["reasons"]),
            "Proposed category": r["category"] or "",
        }
        for r, child in ordered
    ]),
    hide_index=True, width="stretch", key="rq_table",
)

# ------------------------------------------------------------------ per-row decisions
categories = pipeline.list_categories(conn)
cat_by_name = {c["name"]: c for c in categories}
# Service Revenue is set only by Invoices > Match payment (ONE RULE), so it is not offered here
cat_names = [c["name"] for c in categories if c["name"] != "Service Revenue"]
_lock_cache: dict[str, bool] = {}


def _is_locked(yyyy_mm: str) -> bool:
    if yyyy_mm not in _lock_cache:
        _lock_cache[yyyy_mm] = close.is_locked(conn, yyyy_mm)
    return _lock_cache[yyyy_mm]


def _duplicate_of(r: dict) -> dict | None:
    """The existing row with the same account, date and amount (the row this one duplicates)."""
    same = pipeline.search_transactions(conn, month=r["date"][:7], account_id=r["account_id"])
    for o in same:
        if o["id"] != r["id"] and o["date"] == r["date"] and o["amount_cents"] == r["amount_cents"]:
            return o
    return None


def _rule_preview(pattern: str) -> tuple[int, list[int]]:
    """(existing rows, ids of queue rows) whose description contains `pattern` (as add_rule matches: any case)."""
    rx = re.compile(re.escape(pattern), re.IGNORECASE)
    total = sum(1 for o in _ledger_rows() if rx.search(_norm(o["description"])))
    queued = [q["id"] for q in queue if rx.search(_norm(q["description"]))]
    return total, queued


def _sync_defaults(r: dict, cat_key: str, type_key: str) -> None:
    """Initial category/type = the rule's proposal; a changed category resets the type to its default."""
    seen_key = f"_rq_seen_{r['id']}"
    if cat_key not in st.session_state:
        proposed = r["category"] if r["category"] in cat_names else None
        st.session_state[cat_key] = proposed
        st.session_state[type_key] = (
            (r["type"] if r["type"] in TYPES else cat_by_name[proposed]["default_type"]) if proposed else None
        )
        st.session_state[seen_key] = proposed
    elif st.session_state.get(seen_key) != st.session_state[cat_key]:
        chosen = st.session_state[cat_key]
        st.session_state[type_key] = cat_by_name[chosen]["default_type"] if chosen else None
        st.session_state[seen_key] = chosen


def _decide(r: dict, child: bool) -> None:
    rid = r["id"]
    locked = _is_locked(r["date"][:7])
    reasons = set(r["reasons"])
    with st.container(border=True):
        st.text(f"{'    -> ' if child else ''}#{rid}   {r['date']}   {r['account']}   {ui_common.money(r['amount_cents'])}")
        st.text(_norm(r["description"]))
        st.text(f"Why it is here: {_why(r['reasons'])}")
        if r["category"]:
            st.text(f"Proposed: {r['category']} / {r['type'] or '-'}")
        if locked:
            st.caption(f"Month {r['date'][:7]} is locked. Re-open it on Reports to change anything.")

        if r["parent_id"] is not None or (reasons and reasons <= WAIT_REASONS):
            st.caption("Follows the parent: no decision needed on this row.")
            return

        unknown = reasons - KNOWN_REASONS
        show_match = bool(reasons & MATCH_REASONS)
        show_dup = "possible_duplicate" in reasons
        show_confirm = (bool(reasons & CONFIRM_REASONS) or bool(unknown) or not reasons) and r["category_id"] is not None
        show_cat = bool(reasons & CATEGORY_REASONS) or bool(unknown) or not reasons
        if r["category_id"] is None and not show_match:
            show_cat = True

        # -------- category / type / note / rule
        if show_cat:
            cat_key, type_key, note_key = f"rq_cat_{rid}", f"rq_type_{rid}", f"rq_note_{rid}"
            _sync_defaults(r, cat_key, type_key)
            if note_key not in st.session_state:
                st.session_state[note_key] = _free_text(r["notes"])
            c1, c2, c3 = st.columns([2, 1, 2])
            with c1:
                cat = st.selectbox("Category", cat_names, index=None, placeholder="choose...", key=cat_key, disabled=locked)
            with c2:
                typ = st.selectbox("Type", list(TYPES), index=None, placeholder="choose...", key=type_key, disabled=locked)
            with c3:
                note = st.text_input("Note", key=note_key, disabled=locked)

            make_rule = st.checkbox(
                "Create rule from this: always use this category and type for the pattern below",
                key=f"rq_rule_{rid}", disabled=locked,
            )
            pattern = ""
            if make_rule:
                pattern = _norm(st.text_input(
                    "Pattern (the description contains this text, any case)",
                    value=_norm(r["description"]), key=f"rq_pattern_{rid}", disabled=locked,
                ))
                if pattern:
                    total, queued = _rule_preview(pattern)
                    st.text(
                        f"This rule would match {_plural(total, 'existing row', 'existing rows')} "
                        f"({len(queued)} in the review queue). Saved rules apply to future imports and to Re-run rules."
                    )
                else:
                    st.text("Enter a pattern to see how many rows it would match.")

            if st.button("Save", key=f"rq_save_{rid}", disabled=locked or cat is None or typ is None):
                if make_rule and not pattern:
                    st.error("Enter a pattern for the rule, or untick Create rule.")
                else:
                    try:
                        notices = pipeline.set_category(conn, rid, cat_by_name[cat]["id"], typ, notes=note)
                    except ValueError as exc:  # includes LockedMonthError and trigger messages
                        st.error(str(exc))
                    else:
                        kind, msg = "success", f"Saved #{rid} as {cat} / {typ}."
                        if notices:
                            kind, msg = "warning", f"{msg} {' '.join(notices)}"
                        if make_rule:
                            try:
                                pipeline.add_rule(
                                    conn, label=pattern, pattern=pattern, category_id=cat_by_name[cat]["id"],
                                    type=typ, priority=RULE_PRIORITY,
                                )
                            except ValueError as exc:
                                kind, msg = "warning", f"{msg} Rule not added: {exc}"
                            else:
                                others = len([q for q in _rule_preview(pattern)[1] if q != rid])
                                msg += " Rule added."
                                if others:
                                    msg += f" Use Re-run rules to apply it to the other {_plural(others, 'queued row', 'queued rows')}."
                        ui_common.flash(msg, kind)
                        st.rerun()

        # -------- confirm / duplicate / match
        if show_confirm or show_dup or show_match:
            if show_dup:
                other = _duplicate_of(r)
                if other:
                    st.text(f"Duplicates existing row #{other['id']}: {_norm(other['description'])}")
            b1, b2, b3, _ = st.columns([1, 1, 1, 2])
            if show_confirm:
                with b1:
                    if st.button("Confirm", key=f"rq_confirm_{rid}", disabled=locked):
                        try:
                            pipeline.confirm(conn, rid)
                        except ValueError as exc:
                            st.error(str(exc))
                        else:
                            ui_common.flash(f"Confirmed #{rid} as {r['category']} / {r['type']}.")
                            st.rerun()
            if show_dup:
                with b2:
                    if st.button("Mark as duplicate (deletes this row)", key=f"rq_dup_{rid}", disabled=locked):
                        try:
                            pipeline.mark_duplicate(conn, rid)
                        except ValueError as exc:
                            st.error(str(exc))
                        else:
                            ui_common.flash(f"Row #{rid} deleted as a duplicate.")
                            st.rerun()
            if show_match:
                with b3:
                    if st.button("Match to invoice", key=f"rq_match_{rid}", disabled=locked):
                        st.session_state["match_tx_id"] = rid
                        st.switch_page("pages/5_Invoices.py")


st.subheader("Decide")
for row, is_child in ordered:
    _decide(row, is_child)
