from datetime import date

from openpyxl import load_workbook

from tc import invoices
from tc.export_xlsx import export_workbook


def _rows(ws):
    return [r for r in ws.iter_rows(min_row=2, values_only=True) if any(v is not None for v in r)]


def test_export_workbook_golden(golden_db, tmp_path):
    path = tmp_path / "Two_Commas_Accounting_2026.xlsx"
    export_workbook(golden_db, path, year=2026)

    wb = load_workbook(path)  # formulas are kept as strings starting with '=', so this checks for them below
    assert wb.sheetnames == ["Transactions", "Invoice Register", "Bank Tie-out", "Profit & Loss", "Balance Sheet"]

    # no formulas anywhere
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    assert not cell.value.startswith("="), (ws.title, cell.coordinate)

    # Transactions
    tx = wb["Transactions"]
    assert [c.value for c in tx[1]] == ["Date", "Description", "Source", "Amount", "Category", "Type", "Notes"]
    assert len(_rows(tx)) == 235
    dates = [r[0] for r in _rows(tx)]
    assert all(isinstance(d, date) or hasattr(d, "year") for d in dates)
    assert dates == sorted(dates)
    assert tx.freeze_panes == "A2"
    assert tx["A1"].font.bold

    # Invoice Register
    inv = wb["Invoice Register"]
    assert [c.value for c in inv[1]][:4] == ["Invoice No.", "Date", "Commission Month", "Type"]
    assert len(_rows(inv)) == len(invoices.list_invoices(golden_db))

    # Profit & Loss
    pl = wb["Profit & Loss"]
    header = [c.value for c in pl[1]]
    assert header[-1] == "YTD"
    ytd_col = header.index("YTD") + 1
    by_label = {r[0].value: r for r in pl.iter_rows(min_row=2) if r[0].value}
    assert by_label["NET INCOME"][ytd_col - 1].value == 18618.29
    assert by_label["Commission Revenue (cash received)"][ytd_col - 1].value == 32186.00
    assert by_label["NET INCOME"][ytd_col - 1].number_format.startswith('"$"')
    assert pl.freeze_panes == "B2"

    # Bank Tie-out exists with account blocks
    assert wb["Bank Tie-out"].max_row > 1


def test_invoice_register_has_wise_fee_and_net_received(golden_db, tmp_path):
    """LOW: Wise Fee / Net Received are filled from the ledger and agree with the workbook's Invoice Register."""
    import csv
    from pathlib import Path

    invoices.backfill_payments(golden_db)
    golden_db.commit()
    path = tmp_path / "x.xlsx"
    export_workbook(golden_db, path, year=2026)
    ws = load_workbook(path)["Invoice Register"]
    header = [c.value for c in ws[1]]
    got = {r[0]: (r[header.index("Wise Fee")], r[header.index("Net Received")]) for r in _rows(ws)}
    want = {}
    for r in csv.DictReader(open(Path(__file__).parent / "fixtures" / "invoices.csv", newline="", encoding="utf-8")):
        if r["Status"] == "Paid":
            name = r["Invoice No."] if r["Invoice No."].startswith("Wise") else f"Firstbase {r['Invoice No.']}"
            want[name] = (float(r["Wise Fee"]), float(r["Net Received"]))
    assert want and want.keys() <= got.keys()
    for name, expected in want.items():
        assert got[name] == expected, name
    assert got["Wise INV-013"] == (6.11, 3009.89) and got["Wise INV-014"] == (0.0, 2690.0)
    assert got["Wise INV-010"] == (None, None)  # pending: blank, not 0


def test_export_balance_sheet_tab_and_clean_notes(golden_db, tmp_path):
    """LOW L6: Balance Sheet tab matches reports.balance_sheet; Notes carry no [rule:..] / [review:..] tags."""
    from tc import reports

    path = tmp_path / "x.xlsx"
    export_workbook(golden_db, path, year=2026, as_of="2026-09-30")
    ws = load_workbook(path)["Balance Sheet"]
    assert [c.value for c in ws[1]] == ["Section", "Line", "As of 2026-09-30"]
    got = {r[1]: r[2] for r in _rows(ws)}
    want = {b["line"]: b["amount_cents"] / 100 for b in reports.balance_sheet(golden_db, "2026-09-30")}
    assert got == want
    assert got["EQUITY (assets - liabilities)"] == 11812.68  # golden: equity at 30 Sep
    golden_db.execute("UPDATE transactions SET notes = '[rule:x][review:no_rule] checked with WA' WHERE id = 1")
    export_workbook(golden_db, path, year=2026)
    notes = [r[6] for r in _rows(load_workbook(path)["Transactions"])]
    assert "checked with WA" in notes and not any(n and "[rule:" in n or n and "[review:" in n for n in notes)
    export_workbook(golden_db, path, year=2026)
    assert [c.value for c in load_workbook(path)["Balance Sheet"][1]][2] == "As of 2026-10-31"  # default: end of the last transaction month
