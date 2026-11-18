"""Invoice register, WA receipt matching and the commission check (SPEC rules 1 and 2).

Money is integer cents, dates are ISO 'YYYY-MM-DD', months 'YYYY-MM'.
Every function that writes commits before returning; on any failure the module's own writes are
rolled back (SAVEPOINT) and the error is raised as ValueError / LockedMonthError.

Invoice numbers keep their source label: ``Wise INV-013`` and ``Firstbase INV-000013`` are different
invoices. Matching a bank reference to an invoice is on the literal number string, never on the integer.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import date as _date
from typing import Iterator, Sequence

from .db import insert_transaction
from .errors import LockedMonthError, translate_integrity_error

__all__ = [
    "list_invoices", "unpaid", "add_invoice", "void_invoice", "unmatched_receipts", "match_payment",
    "split_receipt", "commission_check", "missing_commission_months", "parse_invoice_refs",
    "propose_match", "backfill_payments", "flag_commission_mismatches",
]

SERVICE_REVENUE = "Service Revenue"
REIMBURSABLE = "Reimbursable Expenses Recoverable"
COMMISSION_RATE_CENTS = 50
MISSING_COMMISSION_GRACE_DAYS = 45

_INV_REF_RE = re.compile(r"INV[.\s]*-\s*(\d+)", re.IGNORECASE)
_WA_RE = re.compile(r"W\.?\s*A\.?\s*RUBBERMATE", re.IGNORECASE)
_REVIEW_TAG_RE = re.compile(r"\[review:[^\]]*\]\s*")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


# ------------------------------------------------------------------ helpers
def _translate(exc: sqlite3.IntegrityError) -> Exception:
    """LockedMonthError for a month-lock trigger, ValueError for every other integrity failure."""
    err = translate_integrity_error(exc)
    if isinstance(err, LockedMonthError):
        return err
    return ValueError(str(exc))


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """All-or-nothing block that commits on success and leaves earlier uncommitted work alone on failure."""
    conn.execute("SAVEPOINT tc_invoices")
    try:
        yield
    except BaseException as exc:
        conn.execute("ROLLBACK TO tc_invoices")
        conn.execute("RELEASE tc_invoices")
        if isinstance(exc, sqlite3.IntegrityError):
            raise _translate(exc) from exc
        raise
    else:
        conn.execute("RELEASE tc_invoices")
        conn.commit()


def _check_date(value: str | None, name: str, *, required: bool = True) -> None:
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        raise ValueError(f"{name} must be an ISO date YYYY-MM-DD, got {value!r}")
    try:
        _date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} is not a valid date: {value!r}") from None


def _check_locked(conn: sqlite3.Connection, tx_date: str) -> None:
    month = tx_date[:7]
    if conn.execute("SELECT 1 FROM month_locks WHERE yyyy_mm = ?", (month,)).fetchone():
        raise LockedMonthError(f"Month {month} is closed and locked", month=month)


def _get_tx(conn: sqlite3.Connection, transaction_id: int) -> sqlite3.Row:
    row = conn.execute(
        """SELECT t.*, c.name AS category_name
             FROM transactions t LEFT JOIN categories c ON c.id = t.category_id
            WHERE t.id = ?""",
        (transaction_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"transaction {transaction_id} does not exist")
    return row


def _category_id(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"category {name!r} is not seeded")
    return row["id"]


def _label(conn: sqlite3.Connection, invoice_id: int) -> str:
    row = conn.execute("SELECT label FROM v_invoice_labels WHERE id = ?", (invoice_id,)).fetchone()
    return row["label"] if row else f"invoice {invoice_id}"


def _target_for(types: set[str]) -> tuple[str, str]:
    """(category, type) the ONE RULE assigns to a receipt that settles invoices of these types."""
    if types == {"Commission"}:
        return SERVICE_REVENUE, "Income"
    return REIMBURSABLE, "Reimbursable"


# ------------------------------------------------------------------ register
def list_invoices(conn: sqlite3.Connection, *, status: str | None = None, type: str | None = None) -> list[dict]:
    """Invoice register, newest first. `paid_on` is the ledger date of the paying receipt (SPEC section 9, point 1)."""
    where, params = [], []
    if status is not None:
        where.append("i.status = ?")
        params.append(status)
    if type is not None:
        where.append("i.type = ?")
        params.append(type)
    sql = """
        SELECT i.id, l.label, i.issue_date, i.due_date, i.commission_month, i.type, i.boxes, i.rate_cents,
               i.amount_cents, i.status,
               (SELECT MAX(t.date) FROM invoice_payments ip JOIN transactions t ON t.id = ip.transaction_id
                 WHERE ip.invoice_id = i.id) AS paid_on,
               (SELECT ip.transaction_id FROM invoice_payments ip JOIN transactions t ON t.id = ip.transaction_id
                 WHERE ip.invoice_id = i.id ORDER BY t.date DESC, ip.id DESC LIMIT 1) AS paid_by_transaction_id,
               rl.label AS replaces_label, i.notes
          FROM invoices i
          JOIN v_invoice_labels l ON l.id = i.id
          LEFT JOIN v_invoice_labels rl ON rl.id = i.replaces_invoice_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.issue_date DESC, i.id DESC"
    return [dict(r) for r in conn.execute(sql, params)]


