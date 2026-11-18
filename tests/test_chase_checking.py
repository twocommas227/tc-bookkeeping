import csv
from collections import Counter
from pathlib import Path

import pytest

from tc.db import to_cents
from tc.importers.chase_checking import (
    ACCOUNT,
    import_chase_checking,
    parse_chase_checking_csv,
    parse_chase_checking_pdf,
)

FIX = Path(__file__).parent / "fixtures"
RAW = FIX / "raw" / "chase5200"
PDFS = [RAW / f"2026-{m:02d}.pdf" for m in range(1, 10)]
PERIODS = {  # file label -> (period_start, period_end); NOT calendar months
    "2026-01": ("2026-01-01", "2026-01-30"),
    "2026-02": ("2026-01-31", "2026-02-27"),
    "2026-03": ("2026-02-28", "2026-03-31"),
    "2026-04": ("2026-04-01", "2026-04-30"),
    "2026-05": ("2026-05-01", "2026-05-29"),
    "2026-06": ("2026-05-30", "2026-06-30"),
    "2026-07": ("2026-07-01", "2026-07-31"),
    "2026-08": ("2026-08-01", "2026-08-31"),
    "2026-09": ("2026-09-01", "2026-09-30"),
}
PRINTED_ENDING = [219578, 209578, 208345, 493729, 193258, 349992, 350593, 250465, 360352]

# Documented, intentional workbook-vs-PDF date differences (do NOT add silently).
# workbook (date, cents) -> PDF (date, cents)
EXCEPTIONS = [
    {
        "workbook": ("2026-08-30", -189218),
        "pdf": ("2026-08-31", -189218),
        "reason": "Payment to Chase card 2110: transaction date 08/30, workbook note 'Posted by Chase 31 Aug'; "
        "statement posting date is 08/31.",
    },
]


@pytest.fixture(scope="module")
def stmts():
    return {p.stem: parse_chase_checking_pdf(p) for p in PDFS}


@pytest.mark.parametrize("pdf", PDFS, ids=lambda p: p.stem)
def test_statement_ties_exactly(pdf, stmts):
    s = stmts[pdf.stem]
    assert len(s["rows"]) > 0
    assert (s["period_start"], s["period_end"]) == PERIODS[pdf.stem]
    assert s["opening_cents"] + sum(r["amount_cents"] for r in s["rows"]) == s["ending_cents"]
    assert s["ending_cents"] == PRINTED_ENDING[int(pdf.stem[-2:]) - 1]
    for r in s["rows"]:
        assert r["account"] == ACCOUNT
        assert r["date"] >= s["period_start"] and r["date"] <= s["period_end"]
        assert isinstance(r["amount_cents"], int) and r["amount_cents"] != 0
        assert r["source_ref"].startswith(s["period_end"] + "|")


def test_chain_and_known_endings(stmts):
    ordered = [stmts[p.stem] for p in PDFS]
    assert ordered[0]["opening_cents"] == 252956
    for a, b in zip(ordered, ordered[1:]):
        assert a["ending_cents"] == b["opening_cents"]
        assert a["period_end"] < b["period_start"]
    assert ordered[-1]["ending_cents"] == 360352
    # statement_balances.csv (workbook) uses calendar month-end dates for the same balances
    with open(FIX / "statement_balances.csv", newline="") as fh:
        wb = [to_cents(r[list(r)[-1]]) for r in csv.DictReader(fh) if "5200" in r[list(r)[0]]]
    assert wb == [s["ending_cents"] for s in ordered]


def test_source_refs_unique_and_stable(stmts):
    refs = [r["source_ref"] for s in stmts.values() for r in s["rows"]]
    assert len(refs) == len(set(refs))
    again = parse_chase_checking_pdf(PDFS[0])
    assert [r["source_ref"] for r in again["rows"]] == [r["source_ref"] for r in stmts["2026-01"]["rows"]]


