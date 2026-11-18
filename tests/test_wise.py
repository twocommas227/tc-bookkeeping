from pathlib import Path

import pytest

from tc.importers.wise import is_jar_move, parse_wise_csv, import_wise

RAW = Path(__file__).parent / "fixtures" / "raw"
FULL = RAW / "wise_full.csv"
OCT = RAW / "wise_2026-10.csv"


def _check_running_balance(rows):
    opening = rows[0]["running_balance_cents"] - rows[0]["amount_cents"]
    bal = opening
    for r in rows:
        bal += r["amount_cents"]
        assert bal == r["running_balance_cents"], (r["source_ref"], r["date"])
    return bal


def test_full_running_balance_reconciles():
    rows = parse_wise_csv(FULL)
    assert len(rows) == 392
    assert [r["datetime"] for r in rows] == sorted(r["datetime"] for r in rows)
    _check_running_balance(rows)


def test_october_running_balance_and_closing():
    rows = parse_wise_csv(OCT)
    assert len(rows) == 8
    assert _check_running_balance(rows) == 1334465
    assert rows[-1]["running_balance_cents"] == 1334465


@pytest.mark.parametrize("path", [FULL, OCT])
def test_reimport_adds_nothing(empty_db, path):
    first = import_wise(empty_db, path)
    assert first["new"] == first["total"] and first["duplicates"] == 0
    second = import_wise(empty_db, path)
    assert second == {"new": 0, "duplicates": second["total"], "total": first["total"]}
    n = empty_db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert n == first["total"]


def test_inserted_rows_are_raw(empty_db):
    import_wise(empty_db, OCT)
    rows = empty_db.execute("SELECT * FROM transactions").fetchall()
    assert len(rows) == 8
    assert all(r["category_id"] is None and r["type"] is None for r in rows)
    assert all(r["review_status"] == "needs_review" for r in rows)
    assert all(r["parent_id"] is None for r in rows)


@pytest.mark.parametrize("path", [FULL, OCT])
def test_no_missing_date_or_amount(path):
    for r in parse_wise_csv(path):
        assert r["date"] and len(r["date"]) == 10 and r["date"][4] == "-"
        assert isinstance(r["amount_cents"], int)
        assert r["source_ref"] and r["account"] == "USD account 1430"
        assert isinstance(r["fee_cents"], int) and r["fee_cents"] >= 0


def test_19_oct_wa_receipt():
    rows = [r for r in parse_wise_csv(OCT) if r["date"] == "2026-10-19"]
    assert len(rows) == 1
    r = rows[0]
    assert r["amount_cents"] == 569989
    assert r["fee_cents"] == 611
    assert "INV-013" in r["description"] and "INV-014" in r["description"]
    assert r["source_ref"] == "TRANSFER-2441134368"
    assert r["raw_type"] == "CREDIT/DEPOSIT"
    assert r["payee"].startswith("W.A. RUBBERMATE")


def test_oct_opening_oct_rows():
    rows = parse_wise_csv(OCT)
    assert rows[0]["source_ref"].startswith("BALANCE_INTEREST-")
    assert rows[0]["amount_cents"] == 4513
    assert rows[-1]["source_ref"] == "TRANSFER-2460784094"
    assert rows[-1]["amount_cents"] == -120000 and rows[-1]["fee_cents"] == 113


def test_jar_moves_flagged():
    rows = parse_wise_csv(FULL)
    jar = [r for r in rows if is_jar_move(r)]
    assert len(jar) == 21
    assert all(r["description"].startswith("Moved") for r in jar)
    assert not any(is_jar_move(r) for r in parse_wise_csv(OCT))


def test_escapes_and_spaces_stripped(tmp_path):
    src = OCT.read_text(encoding="utf-8")
    dirty = src.replace("DIRECT_DEBIT", "DIRECT\\_DEBIT").replace("6******00", "6\\*\\*\\*\\*\\*\\*00  ")
    p = tmp_path / "dirty.csv"
    p.write_text(dirty, encoding="utf-8")
    assert parse_wise_csv(p) == parse_wise_csv(OCT)


def test_direct_debit_payee_falls_back_to_merchant():
    cbp = [r for r in parse_wise_csv(OCT) if r["source_ref"] == "DIRECT_DEBIT-40502598"]
    assert len(cbp) == 1 and cbp[0]["payee"] == "US CBP"
    irs = [r for r in parse_wise_csv(FULL) if r["source_ref"] == "DIRECT_DEBIT-36302000"]
    assert len(irs) == 1 and irs[0]["payee"] == "IRS"
    # Payer/Payee Name still wins when present
    assert [r["payee"] for r in parse_wise_csv(OCT) if r["source_ref"] == "TRANSFER-2459989965"] == ["Joshua F Decker"]
