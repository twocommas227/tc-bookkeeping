import csv

import pytest

from tc import reports
from tc.db import to_cents
from tc.fixtures import FIXTURES_DIR

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct"]


def _by_line(rows):
    return {r["line"]: r for r in rows}


def test_pnl_line_order_and_kinds(golden_db):
    rows = reports.pnl_monthly(golden_db, 2026)
    with open(FIXTURES_DIR / "golden_pnl.csv", newline="", encoding="utf-8") as fh:
        golden_lines = [r["line"] for r in csv.DictReader(fh)]
    assert [r["line"] for r in rows] == golden_lines
    assert {r["line"] for r in rows if r["kind"] == "total"} == {
        "Total Revenue", "Total SG&A", "OPERATING INCOME", "NET INCOME",
        "Net income before the non-cash AR adjustment"}
    assert all(set(r["months"]) == {f"2026-{m:02d}" for m in range(1, 13)} for r in rows)


def test_pnl_matches_golden_csv_cell_by_cell(golden_db):
    rows = reports.pnl_monthly(golden_db, 2026)
    with open(FIXTURES_DIR / "golden_pnl.csv", newline="", encoding="utf-8") as fh:
        golden = list(csv.DictReader(fh))
    assert len(golden) == len(rows) == 12
    for g, r in zip(golden, rows):
        assert r["line"] == g["line"]
        for i, mon in enumerate(MONTHS, 1):
            assert r["months"][f"2026-{i:02d}"] == to_cents(g[mon]), (g["line"], mon)
        assert r["ytd"] == to_cents(g["YTD"]), (g["line"], "YTD")
        assert all(isinstance(v, int) for v in r["months"].values()) and isinstance(r["ytd"], int)
        assert r["months"]["2026-11"] == r["months"]["2026-12"] == 0


def test_pnl_golden_headlines(golden_db):
    p = _by_line(reports.pnl_monthly(golden_db, 2026))
    assert p["Commission Revenue (cash received)"]["ytd"] == 3218600
    assert p["NET INCOME"]["ytd"] == 1861829
    assert p["Net income before the non-cash AR adjustment"]["ytd"] == 1705543
    assert p["NET INCOME"]["months"]["2026-10"] == 574163
    assert p["Bank Charges (Wise fees, Chase MSF, FX fees)"]["months"]["2026-10"] == 950
    assert p["Interest Income"]["months"]["2026-10"] == 4513
    assert p["Net income before the non-cash AR adjustment"]["months"]["2026-09"] == -131672


def test_pnl_excludes_draws_reimbursable_transfers(golden_db):
    p = reports.pnl_monthly(golden_db, 2026)
    names = " ".join(r["line"] for r in p)
    for bad in ("Owner", "Reimbursable", "Transfer", "Payable"):
        assert bad not in names
    # total P&L activity from the ledger = net income before adjustment
    s = golden_db.execute(
        "SELECT SUM(amount_cents) FROM transactions t JOIN categories c ON c.id = t.category_id "
        "WHERE c.statement = 'pnl'").fetchone()[0]
    assert s == _by_line(p)["Net income before the non-cash AR adjustment"]["ytd"]


def test_pnl_other_year_is_empty(golden_db):
    p = reports.pnl_monthly(golden_db, 2025)
    assert all(r["ytd"] == 0 for r in p)


def test_owner_draws(golden_db):
    d = reports.owner_draws(golden_db, 2026)
    assert d["ytd"] == 804800
    assert [d[f"2026-{m:02d}"] for m in range(1, 11)] == [
        39300, 39300, 39300, 68600, 89300, 39300, 230400, 39300, 130000, 90000]
    assert d["2026-11"] == d["2026-12"] == 0


def test_tie_out_wise_all_months_tie(golden_db):
    for m in range(1, 11):
        rows = [r for r in reports.tie_out(golden_db, f"2026-{m:02d}") if r["account_code"] == "WISE_1430"]
        assert len(rows) == 1 and rows[0]["diff_cents"] == 0, m
        assert rows[0]["statement_cents"] == rows[0]["ledger_cents"]
        assert rows[0]["opening_cents"] + rows[0]["activity_cents"] == rows[0]["ledger_cents"]
    assert reports.tie_out(golden_db, "2026-09")[0]["statement_cents"] == 1564590
    assert [r for r in reports.tie_out(golden_db, "2026-10") if r["account_code"] == "WISE_1430"][0][
        "statement_cents"] == 1334465


def test_tie_out_jan_to_sep_all_accounts(golden_db):
    for m in range(1, 10):
        rows = reports.tie_out(golden_db, f"2026-{m:02d}")
        assert [r["account_code"] for r in rows] == ["WISE_1430", "CHASE_5200", "CARD_2110"], m
        assert all(r["diff_cents"] == 0 and r["statement_cents"] is not None for r in rows), m
    sep = {r["account_code"]: r for r in reports.tie_out(golden_db, "2026-09")}
    assert sep["CARD_2110"]["statement_cents"] == -145641          # liability stored negative
    assert sep["CARD_2110"]["as_of"] == "2026-09-28"
    assert sep["CHASE_5200"]["statement_cents"] == 360352


