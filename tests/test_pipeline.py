"""Pipeline end to end (docs/RULES.md section 7): parse_statement, run_import, run_rules, stages 1-8.

Fresh DB -> invoices.csv via invoices.add_invoice -> raw/wise_full.csv + wise_2026-10.csv + 9 Chase *5200 PDFs
+ 8 card *2110 PDFs through run_import -> compared with tests/fixtures/transactions.csv.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from tc import db as dbmod
from tc import invoices, pipeline, rules
from tc.db import connect, init_db, insert_transaction, to_cents
from tc.fixtures import _commission_month, _split_invoice_no
from tc.importers.wise import parse_wise_csv

ROOT = Path(__file__).parent
FIX = ROOT / "fixtures"
RAW = FIX / "raw"
RULES_MD = ROOT.parent / "docs" / "RULES.md"
WISE = "USD account 1430"
CHASE = "BUS COMPLETE CHK 5200"
CARD = "J. DECKER 2110"
JARS = ("Wise USD - Rent & Utilities 0000", "Wise USD - Taxes jar")
OD = "Owner's Draw"
REIMB = "Reimbursable Expenses Recoverable"

WISE_FILES = [RAW / "wise_full.csv", RAW / "wise_2026-10.csv"]
CHASE_PDFS = sorted((RAW / "chase5200").glob("*.pdf"))
CARD_PDFS = sorted((RAW / "card2110").glob("*.pdf"))
ALL_FILES = WISE_FILES + CHASE_PDFS + CARD_PDFS

# RULES.md 7.3: no raw export exists for these key rows
GAP_START, GAP_END = "2026-09-12", "2026-09-30"          # Wise, 6 key rows
CARD_MISSING = ("2026-07-29", "2026-08-28")               # card cycle 07/29-08/28: statement not supplied

# Documented, intentional differences between the workbook and the statements. The importer tests record the
# same ones (tests/test_chase_card.py, tests/test_chase_checking.py): the workbook books the purchase date, the
# statements print the posting date. (workbook rows) -> (statement rows), all with the same amount and category.
DATE_DEVIATIONS = [
    ([("2026-08-30", CHASE, -189218, "Bank Transfer")], [("2026-08-31", CHASE, -189218, "Bank Transfer")],
     "card payment: transaction date 30 Aug in the workbook, Chase posted it 31 Aug"),
    ([("2026-01-01", CARD, -4700, "Software & Web Services")], [("2025-12-31", CARD, -4700, "Software & Web Services")],
     "MYREALPROFIT 12/31/25 booked 2026-01-01"),
    ([("2026-01-01", CARD, -14900, "Other Professional Services")], [("2026-01-02", CARD, -14900, "Other Professional Services")],
     "FIRSTBASE 149.00 statement 01/02"),
    ([("2026-01-15", CARD, -18287, "Other Professional Services")], [("2026-01-16", CARD, -18287, "Other Professional Services")],
     "HTGROWTH 182.87 statement 01/16"),
    ([("2026-02-01", CARD, -14900, "Other Professional Services")], [("2026-02-02", CARD, -14900, "Other Professional Services")],
     "FIRSTBASE 149.00 statement 02/02"),
    ([("2026-03-01", CARD, -14900, "Other Professional Services")], [("2026-03-02", CARD, -14900, "Other Professional Services")],
     "FIRSTBASE 149.00 statement 03/02"),
    ([("2026-04-10", CARD, -7756, REIMB)], [("2026-04-11", CARD, -7756, REIMB)], "PIERPASS 77.56 statement 04/11"),
    ([("2026-04-10", CARD, -2000, REIMB)], [("2026-04-11", CARD, -2000, REIMB)], "PORTCHECK 20.00 statement 04/11"),
    ([("2026-04-26", CARD, -46215, REIMB)], [("2026-04-27", CARD, -46215, REIMB)], "MAERSK 462.15 statement 04/27"),
    ([("2026-09-01", CARD, -1680, "Software & Web Services"), ("2026-09-01", CARD, -1200, "Software & Web Services")],
     [("2026-09-01", CARD, -2880, "Software & Web Services")],
     "GOOGLE 28.80 on the statement, split in the workbook into 16.80 + 12.00"),
]
# The one real disagreement on Chase/card: HTGROWTH 1,000.00 on 16 Apr is WA-reimbursable (INV-000011); the rule
# file cannot know that, so it proposes Other Professional Services and queues it (htgrowth-other, confirm_rule).
CATEGORY_NON_MATCH = (
    ("2026-04-16", CARD, -100000, REIMB), ("2026-04-16", CARD, -100000, "Other Professional Services"),
)


# ---------------------------------------------------------------------- builders
def load_invoices(conn) -> None:
    """invoices.csv through invoices.add_invoice (source labels preserved: 'Wise INV-014' vs 'Firstbase INV-000014')."""
    recs = list(csv.DictReader(open(FIX / "invoices.csv", newline="", encoding="utf-8")))
    ids: dict[tuple[str, str], int] = {}
    for r in sorted(recs, key=lambda x: x["Type"] != "Voided"):  # voided first: reissues point at them
        src, number = _split_invoice_no(r["Invoice No."])
        voided = r["Type"] == "Voided"
        typ = "Commission" if voided else r["Type"]
        kw = dict(
            number=number, source_label=src, issue_date=r["Date"], type=typ, amount_cents=to_cents(r["Gross Amount"]),
            notes=r["Notes"] or None, commission_month=_commission_month(r["Commission Month"]),
        )
        if typ == "Commission":
            kw.update(boxes=int(r["Boxes"]), rate_cents=50)
        ids[(src, number)] = invoices.add_invoice(conn, **kw)
        if voided:
            invoices.void_invoice(conn, ids[(src, number)], "voided per the Invoice Register")
    for new, old in (("INV-013", "INV-012"), ("INV-014", "INV-011")):
        conn.execute("UPDATE invoices SET replaces_invoice_id = ? WHERE id = ?", (ids[("Wise", old)], ids[("Wise", new)]))
    conn.commit()


def new_db() -> sqlite3.Connection:
    conn = connect(":memory:")
    init_db(conn)
    rules.sync_rules_table(conn, rules.load_rules())
    load_invoices(conn)
    return conn


def import_files(conn, files) -> list[dict]:
    # the fixtures have the documented 12-30 Sep Wise hole (RULES.md 7.3); allow it explicitly here
    return [pipeline.run_import(conn, f, allow_wise_gap=True) for f in files]


def clone(conn) -> sqlite3.Connection:
    dst = connect(":memory:")
    conn.backup(dst)
    return dst


def fingerprint(conn) -> dict:
    out = {}
    for table in ("transactions", "invoices", "invoice_payments", "statement_balances"):
        out[table] = [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
    return out


def ledger(conn, *accounts) -> list[dict]:
    marks = ",".join("?" for _ in accounts)
    return [dict(r) for r in conn.execute(
        "SELECT t.id, t.date, a.source_label AS source, t.description, t.amount_cents AS amount, c.name AS category,"
        " t.type, t.source_ref, t.parent_id, t.fee_cents, t.review_status, t.notes, t.payee"
        " FROM transactions t JOIN accounts a ON a.id = t.account_id LEFT JOIN categories c ON c.id = t.category_id"
        f" WHERE a.source_label IN ({marks}) ORDER BY t.date, t.id", accounts)]


def key_rows() -> list[dict]:
    with open(FIX / "transactions.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["amount"] = to_cents(r["amount"])
    return rows


def tup(r: dict) -> tuple:
    return (r["date"], r["source"], r["amount"], r["category"])


@pytest.fixture(scope="module", autouse=True)
def backup_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("backups")
    mp = pytest.MonkeyPatch()
    mp.setenv("TC_BACKUP_DIR", str(d))
    yield d
    mp.undo()


@pytest.fixture(scope="module")
def wise_db():
    conn = new_db()
    import_files(conn, WISE_FILES)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def full_db():
    conn = new_db()
    import_files(conn, ALL_FILES)
    yield conn
    conn.close()


# ---------------------------------------------------------------------- stage 0: parse_statement
def test_parse_statement_detects_every_kind(tmp_path):
    p = pipeline.parse_statement(RAW / "wise_2026-10.csv")
    assert (p["kind"], p["account"], len(p["rows"])) == ("wise_csv", WISE, 8)
    assert (p["period_start"], p["period_end"], p["opening_cents"], p["ending_cents"]) == ("2026-10-01", "2026-10-29", 1564590, 1334465)
    assert p["filename"] == "wise_2026-10.csv" and p["path"] == str(RAW / "wise_2026-10.csv")

    p = pipeline.parse_statement(RAW / "chase5200" / "2026-01.pdf", filename="upload.pdf")
    assert (p["kind"], p["account"], p["filename"]) == ("chase5200_pdf", CHASE, "upload.pdf")
    assert (p["period_start"], p["period_end"], p["opening_cents"], p["ending_cents"]) == ("2026-01-01", "2026-01-30", 252956, 219578)
    assert p["lowest_daily_balance_cents"] == 221365 and p["ink_activity_cents"] == 169386

    p = pipeline.parse_statement(RAW / "card2110" / "2026-01-28.pdf")
    assert (p["kind"], p["account"]) == ("card2110_pdf", CARD)
    assert (p["period_start"], p["period_end"], p["opening_cents"], p["ending_cents"]) == ("2025-12-29", "2026-01-28", -148992, -146787)

    csv_path = tmp_path / "Chase5200_Activity.csv"
    csv_path.write_text(
        "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
        "CREDIT,01/07/2026,REAL TIME TRANSFER RECD FROM ABA/CONTR BNK-021000021,1500.00,ACH_CREDIT,4029.56,,\n"
        "DEBIT,01/05/2026,MONTHLY SERVICE FEE,-15.00,FEE_TRANSACTION,2529.56,,\n",
        encoding="utf-8",
    )
    p = pipeline.parse_statement(csv_path)
    assert (p["kind"], p["account"], len(p["rows"])) == ("chase5200_csv", CHASE, 2)
    assert (p["period_start"], p["period_end"], p["ending_cents"]) == ("2026-01-05", "2026-01-07", 402956)


def test_parse_statement_rejects_unknown_files(tmp_path):
    for name, text in (("notes.txt", "hello"), ("other.csv", "a,b,c\n1,2,3\n"), ("fake.pdf", "not a pdf")):
        f = tmp_path / name
        f.write_text(text)
        with pytest.raises(ValueError):
            pipeline.parse_statement(f)
    with pytest.raises(ValueError):
        pipeline.parse_statement(tmp_path / "missing.csv")
    with pytest.raises(ValueError):
        pipeline.run_import(new_db(), RAW / "wise_2026-10.csv", kind="nonsense")


def test_parse_statement_has_no_db_side_effects():
    # no connection is involved at all; just prove it is repeatable and returns plain data
    a, b = pipeline.parse_statement(RAW / "wise_full.csv"), pipeline.parse_statement(RAW / "wise_full.csv")
    assert a == b and len(a["rows"]) == 392


def test_reexports_for_pages():
    for name in ("review_queue", "confirm", "mark_duplicate", "search_transactions", "set_category", "list_accounts",
                 "list_categories", "list_adjustments", "add_adjustment", "add_rule", "run_import", "run_rules",
                 "parse_statement"):
        assert callable(getattr(pipeline, name)), name
    from tc import pipeline_ui
    assert pipeline.set_category is pipeline_ui.set_category and pipeline.add_rule is rules.add_rule


# ---------------------------------------------------------------------- stages 1-2: import report
def test_import_reports(tmp_path):
    conn = new_db()
    full, octo = import_files(conn, WISE_FILES)
    assert (full["inserted"], full["skipped_duplicate"], full["skipped_before_opening"], full["rejected_locked"]) == (86, 0, 306, 0)
    assert full["ledger_rows"] == 92  # 86 bank rows + 6 jar legs; regime A fee rows are real CSV rows
    assert full["review_counts"] == {"invoice_matched_by_amount": 2, "confirm_rule": 1}
    assert (octo["inserted"], octo["skipped_duplicate"], octo["skipped_before_opening"], octo["rejected_locked"]) == (8, 0, 0, 0)
    assert octo["ledger_rows"] == 12 and octo["review_counts"] == {}  # 8 CSV rows -> 12 ledger rows after the fee split
    again = import_files(conn, WISE_FILES)
    assert [(r["inserted"], r["ledger_rows"]) for r in again] == [(0, 0), (0, 0)]
    assert (again[0]["skipped_duplicate"], again[0]["skipped_before_opening"]) == (86, 306)
    assert again[1]["skipped_duplicate"] == 8


def test_backup_is_written_before_the_import(backup_dir):
    conn = new_db()
    before = set(backup_dir.glob("tc_*_import*.db"))
    pipeline.run_import(conn, RAW / "wise_2026-10.csv")
    new = set(backup_dir.glob("tc_*_import*.db")) - before
    assert len(new) == 1
    snap = sqlite3.connect(str(next(iter(new))))
    try:
        assert snap.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0  # taken before the insert
        assert snap.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 18
    finally:
        snap.close()


def test_locked_month_rows_are_rejected():
    conn = new_db()
    conn.execute("DROP TRIGGER trg_month_lock_requires_tie_out")  # the tie-out is not what is under test
    conn.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES ('2026-10', 'test')")
    conn.commit()
    rep = pipeline.run_import(conn, RAW / "wise_2026-10.csv")
    assert (rep["inserted"], rep["rejected_locked"], rep["ledger_rows"]) == (0, 8, 0)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


def test_tie_out_mismatch_rolls_back_the_file():
    conn = new_db()
    insert_transaction(conn, date="2026-01-10", account_id=2, description="STRAY", amount_cents=100, source_ref="stray")
    conn.commit()
    with pytest.raises(ValueError, match="ledger balance"):
        pipeline.run_import(conn, CHASE_PDFS[0])
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM statement_balances").fetchone()[0] == 0
    assert not conn.in_transaction


def test_wise_running_balance_break_aborts(tmp_path):
    lines = (RAW / "wise_2026-10.csv").read_text(encoding="utf-8").splitlines()
    del lines[3]  # drop one row: the chain no longer adds up
    f = tmp_path / "truncated.csv"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    conn = new_db()
    with pytest.raises(ValueError, match="running balance"):
        pipeline.run_import(conn, f)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


# ---------------------------------------------------------------------- RULES.md 7.1-7.3: Wise vs the answer key
def canon_engine(rows):
    folded: dict[int, int] = defaultdict(int)
    for r in rows:
        if r["parent_id"] is not None and r["category"] == OD:
            folded[r["parent_id"]] += r["amount"]
    return [
        (r["date"], r["source"], r["amount"] + folded.get(r["id"], 0), r["category"])
        for r in rows if not (r["parent_id"] is not None and r["category"] == OD)
    ]


def canon_key(rows):
    fee_for = {}
    for r in rows:
        m = re.match(r"^Wise fee - (TRANSFER-\d+)$", r["description"])
        if m and r["source"] == WISE and r["category"] == OD:
            fee_for[m.group(1)] = r
    parent_fold: dict[int, int] = {}
    fee_ids = set()
    for tid, fee in fee_for.items():
        parents = [i for i, r in enumerate(rows) if re.search(rf"Wise {tid}\b", r["notes"] or "")]
        assert len(parents) == 1, tid
        parent_fold[parents[0]] = fee["amount"]
        fee_ids.add(id(fee))
    return [
        (r["date"], r["source"], r["amount"] + parent_fold.get(i, 0), r["category"])
        for i, r in enumerate(rows) if id(r) not in fee_ids
    ]


def wise_scope():
    key = key_rows()
    in_scope, excluded_gap, excluded_jar_draws = [], [], []
    for r in key:
        if r["source"] == WISE:
            (excluded_gap if GAP_START <= r["date"] <= GAP_END else in_scope).append(r)
        elif r["source"] in JARS:
            (in_scope if r["category"] == "Bank Transfer" else excluded_jar_draws).append(r)
    return in_scope, excluded_gap, excluded_jar_draws


def test_wise_rows_match_the_answer_key_100_percent(wise_db):
    in_scope, excluded_gap, excluded_jar_draws = wise_scope()
    # excluded rows are listed here so that a change in the fixtures is noticed
    assert len(excluded_gap) == 6 and sorted((r["date"], r["amount"]) for r in excluded_gap) == [
        ("2026-09-15", -611), ("2026-09-15", 55000), ("2026-09-25", -50000), ("2026-09-30", -119887),
        ("2026-09-30", -20000), ("2026-09-30", -113)]
    assert len(excluded_jar_draws) == 8 and all(r["category"] == OD for r in excluded_jar_draws)
    assert len([r for r in in_scope if r["source"] in JARS]) == 6 and len([r for r in in_scope if r["source"] == WISE]) == 97

    engine = ledger(wise_db, WISE, *JARS)
    assert len(engine) == 104 and len([r for r in engine if r["source"] == WISE]) == 98  # 7.3 expected figures
    e, k = Counter(canon_engine(engine)), Counter(canon_key(in_scope))
    assert sum(e.values()) == sum(k.values()) == 96
    assert list((e - k).elements()) == [] and list((k - e).elements()) == []


def test_oct_wise_ledger_equals_the_12_key_rows(wise_db):
    key = [r for r in key_rows() if r["source"] == WISE and r["date"] >= "2026-10-01"]
    eng = [r for r in ledger(wise_db, WISE) if r["date"] >= "2026-10-01"]
    assert len(eng) == len(key) == 12
    assert sum(r["amount"] for r in eng) == sum(r["amount"] for r in key) == -230125
    assert Counter(tup(r) for r in eng) == Counter(tup(r) for r in key)
    # the P&L of SPEC G9 from these rows: revenue 570600, bank charges 950, interest 4513
    pnl = {r["category"]: r["amount_cents"] for r in wise_db.execute(
        "SELECT category, amount_cents FROM v_pnl_monthly WHERE yyyy_mm = '2026-10'")}
    assert pnl == {"Service Revenue": 570600, "Bank Charges": -950, "Interest Income": 4513}


def test_wise_tie_out_to_the_statement_balances(wise_db):
    opening = wise_db.execute("SELECT opening_balance_cents FROM accounts WHERE source_label = ?", (WISE,)).fetchone()[0]
    assert opening == 1044457
    expect = {"2026-01-31": 1100178, "2026-02-28": 1073276, "2026-03-31": 1522865, "2026-04-30": 781632,
              "2026-05-31": 1341058, "2026-06-30": 3635952, "2026-07-31": 1793278, "2026-08-31": 1745508}
    for day, balance in expect.items():
        got = opening + wise_db.execute(
            "SELECT SUM(t.amount_cents) FROM transactions t JOIN accounts a ON a.id = t.account_id "
            "WHERE a.source_label = ? AND t.date <= ?", (WISE, day)).fetchone()[0]
        assert got == balance, day
    octo = wise_db.execute(
        "SELECT SUM(t.amount_cents) FROM transactions t JOIN accounts a ON a.id = t.account_id "
        "WHERE a.source_label = ? AND t.date >= '2026-10-01'", (WISE,)).fetchone()[0]
    assert (1564590, octo, 1564590 + octo) == (1564590, -230125, 1334465)


def test_rule4_wise_debit_split_sums_to_csv_amount(wise_db):
    csv_amount = {}
    for f in WISE_FILES:
        for r in parse_wise_csv(f):
            csv_amount[r["source_ref"]] = r["amount_cents"]
    split = 0
    for p in wise_db.execute(
        "SELECT p.id, p.source_ref, p.amount_cents, c.id AS cid, c.amount_cents AS camount, c.running_balance_cents AS crun "
        "FROM transactions p JOIN transactions c ON c.parent_id = p.id"
    ):
        if p["crun"] is None:  # a split written by stage 5 (regime B)
            split += 1
            assert p["amount_cents"] + p["camount"] == csv_amount[p["source_ref"]], p["source_ref"]
    assert split == 4  # Oct: 2 Wise->Chase, the WA receipt, Maersk; the 21 Oct draw keeps its fee inside
    # every parent of a real FEE row (regime A) keeps the CSV amount untouched
    for p in wise_db.execute(
        "SELECT p.source_ref, p.amount_cents, p.fee_cents FROM transactions p JOIN transactions c ON c.parent_id = p.id "
        "WHERE c.running_balance_cents IS NOT NULL"
    ):
        assert p["amount_cents"] == csv_amount[p["source_ref"]] and p["fee_cents"] == 0


def test_rule4_wa_receipt_booked_gross(wise_db):
    row = wise_db.execute("SELECT amount_cents, fee_cents FROM transactions WHERE source_ref = 'TRANSFER-2441134368'").fetchone()
    child = wise_db.execute("SELECT amount_cents, category_id FROM transactions WHERE source_ref = 'FEE-TRANSFER-2441134368'").fetchone()
    assert (row["amount_cents"], row["fee_cents"], child["amount_cents"]) == (570600, 0, -611)


def test_fee_views_empty(wise_db, full_db):
    for conn in (wise_db, full_db):
        assert conn.execute("SELECT COUNT(*) FROM v_fee_parent_mismatch").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM v_revenue_unbacked").fetchone()[0] == 0


def test_rule2_one_wire_many_invoices(wise_db):
    tx = wise_db.execute("SELECT * FROM transactions WHERE source_ref = 'TRANSFER-2441134368'").fetchone()
    cat = wise_db.execute("SELECT name FROM categories WHERE id = ?", (tx["category_id"],)).fetchone()[0]
    assert (tx["date"], tx["amount_cents"], cat, tx["type"], tx["review_status"]) == (
        "2026-10-19", 570600, "Service Revenue", "Income", "auto")
    pays = wise_db.execute(
        "SELECT l.label, ip.amount_cents, l.status FROM invoice_payments ip JOIN v_invoice_labels l ON l.id = ip.invoice_id "
        "WHERE ip.transaction_id = ? ORDER BY ip.id", (tx["id"],)).fetchall()
    assert [tuple(p) for p in pays] == [("Wise INV-013", 301600, "Paid"), ("Wise INV-014", 269000, "Paid")]
    fee = wise_db.execute("SELECT * FROM transactions WHERE parent_id = ?", (tx["id"],)).fetchall()
    assert [(f["amount_cents"], f["source_ref"]) for f in fee] == [(-611, "FEE-TRANSFER-2441134368")]
    # the Firstbase INV-000014 (a different invoice) is untouched by the Wise INV-014 reference
    fb = wise_db.execute("SELECT l.label, ip.transaction_id FROM v_invoice_labels l LEFT JOIN invoice_payments ip "
                         "ON ip.invoice_id = l.id WHERE l.label = 'Firstbase INV-000014'").fetchone()
    assert fb["transaction_id"] != tx["id"]
    assert wise_db.execute("SELECT status FROM invoices WHERE source_label = 'Wise' AND number = 'INV-010'").fetchone()[0] == "Pending"


def test_rule6_customs_and_freight_are_reimbursable(wise_db):
    rows = {r["source_ref"]: r for r in ledger(wise_db, WISE)}
    cbp, maersk = rows["DIRECT_DEBIT-40502598"], rows["TRANSFER-2451302590"]
    assert (cbp["amount"], cbp["category"], cbp["type"], cbp["review_status"]) == (-551014, REIMB, "Reimbursable", "auto")
    assert (maersk["amount"], maersk["category"], maersk["type"]) == (-13500, REIMB, "Reimbursable")
    assert wise_db.execute("SELECT COUNT(*) FROM invoice_payments WHERE transaction_id = ?", (cbp["id"],)).fetchone()[0] == 0


def test_rule3_owner_draws_not_in_pnl(wise_db):
    draws = [r for r in ledger(wise_db, WISE) if r["category"] == OD]
    assert all(r["type"] == "Personal" for r in draws)
    assert not wise_db.execute("SELECT 1 FROM v_pnl_monthly WHERE category = ?", (OD,)).fetchone()
    rows = {r["source_ref"]: r for r in draws}
    # regime B: ONE row, the fee inside (21 Oct); no fee: 29 Oct; regime A: draw + real fee row (3 Sep, S1)
    assert (rows["TRANSFER-2445337879"]["amount"], rows["TRANSFER-2445337879"]["fee_cents"]) == (-60000, 113)
    assert (rows["TRANSFER-2459989965"]["amount"], rows["TRANSFER-2459989965"]["fee_cents"]) == (-30000, 0)
    assert "FEE-TRANSFER-2445337879" not in rows
    assert (rows["TRANSFER-2349562344"]["amount"], rows["FEE-TRANSFER-2349562344"]["amount"]) == (-49522, -478)
    assert rows["DIRECT_DEBIT-36302000"]["amount"] == -141100  # IRS: payee from the Merchant column


# ---------------------------------------------------------------------- review queue
def queue(conn):
    return [(r["date"], r["account"], r["amount_cents"], r["category"], tuple(r["reasons"])) for r in pipeline.review_queue(conn)]


def test_review_queue_exact_after_the_two_wise_imports(wise_db):
    assert queue(wise_db) == [
        ("2026-06-18", WISE, 320100, "Service Revenue", ("invoice_matched_by_amount",)),
        ("2026-07-08", WISE, -1833944, "Payable to WA Rubbermate", ("confirm_rule",)),
        ("2026-08-31", WISE, 281250, "Service Revenue", ("invoice_matched_by_amount",)),
    ]
    assert wise_db.execute("SELECT COUNT(*) FROM transactions WHERE category_id IS NULL").fetchone()[0] == 0


def test_review_queue_after_all_statements(full_db):
    q = sorted(queue(full_db))
    assert q == sorted([
        ("2026-01-16", CARD, -18287, "Other Professional Services", ("confirm_rule",)),
        ("2026-02-27", CARD, -104102, "Other Professional Services", ("confirm_rule",)),
        ("2026-04-16", CARD, -100000, "Other Professional Services", ("confirm_rule",)),
        ("2026-06-18", WISE, 320100, "Service Revenue", ("invoice_matched_by_amount",)),
        ("2026-07-08", WISE, -1833944, "Payable to WA Rubbermate", ("confirm_rule",)),
        ("2026-08-03", CHASE, -149297, "Bank Transfer", ("unpaired_transfer",)),
        ("2026-08-31", WISE, 281250, "Service Revenue", ("invoice_matched_by_amount",)),
    ])
    assert full_db.execute("SELECT COUNT(*) FROM transactions WHERE category_id IS NULL").fetchone()[0] == 0


# ---------------------------------------------------------------------- Chase and card
def test_chase_and_card_rows_vs_the_answer_key(full_db):
    key_all = [r for r in key_rows() if r["source"] in (CHASE, CARD)]
    missing = [r for r in key_all if r["source"] == CARD and CARD_MISSING[0] <= r["date"] <= CARD_MISSING[1]]
    key_in = [r for r in key_all if r not in missing]
    assert (len(key_all), len(missing), len(key_in)) == (118, 10, 108)  # the Aug card cycle is not in the fixtures
    eng = Counter(tup(r) for r in ledger(full_db, CHASE, CARD))
    key = Counter(tup(r) for r in key_in)

    raw_key_only = list((key - eng).elements())
    raw_eng_only = list((eng - key).elements())
    assert len(raw_key_only) == 12 and len(raw_eng_only) == 11  # 12 key rows differ before the documented deviations

    mapped = Counter(key)
    for wb, st, _why in DATE_DEVIATIONS:  # map each workbook row to the statement row that stands for it
        for row in wb:
            assert mapped[row] > 0, row
            mapped[row] -= 1
        mapped.update(st)
    key_only, eng_only = mapped - eng, eng - mapped
    wb_row, eng_row = CATEGORY_NON_MATCH
    assert list(key_only.elements()) == [wb_row] and list(eng_only.elements()) == [eng_row]
    non_matches = len(list(key_only.elements()))
    assert non_matches == 1 and non_matches / len(key_in) <= 0.03  # 0.93%
    assert sum(len(wb) for wb, _, _ in DATE_DEVIATIONS) == 11  # = 12 raw key-only rows - 1 category row


def test_rule7_chase_fee_only_when_balance_below_2000(full_db):
    expected_months, minimum = [], 200000
    for f in CHASE_PDFS:
        p = pipeline.parse_statement(f)
        low, ink = p["lowest_daily_balance_cents"], p["ink_activity_cents"]
        assert low is not None and ink is not None
        if low < minimum and ink < minimum:  # both printed waivers must fail
            expected_months.append(p["period_end"][:7])
    assert expected_months == ["2026-05", "2026-08"]  # April: balance 1,983.45 but Ink activity 3,142.79 waived it
    fee_rows = [r for r in ledger(full_db, CHASE) if r["description"].upper().startswith("MONTHLY SERVICE FEE")]
    assert [(r["date"], r["amount"], r["category"], r["review_status"]) for r in fee_rows] == [
        ("2026-05-29", -1500, "Bank Charges", "auto"), ("2026-08-31", -1500, "Bank Charges", "auto")]
    assert not any("unexpected_bank_fee" in (r["notes"] or "") for r in ledger(full_db, CHASE))


def test_unexpected_chase_fee_is_flagged(monkeypatch):
    """A $15 fee on a statement whose printed waivers passed (April: Ink activity 3,142.79) is queued."""
    original = pipeline._parse

    def with_fee(path, kind, filename):
        parsed = original(path, kind, filename)
        if parsed["filename"] == "2026-04.pdf":  # the April statement as if Chase had charged the fee
            parsed["rows"] = parsed["rows"] + [{
                "date": "2026-04-30", "account": CHASE, "description": "MONTHLY SERVICE FEE", "amount_cents": -1500,
                "source_ref": "test|99|fee", "fee_cents": 0, "running_balance_cents": None, "payee": None,
                "raw_type": "FEE_TRANSACTION"}]
            parsed["ending_cents"] -= 1500
        return parsed

    monkeypatch.setattr(pipeline, "_parse", with_fee)
    conn = new_db()
    reports = import_files(conn, CHASE_PDFS[:4])
    assert reports[3]["review_counts"] == {"unexpected_bank_fee": 1} and reports[:3] == [r for r in reports[:3]]
    assert all(r["review_counts"] == {} for r in reports[:3])
    assert queue(conn) == [("2026-04-30", CHASE, -1500, "Bank Charges", ("unexpected_bank_fee",))]
    pipeline.run_rules(conn)  # the flag belongs to the row: a re-run keeps it
    assert queue(conn) == [("2026-04-30", CHASE, -1500, "Bank Charges", ("unexpected_bank_fee",))]


def test_statement_balances_and_tie_outs(full_db):
    got = {(r["code"], r["as_of"]): r["balance_cents"] for r in full_db.execute(
        "SELECT a.code, s.as_of, s.balance_cents FROM statement_balances s JOIN accounts a ON a.id = s.account_id")}
    assert got[("CHASE_5200", "2026-01-30")] == 219578 and got[("CHASE_5200", "2026-09-30")] == 360352
    assert got[("CARD_2110", "2026-01-28")] == -146787 and got[("CARD_2110", "2026-09-28")] == -145641
    assert len([k for k in got if k[0] == "CHASE_5200"]) == 9 and len([k for k in got if k[0] == "CARD_2110"]) == 8
    diffs = {(r["account_code"], r["as_of"]): r["diff_cents"] for r in full_db.execute("SELECT * FROM v_tie_out")}
    assert all(d == 0 for k, d in diffs.items() if k != ("CARD_2110", "2026-09-28"))
    # the Sep card statement cannot tie: the Aug cycle (net -39,921... see below) is not in the fixtures
    aug_cycle_net = sum(r["amount"] for r in key_rows() if r["source"] == CARD and CARD_MISSING[0] <= r["date"] <= CARD_MISSING[1])
    assert diffs[("CARD_2110", "2026-09-28")] == -aug_cycle_net


def test_missing_previous_statement_is_a_warning_not_an_error():
    conn = new_db()
    rep = pipeline.run_import(conn, CHASE_PDFS[1])  # Feb statement first: January is not imported
    assert rep["inserted"] == 1 and any("previous statement" in w for w in rep["warnings"])
    assert conn.execute("SELECT COUNT(*) FROM statement_balances").fetchone()[0] == 1


def test_rule5_transfers_net_zero_except_the_known_gaps(full_db):
    net = {r["yyyy_mm"]: r["net_cents"] for r in full_db.execute("SELECT yyyy_mm, net_cents FROM v_transfer_net")}
    # Jan-Jul net to zero (Wise->Chase pairs straddle May/Jun and cancel; jar moves cancel)
    assert [net[m] for m in ("2026-01", "2026-02", "2026-03", "2026-04", "2026-07")] == [0, 0, 0, 0, 0]
    assert net["2026-05"] + net["2026-06"] == 0
    assert net["2026-10"] == -149774  # the 2 Oct and 29 Oct Wise->Chase legs: Chase October statement not issued
    # open legs: Chase 3 Aug (card leg of 31 Jul is in the missing Aug cycle) and Chase 29 Sep +1,198.87 (Wise 30 Sep
    # is in the missing Wise 12-30 Sep export)
    assert net["2026-08"] + net["2026-09"] == -149297 + 119887
    jar = full_db.execute(
        "SELECT SUM(t.amount_cents) FROM transactions t JOIN accounts a ON a.id = t.account_id WHERE a.kind = 'jar' "
        "AND t.category_id = (SELECT id FROM categories WHERE name = 'Bank Transfer')").fetchone()[0]
    wise_jar = full_db.execute(
        "SELECT SUM(t.amount_cents) FROM transactions t WHERE t.account_id = 1 AND t.source_ref IN "
        "(SELECT source_ref FROM transactions WHERE account_id IN (4, 5)) AND t.category_id = "
        "(SELECT id FROM categories WHERE name = 'Bank Transfer')").fetchone()[0]
    assert jar + wise_jar == 0  # every jar move has its mirror leg, each on the same date


def test_jar_moves_are_mirrored_on_the_jar_accounts(wise_db):
    legs = wise_db.execute(
        "SELECT t.date, a.source_label, t.amount_cents, t.description, t.review_status FROM transactions t "
        "JOIN accounts a ON a.id = t.account_id WHERE a.kind = 'jar' ORDER BY t.date, t.id").fetchall()
    assert sorted((l["date"], l["amount_cents"]) for l in legs) == [
        ("2026-01-26", 29300), ("2026-02-27", 29300), ("2026-03-25", 29300), ("2026-04-17", -29300),
        ("2026-04-17", 29300), ("2026-07-08", -95000)]
    descs = {(l["date"], l["amount_cents"]): l["description"] for l in legs}
    assert descs[("2026-01-26", 29300)] == "Moved 293.00 USD from USD"
    assert descs[("2026-04-17", -29300)] == "Moved 293.00 USD to USD"
    assert descs[("2026-07-08", -95000)] == "Moved 950.00 USD to USD"
    assert all(l["review_status"] == "auto" for l in legs)


# ---------------------------------------------------------------------- idempotency and re-runs
def test_reimport_is_noop(full_db):
    conn = clone(full_db)
    before = fingerprint(conn)
    for f in ALL_FILES:
        rep = pipeline.run_import(conn, f)
        assert rep["inserted"] == 0 and rep["ledger_rows"] == 0 and rep["review_counts"] == {}, f.name
        assert rep["skipped_duplicate"] > 0, f.name
    assert fingerprint(conn) == before


def test_rerun_is_noop(full_db):
    conn = clone(full_db)
    before = fingerprint(conn)
    rep = pipeline.run_rules(conn)
    assert rep["rows"] > 200 and rep["categorized"] == rep["rows"]
    assert fingerprint(conn) == before
    rep = pipeline.run_rules(conn, month="2026-10")
    assert rep["rows"] == 12 and rep["review_counts"] == {}
    assert fingerprint(conn) == before
    assert pipeline.run_rules(conn, [])["rows"] == 0
    with pytest.raises(ValueError):
        pipeline.run_rules(conn, month="2026-13")


def test_rerun_after_a_rule_edit_reconciles_instead_of_resplitting(wise_db, monkeypatch, tmp_path):
    conn = clone(wise_db)
    edited = tmp_path / "rules.yaml"
    text = rules.RULES_PATH.read_text(encoding="utf-8")
    # make 'Two Commas' transfers a draw: the regime B parents merge (fee inside), regime A parents stay two rows
    text = text.replace('category: "Bank Transfer"\n    type: Transfer\n    is_fee_parent: true\n    transfer_role: wise_out',
                        'category: "Owner\'s Draw"\n    type: Personal\n    is_fee_parent: true\n    fee: keep')
    assert text != rules.RULES_PATH.read_text(encoding="utf-8")
    edited.write_text(text, encoding="utf-8")
    original = rules.RULES_PATH
    monkeypatch.setattr(rules, "RULES_PATH", edited)
    pipeline.run_rules(conn)
    oct_rows = {r["source_ref"]: r for r in ledger(conn, WISE) if r["date"] >= "2026-10-01"}
    assert (oct_rows["TRANSFER-2406973811"]["amount"], oct_rows["TRANSFER-2406973811"]["fee_cents"]) == (-30000, 113)
    assert "FEE-TRANSFER-2406973811" not in oct_rows and len(oct_rows) == 12 - 2
    jan = {r["source_ref"]: r for r in ledger(conn, WISE)}
    assert jan["TRANSFER-1909616554"]["amount"] == -150000 and jan["FEE-TRANSFER-1909616554"]["category"] == OD
    assert conn.execute("SELECT COUNT(*) FROM v_fee_parent_mismatch").fetchone()[0] == 0
    # and back: the original rules split again, same ids for the untouched rows, same totals
    monkeypatch.setattr(rules, "RULES_PATH", original)
    pipeline.run_rules(conn)
    back = {r["source_ref"]: r for r in ledger(conn, WISE) if r["date"] >= "2026-10-01"}
    assert (back["TRANSFER-2406973811"]["amount"], back["FEE-TRANSFER-2406973811"]["amount"]) == (-29887, -113)
    assert sum(r["amount"] for r in back.values()) == -230125


def test_reviewed_rows_survive_rerun(wise_db):
    conn = clone(wise_db)
    oct_wire = conn.execute("SELECT id FROM transactions WHERE source_ref = 'TRANSFER-2441134368'").fetchone()[0]
    jun = conn.execute("SELECT id FROM transactions WHERE date = '2026-06-18' AND amount_cents = 320100").fetchone()[0]
    maersk = conn.execute("SELECT id FROM transactions WHERE source_ref = 'TRANSFER-2451302590'").fetchone()[0]
    cat = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}
    pipeline.confirm(conn, jun)  # accept the proposal
    pipeline.set_category(conn, maersk, cat["Software & Web Services"], "Expense", notes="human decision")
    pipeline.run_rules(conn)
    pipeline.run_rules(conn, [oct_wire, jun, maersk])
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (maersk,)).fetchone()
    assert (row["category_id"], row["review_status"], row["amount_cents"]) == (cat["Software & Web Services"], "reviewed", -13500 - 113 + 113)
    child = conn.execute("SELECT * FROM transactions WHERE parent_id = ?", (maersk,)).fetchone()
    assert child["category_id"] == cat["Bank Charges"] and conn.execute("SELECT COUNT(*) FROM v_fee_parent_mismatch").fetchone()[0] == 0
    j = conn.execute("SELECT * FROM transactions WHERE id = ?", (jun,)).fetchone()
    assert j["review_status"] == "reviewed" and j["category_id"] == cat["Service Revenue"]
    assert conn.execute("SELECT COUNT(*) FROM invoice_payments WHERE transaction_id = ?", (jun,)).fetchone()[0] == 1
    assert (jun, "reviewed") in [(r[0], r[1]) for r in conn.execute("SELECT id, review_status FROM transactions")]


def test_cross_regime_reimport(tmp_path):
    """The same transfer loaded from a regime A file and from a regime B file never produces a second row or fee row."""
    ref = "TRANSFER-1909616554"  # 8 Jan, -1,500.00 with a real FEE row in wise_full.csv (regime A)
    lines = (RAW / "wise_full.csv").read_text(encoding="utf-8").splitlines()
    header = lines[0]
    row = next(l for l in lines if l.startswith(ref))
    assert ",-1500.00," in row
    b_copy = tmp_path / "regime_b.csv"  # the same transfer as a regime B export: the debit includes the fee
    b_copy.write_text(header + "\n" + row.replace(",-1500.00,", ",-1501.13,"), encoding="utf-8")
    a_only = tmp_path / "regime_a.csv"
    fee_row = next(l for l in lines if l.startswith("FEE-" + ref))
    a_only.write_text(header + "\n" + fee_row + "\n" + row, encoding="utf-8")

    # A first, then B: nothing new
    conn = new_db()
    pipeline.run_import(conn, a_only)
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    rep = pipeline.run_import(conn, b_copy)
    assert (rep["inserted"], rep["skipped_duplicate"]) == (0, 1) and conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n == 2
    # B first, then A: the parent and the FEE row (same source_ref as the synthetic one) are both duplicates
    conn = new_db()
    pipeline.run_import(conn, b_copy)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2  # parent + synthetic fee row
    rep = pipeline.run_import(conn, a_only)
    assert (rep["inserted"], rep["skipped_duplicate"]) == (0, 2)
    rows = ledger(conn, WISE)
    assert [(r["source_ref"], r["amount"]) for r in rows] == [(ref, -150000), ("FEE-" + ref, -113)]
    assert pipeline.run_rules(conn)["rows"] == 2 and conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2


def test_possible_duplicate_flag_and_mark_duplicate(tmp_path):
    conn = new_db()
    pipeline.run_import(conn, RAW / "wise_2026-10.csv")
    lines = (RAW / "wise_2026-10.csv").read_text(encoding="utf-8").splitlines()
    interest = next(l for l in lines if l.startswith('"BALANCE_INTEREST-'))
    twin = tmp_path / "twin.csv"  # same date and amount, a different bank id
    twin.write_text(lines[0] + "\n" + interest.replace("817956ec", "00000000"), encoding="utf-8")
    rep = pipeline.run_import(conn, twin)
    assert rep["inserted"] == 1 and rep["review_counts"] == {"possible_duplicate": 1}
    q = pipeline.review_queue(conn)
    assert len(q) == 1 and q[0]["reasons"] == ["possible_duplicate"] and q[0]["category"] == "Interest Income"
    pipeline.run_rules(conn)  # a re-run keeps the flag
    assert [r["reasons"] for r in pipeline.review_queue(conn)] == [["possible_duplicate"]]
    pipeline.mark_duplicate(conn, q[0]["id"])
    assert pipeline.review_queue(conn) == [] and conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 12


def test_orphan_fee_and_unknown_payee_reasons(tmp_path):
    conn = new_db()
    lines = (RAW / "wise_full.csv").read_text(encoding="utf-8").splitlines()
    fee_only = next(l for l in lines if l.startswith("FEE-TRANSFER-1909616554"))
    f = tmp_path / "fee_only.csv"
    f.write_text(lines[0] + "\n" + fee_only, encoding="utf-8")
    rep = pipeline.run_import(conn, f)
    assert rep["review_counts"] == {"orphan_fee": 1}
    q = pipeline.review_queue(conn)
    assert len(q) == 1 and q[0]["category"] is None and q[0]["reasons"] == ["orphan_fee"]
    # a row no rule knows: no_rule + unknown_payee (first time the payee is seen)
    oct_lines = (RAW / "wise_2026-10.csv").read_text(encoding="utf-8").splitlines()
    odd = next(l for l in oct_lines if l.startswith("TRANSFER-2451302590")).replace("Maersk Customs Services USA Inc", "Acme Tools")
    g = tmp_path / "odd.csv"
    g.write_text(oct_lines[0] + "\n" + odd, encoding="utf-8")
    rep = pipeline.run_import(conn, g, allow_wise_gap=True)  # a one-row excerpt of another file: not a chain
    assert rep["review_counts"] == {"no_rule": 1, "unknown_payee": 1}
    row = conn.execute("SELECT * FROM transactions WHERE source_ref = 'TRANSFER-2451302590'").fetchone()
    assert row["category_id"] is None and row["amount_cents"] == -13613 and row["fee_cents"] == 113  # left whole
    child = conn.execute("SELECT * FROM transactions WHERE source_ref = 'FEE-TRANSFER-2451302590'").fetchone()
    assert child is None  # uncategorised: the split waits until the row is categorised


# ---------------------------------------------------------------------- RULES.md section 6: every worked example
def _doc_tables():
    text = RULES_MD.read_text(encoding="utf-8")
    body = text[text.index("## 6. Worked examples"):text.index("## 7. Test plan")]
    label, out = None, []
    lines = body.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^\*\*(Example \d+|S\d)\b", line)
        if m:
            label = m.group(1)
        if line.startswith("| date |"):
            for row in lines[i + 2:]:
                if not row.startswith("|"):
                    break
                cells = [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row.strip())[1:-1]]
                assert len(cells) == 11, row
                out.append((label, cells))
    return out


# card / Chase rows of the examples that the statements print with other dates or text (see DATE_DEVIATIONS)
EXAMPLE_SKIPPED = {
    "Example 12": "card cycle 07/29-08/28 (the 31 Jul card leg) is not in the fixtures; the Chase leg is queued as "
                  "unpaired_transfer instead of auto",
}
_ALIASES = {(wb[0][0], wb[0][2]): st[0][0] for wb, st, _ in DATE_DEVIATIONS if len(wb) == 1}


def test_worked_examples(full_db):
    tables = _doc_tables()
    assert len(tables) >= 28
    checked = 0
    for label, (date, account, desc, amount, category, typ, ref, parent, fee, review, _key) in tables:
        if label in EXAMPLE_SKIPPED:
            continue
        amount, fee = int(amount), int(fee)
        rows = ledger(full_db, account)
        if account == WISE or account in JARS:
            hit = [r for r in rows if r["source_ref"] == ref]
        else:  # statements carry their own source_ref scheme; match on date (aliased) + amount
            when = _ALIASES.get((date, amount), date)
            hit = [r for r in rows if r["date"] == when and r["amount"] == amount]
        assert len(hit) >= 1, (label, date, account, amount)
        r = hit[0]
        got_parent = ""
        if r["parent_id"] is not None:
            got_parent = full_db.execute("SELECT source_ref FROM transactions WHERE id = ?", (r["parent_id"],)).fetchone()[0]
        got = (r["date"] if account in (WISE, *JARS) else date, r["amount"], r["category"], r["type"], got_parent,
               r["fee_cents"], r["review_status"])
        assert got == (date, amount, category, typ, parent, fee, review), (label, got)
        if account == WISE or account in JARS:
            assert (r["description"], r["source_ref"], r["source"]) == (desc, ref, account), label
        checked += 1
    skipped = [label for label, _ in tables if label in EXAMPLE_SKIPPED]
    assert skipped == ["Example 12", "Example 12"]  # only the two Example 12 rows, each with a stated reason
    assert checked == len(tables) - 2 and checked == 26


# ---------------------------------------------------------------------- seed cutoff (ACCEPTANCE H2)
def seeded_db(tmp_path) -> sqlite3.Connection:
    """A database seeded by the seed command (the workbook's 2026 history, cutoffs set)."""
    from tc.fixtures import seed

    path = tmp_path / "seeded.db"
    seed(path)
    return connect(path)


def wise_ledger_at(conn, day: str) -> int:
    return pipeline._ledger_at(conn, conn.execute("SELECT id FROM accounts WHERE code = 'WISE_1430'").fetchone()[0], day)


def wise_november(tmp_path, *, with_october: bool = False):
    """Synthetic Wise export of two interest rows on 1 and 2 Nov 2026 (+12.00, +13.50); the running balance
    continues from 13,344.65 at 31 Oct. with_october=True prepends the eight real October rows to the same file."""
    lines = (RAW / "wise_2026-10.csv").read_text(encoding="utf-8").splitlines()
    interest = next(l for l in lines if l.startswith('"BALANCE_INTEREST-'))
    assert ",45.13,USD," in interest and ",15691.03," in interest and interest.count("01-10-2026") == 2

    def row(ident, day, amount, running):
        return (interest.replace("817956ec-2940-4ea3-2a38-15268ba52481", ident)
                .replace("01-10-2026", day).replace(",45.13,USD,", f",{amount},USD,")
                .replace(",15691.03,", f",{running},"))

    new = [row("aaaaaaaa-0000-4000-8000-000000000002", "02-11-2026", "13.50", "13370.15"),   # newest first
           row("aaaaaaaa-0000-4000-8000-000000000001", "01-11-2026", "12.00", "13356.65")]
    f = tmp_path / ("wise_oct_nov.csv" if with_october else "wise_2026-11.csv")
    f.write_text("\n".join([lines[0]] + new + (lines[1:] if with_october else [])) + "\n", encoding="utf-8")
    return f


def test_import_over_seed_inserts_nothing(tmp_path):
    conn = seeded_db(tmp_path)
    assert wise_ledger_at(conn, "2026-10-31") == 1334465
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    queue = pipeline.review_queue(conn)
    rep = pipeline.run_import(conn, RAW / "wise_2026-10.csv")
    assert (rep["inserted"], rep["skipped_seeded"], rep["skipped_duplicate"], rep["rejected_locked"]) == (0, 8, 0, 0)
    assert rep["ledger_rows"] == 0 and rep["review_counts"] == {} and rep["warnings"] == []
    assert wise_ledger_at(conn, "2026-10-31") == 1334465
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n == 235
    assert pipeline.review_queue(conn) == queue  # nothing was flagged: no possible_duplicate rows
    # the long Wise export is covered too: 86 rows from 2026, 306 before the opening
    full = pipeline.run_import(conn, RAW / "wise_full.csv")
    assert (full["inserted"], full["skipped_seeded"], full["skipped_before_opening"]) == (0, 86, 306)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 235


def test_import_over_seed_chase_and_card_statements(tmp_path):
    conn = seeded_db(tmp_path)
    sep_chase = pipeline.run_import(conn, RAW / "chase5200" / "2026-09.pdf")
    assert (sep_chase["inserted"], sep_chase["skipped_seeded"], sep_chase["skipped_duplicate"]) == (0, 2, 0)
    sep_card = pipeline.run_import(conn, RAW / "card2110" / "2026-09-28.pdf")
    assert (sep_card["inserted"], sep_card["skipped_seeded"], sep_card["skipped_duplicate"]) == (0, 9, 0)
    assert sep_chase["warnings"] == sep_card["warnings"] == []
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 235
    assert conn.execute("SELECT COUNT(*) FROM statement_balances").fetchone()[0] == 28
    # a Chase *5200 download with one row on the cutoff day (30 Sep) and two after it: only the two are new
    f = tmp_path / "Chase5200_Activity.CSV"
    f.write_text(
        "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
        "DEBIT,10/05/2026,ORIG CO NAME:BETTERMENT SEC,-100.00,ACH_DEBIT,3203.52,,\n"
        "DEBIT,10/02/2026,SOME VENDOR,-50.00,ACH_DEBIT,3303.52,,\n"
        "DEBIT,09/30/2026,CUTOFF DAY ROW,-50.00,ACH_DEBIT,3353.52,,\n", encoding="utf-8")
    rep = pipeline.run_import(conn, f)
    assert rep["kind"] == "chase5200_csv"
    assert (rep["inserted"], rep["skipped_seeded"], rep["skipped_duplicate"]) == (2, 1, 0)
    dates = [r[0] for r in conn.execute(
        "SELECT t.date FROM transactions t JOIN accounts a ON a.id = t.account_id "
        "WHERE a.code = 'CHASE_5200' AND t.date > '2026-09-30' ORDER BY t.date")]
    assert dates == ["2026-10-02", "2026-10-05"]
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE description = 'CUTOFF DAY ROW'").fetchone()[0] == 0


def test_wise_statement_after_the_cutoff_is_not_skipped(tmp_path):
    conn = seeded_db(tmp_path)
    rep = pipeline.run_import(conn, wise_november(tmp_path))
    assert (rep["inserted"], rep["skipped_seeded"], rep["skipped_duplicate"], rep["rejected_locked"]) == (2, 0, 0, 0)
    assert rep["review_counts"] == {}
    assert wise_ledger_at(conn, "2026-10-31") == 1334465
    assert wise_ledger_at(conn, "2026-11-02") == 1334465 + 1200 + 1350
    rows = conn.execute("SELECT date, amount_cents, c.name FROM transactions t JOIN categories c ON c.id = t.category_id "
                        "WHERE t.date > '2026-10-31' ORDER BY t.date").fetchall()
    assert [tuple(r) for r in rows] == [("2026-11-01", 1200, "Interest Income"), ("2026-11-02", 1350, "Interest Income")]
    # the same file again: now they are ordinary duplicates, not seeded lines
    again = pipeline.run_import(conn, wise_november(tmp_path))
    assert (again["inserted"], again["skipped_seeded"], again["skipped_duplicate"]) == (0, 0, 2)


def test_mixed_wise_file_skips_only_the_seeded_part(tmp_path):
    conn = seeded_db(tmp_path)
    rep = pipeline.run_import(conn, wise_november(tmp_path, with_october=True))
    assert (rep["inserted"], rep["skipped_seeded"]) == (2, 8)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 237


def test_seed_cutoff_is_inclusive_and_null_means_no_cutoff():
    conn = new_db()  # accounts.seeded_through is NULL: no effect (every other test in this file relies on it)
    assert pipeline.seed_cutoff(conn, WISE) is None
    conn.execute("UPDATE accounts SET seeded_through = '2026-10-02' WHERE source_label = ?", (WISE,))
    conn.commit()
    assert pipeline.seed_cutoff(conn, WISE) == "2026-10-02"
    rep = pipeline.run_import(conn, RAW / "wise_2026-10.csv")  # rows dated 1 Oct and 2 Oct (the cutoff day) are skipped
    assert (rep["skipped_seeded"], rep["inserted"]) == (2, 6)
    assert min(r[0] for r in conn.execute("SELECT date FROM transactions")) == "2026-10-19"


def test_database_without_the_cutoff_column_still_imports(tmp_path):
    path = tmp_path / "old.db"
    conn = connect(path)
    init_db(conn)
    conn.execute("ALTER TABLE accounts DROP COLUMN seeded_through")
    conn.commit()
    assert pipeline.seed_cutoff(conn, WISE) is None
    rep = pipeline.run_import(conn, RAW / "wise_2026-10.csv")
    assert (rep["inserted"], rep["skipped_seeded"]) == (8, 0)


def test_import_backup_dir_ignores_cwd(tmp_path, monkeypatch):
    """M6: the import backup goes to close.backup's directory, never to a cwd-relative 'backups/'."""
    from tc import close
    target = tmp_path / "anchored"
    work = tmp_path / "elsewhere"
    work.mkdir()
    monkeypatch.delenv("TC_BACKUP_DIR", raising=False)
    monkeypatch.setattr(close, "_backup_dir", lambda: target)
    monkeypatch.chdir(work)
    pipeline.run_import(new_db(), RAW / "wise_2026-10.csv")
    assert list(target.glob("tc_*_import.db"))
    assert not (work / "backups").exists()
    # and with no override the directory is absolute and sits at the repo root
    monkeypatch.undo()
    monkeypatch.delenv("TC_BACKUP_DIR", raising=False)
    default = close._backup_dir()
    assert default.is_absolute() and default.name == "backups"
    assert (default.parent / "src" / "tc" / "schema.sql").exists()
    assert dbmod.DEFAULT_DB_PATH.is_absolute()


def test_wise_gap_is_refused(tmp_path):
    """M1: a Wise export that does not continue from the ledger (an export in between is missing) is refused."""
    conn = seeded_db(tmp_path)
    good = wise_november(tmp_path)
    gap = tmp_path / "wise_gap.csv"  # same rows, but the running balance starts 100.00 higher: 100.00 went missing
    gap.write_text(good.read_text(encoding="utf-8").replace("13356.65", "13456.65").replace("13370.15", "13470.15"),
                   encoding="utf-8")
    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    with pytest.raises(ValueError, match="export between them is missing"):
        pipeline.run_import(conn, gap)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n  # rolled back
    rep = pipeline.run_import(conn, good)  # the file that does continue from 13,344.65 still imports
    assert rep["inserted"] == 2 and wise_ledger_at(conn, "2026-11-02") == 1337015


def test_chase_january_has_no_false_lowest_balance_warning():
    """LOW: Jan 2026 ends on its lowest day (219,578); Chase's printed low (221,365) leaves the last day out."""
    conn = new_db()
    rep = pipeline.run_import(conn, RAW / "chase5200" / "2026-01.pdf")
    assert rep["warnings"] == [] and rep["review_counts"] == {}
    for f in sorted((RAW / "chase5200").glob("*.pdf"))[1:]:  # and no other statement warns either
        assert pipeline.run_import(conn, f)["warnings"] == [], f.name


def test_rerun_rules_takes_a_rerun_backup_and_is_a_noop(backup_dir, tmp_path):
    """LOW L3: RULES.md 4.2 -- backup("rerun"), then run_rules; unchanged rules change nothing."""
    conn = new_db()
    pipeline.run_import(conn, RAW / "wise_2026-10.csv")
    before = fingerprint(conn)
    snaps = set(backup_dir.glob("tc_*_rerun.db"))
    rep = pipeline.rerun_rules(conn, from_month="2026-10")
    assert fingerprint(conn) == before
    new = set(backup_dir.glob("tc_*_rerun.db")) - snaps
    assert len(new) == 1 and rep["backup"] == str(next(iter(new)))
    assert pipeline.rerun_rules(conn)["rows"] >= 0 and pipeline.rerun_rules(conn, month="2026-10")["backup"]
    with pytest.raises(ValueError):
        pipeline.rerun_rules(conn, from_month="2026-13")
    with pytest.raises(ValueError):
        pipeline.rerun_rules(conn, from_month="2026-10", month="2026-10")


def test_rerun_cli(tmp_path, monkeypatch):
    path = tmp_path / "cli.db"
    conn = connect(path)
    init_db(conn)
    conn.close()
    monkeypatch.setenv("TC_BACKUP_DIR", str(tmp_path / "bk"))
    assert pipeline._main(["tc.pipeline", "rerun", "--db", str(path), "--from", "2026-01"]) == 0
    assert list((tmp_path / "bk").glob("tc_*_rerun.db"))
    assert pipeline._main(["tc.pipeline", "rerun", "--db", str(tmp_path / "missing.db")]) == 1
    assert pipeline._main(["tc.pipeline", "rerun", "--db", str(path), "--from", "bad"]) == 1