def unpaid(conn: sqlite3.Connection, *, as_of: str | None = None) -> list[dict]:
    """Pending invoices, oldest first. days_open = as_of (default today) minus issue_date."""
    if as_of is None:
        as_of = _date.today().isoformat()
    _check_date(as_of, "as_of")
    ref = _date.fromisoformat(as_of)
    rows = conn.execute(
        """SELECT i.id, l.label, i.type, i.issue_date, i.due_date, i.amount_cents
             FROM invoices i JOIN v_invoice_labels l ON l.id = i.id
            WHERE i.status = 'Pending'
            ORDER BY i.issue_date, i.id"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["days_open"] = (ref - _date.fromisoformat(r["issue_date"])).days
        out.append(d)
    return out


def add_invoice(
    conn: sqlite3.Connection,
    *,
    number: str,
    source_label: str,
    issue_date: str,
    type: str,
    amount_cents: int,
    boxes: int = 0,
    rate_cents: int = 0,
    commission_month: str | None = None,
    due_date: str | None = None,
    replaces_invoice_id: int | None = None,
    notes: str | None = None,
) -> int:
    """Insert a Pending invoice and return its id. Raises ValueError on a duplicate or a CHECK failure."""
    number = (number or "").strip()
    source_label = (source_label or "").strip()
    if not number:
        raise ValueError("invoice number is required")
    if not source_label:
        raise ValueError("source_label is required (numbers overlap between sources)")
    if type not in ("Commission", "Reimbursable"):
        raise ValueError(f"type must be 'Commission' or 'Reimbursable', got {type!r}")
    _check_date(issue_date, "issue_date")
    _check_date(due_date, "due_date", required=False)
    for name, value in (("amount_cents", amount_cents), ("boxes", boxes), ("rate_cents", rate_cents)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer number of cents / boxes")
    if commission_month is not None and not _ISO_MONTH_RE.match(commission_month):
        raise ValueError(f"commission_month must be YYYY-MM, got {commission_month!r}")

    if type == "Commission":
        if rate_cents in (0, COMMISSION_RATE_CENTS):
            rate_cents = COMMISSION_RATE_CENTS
        else:
            raise ValueError(f"a Commission invoice is always {COMMISSION_RATE_CENTS} cents per box")
        if boxes <= 0:
            raise ValueError("a Commission invoice needs boxes > 0")
        if commission_month is None:
            raise ValueError("a Commission invoice needs a commission_month")
        if amount_cents != boxes * rate_cents:
            raise ValueError(
                f"Commission amount must be boxes x rate: {boxes} x {rate_cents} = {boxes * rate_cents}, got {amount_cents}"
            )
    else:
        if amount_cents <= 0:
            raise ValueError("a Reimbursable invoice needs amount_cents > 0")
    if amount_cents < 0:
        raise ValueError("amount_cents cannot be negative")

    if replaces_invoice_id is not None:
        old = conn.execute("SELECT type FROM invoices WHERE id = ?", (replaces_invoice_id,)).fetchone()
        if old is None:
            raise ValueError(f"replaces_invoice_id {replaces_invoice_id} does not exist")
        if old["type"] != "Voided":
            raise ValueError("only a voided invoice can be replaced")

    if conn.execute(
        "SELECT 1 FROM invoices WHERE source_label = ? AND number = ?", (source_label, number)
    ).fetchone():
        raise ValueError(f"{source_label} {number} already exists")

    with _atomic(conn):
        cur = conn.execute(
            """INSERT INTO invoices (number, source_label, issue_date, due_date, commission_month, type,
                                     boxes, rate_cents, amount_cents, status, replaces_invoice_id, notes)
               VALUES (?,?,?,?,?,?,?,?,?,'Pending',?,?)""",
            (number, source_label, issue_date, due_date, commission_month, type,
             boxes, rate_cents, amount_cents, replaces_invoice_id, notes),
        )
        new_id = cur.lastrowid
    return new_id


def void_invoice(conn: sqlite3.Connection, invoice_id: int, reason: str) -> None:
    """Void a Pending invoice with no payments. The reason (and the original type) is appended to notes."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("a reason is required to void an invoice")
    inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if inv is None:
        raise ValueError(f"invoice {invoice_id} does not exist")
    if inv["status"] != "Pending":
        raise ValueError(f"only a Pending invoice can be voided ({_label(conn, invoice_id)} is {inv['status']})")
    if conn.execute("SELECT 1 FROM invoice_payments WHERE invoice_id = ?", (invoice_id,)).fetchone():
        raise ValueError(f"{_label(conn, invoice_id)} has payments and cannot be voided")
    line = f"Voided (was {inv['type']}): {reason}"
    notes = f"{inv['notes']} | {line}" if inv["notes"] else line
    with _atomic(conn):
        conn.execute(
            "UPDATE invoices SET type = 'Voided', status = 'Voided', notes = ? WHERE id = ?", (notes, invoice_id)
        )


# --------------------------------------------------------- WA receipt finder
def _needs_invoice_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM rules WHERE active = 1 AND action = 'needs_invoice' AND pattern <> '.*'"
    ).fetchall()


