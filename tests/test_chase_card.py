"""Chase Ink card *2110 statement importer: reconciliation, balance tie-out, workbook cross-check."""

import csv
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from tc.db import to_cents
from tc.importers.chase_card import ACCOUNT_LABEL, import_chase_card, parse_chase_card_pdf

FIX = Path(__file__).parent / "fixtures"
RAW = FIX / "raw" / "card2110"
CYCLE_ENDS = [f"2026-{m:02d}-28" for m in range(1, 10)]  # Jan..Sep; the Aug PDF is not on file
CARD_BALANCE_LABEL = "Chase card *2110"


def _pdf(cycle_end):
    return RAW / f"{cycle_end}.pdf"


@pytest.fixture(params=CYCLE_ENDS)
def cycle_end(request):
    if not _pdf(request.param).exists():
        pytest.skip(
            f"{request.param}.pdf not available (card *2110 cycle closing {request.param}; "
            "Joshua to supply -> tests/fixtures/raw/card2110/) - not tested, not failed"
        )
    return request.param


@pytest.fixture(scope="module")
def parsed_cache():
    return {}


@pytest.fixture
def stmt(cycle_end, parsed_cache):
    if cycle_end not in parsed_cache:
        parsed_cache[cycle_end] = parse_chase_card_pdf(_pdf(cycle_end))
    return parsed_cache[cycle_end]