def _workbook_multiset():
    out = Counter()
    with open(FIX / "transactions.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["source"] == ACCOUNT and "2026-01-01" <= r["date"] <= "2026-09-30":
                out[(r["date"], to_cents(r["amount"]))] += 1
    return out


def test_matches_workbook_multiset(stmts):
    parsed = Counter((r["date"], r["amount_cents"]) for s in stmts.values() for r in s["rows"])
    workbook = _workbook_multiset()
    assert sum(workbook.values()) == 32
    # every documented exception must really be present on both sides (no stale entries)
    for e in EXCEPTIONS:
        assert parsed[e["pdf"]] >= 1, e
        assert workbook[e["workbook"]] >= 1, e
    adjusted = parsed.copy()
    for e in EXCEPTIONS:
        adjusted[e["pdf"]] -= 1
        adjusted[e["workbook"]] += 1
    adjusted = +adjusted  # drop zero counts
    assert adjusted == workbook, {
        "only_in_pdf": sorted((parsed - workbook).elements()),
        "only_in_workbook": sorted((workbook - parsed).elements()),
    }
    assert len(EXCEPTIONS) == 1


def test_description_is_full_raw_text(stmts):
    jan = stmts["2026-01"]["rows"]
    first = jan[0]
    assert first["description"].startswith("Real Time Transfer Recd From Aba/Contr Bnk-021000021")
    assert "Trn: 1805372007Gc" in first["description"] and "  " not in first["description"]
    aug = {r["amount_cents"]: r for r in stmts["2026-08"]["rows"]}
    assert aug[-189218]["description"] == "08/30 Payment To Chase Card Ending IN 2110"
    assert aug[-1500]["description"] == "Monthly Service Fee"


def _db():
    from tc.db import connect, init_db

    conn = connect(":memory:")
    init_db(conn)
    return conn


def test_import_all_and_idempotent():
    conn = _db()
    total = 0
    for p in PDFS:
        res = import_chase_checking(conn, p)
        assert res["new"] == res["total"] > 0 and res["duplicates"] == 0
        total += res["new"]
    assert total == 32
    again = [import_chase_checking(conn, p) for p in PDFS]
    assert all(r["new"] == 0 and r["duplicates"] == r["total"] for r in again)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 32
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category_id IS NOT NULL").fetchone()[0] == 0
    sb = conn.execute("SELECT as_of, balance_cents FROM statement_balances ORDER BY as_of").fetchall()
    assert len(sb) == 9
    assert sb[-1]["as_of"] == "2026-09-30" and sb[-1]["balance_cents"] == 360352
    # ledger ties to the statement at every statement date
    assert conn.execute("SELECT COUNT(*) FROM v_tie_out WHERE diff_cents <> 0").fetchone()[0] == 0


def test_csv_parser_same_shape(tmp_path):
    f = tmp_path / "Chase5200_Activity.CSV"
    f.write_text(
        "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
        "DEBIT,01/09/2026,ORIG CO NAME:BETTERMENT SEC,-100.00,ACH_DEBIT,2213.65,,\n"
        "DEBIT,01/07/2026,Payment to Chase card,-1715.91,ACCT_XFER,2313.65,,\n"
        "CREDIT,01/07/2026,Real Time Transfer Recd,1500.00,ACH_CREDIT,4029.56,,\n",
        encoding="utf-8",
    )
    s = parse_chase_checking_csv(f)
    assert (s["period_start"], s["period_end"]) == ("2026-01-07", "2026-01-09")
    assert s["opening_cents"] == 252956
    assert s["ending_cents"] == 221365
    assert s["opening_cents"] + sum(r["amount_cents"] for r in s["rows"]) == s["ending_cents"]
    assert [r["amount_cents"] for r in s["rows"]] == [150000, -171591, -10000]
    conn = _db()
    assert import_chase_checking(conn, f) == {"new": 3, "duplicates": 0, "total": 3}
    assert import_chase_checking(conn, f) == {"new": 0, "duplicates": 3, "total": 3}
