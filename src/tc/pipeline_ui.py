"""Pipeline helpers the Streamlit pages call (docs/UI.md 4.4): review queue, search, edits, lists, adjustments.

Money is integer cents, dates are ISO 'YYYY-MM-DD', months 'YYYY-MM'. Every write commits before returning,
or rolls back and raises: ValueError for invalid input, LockedMonthError for a locked month.
`src/tc/pipeline.py` re-exports these functions.
"""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3

from .errors import LockedMonthError, translate_integrity_error

TYPES = ("Income", "Expense", "Transfer", "Personal", "Pass-through", "Reimbursable")
REVIEW_STATUSES = ("needs_review", "auto", "reviewed")
ADJUSTMENT_SECTIONS = ("revenue", "sga", "other_income", "asset", "liability", "equity")

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_TAG_RE = re.compile(r"\[(rule|review):([^\]]*)\]")
_LEADING_TAGS_RE = re.compile(r"^\s*(?:\[(?:rule|review):[^\]]*\]\s*)+")
# reason codes that clear themselves once the parent of a fee row is categorised (RULES.md section 3)
_FEE_WAIT_REASONS = {"orphan_fee", "parent_uncategorised"}


# ------------------------------------------------------------------ helpers
def _check_month(month: str, name: str = "month") -> str:
    if not isinstance(month, str) or not _MONTH_RE.match(month):
        raise ValueError(f"{name} must be YYYY-MM, got {month!r}")
    return month


def _check_date(value: str) -> str:
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        raise ValueError(f"date must be YYYY-MM-DD, got {value!r}")
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"not a valid date: {value!r}") from None
    return value


def _is_locked(conn: sqlite3.Connection, month: str) -> bool:
    return conn.execute("SELECT 1 FROM month_locks WHERE yyyy_mm = ?", (month,)).fetchone() is not None


def _require_unlocked(conn: sqlite3.Connection, date: str) -> None:
    month = date[:7]
    if _is_locked(conn, month):
        raise LockedMonthError(f"Month {month} is closed and locked", month=month)


def _split_notes(notes: str | None) -> tuple[dict[str, str], str]:
    """Split '[rule:x][review:a,b] free text' into ({'rule': 'x', 'review': 'a,b'}, 'free text')."""
    notes = notes or ""
    m = _LEADING_TAGS_RE.match(notes)
    if not m:
        return {}, notes.strip()
    tags = {k: v for k, v in _TAG_RE.findall(m.group(0))}
    return tags, notes[m.end():].strip()


def _join_notes(tags: dict[str, str], free: str) -> str | None:
    head = "".join(f"[{k}:{tags[k]}]" for k in ("rule", "review") if tags.get(k))
    text = (head + (" " if head and free else "") + free).strip()
    return text or None


def _reasons(notes: str | None) -> list[str]:
    tags, _ = _split_notes(notes)
    return [r for r in tags.get("review", "").split(",") if r]


def _get_tx(conn: sqlite3.Connection, transaction_id: int) -> sqlite3.Row:
    if isinstance(transaction_id, bool) or not isinstance(transaction_id, int):
        raise ValueError(f"transaction_id must be an int, got {transaction_id!r}")
    row = conn.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if row is None:
        raise ValueError(f"No transaction with id {transaction_id}")
    return row


def _write(conn: sqlite3.Connection, fn):
    """Run fn() in one transaction: commit on success, roll back and translate errors otherwise."""
    try:
        with conn:
            return fn()
    except sqlite3.IntegrityError as exc:
        err = translate_integrity_error(exc)
        if err is exc:
            raise ValueError(str(exc)) from exc
        raise err from exc


def _fee_category_name(parent_category: str | None) -> str:
    if parent_category == "Owner's Draw":
        return "Owner's Draw"
    if parent_category == "Payable to WA Rubbermate":
        return "Payable to WA Rubbermate"
    return "Bank Charges"


# ------------------------------------------------------------ review queue
def review_queue(conn: sqlite3.Connection, *, month: str | None = None) -> list[dict]:
    """Rows with review_status 'needs_review', ordered by date, id.

    month=None -> every open (unlocked) month; month='YYYY-MM' -> that month only.
    Keys: id, date, account (source_label), description, amount_cents, payee, category, type,
    parent_id, reasons (list of RULES.md reason codes from the [review:...] notes tag), plus
    account_id, category_id, notes for convenience.
    """
    sql = (
        "SELECT t.id, t.date, a.source_label AS account, t.account_id, t.description, t.amount_cents, t.payee,"
        " c.name AS category, t.category_id, t.type, t.parent_id, t.notes"
        " FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " LEFT JOIN categories c ON c.id = t.category_id"
        " WHERE t.review_status = 'needs_review'"
    )
    params: list = []
    if month is not None:
        _check_month(month)
        sql += " AND substr(t.date,1,7) = ?"
        params.append(month)
    else:
        sql += " AND substr(t.date,1,7) NOT IN (SELECT yyyy_mm FROM month_locks)"
    sql += " ORDER BY t.date, t.id"
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d["reasons"] = _reasons(d["notes"])
        out.append(d)
    return out