def _statement_balances():
    out = {}
    with open(FIX / "statement_balances.csv", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["account"] == CARD_BALANCE_LABEL:
                out[r["as_of"]] = -to_cents(r["balance"])  # liability stored negative
    return out


def _workbook_rows():
    with open(FIX / "transactions.csv", newline="", encoding="utf-8") as f:
        return [(r["date"], to_cents(r["amount"])) for r in csv.DictReader(f) if r["source"] == ACCOUNT_LABEL]


# Intentional workbook deviations from the statements (compared per cycle on (date, amount_cents)).
# Each entry: cycle_end, the statement-side (date, cents) rows, the workbook-side rows that stand for them.
# Anything not listed here must match exactly. 8 one-row date shifts + 1 split = 9 entries.
EXCEPTIONS = [
    # Jan cycle (12/29/25-01/28/26): workbook books the 12/31 charge on 1 Jan; two others one day earlier
    {"cycle_end": "2026-01-28", "statement": [("2025-12-31", -4700)], "workbook": [("2026-01-01", -4700)],
     "note": "MYREALPROFIT 12/31/25 booked 2026-01-01 (workbook starts 1 Jan)"},
    {"cycle_end": "2026-01-28", "statement": [("2026-01-02", -14900)], "workbook": [("2026-01-01", -14900)],
     "note": "FIRSTBASE 149.00: statement 01/02, workbook 01/01"},
    {"cycle_end": "2026-01-28", "statement": [("2026-01-16", -18287)], "workbook": [("2026-01-15", -18287)],
     "note": "HTGROWTH 182.87: statement 01/16, workbook 01/15"},
    {"cycle_end": "2026-02-28", "statement": [("2026-02-02", -14900)], "workbook": [("2026-02-01", -14900)],
     "note": "FIRSTBASE 149.00: statement 02/02, workbook 02/01"},
    {"cycle_end": "2026-03-28", "statement": [("2026-03-02", -14900)], "workbook": [("2026-03-01", -14900)],
     "note": "FIRSTBASE 149.00: statement 03/02, workbook 03/01"},
    {"cycle_end": "2026-04-28", "statement": [("2026-04-11", -7756)], "workbook": [("2026-04-10", -7756)],
     "note": "PIERPASS 77.56: statement 04/11, workbook 04/10"},
    {"cycle_end": "2026-04-28", "statement": [("2026-04-11", -2000)], "workbook": [("2026-04-10", -2000)],
     "note": "PORTCHECK 20.00: statement 04/11, workbook 04/10"},
    {"cycle_end": "2026-04-28", "statement": [("2026-04-27", -46215)], "workbook": [("2026-04-26", -46215)],
     "note": "MAERSK 462.15: statement 04/27, workbook 04/26"},
    {"cycle_end": "2026-09-28", "statement": [("2026-09-01", -2880)],
     "workbook": [("2026-09-01", -1680), ("2026-09-01", -1200)],
     "note": "GOOGLE Workspace 28.80 split in workbook into 16.80 Workspace + 12.00 domain renewal"},
]


def test_exception_list_size_is_documented():
    assert len(EXCEPTIONS) == 9
    # every exception preserves the dollar total (date shift or split, never an amount change)
    for e in EXCEPTIONS:
        assert sum(c for _, c in e["statement"]) == sum(c for _, c in e["workbook"]), e["note"]


def test_august_statement_missing_is_skipped_not_failed():
    if not _pdf("2026-08-28").exists():
        pytest.skip("2026-08-28.pdf (cycle 07/29-08/28) is not available: August is not tested "
                    "(Joshua to supply -> tests/fixtures/raw/card2110/2026-08-28.pdf)")
    assert parse_chase_card_pdf(_pdf("2026-08-28"))["cycle_end"] == "2026-08-28"


def test_each_available_statement_reconciles(stmt):
    rows = stmt["rows"]
    assert rows, "no transactions parsed"
    charges = -sum(r["amount_cents"] for r in rows if r["amount_cents"] < 0)  # magnitudes
    payments = sum(r["amount_cents"] for r in rows if r["amount_cents"] > 0)
    # balances are signed asset values (liability negative)
    assert stmt["previous_balance_cents"] - charges + payments == stmt["new_balance_cents"]
    assert stmt["previous_balance_cents"] + sum(r["amount_cents"] for r in rows) == stmt["new_balance_cents"]
    # and each printed ACCOUNT SUMMARY bucket matches the parsed lines
    s = stmt["summary"]
    assert payments == -s["payments_credits"]
    assert charges == s["purchases"] + s["fees"] + s["interest"] + s["cash_advances"] + s["balance_transfers"]


def test_closing_balance_equals_statement_balances_csv(stmt, cycle_end):
    expected = _statement_balances()
    assert stmt["cycle_end"] == cycle_end
    assert stmt["new_balance_cents"] == expected[cycle_end]


def test_opening_balances_chain(stmt, cycle_end):
    expected = _statement_balances()
    prev_end = (date.fromisoformat(stmt["cycle_start"]) - timedelta(days=1)).isoformat()
    # previous balance = prior cycle's closing; for January it is the 28 Dec 2025 opening (-1,489.92)
    assert stmt["previous_balance_cents"] == expected.get(prev_end, -148992)
    assert prev_end.endswith("-28")


def test_sep_cycle_closing_is_145641_liability():
    if not _pdf("2026-09-28").exists():
        pytest.skip("2026-09-28.pdf not available")
    p = parse_chase_card_pdf(_pdf("2026-09-28"))
    assert (p["cycle_start"], p["cycle_end"]) == ("2026-08-29", "2026-09-28")
    assert p["new_balance_cents"] == -145641


def test_dates_are_iso_and_year_resolved(stmt):
    lo = date.fromisoformat(stmt["cycle_start"]) - timedelta(days=5)  # statement lists a few pre-cycle dates
    hi = date.fromisoformat(stmt["cycle_end"])
    for r in stmt["rows"]:
        assert lo <= date.fromisoformat(r["date"]) <= hi, r


def test_dec_charge_in_january_cycle_gets_previous_year():
    if not _pdf("2026-01-28").exists():
        pytest.skip("2026-01-28.pdf not available")
    rows = parse_chase_card_pdf(_pdf("2026-01-28"))["rows"]
    assert ("2025-12-31", -4700) in [(r["date"], r["amount_cents"]) for r in rows]


def test_row_contract_and_signs(stmt):
    seen = set()
    for r in stmt["rows"]:
        assert r["account"] == ACCOUNT_LABEL and isinstance(r["amount_cents"], int)
        assert r["fee_cents"] == 0 and r["description"] == r["description"].strip() and r["description"]
        cyc, idx, h = r["source_ref"].split("|")
        assert cyc == stmt["cycle_end"] and idx.isdigit() and len(h) == 8
        assert r["source_ref"] not in seen
        seen.add(r["source_ref"])
        if r["description"].upper().startswith("PAYMENT THANK YOU"):
            assert r["amount_cents"] > 0 and r["raw_type"] == "PAYMENT"
    assert [int(r["source_ref"].split("|")[1]) for r in stmt["rows"]] == list(range(len(stmt["rows"])))


def test_sep_refund_is_positive():
    if not _pdf("2026-09-28").exists():
        pytest.skip("2026-09-28.pdf not available")
    rows = parse_chase_card_pdf(_pdf("2026-09-28"))["rows"]
    refund = [r for r in rows if r["date"] == "2026-09-03" and r["amount_cents"] == 14900]
    assert len(refund) == 1 and refund[0]["raw_type"] == "CREDIT"


def test_foreign_fee_is_kept_as_transaction():
    if not _pdf("2026-02-28").exists():
        pytest.skip("2026-02-28.pdf not available")
    rows = parse_chase_card_pdf(_pdf("2026-02-28"))["rows"]
    fee = [r for r in rows if r["description"] == "FOREIGN TRANSACTION FEE"]
    assert [(r["date"], r["amount_cents"], r["raw_type"]) for r in fee] == [("2026-02-27", -3123, "FEE")]


def test_workbook_cross_check_per_cycle(stmt, cycle_end):
    start, end = stmt["cycle_start"], stmt["cycle_end"]
    wb = Counter((d, c) for d, c in _workbook_rows() if start <= d <= end)
    st = Counter((r["date"], r["amount_cents"]) for r in stmt["rows"])
    for e in (x for x in EXCEPTIONS if x["cycle_end"] == cycle_end):
        st.subtract(e["statement"])
        wb.subtract(e["workbook"])
    st, wb = +st, +wb  # drop zero counts; a negative count (exception not present) survives as a failure below
    assert not (st - wb), f"statement rows missing from workbook: {sorted((st - wb).items())}"
    assert not (wb - st), f"workbook rows not on statement: {sorted((wb - st).items())}"


def test_documented_exceptions_are_all_real():
    """Each exception must really occur (guards against stale entries silently loosening the check)."""
    wb_all = Counter(_workbook_rows())
    for e in EXCEPTIONS:
        if not _pdf(e["cycle_end"]).exists():
            continue
        st = Counter((r["date"], r["amount_cents"]) for r in parse_chase_card_pdf(_pdf(e["cycle_end"]))["rows"])
        for k in e["statement"]:
            assert st[k] >= 1, e["note"]
        for k in e["workbook"]:
            assert wb_all[k] >= 1, e["note"]


def test_import_is_idempotent_and_records_balance(empty_db):
    conn = empty_db
    acct = conn.execute("SELECT id, opening_balance_cents FROM accounts WHERE source_label = ?",
                        (ACCOUNT_LABEL,)).fetchone()
    available = [c for c in CYCLE_ENDS if _pdf(c).exists()]
    assert available, "no statements on file"
    total_rows = 0
    for c in available:
        res = import_chase_card(conn, _pdf(c))
        assert res["new"] == res["total"] and res["duplicates"] == 0
        total_rows += res["total"]
    count = lambda: conn.execute("SELECT COUNT(*) FROM transactions WHERE account_id=?", (acct["id"],)).fetchone()[0]
    assert count() == total_rows
    for c in available:
        res = import_chase_card(conn, _pdf(c))
        assert res["new"] == 0 and res["duplicates"] == res["total"]
    assert count() == total_rows
    # raw rows: uncategorised, needs_review
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category_id IS NOT NULL OR type IS NOT NULL "
                        "OR review_status <> 'needs_review'").fetchone()[0] == 0
    # closing balances recorded once per cycle, negative liability, as_of = cycle end
    bal = {r["as_of"]: r["balance_cents"] for r in conn.execute(
        "SELECT as_of, balance_cents FROM statement_balances WHERE account_id=?", (acct["id"],))}
    assert bal == {c: v for c, v in _statement_balances().items() if c in available}
    assert bal.get("2026-09-28", -145641) == -145641
    # ledger ties to the statement at the end of the first cycle (opening + cycle rows)
    first = available[0]
    ledger = acct["opening_balance_cents"] + conn.execute(
        "SELECT SUM(amount_cents) FROM transactions WHERE account_id=? AND source_ref LIKE ?",
        (acct["id"], first + "|%")).fetchone()[0]
    assert ledger == bal[first]


def test_existing_statement_balance_is_not_overwritten(empty_db):
    conn = empty_db
    aid = conn.execute("SELECT id FROM accounts WHERE source_label = ?", (ACCOUNT_LABEL,)).fetchone()[0]
    first = next((c for c in CYCLE_ENDS if _pdf(c).exists()), None)
    assert first
    conn.execute("INSERT INTO statement_balances (account_id, as_of, balance_cents, source) VALUES (?,?,?,?)",
                 (aid, first, -1, "manual"))
    import_chase_card(conn, _pdf(first))
    row = conn.execute("SELECT balance_cents, source FROM statement_balances WHERE account_id=? AND as_of=?",
                       (aid, first)).fetchone()
    assert (row[0], row[1]) == (-1, "manual")
