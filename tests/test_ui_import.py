"""Import page (UI.md page 1): page smoke tests with AppTest + the preview -> import -> report flow via
app/import_helpers.py (st.file_uploader cannot be driven by AppTest; the folder box is used for the page flow)."""

import shutil
import sys
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from tc import close, db, fixtures, invoices, rules

APP = Path(__file__).resolve().parents[1] / "app"
PAGE = str(APP / "pages" / "1_Import.py")
RAW = Path(__file__).parent / "fixtures" / "raw"
sys.path.insert(0, str(APP))
import import_helpers as ih  # noqa: E402
from test_pipeline import load_invoices  # noqa: E402

WISE = "USD account 1430"
CARD_ID = 3


@pytest.fixture(autouse=True)
def _clear_cache():
    st.cache_resource.clear()
    yield
    st.cache_resource.clear()


def _make_db(path, *, golden=False):
    conn = db.connect(path)
    db.init_db(conn)
    if golden:
        fixtures.load_golden(conn)
        invoices.backfill_payments(conn)
    else:
        rules.sync_rules_table(conn, rules.load_rules())
        load_invoices(conn)
    conn.commit()
    conn.close()


@pytest.fixture
def empty_path(tmp_path, monkeypatch):
    path = tmp_path / "tc.db"
    _make_db(path)
    monkeypatch.setenv("TC_DB_PATH", str(path))
    monkeypatch.setenv("TC_BACKUP_DIR", str(tmp_path / "backups"))
    return path


@pytest.fixture
def golden_path(tmp_path, monkeypatch):
    path = tmp_path / "tc.db"
    _make_db(path, golden=True)
    monkeypatch.setenv("TC_DB_PATH", str(path))
    monkeypatch.setenv("TC_BACKUP_DIR", str(tmp_path / "backups"))
    return path


@pytest.fixture
def seeded_path(tmp_path, monkeypatch):
    """A database made by the seed command: the 2026 workbook history, accounts.seeded_through set."""
    path = tmp_path / "tc.db"
    fixtures.seed(path)
    monkeypatch.setenv("TC_DB_PATH", str(path))
    monkeypatch.setenv("TC_BACKUP_DIR", str(tmp_path / "backups"))
    return path


@pytest.fixture
def conn(empty_path):
    c = db.connect(empty_path)
    yield c
    c.close()


def _run(month="2026-10"):
    at = AppTest.from_file(PAGE, default_timeout=60)
    at.session_state["month"] = month
    return at.run()


def _query(path, sql, params=()):
    c = db.connect(path)
    try:
        return [tuple(r) for r in c.execute(sql, params)]
    finally:
        c.close()


def _text(at):
    parts = [m.value for m in at.markdown] + [m.value for m in at.subheader] + [m.value for m in at.success] + [m.value for m in at.error]
    parts += [m.value for m in at.warning] + [m.value for m in at.info] + [m.value for m in at.caption]
    return "\n".join(parts)


# ------------------------------------------------------------------ page renders
def test_page_renders_on_empty_db(empty_path):
    at = _run()
    assert not at.exception
    assert "Upload the Wise CSV and the two Chase statements for the month." in _text(at)
    assert not [b for b in at.button if b.key == "import_btn"]


def test_page_renders_on_golden_db(golden_path):
    for month in ("2026-09", "2026-10"):
        at = _run(month)
        assert not at.exception
        assert at.button(key="import_load_dir_btn") is not None


def test_empty_folder_warns(empty_path, tmp_path):
    at = _run()
    at.text_input(key="import_dir").set_value(str(tmp_path / "nowhere"))
    at.button(key="import_load_dir_btn").click().run()
    assert not at.exception
    assert "No .csv or .pdf files in" in _text(at)