def confirm(conn: sqlite3.Connection, transaction_id: int) -> None:
    """Accept the row's proposed category as is: review_status = 'reviewed'."""
    row = _get_tx(conn, transaction_id)
    _require_unlocked(conn, row["date"])
    if row["category_id"] is None:
        raise ValueError(f"Transaction {transaction_id} has no category to confirm; use set_category")

    def work():
        tags, free = _split_notes(row["notes"])
        tags.pop("review", None)
        conn.execute(
            "UPDATE transactions SET review_status = 'reviewed', notes = ? WHERE id = ?",
            (_join_notes(tags, free), transaction_id),
        )

    _write(conn, work)


def mark_duplicate(conn: sqlite3.Connection, transaction_id: int) -> None:
    """Delete a row flagged possible_duplicate, in an unlocked month. Any other row -> ValueError."""
    row = _get_tx(conn, transaction_id)
    _require_unlocked(conn, row["date"])
    if "possible_duplicate" not in _reasons(row["notes"]):
        raise ValueError(f"Transaction {transaction_id} is not flagged possible_duplicate")
    if conn.execute("SELECT 1 FROM transactions WHERE parent_id = ?", (transaction_id,)).fetchone():
        raise ValueError(f"Transaction {transaction_id} has fee rows attached; it cannot be deleted")
    if conn.execute("SELECT 1 FROM invoice_payments WHERE transaction_id = ?", (transaction_id,)).fetchone():
        raise ValueError(f"Transaction {transaction_id} is linked to invoices; unlink them first")
    _write(conn, lambda: conn.execute("DELETE FROM transactions WHERE id = ?", (transaction_id,)))


