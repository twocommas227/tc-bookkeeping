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
    assert wb.sheetnames == ["Transactions", "Invoice Register", "Bank Tie-out", "Profit & Loss"]

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