def _rule_matches(rule: sqlite3.Row, tx: sqlite3.Row) -> bool:
    if rule["account_id"] is not None and rule["account_id"] != tx["account_id"]:
        return False
    if rule["amount_cents"] is not None and rule["amount_cents"] != tx["amount_cents"]:
        return False
    pat = rule["pattern"]
    for text in (tx["description"], tx["payee"]):
        if not text:
            continue
        if rule["match_type"] == "regex":
            try:
                if re.search(pat, text, re.IGNORECASE):
                    return True
            except re.error:
                return False
        elif rule["match_type"] == "exact":
            if text.strip().lower() == pat.strip().lower():
                return True
        elif pat.lower() in text.lower():
            return True
    return False


def _is_wa_receipt(tx: sqlite3.Row, rules: Sequence[sqlite3.Row]) -> bool:
    if tx["amount_cents"] <= 0 or tx["parent_id"] is not None:
        return False
    if _WA_RE.search(tx["description"] or "") or _WA_RE.search(tx["payee"] or ""):
        return True
    return any(_rule_matches(r, tx) for r in rules)


def _wa_receipts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rules = _needs_invoice_rules(conn)
    rows = conn.execute(
        "SELECT * FROM transactions WHERE amount_cents > 0 AND parent_id IS NULL ORDER BY date, id"
    ).fetchall()
    return [r for r in rows if _is_wa_receipt(r, rules)]


def _is_locked(conn: sqlite3.Connection, tx_date: str) -> bool:
    return conn.execute("SELECT 1 FROM month_locks WHERE yyyy_mm = ?", (tx_date[:7],)).fetchone() is not None


