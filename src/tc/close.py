"""Month close gate, month locks, statement balances and backups (SPEC section 5, rule 8; UI.md sections 2-4).

Money is integer cents, months are 'YYYY-MM', dates are ISO 'YYYY-MM-DD'. Functions that write commit before
returning. The schema triggers (trg_month_lock_requires_tie_out, trg_locked_*) are the backstop; the checks
here give readable messages first.
"""

from __future__ import annotations

import calendar
import csv
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from . import invoices, reports
from .db import cents_to_str
from .errors import CloseBlocked, LockedMonthError, translate_integrity_error

__all__ = [
    "CloseBlocked", "LockedMonthError", "is_locked", "lock_info", "expected_statement_dates",
    "set_statement_balance", "checklist", "close_month", "reopen_month", "backup",
]

BACKUP_DIR_ENV = "TC_BACKUP_DIR"
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_CODE_RE = re.compile(r"^([A-Z]+)_(\d{4})$")

# Page files that fix a failed check (UI.md section 2).
_P_IMPORT = "pages/1_Import.py"
_P_REVIEW = "pages/2_Review_queue.py"
_P_REPORTS = "pages/4_Reports.py"
_P_INVOICES = "pages/5_Invoices.py"


# ---------------------------------------------------------------- helpers
def _check_month(month: str) -> None:
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")


def _month_end(month: str) -> str:
    return f"{month}-{calendar.monthrange(int(month[:4]), int(month[5:7]))[1]:02d}"


def _short(code: str, name: str) -> str:
    """'WISE_1430' -> 'Wise *1430', 'CHASE_5200' -> 'Chase *5200', 'CARD_2110' -> 'card *2110'."""
    m = _CODE_RE.match(code or "")
    if not m:
        return name
    prefix = m.group(1)
    return f"{'card' if prefix == 'CARD' else prefix.capitalize()} *{m.group(2)}"


def _require_reason(reason: str, what: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"{what} requires a reason")
    return reason.strip()


# ---------------------------------------------------------------- lock state
def is_locked(conn: sqlite3.Connection, month: str) -> bool:
    """True when a month_locks row exists for ``month``."""
    return conn.execute("SELECT 1 FROM month_locks WHERE yyyy_mm = ?", (month,)).fetchone() is not None


def lock_info(conn: sqlite3.Connection, month: str) -> dict | None:
    """``{yyyy_mm, locked_at, reason}`` of the lock, or None when the month is open."""
    r = conn.execute("SELECT yyyy_mm, locked_at, reason FROM month_locks WHERE yyyy_mm = ?", (month,)).fetchone()
    return dict(r) if r else None


# ---------------------------------------------------------------- statement balances
def expected_statement_dates(conn: sqlite3.Connection, month: str) -> list[dict]:
    """Per active tie-required account: ``{account_id, code, name, kind, default_as_of}``.

    default_as_of = the account's statement_day in ``month`` (card: the 28th), else the month-end. It is only a
    default for the entry form: the date printed on the statement wins (UI.md section 3).
    """
    _check_month(month)
    end = _month_end(month)
    out = []
    for a in conn.execute(
        """SELECT id, code, name, kind, statement_day FROM accounts
            WHERE tie_required = 1 AND active = 1 AND opening_date <= ? ORDER BY id""",
        (end,),
    ):
        day = min(a["statement_day"], int(end[8:10])) if a["statement_day"] else int(end[8:10])
        out.append({
            "account_id": a["id"], "code": a["code"], "name": a["name"], "kind": a["kind"],
            "default_as_of": f"{month}-{day:02d}",
        })
    return out


