"""Import pipeline: parse_statement, run_import, run_rules (docs/RULES.md section 2, stages 1-8).

    run_import(conn, path)  = backup -> stage 1 normalize -> stage 2 dedupe + insert -> run_rules(new rows)
                              -> statement balance. One DB transaction per file: a failure rolls everything back.
    run_rules(conn, ids)    = stages 3-8: categorize (tc.rules.categorize), fee follows parent (two passes),
                              Wise fee split / merge (two regimes), own-account transfer pairing, WA receipt
                              invoice matching (tc.invoices.propose_match), review-queue finalisation.

Money is integer cents. Rows with review_status 'reviewed' are manual overrides: no stage touches them.
Review reasons and the matched rule id live as `[rule:<id>][review:<a,b>]` tags at the start of
`transactions.notes` (the schema has no column for them). Stage 5 keeps a synthetic fee row apart from a real
Wise FEE row (`_is_synthetic_fee`): a real row carries the bank's running balance, a synthetic one has none.

Pages call everything as `pipeline.X`: the review-queue helpers of `tc.pipeline_ui` and `tc.rules.add_rule`
are re-exported at the end of this module.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import sqlite3
import subprocess
from collections import Counter
from pathlib import Path

from tc import close as _close
from tc import invoices as _invoices
from tc import rules as _rules
from tc.db import insert_transaction, to_cents
from tc.errors import translate_integrity_error
from tc.importers import chase_card as _chase_card
from tc.importers import chase_checking as _chase_checking
from tc.importers import wise as _wise

KINDS = ("wise_csv", "chase5200_pdf", "chase5200_csv", "card2110_pdf")
OWNERS_DRAW = "Owner's Draw"
BANK_TRANSFER = "Bank Transfer"
FEE_PREFIX = "FEE-"
INK_WAIVER_CENTS = 200000  # RULES.md 5.4: $2,000 Chase Ink card activity waives the $15 fee too
KEEP_BACKUPS = 30
# Reasons that belong to the row (set at import time, cleared by a human), not recomputed by a re-run.
STICKY_REASONS = ("possible_duplicate", "unexpected_bank_fee")
JAR_ACCOUNTS = {"Rent & Utilities": "Wise USD - Rent & Utilities 0000", "Taxes": "Wise USD - Taxes jar"}
# (out role, in role, allowed range of  in.date - out.date  in days)   RULES.md stage 6
PAIRS = (("wise_out", "chase_in", (-1, 3)), ("card_pay_out", "card_pay_in", (-3, 1)))
COVER_DAYS = 3

_JAR_NAME_RE = re.compile(r"(Rent & Utilities|Taxes)")
_FEE_DESC_RE = re.compile(r"^Wise Charges for:\s*(\S+)", re.IGNORECASE)
_CHASE_PERIOD_RE = re.compile(r"[A-Z][a-z]+ \d{1,2}, \d{4} through [A-Z][a-z]+ \d{1,2}, \d{4}")
_LOWEST_RE = re.compile(r"lowest\s+daily\s+ending\s+balance\s+was\s+\$([\d,]+\.\d{2})", re.IGNORECASE)
_INK_RE = re.compile(r"\$([\d,]+\.\d{2})\s+was\s+your\s+total\s+Ink\s+activity", re.IGNORECASE)


# ====================================================================== parse_statement
def _pdftotext(path: Path) -> str:
    try:
        out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"{path.name}: cannot read the PDF text layer ({exc})") from exc
    return out.stdout


def _detect_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                header = fh.readline()
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"{path.name}: cannot read the file ({exc})") from exc
        low = header.replace("\\", "").replace('"', "").lower()
        if "transferwise id" in low:
            return "wise_csv"
        if "posting date" in low:
            return "chase5200_csv"
        raise ValueError(f"{path.name}: unrecognised CSV (not a Wise export or a Chase checking download)")
    if suffix == ".pdf":
        text = _pdftotext(path)
        if "Opening/Closing Date" in text and "ACCOUNT ACTIVITY" in text:
            return "card2110_pdf"
        if _CHASE_PERIOD_RE.search(text) and "Beginning Balance" in text:
            return "chase5200_pdf"
        raise ValueError(f"{path.name}: unrecognised PDF (not a Chase *5200 or card *2110 statement)")
    raise ValueError(f"{path.name}: unsupported file type {suffix or '(none)'!r}; expected .csv or .pdf")


def _parse(path: str | Path, kind: str | None, filename: str | None) -> dict:
    path = Path(path)
    name = filename or path.name
    if kind is not None and kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {list(KINDS)}")
    if not path.is_file():
        raise ValueError(f"{name}: file not found")
    kind = kind or _detect_kind(path)
    try:
        if kind == "wise_csv":
            rows = _wise.parse_wise_csv(path)
            if not rows:
                raise ValueError(f"{name}: no transactions in the file")
            first, last = rows[0], rows[-1]
            run_first, run_last = first["running_balance_cents"], last["running_balance_cents"]
            out = {
                "account": _wise.ACCOUNT, "rows": rows,
                "period_start": min(r["date"] for r in rows), "period_end": max(r["date"] for r in rows),
                "opening_cents": None if run_first is None else run_first - first["amount_cents"],
                "ending_cents": run_last,
            }
        elif kind in ("chase5200_pdf", "chase5200_csv"):
            stmt = (_chase_checking.parse_chase_checking_pdf if kind == "chase5200_pdf"
                    else _chase_checking.parse_chase_checking_csv)(path)
            out = {
                "account": _chase_checking.ACCOUNT, "rows": stmt["rows"],
                "period_start": stmt["period_start"], "period_end": stmt["period_end"],
                "opening_cents": stmt["opening_cents"], "ending_cents": stmt["ending_cents"],
            }
        else:
            stmt = _chase_card.parse_chase_card_pdf(path)
            out = {
                "account": _chase_card.ACCOUNT_LABEL, "rows": stmt["rows"],
                "period_start": stmt["cycle_start"], "period_end": stmt["cycle_end"],
                "opening_cents": stmt["previous_balance_cents"], "ending_cents": stmt["new_balance_cents"],
            }
            if stmt["previous_balance_cents"] + sum(r["amount_cents"] for r in stmt["rows"]) != stmt["new_balance_cents"]:
                raise ValueError("parsed rows do not reconcile to the statement new balance")
        if kind == "chase5200_pdf":
            text = _pdftotext(path)  # the two printed MSF waiver tests (RULES.md 5.4); None when not printed
            low, ink = _LOWEST_RE.search(text), _INK_RE.search(text)
            out["lowest_daily_balance_cents"] = to_cents(low.group(1)) if low else None
            out["ink_activity_cents"] = to_cents(ink.group(1)) if ink else None
            if out["opening_cents"] + sum(r["amount_cents"] for r in out["rows"]) != out["ending_cents"]:
                raise ValueError(
                    f"opening + rows = {out['opening_cents'] + sum(r['amount_cents'] for r in out['rows'])} "
                    f"but printed ending balance = {out['ending_cents']}"
                )
    except ValueError as exc:
        msg = str(exc)
        raise ValueError(msg if msg.startswith(name) or str(path) in msg else f"{name}: {msg}") from exc
    except (RuntimeError, OSError, KeyError) as exc:
        raise ValueError(f"{name}: cannot parse the file ({exc})") from exc
    out.update(path=str(path), filename=name, kind=kind)
    return out


def parse_statement(path: str | Path, *, filename: str | None = None) -> dict:
    """Detect the file kind (extension + content) and parse it. No database access.

    Returns {path, filename, kind, account, rows, period_start, period_end, opening_cents, ending_cents};
    kind is wise_csv / chase5200_pdf / chase5200_csv / card2110_pdf; cents are signed asset values (the card
    liability is negative); opening/ending are None when the file does not carry them. `filename` is the name
    shown to the user (the page saves uploads to temp files). Raises ValueError for anything unrecognised.
    """
    return _parse(path, None, filename)


# ====================================================================== helpers
class _NoCommit:
    """Connection proxy for tc.rules.categorize, which commits by itself: commit/rollback are no-ops here so
    the whole import stays one transaction (the caller commits or rolls back)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _chunks(seq, n: int = 400):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _qmarks(n: int) -> str:
    return ",".join("?" for _ in range(n))