def unmatched_receipts(conn: sqlite3.Connection, *, month: str | None = None) -> list[dict]:
    """WA receipts not fully allocated to invoices. Receipts in a locked month are not offered."""
    if month is not None and not _ISO_MONTH_RE.match(month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    paid = {
        r["transaction_id"]: r["paid"]
        for r in conn.execute("SELECT transaction_id, SUM(amount_cents) AS paid FROM invoice_payments GROUP BY transaction_id")
    }
    accounts = {r["id"]: r["source_label"] for r in conn.execute("SELECT id, source_label FROM accounts")}
    out = []
    for tx in _wa_receipts(conn):
        if month is not None and tx["date"][:7] != month:
            continue
        if _is_locked(conn, tx["date"]):
            continue
        left = tx["amount_cents"] - paid.get(tx["id"], 0)
        if left <= 0:
            continue
        out.append({
            "id": tx["id"],
            "date": tx["date"],
            "account": accounts[tx["account_id"]],
            "description": tx["description"],
            "amount_cents": tx["amount_cents"],
            "unallocated_cents": left,
        })
    return out


# ------------------------------------------------------------------ matching
def parse_invoice_refs(text: str) -> list[str]:
    """Invoice references in a bank reference, unique, in order of appearance, digits kept as written.

    'INV-013 ... INV-014' -> ['INV-013', 'INV-014']; 'INV.-000016' -> ['INV-000016'];
    the typo 'INV-0000157' is returned as-is (it will simply match no invoice).
    """
    seen: list[str] = []
    for digits in _INV_REF_RE.findall(text or ""):
        ref = f"INV-{digits}"
        if ref not in seen:
            seen.append(ref)
    return seen


def _open_invoices(conn: sqlite3.Connection, tx: sqlite3.Row) -> list[sqlite3.Row]:
    """Invoices a receipt could still settle: not voided, issued on or before the receipt date, and not
    already paid by another transaction (a legacy 'Paid' status without payment rows counts as open)."""
    return conn.execute(
        """SELECT i.* FROM invoices i
            WHERE i.type <> 'Voided' AND i.issue_date <= ?
              AND NOT EXISTS (SELECT 1 FROM invoice_payments ip
                               WHERE ip.invoice_id = i.id AND ip.transaction_id <> ?)
            ORDER BY i.issue_date, i.id""",
        (tx["date"], tx["id"]),
    ).fetchall()


def propose_match(conn: sqlite3.Connection, transaction_id: int) -> dict:
    """RULES.md Stage 7 for one WA receipt. Does not write.

    Returns {'kind': 'commission'|'reimbursable'|'mixed'|'amount_only'|'none',
             'allocations': [(invoice_id, cents)], 'category': name or None, 'type': str or None,
             'review_reasons': [reason codes]}.
    """
    tx = _get_tx(conn, transaction_id)
    result = {"kind": "none", "allocations": [], "category": None, "type": None, "review_reasons": []}
    gross = tx["amount_cents"]
    if gross <= 0:
        return result

    open_inv = _open_invoices(conn, tx)
    tokens = parse_invoice_refs(tx["description"] or "")

    by_ref = [i for t in tokens for i in open_inv if i["number"] == t]
    matched: list[sqlite3.Row] = []
    how = None
    if by_ref and sum(i["amount_cents"] for i in by_ref) == gross:
        matched, how = by_ref, "reference"
    else:
        by_amt = [i for i in open_inv if i["amount_cents"] == gross]
        if len(by_amt) == 1:
            matched, how = by_amt, "amount"

    if not matched:
        voided = conn.execute(
            f"SELECT 1 FROM invoices WHERE type = 'Voided' AND number IN ({','.join('?' for _ in tokens)})",
            tokens,
        ).fetchone() if tokens else None
        result["category"], result["type"] = REIMBURSABLE, "Reimbursable"
        result["review_reasons"] = ["invoice_amount_mismatch" if by_ref else "no_invoice_match"]
        if voided:
            result["review_reasons"].append("voided_invoice_referenced")
        return result

    types = {i["type"] for i in matched}
    category, tx_type = _target_for(types)
    reasons: list[str] = []
    if how == "amount":
        kind = "amount_only"
        reasons.append("invoice_matched_by_amount")
    elif types == {"Commission"}:
        kind = "commission"
    elif "Commission" in types:
        kind = "mixed"
        reasons.append("mixed_invoice_wire")
    else:
        kind = "reimbursable"
    result.update(
        kind=kind,
        allocations=[(i["id"], i["amount_cents"]) for i in matched],
        category=category,
        type=tx_type,
        review_reasons=reasons,
    )
    return result


def _apply_match(conn: sqlite3.Connection, tx: sqlite3.Row, allocations: Sequence[tuple[int, int]]) -> None:
    """Validate and write one match. No commit; the caller wraps it in _atomic."""
    if tx["amount_cents"] <= 0:
        raise ValueError("only a receipt (positive amount) can be matched to invoices")
    _check_locked(conn, tx["date"])
    allocs = [(int(i), int(a)) for i, a in allocations]
    if not allocs:
        raise ValueError("no invoices selected")
    ids = [i for i, _ in allocs]
    if len(set(ids)) != len(ids):
        raise ValueError("an invoice can be listed only once per receipt")
    total = sum(a for _, a in allocs)
    if total != tx["amount_cents"]:
        raise ValueError(
            f"allocations ({total}) must equal the receipt ({tx['amount_cents']}); left {tx['amount_cents'] - total}"
        )
    if conn.execute("SELECT 1 FROM invoice_payments WHERE transaction_id = ?", (tx["id"],)).fetchone():
        raise ValueError("this receipt is already matched to invoices")

    types: set[str] = set()
    for inv_id, amount in allocs:
        inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (inv_id,)).fetchone()
        if inv is None:
            raise ValueError(f"invoice {inv_id} does not exist")
        label = _label(conn, inv_id)
        if inv["type"] == "Voided":
            raise ValueError(f"{label} is voided and cannot be paid")
        if amount <= 0:
            raise ValueError(f"allocation for {label} must be positive")
        if amount != inv["amount_cents"]:
            raise ValueError(
                f"{label}: allocation {amount} must equal the gross invoice amount {inv['amount_cents']} "
                "(partial payments are not supported)"
            )
        other = conn.execute(
            "SELECT transaction_id FROM invoice_payments WHERE invoice_id = ? AND transaction_id <> ?",
            (inv_id, tx["id"]),
        ).fetchone()
        if other:
            raise ValueError(f"{label} is already paid by transaction {other['transaction_id']}")
        types.add(inv["type"])

    if "Commission" in types and len(types) > 1:
        raise ValueError(
            "ONE RULE: one receipt cannot settle Commission and Reimbursable invoices together; use split_receipt"
        )
    if tx["category_name"] == SERVICE_REVENUE and types != {"Commission"}:
        raise ValueError(
            "ONE RULE: Service Revenue can only settle a Commission invoice; "
            "recategorise the receipt as Reimbursable Expenses Recoverable first"
        )

    category, default_type = _target_for(types)
    new_type = tx["type"] if tx["category_name"] == category and tx["type"] else default_type
    notes = _REVIEW_TAG_RE.sub("", tx["notes"]) if tx["notes"] else tx["notes"]
    conn.execute(  # category first: trg_one_rule_invoice_payment reads it
        "UPDATE transactions SET category_id = ?, type = ?, review_status = 'reviewed', notes = ? WHERE id = ?",
        (_category_id(conn, category), new_type, notes, tx["id"]),
    )
    for inv_id, amount in allocs:
        conn.execute(
            "INSERT INTO invoice_payments (transaction_id, invoice_id, amount_cents) VALUES (?,?,?)",
            (tx["id"], inv_id, amount),
        )
        conn.execute("UPDATE invoices SET status = 'Paid' WHERE id = ?", (inv_id,))


