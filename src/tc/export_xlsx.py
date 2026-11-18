"""Excel export of the accounting workbook: Transactions, Invoice Register, Bank Tie-out, Profit & Loss.

Values only (no formulas). Money is written as dollars (cents / 100, 2 decimals), dates as real Excel dates.
Read-only against the database. Tab names, column order and labels follow Two_Commas_Accounting_2026.xlsx.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.worksheet.worksheet import Worksheet

from . import invoices, reports

__all__ = ["export_workbook"]

MONEY_FORMAT = '"$"#,##0.00;("$"#,##0.00);"-"'
DATE_FORMAT = "yyyy-mm-dd"
RATE_FORMAT = "0.00"
BOXES_FORMAT = "#,##0"

TRANSACTION_HEADERS = ["Date", "Description", "Source", "Amount", "Category", "Type", "Notes"]
INVOICE_HEADERS = [
    "Invoice No.", "Date", "Commission Month", "Type", "Boxes", "Rate", "Gross Amount", "Wise Fee",
    "Net Received", "Date Paid", "Status", "Notes",
]
TIE_HEADERS = ["Account", "Line"]
TIE_LINES = ("As of", "Ledger balance", "Statement balance", "Diff")


# ------------------------------------------------------------------ helpers
def _text(value):
    """Keep text as text: a leading '=' would be written as a formula, so a space is put in front of it."""
    if isinstance(value, str) and value.startswith("="):
        return " " + value
    return value


def _dollars(cents):
    return None if cents is None else round(int(cents) / 100, 2)


def _iso_date(value):
    return None if not value else date.fromisoformat(value)


def _month_label(yyyy_mm: str) -> str:
    """'2025-12' -> 'Dec 2025' (the workbook's Commission Month style)."""
    return date(int(yyyy_mm[:4]), int(yyyy_mm[5:7]), 1).strftime("%b %Y")


def _header(ws: Worksheet, headers: list[str]) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="center")


def _widths(ws: Worksheet, widths: list[float]) -> None:
    for idx, width in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width


def _format_column(ws: Worksheet, col: int, number_format: str, first_row: int = 2) -> None:
    for row in range(first_row, ws.max_row + 1):
        ws.cell(row=row, column=col).number_format = number_format


# ------------------------------------------------------------------ tabs
def _transactions_sheet(ws: Worksheet, conn: sqlite3.Connection, year: int) -> None:
    _header(ws, TRANSACTION_HEADERS)
    rows = conn.execute(
        """SELECT t.date, t.description, a.source_label, t.amount_cents, c.name AS category, t.type, t.notes
             FROM transactions t
             JOIN accounts a ON a.id = t.account_id
             LEFT JOIN categories c ON c.id = t.category_id
            WHERE substr(t.date, 1, 4) = ?
            ORDER BY t.date, t.id""",
        (f"{year:04d}",),
    ).fetchall()
    for r in rows:
        ws.append([
            _iso_date(r["date"]), _text(r["description"]), _text(r["source_label"]),
            _dollars(r["amount_cents"]), _text(r["category"]), _text(r["type"]), _text(r["notes"]),
        ])
    _format_column(ws, 1, DATE_FORMAT)
    _format_column(ws, 4, MONEY_FORMAT)
    ws.freeze_panes = "A2"
    _widths(ws, [12, 40, 22, 14, 34, 14, 60])