def _day(text: str) -> _dt.date:
    return _dt.date.fromisoformat(text)


def _backup(conn: sqlite3.Connection, reason: str) -> Path:
    """Online backup via close.backup (dir = $TC_BACKUP_DIR, else <repo root>/backups; never cwd-relative).

    Keeps the last KEEP_BACKUPS backups of this reason; month-close and re-open backups are other files.
    """
    dest = _close.backup(conn, reason)
    older = sorted(dest.parent.glob(f"tc_*_{reason}.db"), key=lambda p: p.stat().st_mtime)
    for old in older[:-KEEP_BACKUPS]:
        try:
            old.unlink()
        except OSError:
            pass
    return dest


def _is_synthetic_fee(child, parent_id: int) -> bool:
    """A fee row written by stage 5 (not a bank row): no bank running balance, payee 'Wise', linked to its parent."""
    return child["parent_id"] == parent_id and child["running_balance_cents"] is None and child["payee"] == "Wise"


def _ledger_at(conn: sqlite3.Connection, account_id: int, day: str) -> int:
    return conn.execute(
        "SELECT a.opening_balance_cents + COALESCE((SELECT SUM(t.amount_cents) FROM transactions t "
        "WHERE t.account_id = a.id AND t.date <= ?), 0) FROM accounts a WHERE a.id = ?",
        (day, account_id),
    ).fetchone()[0]


# ====================================================================== stage 1: normalize
def _seed_cutoff(acct: sqlite3.Row) -> str | None:
    """accounts.seeded_through (set by `python -m tc.fixtures seed`); None for NULL or a database without the column."""
    return acct["seeded_through"] if "seeded_through" in acct.keys() else None


def seed_cutoff(conn: sqlite3.Connection, source_label: str) -> str | None:
    """The seed cutoff date of the account with this source label (None: no cutoff, or unknown account)."""
    acct = conn.execute("SELECT * FROM accounts WHERE source_label = ?", (source_label,)).fetchone()
    return _seed_cutoff(acct) if acct is not None else None


def _normalize(conn: sqlite3.Connection, parsed: dict, acct: sqlite3.Row) -> tuple[list[dict], int, int, int]:
    """Stage 1. Returns (rows, skipped_before_opening, rejected_locked, skipped_seeded).

    A row dated on or before the account's seeded_through is skipped and counted (never inserted, never sent
    through the rules): the 2026 workbook history already holds it. NULL seeded_through = no effect.
    """
    seeded_through = _seed_cutoff(acct)
    name = parsed["filename"]
    labels = {r[0] for r in conn.execute("SELECT source_label FROM accounts")}
    locked = {r[0] for r in conn.execute("SELECT yyyy_mm FROM month_locks")}

    if parsed["kind"] == "wise_csv":  # running-balance chain over the whole file (sorted by timestamp)
        prev = None
        for r in parsed["rows"]:
            run = r.get("running_balance_cents")
            if prev is not None and run is not None and prev + r["amount_cents"] != run:
                raise ValueError(
                    f"{name}: running balance breaks at {r['source_ref']} ({r['date']}): "
                    f"{prev} + {r['amount_cents']} <> {run}; the file is truncated or edited"
                )
            if run is not None:
                prev = run

    out: list[dict] = []
    before = rejected = seeded = 0
    for r in parsed["rows"]:
        if r["account"] not in labels:
            raise ValueError(f"{name}: unknown account {r['account']!r}")
        if r["account"] != acct["source_label"]:
            raise ValueError(f"{name}: row account {r['account']!r} differs from the file account")
        amount, fee = r["amount_cents"], r.get("fee_cents") or 0
        if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
            raise ValueError(f"{name}: {r.get('source_ref')}: amount must be a non-zero integer of cents")
        if isinstance(fee, bool) or not isinstance(fee, int) or fee < 0:
            raise ValueError(f"{name}: {r.get('source_ref')}: fee must be an integer >= 0")
        desc = " ".join((r.get("description") or "").split())
        if not desc or not r.get("source_ref"):
            raise ValueError(f"{name}: a row has no description or source_ref")
        if r["date"] < acct["opening_date"]:
            before += 1
            continue
        if seeded_through and r["date"] <= seeded_through:
            seeded += 1
            continue
        if r["date"][:7] in locked:
            rejected += 1
            continue
        out.append({
            "date": r["date"], "description": desc, "amount_cents": amount, "cash": amount,
            "source_ref": r["source_ref"], "fee_cents": fee,
            "running_balance_cents": r.get("running_balance_cents"),
            "payee": (r.get("payee") or "").strip() or None, "raw_type": r.get("raw_type"),
            "stamp": r.get("datetime") or "",
        })
    out.sort(key=lambda r: (r["date"], r["stamp"]))  # stable: parser order breaks ties; oldest first
    return out, before, rejected, seeded