def match_payment(conn: sqlite3.Connection, transaction_id: int, allocations: list[tuple[int, int]]) -> None:
    """Settle invoices with one receipt. `allocations` = (invoice_id, gross cents); the sum must equal the receipt.

    Sets category / type by the ONE RULE (all Commission -> Service Revenue / Income, else Reimbursable
    Expenses Recoverable / Reimbursable), marks the invoices Paid. Raises ValueError, LockedMonthError.
    """
    tx = _get_tx(conn, transaction_id)
    _check_locked(conn, tx["date"])
    with _atomic(conn):
        _apply_match(conn, tx, allocations)


def split_receipt(
    conn: sqlite3.Connection, transaction_id: int, groups: list[list[tuple[int, int]]]
) -> list[int]:
    """Split a mixed wire into one row per group and match each group (RULES.md section 3, action 4).

    The first group stays on the original row (amount reduced); every further group becomes a new row with
    source_ref '<original>#2', '#3', ... on the same date and account. Amounts add up to the original receipt.
    Each group must be all-Commission or all-non-Commission. Returns the transaction ids, original first.
    """
    tx = _get_tx(conn, transaction_id)
    _check_locked(conn, tx["date"])
    if not groups or any(not g for g in groups):
        raise ValueError("groups must be a non-empty list of non-empty allocation lists")
    group_totals = [sum(int(a) for _, a in g) for g in groups]
    if sum(group_totals) != tx["amount_cents"]:
        raise ValueError(
            f"groups ({sum(group_totals)}) must add up to the receipt ({tx['amount_cents']})"
        )
    for g in groups:
        types = {
            r["type"] for r in conn.execute(
                f"SELECT type FROM invoices WHERE id IN ({','.join('?' for _ in g)})", [int(i) for i, _ in g]
            )
        }
        if len(types) > 1:
            raise ValueError("ONE RULE: each group must be all Commission or all Reimbursable invoices")
    if conn.execute("SELECT 1 FROM invoice_payments WHERE transaction_id = ?", (tx["id"],)).fetchone():
        raise ValueError("this receipt is already matched to invoices")

    with _atomic(conn):
        ids = [tx["id"]]
        if len(groups) > 1:
            conn.execute(
                "UPDATE transactions SET amount_cents = ?, running_balance_cents = NULL WHERE id = ?",
                (group_totals[0], tx["id"]),
            )
            clean_notes = _REVIEW_TAG_RE.sub("", tx["notes"]) if tx["notes"] else None
            for n, total in enumerate(group_totals[1:], start=2):
                ids.append(insert_transaction(
                    conn,
                    date=tx["date"],
                    account_id=tx["account_id"],
                    description=tx["description"],
                    amount_cents=total,
                    source_ref=f"{tx['source_ref']}#{n}",
                    payee=tx["payee"],
                    raw_type=tx["raw_type"],
                    notes=clean_notes,
                    review_status="needs_review",
                ))
        for txn_id, group in zip(ids, groups):
            _apply_match(conn, _get_tx(conn, txn_id), group)
    return ids


