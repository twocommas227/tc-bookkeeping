"""Chase Business Complete Checking *5200 importer (PDF statement or Chase CSV download).

Emits normalized rows per docs/SPEC.md section 4. No categorisation here (pipeline job).
Money = integer cents, dates ISO. Statement periods are NOT calendar months; the printed
MM/DD posting dates get their year from the statement period.
"""

from __future__ import annotations

import csv
import hashlib
import re
import sqlite3
import subprocess
from datetime import date, datetime
from pathlib import Path

from tc.db import insert_transaction, to_cents

ACCOUNT = "BUS COMPLETE CHK 5200"
SOURCE = "Chase PDF"

_MONEY = r"-?\$?-?[\d,]+\.\d{2}"
_PERIOD_RE = re.compile(r"([A-Z][a-z]+ \d{1,2}, \d{4}) through ([A-Z][a-z]+ \d{1,2}, \d{4})")
_BEGIN_RE = re.compile(rf"Beginning Balance\s+({_MONEY})\s*$", re.M)
_END_RE = re.compile(rf"Ending Balance\s+(?:\d+\s+)?({_MONEY})\s*$", re.M)
_ROW_RE = re.compile(rf"^\s+(\d{{2}}/\d{{2}})\s+(.*\S)\s{{2,}}({_MONEY})\s*$")
_DATE_START_RE = re.compile(r"^\s+\d{2}/\d{2}\s")
_NOISE_RE = re.compile(
    r"^\s*(Page \d+ of \d+|Account Number:.*|DATE\s+DESCRIPTION\s+AMOUNT|\*(start|end)\*.*)\s*$"
)

# section header (as printed) -> sign of the amounts in it
_SECTIONS = {
    "DEPOSITS AND ADDITIONS": 1,
    "ELECTRONIC WITHDRAWALS": -1,
    "FEES": -1,
    "CHECKS PAID": -1,
    "OTHER WITHDRAWALS": -1,
    "ATM & DEBIT CARD WITHDRAWALS": -1,
}
_SECTION_END = ("DAILY ENDING BALANCE", "IN CASE OF ERRORS")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _long_date(text: str) -> date:
    return datetime.strptime(text, "%B %d, %Y").date()


def _make_ref(period_end: str, index: int, iso: str, cents: int, desc: str) -> str:
    h = hashlib.sha1(f"{iso}|{cents}|{desc}".encode("utf-8")).hexdigest()[:8]
    return f"{period_end}|{index}|{h}"


def _row(period_end, index, iso, cents, desc, raw_type, running=None) -> dict:
    return {
        "date": iso,
        "account": ACCOUNT,
        "description": desc,
        "amount_cents": cents,
        "source_ref": _make_ref(period_end, index, iso, cents, desc),
        "fee_cents": 0,
        "running_balance_cents": running,
        "payee": None,
        "raw_type": raw_type,
    }


def _resolve_date(mmdd: str, start: date, end: date) -> date:
    month, day = (int(x) for x in mmdd.split("/"))
    for year in sorted({start.year, end.year}):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        if start <= d <= end:
            return d
    raise ValueError(f"posting date {mmdd} is outside statement period {start}..{end}")