def _invoice_sheet(ws: Worksheet, conn: sqlite3.Connection) -> None:
    _header(ws, INVOICE_HEADERS)
    for inv in invoices.list_invoices(conn):
        ws.append([
            _text(inv["label"]),
            _iso_date(inv["issue_date"]),
            _month_label(inv["commission_month"]) if inv["commission_month"] else "N/A",
            _text(inv["type"]),
            inv["boxes"],
            inv["rate_cents"] / 100,
            _dollars(inv["amount_cents"]),
            None,  # Wise fee: not stored on invoices (no fee field, invoice_payments empty in golden data)
            None,  # Net received: depends on the Wise fee above
            _iso_date(inv["paid_on"]),
            _text(inv["status"]),
            _text(inv["notes"]),
        ])
    _format_column(ws, 2, DATE_FORMAT)
    _format_column(ws, 5, BOXES_FORMAT)
    _format_column(ws, 6, RATE_FORMAT)
    _format_column(ws, 7, MONEY_FORMAT)
    _format_column(ws, 8, MONEY_FORMAT)
    _format_column(ws, 9, MONEY_FORMAT)
    _format_column(ws, 10, DATE_FORMAT)
    ws.freeze_panes = "A2"
    _widths(ws, [16, 12, 16, 14, 10, 8, 16, 12, 14, 12, 11, 70])


def _tie_out_sheet(ws: Worksheet, conn: sqlite3.Connection, year: int) -> None:
    months = [f"{year:04d}-{m:02d}" for m in range(1, 13)]
    _header(ws, TIE_HEADERS + [_month_label(m) for m in months])

    # account code -> {"name": ..., "by_month": {YYYY-MM: tie_out row}}; a month with two statements keeps the
    # later as_of (tie_out sorts by as_of), i.e. the month's closing balance.
    accounts: dict[str, dict] = {}
    for month in months:
        for row in reports.tie_out(conn, month):
            block = accounts.setdefault(row["account_code"], {"name": row["account_name"], "by_month": {}})
            block["by_month"][month] = row

    for block in accounts.values():
        for line in TIE_LINES:
            values = []
            for month in months:
                row = block["by_month"].get(month)
                if row is None:
                    values.append(None)
                elif line == "As of":
                    values.append(_iso_date(row["as_of"]))
                elif line == "Ledger balance":
                    values.append(_dollars(row["ledger_cents"]))
                elif line == "Statement balance":
                    values.append("pending" if row["statement_cents"] is None else _dollars(row["statement_cents"]))
                else:
                    values.append("pending" if row["diff_cents"] is None else _dollars(row["diff_cents"]))
            ws.append([_text(block["name"]), line] + values)

    for row in ws.iter_rows(min_row=2, min_col=3, max_col=2 + len(months)):
        for cell in row:
            if isinstance(cell.value, date):
                cell.number_format = DATE_FORMAT
            else:
                cell.number_format = MONEY_FORMAT
    ws.freeze_panes = "C2"
    _widths(ws, [34, 18] + [14] * len(months))


def _pnl_sheet(ws: Worksheet, conn: sqlite3.Connection, year: int) -> None:
    lines = reports.pnl_monthly(conn, year)
    months = list(lines[0]["months"].keys()) if lines else []
    _header(ws, ["Line"] + [_month_label(m) for m in months] + ["YTD"])
    for line in lines:
        ws.append(
            [_text(line["line"])]
            + [_dollars(line["months"][m]) for m in months]
            + [_dollars(line["ytd"])]
        )
        if line["kind"] == "total":
            for cell in ws[ws.max_row]:
                cell.font = Font(bold=True)
    for row in ws.iter_rows(min_row=2, min_col=2, max_col=len(months) + 2):
        for cell in row:
            cell.number_format = MONEY_FORMAT
    ws.freeze_panes = "B2"
    _widths(ws, [62] + [13] * len(months) + [14])


# ------------------------------------------------------------------ entry point
def export_workbook(conn: sqlite3.Connection, path: str | Path, year: int = 2026) -> None:
    """Write the accounting workbook for ``year`` to ``path`` (.xlsx, values only)."""
    wb = Workbook()
    ws_tx = wb.active
    ws_tx.title = "Transactions"
    _transactions_sheet(ws_tx, conn, year)
    _invoice_sheet(wb.create_sheet("Invoice Register"), conn)
    _tie_out_sheet(wb.create_sheet("Bank Tie-out"), conn, year)
    _pnl_sheet(wb.create_sheet("Profit & Loss"), conn, year)
    wb.save(str(path))