# --------------------------------------------------------------------- checks
def commission_check(conn: sqlite3.Connection, year: int) -> dict:
    """Commission invoices paid in `year` (by receipt date) vs Service Revenue in the P&L view."""
    y = f"{int(year):04d}"
    paid = conn.execute(
        """SELECT COALESCE(SUM(ip.amount_cents), 0) FROM invoice_payments ip
             JOIN invoices i ON i.id = ip.invoice_id
             JOIN transactions t ON t.id = ip.transaction_id
            WHERE i.type = 'Commission' AND substr(t.date,1,4) = ?""",
        (y,),
    ).fetchone()[0]
    revenue = conn.execute(
        """SELECT COALESCE(SUM(amount_cents), 0) FROM v_pnl_monthly
            WHERE category = 'Service Revenue' AND substr(yyyy_mm,1,4) = ?""",
        (y,),
    ).fetchone()[0]
    unbacked = [
        {
            "transaction_id": r["transaction_id"],
            "date": r["date"],
            "amount_cents": r["amount_cents"],
            "commission_backed_cents": r["commission_backed_cents"],
        }
        for r in conn.execute(
            "SELECT * FROM v_revenue_unbacked WHERE substr(date,1,4) = ? ORDER BY date, transaction_id", (y,)
        )
    ]
    return {
        "invoice_paid_cents": paid,
        "revenue_cents": revenue,
        "diff_cents": paid - revenue,
        "unbacked": unbacked,
    }


