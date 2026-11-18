"""The golden numbers of docs/SPEC.md section 8 (G1-G11), asserted directly on the seeded workbook data."""

import shutil
import sqlite3

import pytest

from tc import close, invoices, reports


@pytest.fixture
def gdb(golden_db):
    invoices.backfill_payments(golden_db)
    golden_db.commit()
    return golden_db


def _tie(conn, month):
    return {t["account_code"]: t for t in reports.tie_out(conn, month)}


def test_golden_row_count(gdb):
    assert gdb.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 235  # G1


def test_golden_tie_outs(gdb):
    wise = {"2026-01": 1100178, "2026-02": 1073276, "2026-03": 1522865, "2026-04": 781632, "2026-05": 1341058,
            "2026-06": 3635952, "2026-07": 1793278, "2026-08": 1745508, "2026-09": 1564590, "2026-10": 1334465}
    chase = [219578, 209578, 208345, 493729, 193258, 349992, 350593, 250465, 360352]
    card = [146787, 261233, 149308, 288971, 161153, 149286, 149297, 189218, 145641]
    for m, want in wise.items():  # G2
        t = _tie(gdb, m)["WISE_1430"]
        assert (t["statement_cents"], t["diff_cents"]) == (want, 0), m
    for m in range(1, 10):  # G3, G4: Jan-Sep, every account ties to 0.00
        tie = _tie(gdb, f"2026-{m:02d}")
        assert all(t["diff_cents"] == 0 for t in tie.values()), m
        assert tie["CHASE_5200"]["statement_cents"] == chase[m - 1], m
        assert tie["CARD_2110"]["statement_cents"] == -card[m - 1], m
    assert _tie(gdb, "2026-10")["CHASE_5200"]["statement_cents"] is None  # October is pending for Chase and the card
    assert _tie(gdb, "2026-10")["CARD_2110"]["statement_cents"] is None


def _pnl(conn):
    return {r["line"]: r for r in reports.pnl_monthly(conn, 2026)}


def test_golden_ytd_pnl(gdb):
    p = _pnl(gdb)
    assert p["Commission Revenue (cash received)"]["ytd"] == 3218600  # G5
    assert p["NET INCOME"]["ytd"] == 1861829  # G6
    assert p["Net income before the non-cash AR adjustment"]["ytd"] == 1705543  # G7


def test_golden_owner_draws(gdb):
    assert reports.owner_draws(gdb, 2026)["ytd"] == 804800  # G8


def test_golden_oct_2026(gdb):
    p = _pnl(gdb)
    m = "2026-10"  # G9
    assert p["Commission Revenue (cash received)"]["months"][m] == 570600
    assert p["Bank Charges (Wise fees, Chase MSF, FX fees)"]["months"][m] == 950
    assert p["Interest Income"]["months"][m] == 4513
    assert p["NET INCOME"]["months"][m] == 574163


def test_golden_sep_2026(gdb):
    assert _pnl(gdb)["Net income before the non-cash AR adjustment"]["months"]["2026-09"] == -131672  # G10


def test_golden_commission_check(gdb):
    cc = invoices.commission_check(gdb, 2026)  # G11
    assert (cc["invoice_paid_cents"], cc["revenue_cents"], cc["diff_cents"], cc["unbacked"]) == (3218600, 3218600, 0, [])


def test_backup_roundtrip(gdb, tmp_path, monkeypatch):
    """A restore is a file copy: the restored database reproduces the golden numbers."""
    monkeypatch.setenv("TC_BACKUP_DIR", str(tmp_path))
    snap = close.backup(gdb, "roundtrip")
    restored = tmp_path / "restored.db"
    shutil.copy(snap, restored)
    conn = sqlite3.connect(str(restored))
    conn.row_factory = sqlite3.Row
    try:
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 235
        p = {r["line"]: r for r in reports.pnl_monthly(conn, 2026)}
        assert p["NET INCOME"]["ytd"] == 1861829
        assert p["Commission Revenue (cash received)"]["ytd"] == 3218600
    finally:
        conn.close()


def test_invoice_label_always_has_source(gdb):
    labels = [r[0] for r in gdb.execute("SELECT label FROM v_invoice_labels")]
    assert len(labels) == 18 and all(l.startswith(("Wise ", "Firstbase ")) for l in labels)
    assert "Wise INV-014" in labels and "Firstbase INV-000014" in labels  # the two series never collide
    assert len(set(labels)) == len(labels)