def _pdf_text(path: Path) -> str:
    out = subprocess.run(
        ["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True
    )
    return out.stdout.replace("\f", "\n")


def parse_chase_checking_pdf(path: str | Path) -> dict:
    """Parse one statement PDF -> {period_start, period_end, opening_cents, ending_cents, rows}."""
    path = Path(path)
    text = _pdf_text(path)

    m = _PERIOD_RE.search(text)
    if not m:
        raise ValueError(f"{path.name}: statement period not found")
    start, end = _long_date(m.group(1)), _long_date(m.group(2))
    mb, me = _BEGIN_RE.search(text), _END_RE.search(text)
    if not mb or not me:
        raise ValueError(f"{path.name}: beginning/ending balance not found in summary")
    opening, ending = to_cents(mb.group(1)), to_cents(me.group(1))

    entries: list[dict] = []  # {date, desc(list), cents, section}
    sign = None  # None = not inside a transaction section
    current: dict | None = None
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        header = re.sub(r"\s*\(continued\)$", "", stripped, flags=re.I)
        if header in _SECTIONS:  # printed in capitals; the summary's "Fees" line does not match
            section, sign, current = header, _SECTIONS[header], None
            continue
        if stripped.startswith(_SECTION_END):
            section, sign, current = None, None, None
            continue
        if sign is None or not stripped:
            continue
        if stripped.startswith("Total "):
            current = None
            continue
        if _NOISE_RE.match(line) or _PERIOD_RE.search(line):
            continue
        rm = _ROW_RE.match(line)
        if rm:
            posted = _resolve_date(rm.group(1), start, end)
            cents = abs(to_cents(rm.group(3))) * sign
            current = {"date": posted.isoformat(), "parts": [rm.group(2)], "cents": cents, "section": section}
            entries.append(current)
        elif _DATE_START_RE.match(line):
            raise ValueError(f"{path.name}: unparsed transaction line: {stripped!r}")
        elif current is not None:
            current["parts"].append(stripped)

    period_end = end.isoformat()
    rows = []
    for i, e in enumerate(entries):
        desc = _clean(" ".join(e["parts"]))
        rows.append(_row(period_end, i, e["date"], e["cents"], desc, e["section"]))
    return {
        "period_start": start.isoformat(),
        "period_end": period_end,
        "opening_cents": opening,
        "ending_cents": ending,
        "rows": rows,
    }


def parse_chase_checking_csv(path: str | Path) -> dict:
    """Parse Chase's CSV download (Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #).

    Same shape as the PDF parser. opening/ending come from the printed running balance
    (None when the Balance column is blank). Rows are indexed oldest first.
    """
    path = Path(path)
    with open(path, newline="", encoding="utf-8-sig") as fh:
        recs = [r for r in csv.DictReader(fh) if (r.get("Posting Date") or "").strip()]
    if not recs:
        raise ValueError(f"{path.name}: no data rows")
    parsed = []
    for r in recs:
        iso = datetime.strptime(r["Posting Date"].strip(), "%m/%d/%Y").date().isoformat()
        bal = (r.get("Balance") or "").strip()
        parsed.append(
            (iso, to_cents(r["Amount"]), _clean(r["Description"] or ""),
             (r.get("Type") or r.get("Details") or "").strip() or None,
             to_cents(bal) if bal else None)
        )
    if parsed[0][0] > parsed[-1][0]:  # Chase downloads are newest first -> make chronological
        parsed.reverse()
    period_start, period_end = parsed[0][0], parsed[-1][0]
    first_bal, last_bal = parsed[0][4], parsed[-1][4]
    rows = [_row(period_end, i, d, c, desc, rt, bal) for i, (d, c, desc, rt, bal) in enumerate(parsed)]
    return {
        "period_start": period_start,
        "period_end": period_end,
        "opening_cents": None if first_bal is None else first_bal - parsed[0][1],
        "ending_cents": last_bal,
        "rows": rows,
    }


def import_chase_checking(conn: sqlite3.Connection, path: str | Path) -> dict:
    """Insert raw rows (category NULL, needs_review); skip existing (account_id, source_ref).

    PDF: refuses a statement whose opening + rows != printed ending, and records the ending
    balance in statement_balances (as_of = period_end) unless one exists. CSV downloads are
    not statements, so no statement balance is written. All-or-nothing via a savepoint.
    Returns {'new', 'duplicates', 'total'}.
    """
    path = Path(path)
    is_pdf = path.suffix.lower() == ".pdf"
    stmt = parse_chase_checking_pdf(path) if is_pdf else parse_chase_checking_csv(path)
    rows = stmt["rows"]
    if is_pdf:
        total = stmt["opening_cents"] + sum(r["amount_cents"] for r in rows)
        if total != stmt["ending_cents"]:
            raise ValueError(
                f"{path.name}: opening + rows = {total} but printed ending balance = {stmt['ending_cents']}"
            )

    acct = conn.execute("SELECT id FROM accounts WHERE source_label = ?", (ACCOUNT,)).fetchone()
    if acct is None:
        raise ValueError(f"account {ACCOUNT!r} not found in accounts")
    account_id = acct[0]

    new = dup = 0
    conn.execute("SAVEPOINT import_chase_checking")
    try:
        for r in rows:
            if conn.execute(
                "SELECT 1 FROM transactions WHERE account_id = ? AND source_ref = ?",
                (account_id, r["source_ref"]),
            ).fetchone():
                dup += 1
                continue
            insert_transaction(
                conn,
                date=r["date"],
                account_id=account_id,
                description=r["description"],
                amount_cents=r["amount_cents"],
                source_ref=r["source_ref"],
                fee_cents=r["fee_cents"],
                payee=r["payee"],
                raw_type=r["raw_type"],
                running_balance_cents=r["running_balance_cents"],
                review_status="needs_review",
            )
            new += 1
        if is_pdf and not conn.execute(
            "SELECT 1 FROM statement_balances WHERE account_id = ? AND as_of = ?",
            (account_id, stmt["period_end"]),
        ).fetchone():
            conn.execute(
                "INSERT INTO statement_balances (account_id, as_of, balance_cents, source) VALUES (?,?,?,?)",
                (account_id, stmt["period_end"], stmt["ending_cents"], SOURCE),
            )
    except Exception:
        conn.execute("ROLLBACK TO import_chase_checking")
        conn.execute("RELEASE import_chase_checking")
        raise
    conn.execute("RELEASE import_chase_checking")
    conn.commit()
    return {"new": new, "duplicates": dup, "total": len(rows)}
