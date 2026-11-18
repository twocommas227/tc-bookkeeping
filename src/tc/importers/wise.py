"""Wise USD statement CSV importer (account 'USD account 1430').

Emits raw normalized rows (SPEC section 4). No categorisation and no fee splitting here:
that is the pipeline's job. Money is integer cents (Decimal parsing), dates are ISO.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from tc.db import insert_transaction, to_cents

ACCOUNT = "USD account 1430"
JAR_MOVE_PREFIX = "JAR_MOVE:"
_JAR_RE = re.compile(r"^Moved\s+[\d.,]+\s+[A-Z]{3}\s+(to|from)\s+", re.IGNORECASE)
_ESCAPE_RE = re.compile(r"\\(.)")
_REQUIRED = ("TransferWise ID", "Date", "Date Time", "Amount", "Description", "Running Balance")


def _clean(value: str | None) -> str:
    """Strip markdown-style backslash escapes (DIRECT\\_DEBIT, \\*) and surrounding spaces."""
    return _ESCAPE_RE.sub(r"\1", value or "").strip()


def _money(value: str) -> int | None:
    return to_cents(value) if value else None


def _iso_date(value: str) -> str:
    return datetime.strptime(value, "%d-%m-%Y").strftime("%Y-%m-%d")


def is_jar_move(row: dict) -> bool:
    """True for Wise internal moves between the main balance and a jar (pipeline pairs them)."""
    return (row.get("raw_type") or "").startswith(JAR_MOVE_PREFIX)


def parse_wise_csv(path: str | Path) -> list[dict]:
    """Parse a Wise statement CSV into normalized rows, oldest first.

    Same-day rows are ordered by 'Date Time'; exact ties keep reversed file order
    (the file is newest first). amount_cents is as in the CSV (debits include the fee).
    """
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        fields = {_clean(f) for f in (reader.fieldnames or [])}
        missing = [c for c in _REQUIRED if c not in fields]
        if missing:
            raise ValueError(f"{path}: not a Wise CSV, missing column(s) {missing}")
        raw_rows = [{_clean(k): _clean(v) for k, v in r.items() if k is not None} for r in reader]

    parsed = []
    for n, r in enumerate(raw_rows, start=2):
        try:
            if not r["TransferWise ID"]:
                raise ValueError("empty TransferWise ID")
            amount = _money(r["Amount"])
            if amount is None:
                raise ValueError("empty Amount")
            date = _iso_date(r["Date"])
            stamp = datetime.strptime(r["Date Time"], "%d-%m-%Y %H:%M:%S.%f")
        except (ValueError, KeyError) as exc:
            raise ValueError(f"{path} line {n}: {exc}") from exc

        ttype, tdetail = r.get("Transaction Type", ""), r.get("Transaction Details Type", "")
        raw_type = "/".join(p for p in (ttype, tdetail) if p)
        if _JAR_RE.match(r["Description"]):
            raw_type = JAR_MOVE_PREFIX + raw_type
        payer, payee = r.get("Payer Name", ""), r.get("Payee Name", "")
        merchant = r.get("Merchant", "")  # direct debits (IRS, US CBP) carry the counterparty only here
        parsed.append(
            {
                "date": date,
                "account": ACCOUNT,
                "description": r["Description"],
                "amount_cents": amount,
                "source_ref": r["TransferWise ID"],
                "fee_cents": abs(_money(r.get("Total fees", "")) or 0),
                "running_balance_cents": _money(r["Running Balance"]),
                "payee": (payee if amount < 0 else payer) or payee or payer or merchant or None,
                "raw_type": raw_type or None,
                "datetime": stamp.isoformat(timespec="milliseconds"),
            }
        )

    parsed.reverse()  # file is newest first; stable sort keeps that order for exact ties
    parsed.sort(key=lambda p: p["datetime"])
    return parsed


def import_wise(conn: sqlite3.Connection, path: str | Path) -> dict:
    """Insert raw Wise rows (category NULL, needs_review); skip existing (account_id, source_ref).

    Returns {'new', 'duplicates', 'total'}. All-or-nothing: a failure (e.g. a row dated in a
    locked month) rolls the whole file back.
    """
    rows = parse_wise_csv(path)
    acct = conn.execute("SELECT id FROM accounts WHERE source_label = ?", (ACCOUNT,)).fetchone()
    if acct is None:
        raise ValueError(f"account {ACCOUNT!r} not found in accounts")
    account_id = acct[0]

    new = dup = 0
    conn.execute("SAVEPOINT import_wise")
    try:
        seen: set[str] = set()
        for r in rows:
            exists = r["source_ref"] in seen or conn.execute(
                "SELECT 1 FROM transactions WHERE account_id = ? AND source_ref = ?",
                (account_id, r["source_ref"]),
            ).fetchone()
            if exists:
                dup += 1
                continue
            seen.add(r["source_ref"])
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
    except Exception:
        conn.execute("ROLLBACK TO import_wise")
        conn.execute("RELEASE import_wise")
        raise
    conn.execute("RELEASE import_wise")
    conn.commit()
    return {"new": new, "duplicates": dup, "total": len(rows)}