# ====================================================================== stage 2: dedupe + insert
def _insert_rows(conn: sqlite3.Connection, rows: list[dict], account_id: int) -> tuple[list[int], int, dict]:
    pre_max = conn.execute("SELECT COALESCE(MAX(id), 0) FROM transactions").fetchone()[0]
    batch_refs = {r["source_ref"] for r in rows}
    seen: set[str] = set()
    new_ids: list[int] = []
    dups = 0
    extra: dict[int, list[str]] = {}

    def exists(ref: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM transactions WHERE account_id = ? AND source_ref = ?", (account_id, ref)
        ).fetchone() is not None

    for r in rows:
        ref = r["source_ref"]
        if ref in seen or exists(ref):
            dups += 1
            continue
        if ref.startswith(FEE_PREFIX):
            parent_ref = ref[len(FEE_PREFIX):]
            if exists(parent_ref) and parent_ref not in batch_refs:
                dups += 1  # the parent came from a regime B file: its fee is inside it or in a synthetic FEE row
                continue
        seen.add(ref)
        new_id = insert_transaction(
            conn, date=r["date"], account_id=account_id, description=r["description"],
            amount_cents=r["cash"], source_ref=ref, fee_cents=r["fee_cents"], payee=r["payee"],
            raw_type=r["raw_type"], running_balance_cents=r["running_balance_cents"],
            review_status="needs_review",
        )
        new_ids.append(new_id)
        if not ref.startswith(FEE_PREFIX) and conn.execute(
            "SELECT 1 FROM transactions WHERE id <= ? AND account_id = ? AND date = ? AND amount_cents = ? "
            "AND source_ref <> ? LIMIT 1",
            (pre_max, account_id, r["date"], r["cash"], ref),
        ).fetchone():
            extra[new_id] = ["possible_duplicate"]
    return new_ids, dups, extra


# ====================================================================== statement checks (RULES.md 5.1-5.4)
def _wise_continuity(conn: sqlite3.Connection, parsed: dict, acct: sqlite3.Row, new_ids: list[int],
                     allow_gap: bool = False) -> list[str]:
    """M1: the ledger just before the first new Wise row must equal that day's opening balance in the file.

    The file's own chain was already verified row by row; this ties the file to the LEDGER, so a missing export
    (e.g. 12-30 Sep) is refused instead of silently leaving a hole. The opening of the first new row's day is the
    first file row of that day: running balance minus its amount. Skipped when nothing is imported, or when the
    ledger holds no earlier rows for the account (a first import has nothing to compare with).
    """
    if not new_ids:
        return []
    day = conn.execute(
        f"SELECT MIN(date) FROM transactions WHERE id IN ({_qmarks(len(new_ids))})", new_ids).fetchone()[0]
    prior = (_day(day) - _dt.timedelta(days=1)).isoformat()
    if conn.execute("SELECT 1 FROM transactions WHERE account_id = ? AND date < ? LIMIT 1",
                    (acct["id"], day)).fetchone() is None:
        return []
    first = next((r for r in parsed["rows"] if r["date"] == day), None)
    if first is None or first.get("running_balance_cents") is None:
        return []
    expected = first["running_balance_cents"] - first["amount_cents"]
    got = _ledger_at(conn, acct["id"], prior)
    if got != expected:
        if allow_gap:
            return [f"{parsed['filename']}: Wise continuity NOT checked (allow_wise_gap): ledger {got} on {prior} "
                    f"vs {expected} in the file"]
        raise ValueError(
            f"{parsed['filename']}: the ledger balance on {prior} is {got} but this file continues from {expected} "
            f"on {day}; a Wise export between them is missing (or a row was changed). Import the missing period first"
        )
    return []


def _tie_checks(conn: sqlite3.Connection, parsed: dict, acct: sqlite3.Row,
                new_ids: list[int] | None = None, allow_wise_gap: bool = False) -> tuple[bool, list[str]]:
    """Ledger vs statement before commit. Returns (verified, warnings); a mismatch raises ValueError.

    Opening is verified when the previous statement's balance is on file (or the statement starts at the
    account's opening); without it (e.g. the missing Aug 2026 card cycle) the check is skipped with a warning,
    because the ledger legitimately cannot tie yet.
    """
    if parsed["kind"] == "wise_csv":
        return False, _wise_continuity(conn, parsed, acct, new_ids or [], allow_wise_gap)
    if parsed["kind"] not in ("chase5200_pdf", "card2110_pdf"):
        return False, []
    prior = (_day(parsed["period_start"]) - _dt.timedelta(days=1)).isoformat()
    verified = prior <= acct["opening_date"] or conn.execute(
        "SELECT 1 FROM statement_balances WHERE account_id = ? AND as_of = ?", (acct["id"], prior)
    ).fetchone() is not None
    if not verified:
        return False, [
            f"{parsed['filename']}: previous statement (balance on {prior}) is not imported; "
            "opening/closing tie-out not checked"
        ]
    got = _ledger_at(conn, acct["id"], prior)
    if got != parsed["opening_cents"]:
        raise ValueError(
            f"{parsed['filename']}: ledger balance on {prior} is {got} but the statement opens at "
            f"{parsed['opening_cents']}; an earlier statement is missing or a row was changed"
        )
    got = _ledger_at(conn, acct["id"], parsed["period_end"])
    if got != parsed["ending_cents"]:
        raise ValueError(
            f"{parsed['filename']}: ledger balance on {parsed['period_end']} is {got} but the statement closes at "
            f"{parsed['ending_cents']}"
        )
    return True, []