def test_tie_out_pending_is_none_not_zero(golden_db):
    rows = {r["account_code"]: r for r in reports.tie_out(golden_db, "2026-10")}
    assert list(rows) == ["WISE_1430", "CHASE_5200", "CARD_2110"]
    for code in ("CHASE_5200", "CARD_2110"):
        assert rows[code]["statement_cents"] is None
        assert rows[code]["diff_cents"] is None
        assert rows[code]["source"] is None
    assert rows["CARD_2110"]["as_of"] == "2026-10-28" and rows["CHASE_5200"]["as_of"] == "2026-10-31"
    assert rows["WISE_1430"]["diff_cents"] == 0


def test_tie_out_two_statement_balances_one_month(golden_db):
    conn = golden_db
    aid = conn.execute("SELECT id FROM accounts WHERE code = 'CHASE_5200'").fetchone()[0]
    conn.execute("INSERT INTO statement_balances (account_id, as_of, balance_cents, source) VALUES (?,?,?,?)",
                 (aid, "2026-09-26", 111, "Chase PDF"))
    rows = [r for r in reports.tie_out(conn, "2026-09") if r["account_code"] == "CHASE_5200"]
    assert [r["as_of"] for r in rows] == ["2026-09-26", "2026-09-30"]
    assert rows[0]["diff_cents"] == rows[0]["ledger_cents"] - 111 and rows[0]["diff_cents"] != 0
    assert rows[1]["diff_cents"] == 0
    assert rows[0]["source"] == "Chase PDF"


def test_tie_out_empty_db_lists_pending_accounts(empty_db):
    rows = reports.tie_out(empty_db, "2026-03")
    assert [r["account_code"] for r in rows] == ["WISE_1430", "CHASE_5200", "CARD_2110"]
    assert all(r["statement_cents"] is None and r["activity_cents"] == 0 for r in rows)


def test_transfers_in_transit(golden_db):
    assert reports.transfers_in_transit(golden_db, "2026-09") == 0
    assert reports.transfers_in_transit(golden_db, "2026-05") == -240000   # 31 May Wise -> 1 Jun Chase timing
    assert reports.transfers_in_transit(golden_db, "2026-10") == -149774
    assert reports.transfers_in_transit(golden_db, "2025-12") == 0


def test_balance_sheet_30_sep_matches_workbook(golden_db):
    bs = reports.balance_sheet(golden_db, "2026-09-30")
    by = {r["line"]: r for r in bs}
    assert [r["line"] for r in bs] == [
        "Wise US Checking *1430", "Wise jars (Taxes / Rent)", "Chase Checking *5200", "Total Cash",
        "Reimbursable Expenses Recoverable (net)", "TOTAL ASSETS",
        "Chase Credit Card *2110", "Loan from Shareholder", "Total Liabilities",
        "EQUITY (assets - liabilities)"]
    assert [r["section"] for r in bs] == ["assets"] * 6 + ["liabilities"] * 3 + ["equity"]
    assert by["Wise US Checking *1430"]["amount_cents"] == 1564590
    assert by["Wise jars (Taxes / Rent)"]["amount_cents"] == 0
    assert by["Chase Checking *5200"]["amount_cents"] == 360352
    assert by["Total Cash"]["amount_cents"] == 1924942
    assert by["Reimbursable Expenses Recoverable (net)"]["amount_cents"] == -189843
    assert by["TOTAL ASSETS"]["amount_cents"] == 1735099
    assert by["Chase Credit Card *2110"]["amount_cents"] == 145641      # liability shown positive
    assert by["Loan from Shareholder"]["amount_cents"] == 408190
    assert by["Total Liabilities"]["amount_cents"] == 553831
    assert by["EQUITY (assets - liabilities)"]["amount_cents"] == 1181268
    assert by["TOTAL ASSETS"]["kind"] == "total" and by["Loan from Shareholder"]["kind"] == "detail"


def test_balance_sheet_equity_is_assets_minus_liabilities_any_date(golden_db):
    for as_of in ("2026-01-31", "2026-06-30", "2026-07-08", "2026-10-31"):
        by = {r["line"]: r["amount_cents"] for r in reports.balance_sheet(golden_db, as_of)}
        assert by["EQUITY (assets - liabilities)"] == by["TOTAL ASSETS"] - by["Total Liabilities"]


def test_balance_sheet_oct_wise_only_ledger(golden_db):
    by = {r["line"]: r["amount_cents"] for r in reports.balance_sheet(golden_db, "2026-10-31")}
    assert by["Wise US Checking *1430"] == 1334465


def test_balance_sheet_opening_adjustments_are_documented(golden_db):
    rows = golden_db.execute(
        "SELECT section, non_cash, reason FROM adjustments WHERE section IN ('asset','liability') ORDER BY id"
    ).fetchall()
    assert [(r["section"], r["non_cash"]) for r in rows] == [("asset", 1), ("liability", 1)]
    assert "plug" in rows[0]["reason"] and "-189843" in rows[0]["reason"]
    assert "Balance Sheet" in rows[1]["reason"]


@pytest.mark.parametrize("fn,args", [
    (reports.pnl_monthly, (2026,)), (reports.owner_draws, (2026,)), (reports.tie_out, ("2026-09",)),
    (reports.transfers_in_transit, ("2026-09",)), (reports.balance_sheet, ("2026-09-30",)),
])
def test_reports_are_read_only(golden_db, fn, args):
    before = golden_db.total_changes
    fn(golden_db, *args)
    assert golden_db.total_changes == before