# ------------------------------------------------------------------ page flow through the folder box
def test_page_folder_import_wise_oct_twice(empty_path, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(RAW / "wise_2026-10.csv", inbox / "wise_2026-10.csv")

    at = _run()
    at.text_input(key="import_dir").set_value(str(inbox))
    at.button(key="import_load_dir_btn").click().run()
    assert not at.exception
    text = _text(at)
    assert "Preview - nothing is written yet" in text and "13,344.65" in text
    assert _query(empty_path, "SELECT COUNT(*) FROM transactions") == [(0,)]  # preview writes nothing
    at.button(key="import_btn").click().run()
    assert not at.exception
    assert "wise_2026-10.csv - 8 lines inserted, 0 duplicates skipped" in _text(at)
    assert "12 ledger rows after fee split" in _text(at)
    assert _query(empty_path, "SELECT COUNT(*) FROM transactions") == [(12,)]
    assert _query(empty_path, "SELECT balance_cents, as_of FROM statement_balances") == [(1334465, "2026-10-29")]

    at.text_input(key="import_dir").set_value(str(inbox))
    at.button(key="import_load_dir_btn").click().run()
    at.button(key="import_btn").click().run()
    assert not at.exception
    assert "0 inserted, 8 duplicates - already imported." in _text(at)
    assert _query(empty_path, "SELECT COUNT(*) FROM transactions") == [(12,)]


# ------------------------------------------------------------------ helpers: preview -> import -> report
def test_preview_wise_summary(conn):
    item = ih.preview_file(conn, RAW / "wise_2026-10.csv", "upload.csv")
    assert item["error"] is None and item["importable"]
    row = ih.summary_row(item)
    assert (row["File"], row["Detected as"], row["Account"], row["Lines"]) == ("upload.csv", "Wise CSV", WISE, 8)
    assert (row["Period"], row["Opening"], row["Ending"], row["Locked?"]) == ("2026-10-01 to 2026-10-29", "15,645.90", "13,344.65", "no")
    assert ih.lines_table(item["parsed"]["rows"])[0]["Source ref"].startswith("BALANCE_INTEREST")
    assert ih.wise_default_as_of(item["parsed"]) == "2026-10-29"
    assert ih.wise_default_as_of({"filename": "x_2026-10-01_2026-10-31.csv", "period_end": "2026-10-29"}) == "2026-10-31"
    assert db.cents_to_str(1334465) == "13,344.65"


def test_preview_card_pdf_shows_liability_positive(conn):
    item = ih.preview_file(conn, RAW / "card2110" / "2026-09-28.pdf")
    assert item["error"] is None and item["parsed"]["ending_cents"] == -145641
    row = ih.summary_row(item)
    assert (row["Detected as"], row["Ending"], row["Opening"]) == ("Card *2110 PDF", "1,456.41", "1,892.18")


def test_import_flow_wise_balance_and_second_import(conn, empty_path):
    item = ih.preview_file(conn, RAW / "wise_2026-10.csv")
    assert ih.balance_candidates([item]) == [item]
    state = ih.wise_balance_state(conn, item, "2026-10-29")
    assert state == {"locked": False, "existing_cents": None, "conflict": False, "default_save": True}

    [res] = ih.import_all(conn, [item], {item["path"]: "2026-10-29"})
    rep = res["report"]
    assert (rep["inserted"], rep["skipped_duplicate"], rep["skipped_before_opening"], rep["rejected_locked"]) == (8, 0, 0, 0)
    assert rep["ledger_rows"] == 12 and rep["review_counts"] == {}
    assert ih.format_report(res) == (
        "wise_2026-10.csv - 8 lines inserted, 0 duplicates skipped, 0 before opening date, 0 rejected (locked month). "
        "12 ledger rows after fee split. Needs review: 0."
    )
    assert ih.result_level(res) == "success"
    assert res["balance_msg"] == f"Statement balance 13,344.65 saved for {WISE} at 2026-10-29."
    assert _query(empty_path, "SELECT balance_cents FROM statement_balances") == [(1334465,)]

    again = ih.preview_file(conn, RAW / "wise_2026-10.csv")
    assert ih.wise_balance_state(conn, again, "2026-10-29")["conflict"] is False
    [res2] = ih.import_all(conn, [again])
    assert res2["report"]["inserted"] == 0 and res2["report"]["skipped_duplicate"] == 8
    assert ih.format_report(res2) == "wise_2026-10.csv - 0 inserted, 8 duplicates - already imported."
    assert _query(empty_path, "SELECT COUNT(*) FROM transactions") == [(12,)]


def test_wise_balance_conflict_is_not_ticked(conn):
    item = ih.preview_file(conn, RAW / "wise_2026-10.csv")
    ih.import_all(conn, [item])
    acct = next(a for a in ih.pipeline.list_accounts(conn) if a["source_label"] == WISE)
    close.set_statement_balance(conn, acct["id"], "2026-10-29", 1234500)
    state = ih.wise_balance_state(conn, item, "2026-10-29")
    assert state["conflict"] and state["existing_cents"] == 1234500 and not state["default_save"]


def test_invalid_file_gives_readable_error_and_writes_nothing(conn, empty_path, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("hello")
    broken = tmp_path / "other.csv"
    broken.write_text("a,b,c\n1,2,3\n")
    fake = tmp_path / "fake.pdf"
    fake.write_text("not a pdf")
    items = ih.preview_files(conn, [("notes.txt", bad), ("other.csv", broken), ("fake.pdf", fake), ("gone.csv", tmp_path / "gone.csv")])
    for it in items:
        assert it["parsed"] is None and not it["importable"]
        assert it["error"].startswith(it["filename"] + ":")
    assert "unsupported file type '.txt'" in items[0]["error"]
    before = _query(empty_path, "SELECT COUNT(*) FROM transactions")
    results = ih.import_all(conn, items)
    assert all(r["report"] is None and r["error"] for r in results)
    assert ih.format_report(results[0]).startswith("notes.txt: ")
    assert ih.result_level(results[0]) == "error"
    assert _query(empty_path, "SELECT COUNT(*) FROM transactions") == before == [(0,)]
    assert _query(empty_path, "SELECT COUNT(*) FROM statement_balances") == [(0,)]


def test_locked_month_rows_are_listed_not_imported(conn, empty_path):
    conn.execute("DROP TRIGGER trg_month_lock_requires_tie_out")  # the tie-out is not what is under test
    conn.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES ('2026-10', 'test')")
    conn.commit()
    item = ih.preview_file(conn, RAW / "wise_2026-10.csv")
    assert item["locked_months"] == ["2026-10"] and len(item["locked_rows"]) == 8 and not item["importable"]
    assert ih.summary_row(item)["Locked?"] == "yes: 2026-10"
    [res] = ih.import_all(conn, [item], {item["path"]: "2026-10-29"})
    assert res["report"] is None and len(res["locked_rows"]) == 8
    assert "not imported: 8 line(s) fall in locked month(s) 2026-10" in ih.format_report(res)
    assert ih.result_level(res) == "warning"
    assert _query(empty_path, "SELECT COUNT(*) FROM transactions") == [(0,)]
    state = ih.wise_balance_state(conn, item, "2026-10-29")
    assert state["locked"] and not state["default_save"]


def test_report_format_with_rejections_and_review_counts():
    res = {"filename": "f.csv", "error": None, "locked_rows": [], "locked_months": [],
           "report": {"inserted": 3, "skipped_duplicate": 1, "skipped_before_opening": 2, "rejected_locked": 4,
                      "ledger_rows": 5, "review_counts": {"confirm_rule": 1, "invoice_matched_by_amount": 2}}}
    assert ih.format_report(res) == (
        "f.csv - 3 lines inserted, 1 duplicates skipped, 2 before opening date, 4 rejected (locked month). "
        "5 ledger rows after fee split. Needs review: 3 (confirm_rule: 1, invoice_matched_by_amount: 2)."
    )
    assert ih.result_level(res) == "warning"


def test_statement_does_not_tie_is_an_error_not_a_crash(conn, empty_path, tmp_path):
    lines = (RAW / "wise_2026-10.csv").read_text(encoding="utf-8").splitlines()
    broken = tmp_path / "broken.csv"
    broken.write_text("\n".join(lines[:3] + lines[4:]) + "\n", encoding="utf-8")  # drop one row: running balance breaks
    item = ih.preview_file(conn, broken)
    results = ih.import_all(conn, [item])
    assert len(results) == 1
    assert results[0]["report"] is None
    assert results[0]["error"] and results[0]["error"].startswith("broken.csv")
    assert _query(empty_path, "SELECT COUNT(*) FROM transactions") == [(0,)]


# ------------------------------------------------------------------ manual statement balance
def test_manual_balance_bank(conn, empty_path):
    chase = next(a for a in ih.pipeline.list_accounts(conn, tie_required_only=True) if a["kind"] != "card" and a["source_label"] != WISE)
    msg = ih.save_manual_balance(conn, chase["id"], "2026-05-29", "1,932.58")
    assert "1,932.58" in msg
    assert _query(empty_path, "SELECT account_id, as_of, balance_cents, source FROM statement_balances") == [
        (chase["id"], "2026-05-29", 193258, ih.MANUAL_SOURCE)]


def test_manual_balance_card_is_stored_negative(conn, empty_path):
    card = next(a for a in ih.pipeline.list_accounts(conn) if a["kind"] == "card")
    msg = ih.save_manual_balance(conn, card["id"], "2026-09-28", "1,456.41")
    assert "negative" in msg
    assert _query(empty_path, "SELECT account_id, as_of, balance_cents FROM statement_balances") == [(card["id"], "2026-09-28", -145641)]


def test_manual_balance_errors_are_readable(conn, empty_path):
    acct = ih.pipeline.list_accounts(conn, tie_required_only=True)[0]["id"]
    for as_of, amount, needle in (("31/10/2026", "1.00", "not a date"), ("2026-10-31", "abc", "not an amount"),
                                  ("2026-10-31", "", "Type the statement")):
        with pytest.raises(ValueError, match=needle):
            ih.save_manual_balance(conn, acct, as_of, amount)
    assert _query(empty_path, "SELECT COUNT(*) FROM statement_balances") == [(0,)]


def test_page_manual_form_writes_one_row_card_negative(empty_path):
    at = _run()
    month = at.session_state["month"]
    card = next(a for a in ih.pipeline.list_accounts(db.connect(empty_path)) if a["kind"] == "card")
    at.selectbox(key="import_bal_account").set_value(card["id"]).run()
    assert at.text_input(key="import_bal_as_of").value == f"{month}-28"  # the card closes on the 28th
    at.text_input(key="import_bal_as_of").set_value("2026-09-28")
    at.text_input(key="import_bal_amount").set_value("1,456.41")
    at.button(key="import_bal_btn").click().run()
    assert not at.exception
    assert _query(empty_path, "SELECT account_id, as_of, balance_cents FROM statement_balances") == [(card["id"], "2026-09-28", -145641)]
    assert "Saved statement balance 1,456.41" in _text(at)


def test_page_manual_form_bad_amount_shows_error(empty_path):
    at = _run()
    at.text_input(key="import_bal_amount").set_value("lots")
    at.button(key="import_bal_btn").click().run()
    assert not at.exception
    assert any("not an amount" in e.value for e in at.error)
    assert _query(empty_path, "SELECT COUNT(*) FROM statement_balances") == [(0,)]


# ------------------------------------------------------------------ seeded 2026 history (ACCEPTANCE H2)
def _report(**kw):
    rep = {"inserted": 0, "skipped_duplicate": 0, "skipped_before_opening": 0, "skipped_seeded": 0, "rejected_locked": 0,
           "ledger_rows": 0, "review_counts": {}}
    rep.update(kw)
    return {"filename": "f.csv", "error": None, "locked_rows": [], "locked_months": [], "report": rep}


def test_report_text_for_seeded_lines():
    assert ih.format_report(_report(skipped_seeded=8)) == (
        "f.csv - 0 inserted. 8 lines already in your seeded 2026 history were skipped.")
    assert ih.format_report(_report(skipped_seeded=1)) == (
        "f.csv - 0 inserted. 1 line already in your seeded 2026 history was skipped.")
    assert ih.format_report(_report(skipped_seeded=3, skipped_duplicate=2)) == (
        "f.csv - 0 inserted, 2 duplicates - already imported. 3 lines already in your seeded 2026 history were skipped.")
    mixed = ih.format_report(_report(inserted=2, skipped_seeded=8, ledger_rows=2))
    assert mixed.startswith("f.csv - 2 lines inserted, 0 duplicates skipped") and mixed.endswith(
        "Needs review: 0. 8 lines already in your seeded 2026 history were skipped.")
    assert "seeded" not in ih.format_report(_report(inserted=2, ledger_rows=2))  # nothing seeded: the old text
    assert ih.result_level(_report(skipped_seeded=8)) == "success"
    assert ih.seeded_text(0) == "" and ih.seeded_text(5, future=True) == (
        "5 lines already in your seeded 2026 history will be skipped.")


def test_seeded_wise_file_is_previewed_and_skipped(seeded_path):
    conn = db.connect(seeded_path)
    try:
        item = ih.preview_file(conn, RAW / "wise_2026-10.csv")
        assert (item["seeded_rows"], item["seed_cutoff"], item["importable"]) == (8, "2026-10-31", True)
        before = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        [res] = ih.import_all(conn, [item])
        assert res["report"]["inserted"] == 0 and res["report"]["skipped_seeded"] == 8
        assert ih.format_report(res) == (
            "wise_2026-10.csv - 0 inserted. 8 lines already in your seeded 2026 history were skipped.")
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before == 235
    finally:
        conn.close()


def test_seeded_lines_in_a_locked_month_do_not_block_the_file(seeded_path):
    conn = db.connect(seeded_path)
    try:
        conn.execute("DROP TRIGGER trg_month_lock_requires_tie_out")  # the tie-out is not what is under test
        conn.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES ('2026-10', 'test')")
        conn.commit()
        item = ih.preview_file(conn, RAW / "wise_2026-10.csv")
        assert item["locked_months"] == [] and item["importable"] and item["seeded_rows"] == 8
        [res] = ih.import_all(conn, [item])
        assert (res["report"]["inserted"], res["report"]["skipped_seeded"], res["report"]["rejected_locked"]) == (0, 8, 0)
    finally:
        conn.close()


def test_page_shows_the_seeded_count_before_and_after_import(seeded_path, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    shutil.copy(RAW / "wise_2026-10.csv", inbox / "wise_2026-10.csv")
    at = _run()
    at.text_input(key="import_dir").set_value(str(inbox))
    at.button(key="import_load_dir_btn").click().run()
    assert not at.exception
    assert any("8 lines already in your seeded 2026 history will be skipped" in i.value for i in at.info)
    at.button(key="import_btn").click().run()
    assert not at.exception
    assert any(s.value == "wise_2026-10.csv - 0 inserted. 8 lines already in your seeded 2026 history were skipped."
               for s in at.success)
    assert _query(seeded_path, "SELECT COUNT(*) FROM transactions") == [(235,)]


def test_imported_originals_are_archived(conn, tmp_path, monkeypatch):
    """LOW L8: every imported file is kept (the page's temp copy disappears); failed files are not archived."""
    folder = tmp_path / "kept"
    monkeypatch.setenv("TC_UPLOAD_DIR", str(folder))
    item = ih.preview_file(conn, RAW / "wise_2026-10.csv", "my upload.csv")
    [res] = ih.import_all(conn, [item])
    assert res["error"] is None and res["archive_error"] is None
    kept = list(folder.glob("*_my upload.csv"))
    assert len(kept) == 1 and res["archived"] == str(kept[0])
    assert kept[0].read_bytes() == (RAW / "wise_2026-10.csv").read_bytes()
    ih.import_all(conn, [ih.preview_file(conn, RAW / "wise_2026-10.csv", "my upload.csv")])  # same name again
    assert len(list(folder.glob("*my upload.csv"))) == 2  # never overwritten
    bad = tmp_path / "junk.csv"
    bad.write_text("not,a,statement\n", encoding="utf-8")
    [res3] = ih.import_all(conn, [ih.preview_file(conn, bad)])
    assert res3["error"] and res3["archived"] is None and len(list(folder.glob("*"))) == 2
    monkeypatch.delenv("TC_UPLOAD_DIR")
    assert ih.upload_dir().is_absolute() and ih.upload_dir().name == "uploads"
