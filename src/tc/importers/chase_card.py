"""Chase Ink card *2110 statement importer.

Parses the "ACCOUNT SUMMARY" and "ACCOUNT ACTIVITY" blocks of a Chase card PDF (text layer, via
``pdftotext -layout``) into normalized rows (docs/SPEC.md section 4) and loads them, uncategorised.

Signs: the statement prints purchases as +, payments / credits as -. This module flips them to the
ledger convention: charge = negative, payment / refund = positive. Balances are signed asset values,
so the card liability is NEGATIVE (statement "New Balance $1,456.41" -> -145641) and, for every cycle,
``previous_balance_cents + sum(row amount_cents) == new_balance_cents``.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
from datetime import date
from pathlib import Path

from tc.db import insert_transaction, to_cents

ACCOUNT_LABEL = "J. DECKER 2110"
BALANCE_SOURCE = "Chase PDF"

_MONEY = r"-?\$?[\d,]+\.\d{2}"
_TX_LINE = re.compile(rf"^\s*(\d{{2}})/(\d{{2}})\s{{2,}}(\S.*?)\s{{2,}}(-?[\d,]+\.\d{{2}})\s*$")
_CYCLE = re.compile(r"Opening/Closing Date\s+(\d{2})/(\d{2})/(\d{2})\s*-\s*(\d{2})/(\d{2})/(\d{2})")
_SUMMARY_LABELS = {
    "Previous Balance": "previous",
    "Payment, Credits": "payments_credits",
    "Purchases": "purchases",
    "Cash Advances": "cash_advances",
    "Balance Transfers": "balance_transfers",
    "Fees Charged": "fees",
    "Interest Charged": "interest",
    "New Balance": "new",
}


def _text(path: Path) -> str:
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", str(path), "-"],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError as exc:  # pragma: no cover
        raise RuntimeError("pdftotext (poppler) is required to read Chase PDFs") from exc
    return out.stdout


def _signed_money(token: str) -> int:
    """Statement money token ('-$1,715.91', '+$1,693.86', '$0.00') -> cents as printed."""
    neg = token.strip().startswith("-")
    cents = to_cents(token.replace("+", "").replace("-", ""))
    return -cents if neg else cents


def _resolve_date(month: int, day: int, cycle_end: date) -> date:
    """Statement dates are MM/DD. The year is the closing year unless that lands after the close
    (Dec dates in a January cycle belong to the previous year)."""
    d = date(cycle_end.year, month, day)
    if d > cycle_end:
        d = date(cycle_end.year - 1, month, day)
    if (cycle_end - d).days > 62:
        raise ValueError(f"transaction date {month:02d}/{day:02d} is not near cycle end {cycle_end}")
    return d


def _raw_type(description: str, amount_cents: int) -> str:
    up = description.upper()
    if up.startswith("PAYMENT"):
        return "PAYMENT"
    if "FEE" in up and amount_cents < 0:
        return "FEE"
    if "INTEREST" in up and amount_cents < 0:
        return "INTEREST"
    return "CREDIT" if amount_cents > 0 else "PURCHASE"


def parse_chase_card_pdf(path: str | Path) -> dict:
    """Parse one Chase card statement PDF.

    Returns dict(cycle_start, cycle_end, previous_balance_cents, new_balance_cents, summary, rows).
    Balances are signed asset values (liability negative). ``summary`` holds the printed ACCOUNT
    SUMMARY buckets in magnitude-as-printed cents (statement sign) for cross-checks.
    """
    path = Path(path)
    text = _text(path)

    m = _CYCLE.search(text)
    if not m:
        raise ValueError(f"{path.name}: no 'Opening/Closing Date' line found")
    sm, sd, sy, em, ed, ey = (int(x) for x in m.groups())
    cycle_start = date(2000 + sy, sm, sd)
    cycle_end = date(2000 + ey, em, ed)

    summary: dict[str, int] = {}
    for line in text.splitlines():
        for label, key in _SUMMARY_LABELS.items():
            mm = re.match(rf"^\s*{re.escape(label)}\s+({_MONEY.replace('-?', '[-+]?')})\s*$", line)
            if mm and key not in summary:
                summary[key] = _signed_money(mm.group(1))
    missing = {"previous", "new"} - summary.keys()
    if missing:
        raise ValueError(f"{path.name}: account summary missing {sorted(missing)}")

    start = text.find("ACCOUNT ACTIVITY")
    if start < 0:
        raise ValueError(f"{path.name}: no 'ACCOUNT ACTIVITY' block found")
    end = text.find("INTEREST CHARGES", start)
    block = text[start:end if end > 0 else None]

    rows = []
    for idx, line in enumerate(l for l in block.splitlines() if _TX_LINE.match(l)):
        mm = _TX_LINE.match(line)
        month, day, desc, amt = int(mm.group(1)), int(mm.group(2)), mm.group(3).strip(), mm.group(4)
        d = _resolve_date(month, day, cycle_end).isoformat()
        amount_cents = -to_cents(amt)  # statement: purchase +, payment -  ->  ledger: charge -, payment +
        digest = hashlib.sha1(f"{d}{amount_cents}{desc}".encode("utf-8")).hexdigest()[:8]
        rows.append({
            "date": d,
            "account": ACCOUNT_LABEL,
            "description": desc,
            "amount_cents": amount_cents,
            "source_ref": f"{cycle_end.isoformat()}|{idx}|{digest}",
            "fee_cents": 0,
            "running_balance_cents": None,
            "payee": None,
            "raw_type": _raw_type(desc, amount_cents),
        })

    return {
        "cycle_start": cycle_start.isoformat(),
        "cycle_end": cycle_end.isoformat(),
        "previous_balance_cents": -summary["previous"],
        "new_balance_cents": -summary["new"],
        "summary": summary,
        "rows": rows,
    }


def import_chase_card(conn: sqlite3.Connection, path: str | Path) -> dict:
    """Load one statement: raw rows (category NULL) plus the closing balance. Idempotent.

    Returns dict(new, duplicates, total). Raises ValueError (nothing written) if the parsed rows do
    not reconcile to the statement's previous/new balance.
    """
    parsed = parse_chase_card_pdf(path)
    rows = parsed["rows"]
    if parsed["previous_balance_cents"] + sum(r["amount_cents"] for r in rows) != parsed["new_balance_cents"]:
        raise ValueError(f"{Path(path).name}: parsed rows do not reconcile to the statement new balance")

    acct = conn.execute("SELECT id FROM accounts WHERE source_label = ?", (ACCOUNT_LABEL,)).fetchone()
    if acct is None:
        raise ValueError(f"account {ACCOUNT_LABEL!r} not found")
    account_id = acct["id"]

    new = duplicates = 0
    for r in rows:
        exists = conn.execute(
            "SELECT 1 FROM transactions WHERE account_id = ? AND source_ref = ?",
            (account_id, r["source_ref"]),
        ).fetchone()
        if exists:
            duplicates += 1
            continue
        insert_transaction(
            conn,
            date=r["date"], account_id=account_id, description=r["description"],
            amount_cents=r["amount_cents"], source_ref=r["source_ref"], fee_cents=r["fee_cents"],
            payee=r["payee"], raw_type=r["raw_type"], running_balance_cents=r["running_balance_cents"],
        )
        new += 1

    conn.execute(
        "INSERT OR IGNORE INTO statement_balances (account_id, as_of, balance_cents, source) VALUES (?, ?, ?, ?)",
        (account_id, parsed["cycle_end"], parsed["new_balance_cents"], BALANCE_SOURCE),
    )
    conn.commit()
    return {"new": new, "duplicates": duplicates, "total": len(rows)}
