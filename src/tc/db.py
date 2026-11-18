"""SQLite access layer. Money is integer cents; dates are ISO 'YYYY-MM-DD' strings."""

from __future__ import annotations

import sqlite3
import sys
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

DEFAULT_DB_PATH = Path("data") / "tc.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_TX_COLUMNS = frozenset(
    {
        "date", "account_id", "description", "amount_cents", "category_id", "type",
        "source_ref", "parent_id", "fee_cents", "payee", "raw_type",
        "running_balance_cents", "notes", "review_status",
    }
)


def connect(path: str | Path | None = None, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection (rows are sqlite3.Row) with foreign keys ON.

    path=None -> data/tc.db (parent directory is created); ':memory:' is allowed.
    check_same_thread=False lets a cached connection be used from other threads (Streamlit).
    """
    target = DEFAULT_DB_PATH if path is None else path
    if str(target) != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables, views, triggers and seeds from schema.sql on an empty database."""
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("PRAGMA foreign_keys = ON")  # executescript may reset session state
    conn.commit()


def to_cents(value: str | Decimal | float | int) -> int:
    """Convert a dollar amount to integer cents, rounding half away from zero (ROUND_HALF_UP).

    Floats are converted through their shortest repr (never float arithmetic).
    Strings may contain '$', ',' and spaces.
    """
    if isinstance(value, bool):
        raise TypeError("bool is not a money value")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, (str, float, int)):
        text = str(value).strip().replace(",", "").replace("$", "").replace(" ", "")
        try:
            d = Decimal(text)
        except InvalidOperation:
            raise ValueError(f"not a money amount: {value!r}") from None
    else:
        raise TypeError(f"unsupported money type: {type(value).__name__}")
    if not d.is_finite():
        raise ValueError(f"not a finite money amount: {value!r}")
    return int((d * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def cents_to_str(cents: int) -> str:
    """Format integer cents as '1,234.56' (negative: '-1,234.56')."""
    sign = "-" if cents < 0 else ""
    dollars, rem = divmod(abs(int(cents)), 100)
    return f"{sign}{dollars:,}.{rem:02d}"


def insert_transaction(conn: sqlite3.Connection, **fields) -> int:
    """Insert one transactions row and return its id. Does not commit.

    Required: date, account_id, description, amount_cents, source_ref.
    Optional: any other transactions column (category_id and type must be set together).
    """
    unknown = set(fields) - _TX_COLUMNS
    if unknown:
        raise ValueError(f"unknown transaction field(s): {sorted(unknown)}")
    cols = list(fields)
    sql = f"INSERT INTO transactions ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    return conn.execute(sql, [fields[c] for c in cols]).lastrowid


def list_transactions(
    conn: sqlite3.Connection,
    account: int | str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> list[sqlite3.Row]:
    """Transactions ordered by date, id. account = id, accounts.code or accounts.source_label.

    start / end are inclusive ISO dates.
    """
    where, params = [], []
    if account is not None:
        if isinstance(account, int):
            where.append("t.account_id = ?")
        else:
            where.append("t.account_id IN (SELECT id FROM accounts WHERE code = ? OR source_label = ?)")
            params.append(account)
        params.append(account)
    if start is not None:
        where.append("t.date >= ?")
        params.append(start)
    if end is not None:
        where.append("t.date <= ?")
        params.append(end)
    sql = "SELECT t.* FROM transactions t"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY t.date, t.id"
    return conn.execute(sql, params).fetchall()


def _main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "init" and len(argv) <= 3:
        path = Path(argv[2]) if len(argv) == 3 else DEFAULT_DB_PATH
        if path.exists() and path.stat().st_size > 0:
            print(f"refusing to overwrite existing database: {path}", file=sys.stderr)
            return 1
        conn = connect(path)
        try:
            init_db(conn)
        finally:
            conn.close()
        print(f"initialised {path}")
        return 0
    print("usage: python -m tc.db init [path]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