# ------------------------------------------------------------------ search
def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search_transactions(
    conn: sqlite3.Connection,
    *,
    month: str | None = None,
    account_id: int | None = None,
    category_id: int | None = None,
    type: str | None = None,
    review_status: str | None = None,
    search: str | None = None,
) -> list[dict]:
    """Filtered transactions ordered by date, id (all filters optional, combined with AND).

    Keys: id, date, account (source_label), description, amount_cents, category, type, notes,
    review_status, parent_id, source_ref, invoice_labels (list of 'Wise INV-014' style strings),
    plus account_id, category_id, fee_cents, payee.
    """
    where, params = [], []
    if month is not None:
        _check_month(month)
        where.append("substr(t.date,1,7) = ?")
        params.append(month)
    if account_id is not None:
        where.append("t.account_id = ?")
        params.append(account_id)
    if category_id is not None:
        where.append("t.category_id = ?")
        params.append(category_id)
    if type is not None:
        if type not in TYPES:
            raise ValueError(f"unknown type: {type!r}")
        where.append("t.type = ?")
        params.append(type)
    if review_status is not None:
        if review_status not in REVIEW_STATUSES:
            raise ValueError(f"unknown review_status: {review_status!r}")
        where.append("t.review_status = ?")
        params.append(review_status)
    if search is not None and search.strip():
        pat = _like(search.strip())
        where.append(
            "(t.description LIKE ? ESCAPE '\\' OR t.payee LIKE ? ESCAPE '\\' OR t.notes LIKE ? ESCAPE '\\')"
        )
        params += [pat, pat, pat]
    sql = (
        "SELECT t.id, t.date, a.source_label AS account, t.account_id, t.description, t.amount_cents,"
        " c.name AS category, t.category_id, t.type, t.notes, t.review_status, t.parent_id, t.source_ref,"
        " t.fee_cents, t.payee"
        " FROM transactions t JOIN accounts a ON a.id = t.account_id"
        " LEFT JOIN categories c ON c.id = t.category_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.date, t.id"
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    labels: dict[int, list[str]] = {}
    for r in conn.execute(
        "SELECT ip.transaction_id AS tid, l.label FROM invoice_payments ip"
        " JOIN v_invoice_labels l ON l.id = ip.invoice_id ORDER BY ip.id"
    ):
        labels.setdefault(r["tid"], []).append(r["label"])
    for d in rows:
        d["invoice_labels"] = labels.get(d["id"], [])
    return rows


# RULES.md 4.3/4.4: a regime B Wise parent (no real FEE row) keeps its fee inside the row for an owner draw
# and is split into parent + fee row for every other category. A category flip moves it between the two.
def _merge_or_split_fee(
    conn: sqlite3.Connection, row: sqlite3.Row, new_category: str, fee_cat: sqlite3.Row
) -> list[str]:
    ref = f"FEE-{row['source_ref']}"
    child = conn.execute(
        "SELECT * FROM transactions WHERE account_id = ? AND source_ref = ? AND id <> ?",
        (row["account_id"], ref, row["id"]),
    ).fetchone()
    synthetic = (  # written by the pipeline's split: no bank running balance, payee 'Wise'
        child is not None and child["parent_id"] == row["id"]
        and child["running_balance_cents"] is None and child["payee"] == "Wise"
    )
    if child is not None and not synthetic:
        return []  # regime A: the fee is a real bank row; never merged or split
    if new_category == "Owner's Draw":
        if synthetic and child["review_status"] != "reviewed":  # merge: the fee moves back inside the draw
            conn.execute(
                "UPDATE transactions SET amount_cents = amount_cents + ?, fee_cents = ? WHERE id = ?",
                (child["amount_cents"], -child["amount_cents"], row["id"]),
            )
            conn.execute("DELETE FROM transactions WHERE id = ?", (child["id"],))
        elif synthetic:  # a reviewed fee row is never deleted: say so instead of leaving the draw looking unmerged
            return [
                f"The Wise fee row #{child['id']} ({-child['amount_cents'] / 100:,.2f}) was already reviewed, so it "
                "stays a separate row; it now follows the draw as Owner's Draw. The draw and its fee still add up "
                "to the bank amount."
            ]
    elif child is None and row["fee_cents"] > 0:  # split: parent + child sum to the CSV cash movement
        fee = row["fee_cents"]
        conn.execute(
            "UPDATE transactions SET amount_cents = amount_cents + ?, fee_cents = 0 WHERE id = ?", (fee, row["id"])
        )
        conn.execute(
            "INSERT INTO transactions (date, account_id, description, amount_cents, category_id, type, source_ref,"
            " parent_id, fee_cents, payee, raw_type, review_status) VALUES (?,?,?,?,?,?,?,?,0,'Wise',?,'auto')",
            (row["date"], row["account_id"], f"Wise Charges for: {row['source_ref']}", -fee, fee_cat["id"],
             fee_cat["default_type"], ref, row["id"], row["raw_type"]),
        )
    return []


# ------------------------------------------------------------ set_category
def set_category(
    conn: sqlite3.Connection,
    transaction_id: int,
    category_id: int,
    type: str,
    *,
    notes: str | None = None,
) -> list[str]:
    """Manual override: set category + type, mark 'reviewed', re-point fee children (fee follows parent).

    Returns a list of plain-words notices to show the user (empty when there is nothing to add), e.g. a reviewed
    Wise fee row that could not be merged back into an owner draw.

    notes=None keeps the row's free text; a string replaces it ('' clears it). The [rule:...] tag is kept,
    the [review:...] tag is dropped. Fee rows (parent_id set) cannot be edited directly.
    """
    row = _get_tx(conn, transaction_id)
    if row["parent_id"] is not None:
        raise ValueError(f"Transaction {transaction_id} is a fee row: it follows its parent")
    if type not in TYPES:
        raise ValueError(f"unknown type: {type!r}")
    cat = (
        conn.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
        if isinstance(category_id, int) and not isinstance(category_id, bool)
        else None
    )
    if cat is None:
        raise ValueError(f"No category with id {category_id!r}")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("notes must be a string or None")
    _require_unlocked(conn, row["date"])
    if cat["name"] == "Service Revenue":
        backed = conn.execute(
            "SELECT COALESCE(SUM(ip.amount_cents),0) FROM invoice_payments ip JOIN invoices i ON i.id = ip.invoice_id"
            " WHERE ip.transaction_id = ? AND i.type = 'Commission'",
            (transaction_id,),
        ).fetchone()[0]
        if backed != row["amount_cents"]:
            raise ValueError("Service Revenue is only set by matching Commission invoices (ONE RULE)")

    fee_name = _fee_category_name(cat["name"])
    fee_cat = conn.execute("SELECT id, default_type FROM categories WHERE name = ?", (fee_name,)).fetchone()

    notices: list[str] = []

    def work():
        tags, free = _split_notes(row["notes"])
        tags.pop("review", None)
        if notes is not None:
            free = notes.strip()
        conn.execute(
            "UPDATE transactions SET category_id = ?, type = ?, review_status = 'reviewed', notes = ? WHERE id = ?",
            (category_id, type, _join_notes(tags, free), transaction_id),
        )
        notices.extend(_merge_or_split_fee(conn, row, cat["name"], fee_cat))
        # fee children follow the parent (parent updated first: the child trigger reads the parent's category)
        for ch in conn.execute(
            "SELECT id, notes, review_status FROM transactions WHERE parent_id = ?", (transaction_id,)
        ).fetchall():
            ctags, cfree = _split_notes(ch["notes"])
            status = ch["review_status"]
            left = [r for r in ctags.get("review", "").split(",") if r and r not in _FEE_WAIT_REASONS]
            if left:
                ctags["review"] = ",".join(left)
            else:
                ctags.pop("review", None)
                if status == "needs_review":
                    status = "auto"
            conn.execute(
                "UPDATE transactions SET category_id = ?, type = ?, review_status = ?, notes = ? WHERE id = ?",
                (fee_cat["id"], fee_cat["default_type"], status, _join_notes(ctags, cfree), ch["id"]),
            )

    _write(conn, work)
    return notices


def set_date(conn: sqlite3.Connection, transaction_id: int, new_date: str, *, note: str | None = None) -> None:
    """Move a row to another date (open months only), e.g. to follow the workbook date instead of the statement date.

    Both the row's current month and the new month must be unlocked (LockedMonthError otherwise). A fee row cannot
    be moved on its own: it follows its parent, and the parent's fee children move with it. The old date is kept in
    the free-text notes ("date was YYYY-MM-DD"). Commits, or rolls back and raises ValueError.
    """
    row = _get_tx(conn, transaction_id)
    if row["parent_id"] is not None:
        raise ValueError(f"Transaction {transaction_id} is a fee row: it follows its parent")
    if not isinstance(new_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", new_date):
        raise ValueError(f"date must be YYYY-MM-DD, got {new_date!r}")
    try:
        _dt.date.fromisoformat(new_date)
    except ValueError as exc:
        raise ValueError(f"not a calendar date: {new_date!r}") from exc
    if new_date == row["date"]:
        return
    _require_unlocked(conn, row["date"])
    _require_unlocked(conn, new_date)

    def work():
        tags, free = _split_notes(row["notes"])
        stamp = (note or "").strip() or f"date was {row['date']}"
        free = f"{free} ({stamp})" if free else f"({stamp})"
        conn.execute("UPDATE transactions SET date = ?, notes = ? WHERE id = ?",
                     (new_date, _join_notes(tags, free), transaction_id))
        conn.execute("UPDATE transactions SET date = ? WHERE parent_id = ?", (new_date, transaction_id))

    _write(conn, work)


# ------------------------------------------------------------------- lists
def list_accounts(conn: sqlite3.Connection, *, tie_required_only: bool = False) -> list[dict]:
    """Accounts ordered by id: id, code, source_label, name, kind, statement_day."""
    sql = "SELECT id, code, source_label, name, kind, statement_day FROM accounts"
    if tie_required_only:
        sql += " WHERE tie_required = 1 AND active = 1"
    sql += " ORDER BY id"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def list_categories(conn: sqlite3.Connection) -> list[dict]:
    """Categories ordered by sort_order: id, name, statement, section, default_type."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT id, name, statement, section, default_type FROM categories ORDER BY sort_order, id"
        ).fetchall()
    ]


# ------------------------------------------------------------- adjustments
def list_adjustments(conn: sqlite3.Connection, *, month: str | None = None) -> list[dict]:
    """Adjustment rows ordered by date, id (month='YYYY-MM' filters). `non_cash` is a bool."""
    sql = "SELECT * FROM adjustments"
    params: list = []
    if month is not None:
        _check_month(month)
        sql += " WHERE substr(date,1,7) = ?"
        params.append(month)
    sql += " ORDER BY date, id"
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d["non_cash"] = bool(d["non_cash"])
        out.append(d)
    return out


def add_adjustment(
    conn: sqlite3.Connection,
    *,
    date: str,
    label: str,
    section: str,
    amount_cents: int,
    reason: str,
    non_cash: bool = True,
) -> int:
    """Insert a manual adjustment and return its id. Locked month -> LockedMonthError."""
    _check_date(date)
    if not isinstance(label, str) or not label.strip():
        raise ValueError("label is required")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("reason is required")
    if section not in ADJUSTMENT_SECTIONS:
        raise ValueError(f"unknown section: {section!r}")
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        raise ValueError(f"amount_cents must be an int (cents), got {amount_cents!r}")
    _require_unlocked(conn, date)
    return _write(
        conn,
        lambda: conn.execute(
            "INSERT INTO adjustments (date, label, section, amount_cents, non_cash, reason)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (date, label.strip(), section, amount_cents, 1 if non_cash else 0, reason.strip()),
        ).lastrowid,
    )