def flag_commission_mismatches(conn: sqlite3.Connection) -> list[dict]:
    """Commission invoices (and voided invoices that carried boxes) whose boxes x rate != amount.

    The schema CHECK makes this impossible for a live Commission row; the list exists to prove it (and to
    catch a voided row edited by hand). Never raises.
    """
    out = []
    for r in conn.execute(
        """SELECT i.id, l.label, i.type, i.boxes, i.rate_cents, i.amount_cents
             FROM invoices i JOIN v_invoice_labels l ON l.id = i.id
            WHERE i.type = 'Commission' OR (i.type = 'Voided' AND i.boxes > 0)
            ORDER BY i.issue_date, i.id"""
    ):
        expected = r["boxes"] * r["rate_cents"]
        if expected != r["amount_cents"]:
            out.append({**dict(r), "expected_cents": expected, "diff_cents": r["amount_cents"] - expected})
    return out


def missing_commission_months(conn: sqlite3.Connection, *, through_month: str, today: str | None = None) -> list[str]:
    """Months from January of through_month's year up to through_month with no Commission invoice, once more
    than 45 days have passed since that month ended."""
    if not _ISO_MONTH_RE.match(through_month or ""):
        raise ValueError(f"through_month must be YYYY-MM, got {through_month!r}")
    today_d = _date.fromisoformat(today) if today else _date.today()
    year, last = int(through_month[:4]), int(through_month[5:7])
    if not 1 <= last <= 12:
        raise ValueError(f"through_month must be YYYY-MM, got {through_month!r}")
    have = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT commission_month FROM invoices WHERE type = 'Commission' AND commission_month IS NOT NULL"
        )
    }
    missing = []
    for m in range(1, last + 1):
        month = f"{year:04d}-{m:02d}"
        if month in have:
            continue
        month_end = _date(year + (m == 12), 1 if m == 12 else m + 1, 1).toordinal() - 1
        if today_d.toordinal() - month_end > MISSING_COMMISSION_GRACE_DAYS:
            missing.append(month)
    return missing


# ------------------------------------------------------------------ backfill
def backfill_payments(conn: sqlite3.Connection) -> int:
    """Create invoice_payments rows for existing WA receipts from unambiguous matches only.

    Runs propose_match oldest-first and writes kinds 'commission' and 'reimbursable' (never 'mixed',
    'amount_only' or 'none'). A receipt whose current category contradicts the proposal is left alone,
    and existing categories / types are never changed (an uncategorised receipt gets the proposed one).
    Idempotent: receipts that already have payments are skipped. Returns the number of rows created.
    """
    created = 0
    with _atomic(conn):
        for tx in _wa_receipts(conn):
            if conn.execute("SELECT 1 FROM invoice_payments WHERE transaction_id = ?", (tx["id"],)).fetchone():
                continue
            prop = propose_match(conn, tx["id"])
            if prop["kind"] not in ("commission", "reimbursable"):
                continue
            current = conn.execute(
                "SELECT c.name FROM categories c WHERE c.id = ?", (tx["category_id"],)
            ).fetchone() if tx["category_id"] is not None else None
            if current is not None and current["name"] != prop["category"]:
                continue
            if current is None:
                conn.execute(
                    "UPDATE transactions SET category_id = ?, type = ?, review_status = 'auto' WHERE id = ?",
                    (_category_id(conn, prop["category"]), prop["type"], tx["id"]),
                )
            for inv_id, amount in prop["allocations"]:
                conn.execute(
                    "INSERT INTO invoice_payments (transaction_id, invoice_id, amount_cents) VALUES (?,?,?)",
                    (tx["id"], inv_id, amount),
                )
                conn.execute("UPDATE invoices SET status = 'Paid' WHERE id = ?", (inv_id,))
                created += 1
    return created