def set_statement_balance(
    conn: sqlite3.Connection, account_id: int, as_of: str, balance_cents: int, *, source: str | None = None
) -> None:
    """Upsert a statement balance by (account_id, as_of). ``balance_cents`` is the signed asset value
    (a card liability is negative). Raises LockedMonthError when ``as_of`` is in a locked month."""
    if not isinstance(as_of, str) or not _DATE_RE.match(as_of):
        raise ValueError(f"as_of must be YYYY-MM-DD, got {as_of!r}")
    if isinstance(balance_cents, bool) or not isinstance(balance_cents, int):
        raise ValueError(f"balance_cents must be an integer number of cents, got {balance_cents!r}")
    if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone() is None:
        raise ValueError(f"no account with id {account_id!r}")
    month = as_of[:7]
    if is_locked(conn, month):
        raise LockedMonthError(f"Month {month} is closed and locked", month=month)
    try:
        conn.execute(
            """INSERT INTO statement_balances (account_id, as_of, balance_cents, source) VALUES (?,?,?,?)
               ON CONFLICT (account_id, as_of) DO UPDATE SET
                   balance_cents = excluded.balance_cents,
                   source = COALESCE(excluded.source, statement_balances.source),
                   entered_at = datetime('now')""",
            (account_id, as_of, balance_cents, source),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise translate_integrity_error(exc) from exc


# ---------------------------------------------------------------- checklist
def _item(key: str, step: int, label: str, ok: bool, blocking: bool, detail: str, page: str) -> dict:
    return {"key": key, "step": step, "label": label, "ok": bool(ok), "blocking": blocking, "detail": detail, "page": page}


def _count(conn: sqlite3.Connection, sql: str, *params) -> int:
    return conn.execute(sql, params).fetchone()[0]


def checklist(conn: sqlite3.Connection, month: str) -> list[dict]:
    """The check items of UI.md section 2, in step order 1..7.

    Each item: ``key, step, label, ok, blocking, detail, page``. ``blocking`` items must be ok for
    close_month to lock; ``tail_covered`` is a warning that close_month needs acknowledged.
    """
    _check_month(month)
    end = _month_end(month)
    items: list[dict] = []

    # 1 Import (warning: a month without activity on one account is possible; step 4 is the real gate)
    rows = conn.execute(
        """SELECT a.code, a.name, COUNT(t.id) AS n FROM accounts a
           LEFT JOIN transactions t ON t.account_id = a.id AND substr(t.date,1,7) = ?
           WHERE a.tie_required = 1 AND a.active = 1 GROUP BY a.id ORDER BY a.id""",
        (month,),
    ).fetchall()
    items.append(_item(
        "imported", 1, "Import", all(r["n"] > 0 for r in rows), False,
        " ".join(f"{_short(r['code'], r['name'])}: {r['n']} rows." for r in rows) or "No tie-required accounts.",
        _P_IMPORT,
    ))

    # 2 Auto-categorise
    total = _count(conn, "SELECT COUNT(*) FROM transactions WHERE substr(date,1,7) = ?", month)
    uncat = _count(conn, "SELECT COUNT(*) FROM transactions WHERE substr(date,1,7) = ? AND category_id IS NULL", month)
    items.append(_item(
        "categorized", 2, "Auto-categorise", uncat == 0, True,
        f"{total - uncat} of {total} rows categorised." + (f" {uncat} uncategorised." if uncat else ""),
        _P_REVIEW,
    ))

    # 3 Review unknowns (direct SQL on review_status; no pipeline import)
    review = _count(
        conn, "SELECT COUNT(*) FROM transactions WHERE substr(date,1,7) = ? AND review_status = 'needs_review'", month
    )
    items.append(_item("reviewed", 3, "Review unknowns", review == 0, True, f"{review} rows need review.", _P_REVIEW))

    # 4 Statement balances entered
    missing = conn.execute(
        """SELECT a.code, a.name FROM accounts a
            WHERE a.tie_required = 1 AND a.active = 1
              AND NOT EXISTS (SELECT 1 FROM statement_balances sb
                               WHERE sb.account_id = a.id AND substr(sb.as_of,1,7) = ?)
            ORDER BY a.id""",
        (month,),
    ).fetchall()
    items.append(_item(
        "balances_entered", 4, "Enter statement ending balances", not missing, True,
        "All statement balances entered." if not missing
        else "Missing statement balance: " + ", ".join(_short(r["code"], r["name"]) for r in missing) + ".",
        _P_REPORTS,
    ))

    # 5 Tie-out = 0.00 (pending rows count as not tied)
    ties = reports.tie_out(conn, month)
    parts, ok5 = [], True
    for t in ties:
        label = _short(t["account_code"], t["account_name"])
        if t["diff_cents"] is None:
            parts.append(f"{label} pending.")
            ok5 = False
        else:
            parts.append(f"{label} {cents_to_str(t['diff_cents'])}.")
            ok5 = ok5 and t["diff_cents"] == 0
    if not ties:
        ok5 = False
        parts.append("No tie-required accounts.")
    items.append(_item("tie_out_zero", 5, "Tie-out = 0.00", ok5, True, " ".join(parts), _P_REPORTS))

    # 6 Invoice check
    unbacked = _count(conn, "SELECT COUNT(*) FROM v_revenue_unbacked WHERE substr(date,1,7) = ?", month)
    items.append(_item(
        "revenue_backed", 6, "Revenue backed by commission invoices", unbacked == 0, True,
        "Every Service Revenue row is backed by Commission invoices." if not unbacked
        else f"{unbacked} Service Revenue rows are not fully backed by Commission invoices.",
        _P_INVOICES,
    ))
    mismatch = _count(
        conn,
        """SELECT COUNT(*) FROM v_fee_parent_mismatch m JOIN transactions t ON t.id = m.fee_id
            WHERE substr(t.date,1,7) = ?""",
        month,
    )
    items.append(_item(
        "fees_follow_parent", 6, "Fee rows follow their parent", mismatch == 0, True,
        "Every fee row follows its parent." if not mismatch else f"{mismatch} fee rows do not follow their parent.",
        _P_REVIEW,
    ))
    cc = invoices.commission_check(conn, int(month[:4]))
    items.append(_item(
        "commission_equals_revenue", 6, "Commission paid = revenue", cc["diff_cents"] == 0, False,
        f"Commission paid {cents_to_str(cc['invoice_paid_cents'])} "
        f"{'=' if cc['diff_cents'] == 0 else '<>'} revenue {cents_to_str(cc['revenue_cents'])} ({month[:4]})."
        + ("" if cc["diff_cents"] == 0 else f" Difference {cents_to_str(cc['diff_cents'])}."),
        _P_INVOICES,
    ))
    missing_months = invoices.missing_commission_months(conn, through_month=month)
    pending = [u for u in invoices.unpaid(conn)]
    detail = ("Commission invoice missing: " + ", ".join(missing_months) + ". ") if missing_months else ""
    detail += (
        "Unpaid: " + "; ".join(f"{u['label']} {cents_to_str(u['amount_cents'])}" for u in pending) + "."
        if pending else "Nothing unpaid."
    )
    items.append(_item("unpaid_listed", 6, "Unpaid invoices and missing commission invoices", not missing_months,
                       False, detail, _P_INVOICES))

    # 7 Tail covered (warning; needs a tick)
    uncovered = conn.execute(
        """SELECT a.code, a.name, (SELECT MAX(sb.as_of) FROM statement_balances sb WHERE sb.account_id = a.id
                                    AND sb.as_of <= ?) AS last_as_of
             FROM accounts a
            WHERE a.tie_required = 1 AND a.active = 1
              AND NOT EXISTS (SELECT 1 FROM statement_balances sb WHERE sb.account_id = a.id AND sb.as_of >= ?)
            ORDER BY a.id""",
        (end, end),
    ).fetchall()
    if uncovered:
        detail = ("Statement stops before month-end: "
                  + ", ".join(f"{_short(r['code'], r['name'])} ({r['last_as_of'] or 'no balance'})" for r in uncovered)
                  + f". Rows dated up to {end} on the next statement are refused once {month} is locked; "
                    "import that statement first, or acknowledge.")
    else:
        detail = "Every statement runs to month-end or beyond."
    items.append(_item("tail_covered", 7, "Statement covers the month tail", not uncovered, False, detail, _P_REPORTS))
    return items


# ---------------------------------------------------------------- backup
def _backup_dir() -> Path:
    env = os.environ.get(BACKUP_DIR_ENV)
    return Path(env) if env else Path(__file__).resolve().parents[2] / "backups"


def backup(conn: sqlite3.Connection, reason: str) -> Path:
    """Copy the database with SQLite's online backup API to ``<dir>/tc_YYYYMMDD_HHMMSS_<reason>.db``.

    <dir> is $TC_BACKUP_DIR, else <repo root>/backups. Works for :memory: connections. An existing file is
    never overwritten: the timestamp is bumped by whole seconds until the name is free. Pending uncommitted
    changes on ``conn`` are committed first (the online backup of a connection with an open write transaction
    never finishes).
    """
    if conn.in_transaction:
        conn.commit()
    tag = re.sub(r"[^A-Za-z0-9_-]+", "_", str(reason or "").strip()).strip("_") or "backup"
    folder = _backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().replace(microsecond=0)
    while (path := folder / f"tc_{stamp:%Y%m%d_%H%M%S}_{tag}.db").exists():
        stamp += timedelta(seconds=1)
    with closing(sqlite3.connect(str(path))) as dest:
        conn.backup(dest)
    return path


def _export_csv(conn: sqlite3.Connection, month: str, backup_path: Path) -> Path:
    path = backup_path.with_name(f"{backup_path.stem}_{month}_transactions.csv")
    cols = ["id", "date", "account", "description", "amount_cents", "amount", "category", "type", "payee",
            "source_ref", "parent_id", "fee_cents", "review_status", "notes"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in conn.execute(
            """SELECT t.id, t.date, a.source_label AS account, t.description, t.amount_cents, c.name AS category,
                      t.type, t.payee, t.source_ref, t.parent_id, t.fee_cents, t.review_status, t.notes
                 FROM transactions t JOIN accounts a ON a.id = t.account_id
                 LEFT JOIN categories c ON c.id = t.category_id
                WHERE substr(t.date,1,7) = ? ORDER BY t.date, t.id""",
            (month,),
        ):
            w.writerow([r["id"], r["date"], r["account"], r["description"], r["amount_cents"],
                        cents_to_str(r["amount_cents"]), r["category"], r["type"], r["payee"], r["source_ref"],
                        r["parent_id"], r["fee_cents"], r["review_status"], r["notes"]])
    return path


def _export_invoices_csv(conn: sqlite3.Connection, month: str, backup_path: Path) -> Path:
    """Invoice Register snapshot (all invoices, as at close) next to the backup and the transactions CSV."""
    path = backup_path.with_name(f"{backup_path.stem}_{month}_invoices.csv")
    cols = ["label", "issue_date", "due_date", "commission_month", "type", "boxes", "rate_cents", "rate",
            "amount_cents", "amount", "status", "paid_on", "replaces_label", "notes"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in reversed(invoices.list_invoices(conn)):  # oldest first, like the register
            rate = r["rate_cents"]
            w.writerow([r["label"], r["issue_date"], r["due_date"], r["commission_month"], r["type"], r["boxes"],
                        rate, "" if rate is None else cents_to_str(rate), r["amount_cents"],
                        cents_to_str(r["amount_cents"]), r["status"], r["paid_on"], r["replaces_label"], r["notes"]])
    return path


# ---------------------------------------------------------------- close / reopen
def close_month(conn: sqlite3.Connection, month: str, reason: str, *, acknowledge_tail: bool = False) -> Path:
    """Back up, run the blocking checks, lock the month, export its transactions to CSV next to the backup.

    Returns the backup path. Raises CloseBlocked (``.items`` = failed check items) when a blocking check fails,
    or when the tail warning is unacknowledged (``acknowledge_tail=False`` and tail_covered not ok).
    """
    _check_month(month)
    reason = _require_reason(reason, "closing a month")
    if is_locked(conn, month):
        raise LockedMonthError(f"Month {month} is already closed and locked", month=month)
    path = backup(conn, "close")

    items = checklist(conn, month)
    failed = [i for i in items if not i["ok"] and (i["blocking"] or (i["key"] == "tail_covered" and not acknowledge_tail))]
    if failed:
        lines = [f"step {i['step']} {i['label']}: {i['detail']}" for i in failed]
        raise CloseBlocked(f"Cannot close {month}:\n" + "\n".join(lines), items=failed)

    try:
        conn.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES (?, ?)", (month, reason))
        _export_csv(conn, month, path)
        _export_invoices_csv(conn, month, path)
        conn.commit()
    except sqlite3.IntegrityError as exc:  # trigger backstop
        conn.rollback()
        raise CloseBlocked(f"Cannot close {month}: {exc}") from exc
    except Exception:
        conn.rollback()
        raise
    return path


def reopen_month(conn: sqlite3.Connection, month: str, reason: str) -> Path:
    """Back up, then delete the month lock. A typed reason is required. Returns the backup path."""
    _check_month(month)
    _require_reason(reason, "re-opening a month")
    if not is_locked(conn, month):
        raise ValueError(f"month {month} is not locked")
    path = backup(conn, "reopen")
    conn.execute("DELETE FROM month_locks WHERE yyyy_mm = ?", (month,))
    conn.commit()
    return path