def _chase_fee_flags(conn, acct, parsed, new_ids: set[int]) -> tuple[dict[int, list[str]], list[str]]:
    """RULES.md 5.4: the $15 fee is expected only when the lowest daily balance < $2,000 AND the Ink-card
    activity waiver did not apply (both are printed on the statement). A fee row charged when it is not
    expected, or a second one, gets `unexpected_bank_fee`. The ledger-computed low (end-of-day balance on days
    with activity, fee rows excluded) is only used when the statement does not print its own."""
    fee, minbal = acct["monthly_fee_cents"], acct["fee_waiver_min_balance_cents"]
    if parsed["kind"] != "chase5200_pdf" or not fee or not minbal:
        return {}, []
    start, end = parsed["period_start"], parsed["period_end"]
    fee_rows = conn.execute(
        "SELECT id, date FROM transactions WHERE account_id = ? AND date BETWEEN ? AND ? AND amount_cents = ? "
        "AND description LIKE 'MONTHLY SERVICE FEE%' ORDER BY date, id",
        (acct["id"], start, end, -fee),
    ).fetchall()
    fee_ids = {r["id"] for r in fee_rows}
    bal = _ledger_at(conn, acct["id"], (_day(start) - _dt.timedelta(days=1)).isoformat())
    by_day: dict[str, int] = {}
    for r in conn.execute(
        "SELECT id, date, amount_cents FROM transactions WHERE account_id = ? AND date BETWEEN ? AND ?",
        (acct["id"], start, end),
    ):
        if r["id"] not in fee_ids:
            by_day[r["date"]] = by_day.get(r["date"], 0) + r["amount_cents"]
    computed = None
    for day in sorted(by_day):
        bal += by_day[day]
        computed = bal if computed is None else min(computed, bal)
    stated, ink = parsed.get("lowest_daily_balance_cents"), parsed.get("ink_activity_cents")
    low = stated if stated is not None else computed
    expected = low is not None and low < minbal and (ink is None or ink < INK_WAIVER_CENTS)
    flags: dict[int, list[str]] = {}
    warnings: list[str] = []
    for i, r in enumerate(fee_rows):
        if (i > 0 or not expected) and r["id"] in new_ids:
            flags[r["id"]] = ["unexpected_bank_fee"]
    if expected and not fee_rows:
        warnings.append(f"{parsed['filename']}: monthly fee expected (lowest daily balance {low}) but not charged")
    if stated is not None and computed is not None and stated != computed:
        warnings.append(
            f"{parsed['filename']}: ledger-computed lowest daily balance {computed} <> printed {stated}; a date may be off"
        )
    return flags, warnings


def _save_statement_balance(conn: sqlite3.Connection, parsed: dict, acct: sqlite3.Row, verified: bool) -> str | None:
    """Tie-out row at the statement's own closing date (RULES.md 5.1). Wise balances are saved by the page."""
    if parsed["kind"] not in ("chase5200_pdf", "card2110_pdf"):
        return None
    as_of, balance = parsed["period_end"], parsed["ending_cents"]
    if conn.execute("SELECT 1 FROM month_locks WHERE yyyy_mm = ?", (as_of[:7],)).fetchone():
        return None
    same = conn.execute(
        "SELECT as_of, balance_cents FROM statement_balances WHERE account_id = ? AND as_of = ?", (acct["id"], as_of)
    ).fetchone()
    if same:
        if same["balance_cents"] != balance:
            raise ValueError(
                f"{parsed['filename']}: a statement balance of {same['balance_cents']} is already on file for {as_of}"
            )
        return None
    if parsed["kind"] == "chase5200_pdf":  # a calendar month-end row for the month is kept; nothing is added
        month_row = conn.execute(
            "SELECT as_of, balance_cents FROM statement_balances WHERE account_id = ? AND substr(as_of,1,7) = ?",
            (acct["id"], as_of[:7]),
        ).fetchone()
        if month_row:
            if verified and _ledger_at(conn, acct["id"], month_row["as_of"]) != month_row["balance_cents"]:
                raise ValueError(
                    f"{parsed['filename']}: ledger on {month_row['as_of']} does not equal the statement balance on file"
                )
            return None
    conn.execute(
        "INSERT INTO statement_balances (account_id, as_of, balance_cents, source) VALUES (?,?,?,?)",
        (acct["id"], as_of, balance, "Chase PDF"),
    )
    return as_of


# ====================================================================== stages 3-8
class _Engine:
    """One run of stages 3-8 over a set of rows. Works on the open transaction; the caller commits."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.rules = _rules.load_rules()
        self.rule_by_id = {r.id: r for r in self.rules}
        self.cat_id = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}
        self.cat_name = {v: k for k, v in self.cat_id.items()}
        self.cat_type = {r["name"]: r["default_type"] for r in conn.execute("SELECT name, default_type FROM categories")}
        self.acct_id = {r["source_label"]: r["id"] for r in conn.execute("SELECT id, source_label FROM accounts")}
        self.acct_label = {v: k for k, v in self.acct_id.items()}
        self.acct_kind = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM accounts")}
        self.S: dict[int, dict] = {}

    # ---------------------------------------------------------------- state helpers
    def _fee_category(self, parent_category: str | None) -> tuple[str, str]:
        name = parent_category if parent_category in (OWNERS_DRAW, "Payable to WA Rubbermate") else "Bank Charges"
        return name, self.cat_type[name]

    def _load(self, ids) -> list[dict]:
        out = []
        for chunk in _chunks(ids):
            out += self.conn.execute(
                "SELECT t.id, t.date, t.account_id, t.description, t.amount_cents, t.fee_cents, t.payee, t.raw_type,"
                " t.source_ref, t.parent_id, t.notes, t.review_status, t.running_balance_cents, c.name AS category"
                f" FROM transactions t LEFT JOIN categories c ON c.id = t.category_id WHERE t.id IN ({_qmarks(len(chunk))})",
                chunk,
            ).fetchall()
        return [dict(r) for r in out]

    def _state(self, row: dict, **extra) -> dict:
        tags, _ = _rules._split_tags(row["notes"])
        rule_id = tags.get("rule") or None
        st = dict(row)
        st.update(
            account=self.acct_label[row["account_id"]], rule_id=rule_id, rule=self.rule_by_id.get(rule_id),
            reasons=[r for r in tags.get("review", "").split(",") if r],
            light=False, frozen=False, synth_child=None, **extra,
        )
        return st

    def _set_category(self, st: dict, name: str | None, typ: str | None = None) -> None:
        if name is None:
            self.conn.execute("UPDATE transactions SET category_id = NULL, type = NULL WHERE id = ?", (st["id"],))
        else:
            self.conn.execute(
                "UPDATE transactions SET category_id = ?, type = ? WHERE id = ?",
                (self.cat_id[name], typ or self.cat_type[name], st["id"]),
            )
        st["category"] = name

    def _add(self, st: dict, reason: str) -> None:
        if reason not in st["reasons"]:
            st["reasons"].append(reason)

    def _category_of(self, tx_id: int) -> str | None:
        if tx_id in self.S:
            return self.S[tx_id]["category"]
        r = self.conn.execute(
            "SELECT c.name FROM transactions t LEFT JOIN categories c ON c.id = t.category_id WHERE t.id = ?", (tx_id,)
        ).fetchone()
        return r[0] if r else None

    # ---------------------------------------------------------------- scope
    def eligible(self, ids) -> list[int]:
        """Not reviewed, not in a locked month, not a synthesised jar leg."""
        out = []
        for chunk in _chunks(ids):
            out += [r[0] for r in self.conn.execute(
                f"SELECT t.id FROM transactions t WHERE t.id IN ({_qmarks(len(chunk))}) AND t.review_status <> 'reviewed' "
                "AND substr(t.date,1,7) NOT IN (SELECT yyyy_mm FROM month_locks) "
                "AND NOT (t.account_id IN (SELECT id FROM accounts WHERE kind = 'jar') AND EXISTS "
                "(SELECT 1 FROM transactions m WHERE m.source_ref = t.source_ref AND m.account_id <> t.account_id))",
                chunk,
            )]
        return sorted(out)

    def _scope(self, seed) -> list[int]:
        ids = {int(i) for i in seed}
        for _ in range(2):  # fee children of the parents, parents of the fee children
            add: set[int] = set()
            for chunk in _chunks(ids):
                q = _qmarks(len(chunk))
                add |= {r[0] for r in self.conn.execute(f"SELECT id FROM transactions WHERE parent_id IN ({q})", chunk)}
                add |= {r[0] for r in self.conn.execute(
                    f"SELECT parent_id FROM transactions WHERE id IN ({q}) AND parent_id IS NOT NULL", chunk)}
            ids |= add
        return self.eligible(ids)

    # ---------------------------------------------------------------- run
    def run(self, seed, extra: dict[int, list[str]] | None = None) -> dict:
        extra = extra or {}
        scope = self._scope(seed)
        self.S = {r["id"]: self._state(r) for r in self._load(scope)}
        self._reset()
        self._stage3(extra)
        self._stage4()
        self._stage5()
        self._stage6()
        self._stage7()
        touched = self._stage8()
        main = [s for s in self.S.values() if not s["light"]]
        counts: Counter = Counter()
        for s in main:
            counts.update(s["reasons"])
        return {
            "rows": len(scope),
            "categorized": sum(1 for i in scope if i in self.S and self.S[i]["category"] is not None),
            "review_counts": dict(counts),
            "touched": touched,
        }

    # ---------------------------------------------------------------- reset (re-run semantics, RULES.md 4.2/4.3)
    def _reset(self) -> None:
        conn = self.conn
        for st in self.S.values():
            st["sticky"] = [r for r in STICKY_REASONS if r in st["reasons"]]
        for st in self.S.values():  # undo an earlier split in place so rules see the bank's cash amount
            if st["parent_id"] is not None:
                continue
            child = conn.execute(
                "SELECT * FROM transactions WHERE account_id = ? AND source_ref = ? AND id <> ?",
                (st["account_id"], FEE_PREFIX + st["source_ref"], st["id"]),
            ).fetchone()
            if child is None:
                continue
            fee = -child["amount_cents"]
            if not _is_synthetic_fee(child, st["id"]):  # real Wise FEE row (regime A): the fee is not inside
                if fee > 0 and st["fee_cents"] != fee:
                    conn.execute("UPDATE transactions SET fee_cents = ? WHERE id = ?", (fee, st["id"]))
                    st["fee_cents"] = fee
                continue
            if child["review_status"] == "reviewed":
                st["frozen"] = True  # RULES.md 4.3: a reviewed child blocks merge/split; nothing is changed
                continue
            cash = st["amount_cents"] + child["amount_cents"]
            conn.execute("UPDATE transactions SET amount_cents = ?, fee_cents = ? WHERE id = ?", (cash, fee, st["id"]))
            st["amount_cents"], st["fee_cents"], st["synth_child"] = cash, fee, child["id"]
        ids = sorted(self.S)
        for chunk in _chunks(ids):
            q = _qmarks(len(chunk))
            conn.execute(f"UPDATE transactions SET category_id = NULL, type = NULL WHERE id IN ({q})", chunk)
            paid = [r[0] for r in conn.execute(
                f"SELECT DISTINCT invoice_id FROM invoice_payments WHERE transaction_id IN ({q})", chunk)]
            conn.execute(f"DELETE FROM invoice_payments WHERE transaction_id IN ({q})", chunk)
            for inv in paid:  # RULES.md 4.2: the invoices go back to Pending unless another receipt holds them
                conn.execute(
                    "UPDATE invoices SET status = 'Pending' WHERE id = ? AND status = 'Paid' AND type <> 'Voided' "
                    "AND NOT EXISTS (SELECT 1 FROM invoice_payments WHERE invoice_id = ?)", (inv, inv))
        for st in self.S.values():
            st["category"] = None
            new_notes = _rules._retag(st["notes"], rule=st["rule_id"], reasons=[])
            if new_notes != st["notes"]:
                conn.execute("UPDATE transactions SET notes = ? WHERE id = ?", (new_notes, st["id"]))
                st["notes"] = new_notes
            st["reasons"] = []

    # ---------------------------------------------------------------- stage 3
    def _stage3(self, extra: dict[int, list[str]]) -> None:
        _rules.categorize(_NoCommit(self.conn), sorted(self.S))
        for row in self._load(sorted(self.S)):
            st = self.S[row["id"]]
            tags, _ = _rules._split_tags(row["notes"])
            st.update(
                notes=row["notes"], category=row["category"], rule_id=tags.get("rule") or None,
                reasons=[r for r in tags.get("review", "").split(",") if r],
            )
            st["rule"] = self.rule_by_id.get(st["rule_id"])
            kept = set(st["sticky"]) | set(extra.get(st["id"], []))
            for reason in STICKY_REASONS:
                if reason in kept:
                    self._add(st, reason)

    # ---------------------------------------------------------------- stage 4
    def _parent_ref(self, st: dict) -> str | None:
        if st["source_ref"].startswith(FEE_PREFIX):
            return st["source_ref"][len(FEE_PREFIX):]
        m = _FEE_DESC_RE.match(st["description"])
        return m.group(1) if m else None

    def _stage4(self) -> None:
        conn = self.conn
        for st in sorted(self.S.values(), key=lambda s: (s["date"], s["id"])):  # pass 1: link only
            if not (st["rule"] and st["rule"].action == "follow_parent"):
                continue
            ref = self._parent_ref(st)
            parent = None
            if ref:
                parent = conn.execute(
                    "SELECT id FROM transactions WHERE account_id = ? AND source_ref = ? AND id <> ?",
                    (st["account_id"], ref, st["id"]),
                ).fetchone()
            if parent is None:
                self._add(st, "orphan_fee")
                if st["parent_id"] is not None:
                    conn.execute("UPDATE transactions SET parent_id = NULL WHERE id = ?", (st["id"],))
                    st["parent_id"] = None
                continue
            if st["parent_id"] != parent["id"]:
                conn.execute("UPDATE transactions SET parent_id = ? WHERE id = ?", (parent["id"], st["id"]))
                st["parent_id"] = parent["id"]
        for st in sorted(self.S.values(), key=lambda s: (s["date"], s["id"])):  # pass 2: assign
            if st["parent_id"] is None:
                continue
            pid = st["parent_id"]
            p_cat = self._category_of(pid)
            p_rule = self.S[pid]["rule"] if pid in self.S else None
            decided = p_cat is not None or (p_rule is not None and p_rule.action == "needs_invoice")
            if not decided:
                self._set_category(st, None)
                self._add(st, "parent_uncategorised")
            else:
                name, typ = self._fee_category(p_cat)
                self._set_category(st, name, typ)

    # ---------------------------------------------------------------- stage 5
    def _stage5(self) -> None:
        conn = self.conn
        for st in sorted(self.S.values(), key=lambda s: (s["date"], s["id"])):
            if st["parent_id"] is not None or st["frozen"]:
                continue
            real = conn.execute(
                "SELECT 1 FROM transactions WHERE account_id = ? AND source_ref = ? AND id <> ? "
                "AND NOT (running_balance_cents IS NULL AND payee = 'Wise' AND parent_id = ?)",
                (st["account_id"], FEE_PREFIX + st["source_ref"], st["id"], st["id"]),
            ).fetchone()
            if real:  # regime A: the fee is its own CSV row; never split, never merged
                if st["fee_cents"] != 0:
                    conn.execute("UPDATE transactions SET fee_cents = 0 WHERE id = ?", (st["id"],))
                    st["fee_cents"] = 0
                continue
            fee = st["fee_cents"]
            if fee <= 0:
                continue
            cash = st["amount_cents"]
            whole = st["rule"] is None or st["category"] == OWNERS_DRAW
            child_id = st["synth_child"]
            if whole:  # uncategorised, or an owner draw keeping the fee inside: ONE row
                if child_id is not None:
                    conn.execute("DELETE FROM transactions WHERE id = ?", (child_id,))
                    self.S.pop(child_id, None)
                    st["synth_child"] = None
                continue
            conn.execute(
                "UPDATE transactions SET amount_cents = ?, fee_cents = 0 WHERE id = ?", (cash + fee, st["id"])
            )
            st["amount_cents"], st["fee_cents"] = cash + fee, 0
            name, typ = self._fee_category(st["category"])
            if child_id is not None:
                child = self.S.get(child_id)
                conn.execute("UPDATE transactions SET category_id = ?, type = ? WHERE id = ?",
                             (self.cat_id[name], typ, child_id))
                if child:
                    child["category"] = name
                continue
            fee_row = {"account": st["account"], "description": f"Wise Charges for: {st['source_ref']}",
                       "amount_cents": -fee, "raw_type": st["raw_type"], "payee": "Wise", "fee_cents": 0}
            rule, _ = _rules._decide(self.rules, fee_row)
            new_id = insert_transaction(
                conn, date=st["date"], account_id=st["account_id"], description=fee_row["description"],
                amount_cents=-fee, source_ref=FEE_PREFIX + st["source_ref"], parent_id=st["id"], fee_cents=0,
                payee="Wise", raw_type=st["raw_type"], category_id=self.cat_id[name], type=typ,
                review_status="auto", notes=f"[rule:{rule.id}]" if rule else None,
            )
            assert st["amount_cents"] + (-fee) == cash  # parent + child = the CSV cash movement
            child = self._state(self._load([new_id])[0])
            self.S[new_id] = child
            st["synth_child"] = new_id

    # ---------------------------------------------------------------- stage 6
    def _role_of(self, notes: str | None) -> str | None:
        tags, _ = _rules._split_tags(notes)
        rule = self.rule_by_id.get(tags.get("rule") or "")
        return rule.transfer_role if rule else None

    def _stage6(self) -> None:
        conn = self.conn
        # 6a: the jar leg of a jar move is synthesised from the main-account row
        for st in sorted(self.S.values(), key=lambda s: (s["date"], s["id"])):
            if not (st["rule"] and st["rule"].transfer_role == "jar_move" and st["category"] == BANK_TRANSFER):
                continue
            m = _JAR_NAME_RE.search(st["description"])
            jar_id = self.acct_id.get(JAR_ACCOUNTS.get(m.group(1), "")) if m else None
            if jar_id is None:
                continue
            desc = re.sub(r"\bto (Rent & Utilities|Taxes)$", "from USD", st["description"])
            desc = re.sub(r"\bfrom (Rent & Utilities|Taxes)$", "to USD", desc) if desc == st["description"] else desc
            mirror = conn.execute(
                "SELECT id, date, amount_cents, review_status FROM transactions WHERE account_id = ? AND source_ref = ?",
                (jar_id, st["source_ref"]),
            ).fetchone()
            if mirror is None:
                insert_transaction(
                    conn, date=st["date"], account_id=jar_id, description=desc, amount_cents=-st["amount_cents"],
                    source_ref=st["source_ref"], category_id=self.cat_id[BANK_TRANSFER],
                    type=self.cat_type[BANK_TRANSFER], review_status="auto",
                )
            elif mirror["review_status"] != "reviewed" and (
                mirror["amount_cents"] != -st["amount_cents"] or mirror["date"] != st["date"]
            ):
                conn.execute("UPDATE transactions SET amount_cents = ?, date = ? WHERE id = ?",
                             (-st["amount_cents"], st["date"], mirror["id"]))

        # 6b/6c: greedy one-to-one pairing over every leg in the database, oldest first
        legs = []
        for r in conn.execute(
            "SELECT t.id, t.date, t.account_id, t.amount_cents, t.notes, t.review_status FROM transactions t "
            "JOIN categories c ON c.id = t.category_id WHERE c.name = ? AND t.parent_id IS NULL "
            "ORDER BY t.date, t.id", (BANK_TRANSFER,)
        ):
            role = self._role_of(r["notes"])
            if role and role != "jar_move":
                legs.append({**dict(r), "role": role})
        asof: dict[int, str] = {}
        for r in conn.execute("SELECT account_id, MAX(as_of) AS m FROM statement_balances GROUP BY account_id"):
            asof[r["account_id"]] = r["m"]
        role_accounts: dict[str, set[int]] = {}
        for rule in self.rules:
            if rule.transfer_role and "account" in rule.match and rule.match["account"] in self.acct_id:
                role_accounts.setdefault(rule.transfer_role, set()).add(self.acct_id[rule.match["account"]])

        def covered(leg: dict, partner_role: str) -> bool:
            need = (_day(leg["date"]) + _dt.timedelta(days=COVER_DAYS)).isoformat()
            return any(asof.get(a, "") >= need for a in role_accounts.get(partner_role, ()))

        flagged: set[int] = set()
        for out_role, in_role, (lo, hi) in PAIRS:
            outs = [x for x in legs if x["role"] == out_role]
            ins = [x for x in legs if x["role"] == in_role]
            taken: set[int] = set()
            for o in outs:
                cands = [
                    i for i in ins
                    if i["id"] not in taken and i["amount_cents"] == -o["amount_cents"]
                    and lo <= (_day(i["date"]) - _day(o["date"])).days <= hi
                ]
                if cands:
                    best = min(cands, key=lambda i: (abs((_day(i["date"]) - _day(o["date"])).days), i["date"], i["id"]))
                    taken.add(best["id"])
                    o["paired"] = True
                elif covered(o, in_role):
                    flagged.add(o["id"])  # else: in transit, not a review item
            for i in ins:
                if i["id"] not in taken and covered(i, out_role):
                    flagged.add(i["id"])
        locked = {r[0] for r in conn.execute("SELECT yyyy_mm FROM month_locks")}
        for leg in legs:
            if leg["id"] in self.S:
                if leg["id"] in flagged:
                    self._add(self.S[leg["id"]], "unpaired_transfer")
                continue
            if leg["review_status"] == "reviewed" or leg["date"][:7] in locked:
                continue
            row = self._load([leg["id"]])[0]
            st = self._state(row)
            old = list(st["reasons"])
            st["reasons"] = [r for r in old if r != "unpaired_transfer"]
            if leg["id"] in flagged:
                st["reasons"].append("unpaired_transfer")
            if set(st["reasons"]) != set(old):  # a stale flag cleared, or a new one: finalise only these
                st["light"] = True
                self.S[leg["id"]] = st

    # ---------------------------------------------------------------- stage 7
    def _stage7(self) -> None:
        conn = self.conn
        receipts = sorted(
            (s for s in self.S.values() if s["rule"] and s["rule"].action == "needs_invoice" and s["parent_id"] is None),
            key=lambda s: (s["date"], s["id"]),
        )
        for st in receipts:  # oldest first: an invoice can be consumed only once
            prop = _invoices.propose_match(conn, st["id"])
            for reason in prop["review_reasons"]:
                self._add(st, reason)
            if prop["category"] is None:
                self._add(st, "no_invoice_match")
                continue
            self._set_category(st, prop["category"], prop["type"])  # BEFORE the inserts: the trigger reads it
            total = 0
            for inv_id, cents in prop["allocations"]:
                conn.execute(
                    "INSERT INTO invoice_payments (transaction_id, invoice_id, amount_cents) VALUES (?,?,?)",
                    (st["id"], inv_id, cents),
                )
                conn.execute("UPDATE invoices SET status = 'Paid' WHERE id = ?", (inv_id,))
                total += cents
            assert not prop["allocations"] or total == st["amount_cents"]  # SPEC rule 2

    # ---------------------------------------------------------------- stage 8
    def _stage8(self) -> list[int]:
        conn = self.conn
        touched = []
        for chunk in _chunks(sorted(self.S)):
            rows = {r["id"]: r for r in conn.execute(
                f"SELECT id, category_id, notes, review_status FROM transactions WHERE id IN ({_qmarks(len(chunk))})", chunk)}
            for tx_id in chunk:
                st, cur = self.S[tx_id], rows[tx_id]
                status = "needs_review" if (cur["category_id"] is None or st["reasons"]) else "auto"
                notes = _rules._retag(cur["notes"], rule=st["rule_id"], reasons=st["reasons"])
                if (status, notes) != (cur["review_status"], cur["notes"]):
                    conn.execute("UPDATE transactions SET review_status = ?, notes = ? WHERE id = ?", (status, notes, tx_id))
                touched.append(tx_id)
        return touched


# ====================================================================== public API
def _run_rules_tx(conn: sqlite3.Connection, ids, extra: dict[int, list[str]] | None = None) -> dict:
    return _Engine(conn).run(ids, extra)


def _all_open_ids(conn: sqlite3.Connection, month: str | None) -> list[int]:
    sql = "SELECT id FROM transactions"
    params: list = []
    if month is not None:
        sql += " WHERE substr(date,1,7) = ?"
        params.append(month)
    return [r[0] for r in conn.execute(sql, params)]


def run_rules(conn: sqlite3.Connection, row_ids: list[int] | None = None, *, month: str | None = None) -> dict:
    """RULES.md stages 3-8 on `row_ids`; None = every unreviewed row of `month` (or of all open months).

    Rows are reconciled, never re-split: a re-run with unchanged rules changes nothing. Reviewed rows, rows in
    locked months and synthesised jar legs are left alone. Commits, or rolls back and raises.
    Returns {'rows', 'categorized', 'review_counts'}.
    """
    if month is not None and not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month):
        raise ValueError(f"month must be YYYY-MM, got {month!r}")
    try:
        ids = _all_open_ids(conn, month) if row_ids is None else list(row_ids)
        rep = _run_rules_tx(conn, ids)
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        err = translate_integrity_error(exc)
        raise (err if err is not exc else ValueError(str(exc))) from exc
    except BaseException:
        conn.rollback()
        raise
    return {k: rep[k] for k in ("rows", "categorized", "review_counts")}


def run_import(conn: sqlite3.Connection, path: str | Path, *, kind: str | None = None,
               allow_wise_gap: bool = False) -> dict:
    """RULES.md run_import(file): backup, parse, normalize, insert, stages 3-8, statement balance.

    One DB transaction per file; any failure rolls it back and raises ValueError. Rows dated before the
    account's opening date are skipped, rows dated on or before the account's seed cutoff
    (accounts.seeded_through, set by the seed command) are skipped, rows in a locked month are rejected.
    Returns {'inserted', 'skipped_duplicate', 'skipped_before_opening', 'skipped_seeded', 'rejected_locked',
     'ledger_rows', 'review_counts', 'warnings', 'kind', 'account', 'filename'}.
    A Wise file that does not continue from the ledger is refused (M1) unless allow_wise_gap=True, which only
    downgrades the refusal to a warning (the known 12-30 Sep 2026 hole in the test fixtures; the pages never set it).
    'skipped_seeded' counts lines already in the seeded 2026 history (0 when the account has no cutoff).
    """
    parsed = _parse(path, kind, None)
    if conn.in_transaction:
        conn.commit()
    acct = conn.execute("SELECT * FROM accounts WHERE source_label = ?", (parsed["account"],)).fetchone()
    if acct is None:
        raise ValueError(f"{parsed['filename']}: account {parsed['account']!r} is not in accounts")
    rows, before, rejected, seeded = _normalize(conn, parsed, acct)
    _backup(conn, "import")
    conn.execute("BEGIN")
    try:
        max_before = conn.execute("SELECT COALESCE(MAX(id), 0) FROM transactions").fetchone()[0]
        new_ids, dups, extra = _insert_rows(conn, rows, acct["id"])
        verified, warnings = _tie_checks(conn, parsed, acct, new_ids, allow_wise_gap)
        if verified:
            flags, more = _chase_fee_flags(conn, acct, parsed, set(new_ids))
            warnings += more
            for tx_id, reasons in flags.items():
                extra.setdefault(tx_id, []).extend(reasons)
        _save_statement_balance(conn, parsed, acct, verified)  # first: transfer coverage reads statement dates
        rep = _run_rules_tx(conn, new_ids, extra) if new_ids else {"review_counts": {}}
        ledger_rows = conn.execute("SELECT COUNT(*) FROM transactions WHERE id > ?", (max_before,)).fetchone()[0]
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        err = translate_integrity_error(exc)
        raise (err if err is not exc else ValueError(f"{parsed['filename']}: {exc}")) from exc
    except BaseException:
        conn.rollback()
        raise
    return {
        "inserted": len(new_ids), "skipped_duplicate": dups, "skipped_before_opening": before,
        "skipped_seeded": seeded, "rejected_locked": rejected, "ledger_rows": ledger_rows,
        "review_counts": rep["review_counts"],
        "warnings": warnings, "kind": parsed["kind"], "account": parsed["account"], "filename": parsed["filename"],
    }


# ====================================================================== re-exports for the pages
from tc.pipeline_ui import (  # noqa: E402,F401
    add_adjustment,
    confirm,
    list_accounts,
    list_adjustments,
    list_categories,
    mark_duplicate,
    review_queue,
    search_transactions,
    set_category,
    set_date,
)
from tc.rules import add_rule  # noqa: E402,F401

__all__ = [
    "parse_statement", "run_import", "run_rules", "seed_cutoff", "review_queue", "confirm", "mark_duplicate",
    "search_transactions", "set_category", "list_accounts", "list_categories", "list_adjustments",
    "add_adjustment", "add_rule", "set_date",
]
