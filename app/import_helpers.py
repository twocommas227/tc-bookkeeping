"""Import page logic with no Streamlit in it (UI.md page 1): preview -> import -> report text.

Only tc modules are called (pipeline, close, reports, db); there is no SQL here.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

from tc import close, db, pipeline, reports

KIND_LABELS = {
    "wise_csv": "Wise CSV",
    "chase5200_pdf": "Chase *5200 PDF",
    "chase5200_csv": "Chase *5200 CSV",
    "card2110_pdf": "Card *2110 PDF",
}
SUFFIXES = (".csv", ".pdf")
WISE_SOURCE = "Wise CSV"
MANUAL_SOURCE = "Manual (Import page)"
_PERIOD_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})")


# ------------------------------------------------------------------ files
def list_folder(folder: str | Path) -> list[Path]:
    """The .csv / .pdf files directly inside `folder`, sorted by name; [] when it is missing or empty."""
    p = Path(str(folder).strip()).expanduser()
    if not p.is_dir():
        return []
    return sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in SUFFIXES)


def save_upload(name: str, data: bytes, dest_dir: str | Path) -> Path:
    """Write an uploaded file under `dest_dir` (keeping its extension) and return the path."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / Path(name).name
    n = 1
    while path.exists():  # two uploads with the same name
        path = dest / f"{path.stem.split('~')[0]}~{n}{Path(name).suffix}"
        n += 1
    path.write_bytes(data)
    return path


# ------------------------------------------------------------------ preview (B)
def _account_kinds(conn: sqlite3.Connection) -> dict[str, dict]:
    return {a["source_label"]: a for a in pipeline.list_accounts(conn)}


def error_text(filename: str, exc: Exception) -> str:
    """One readable line: '<file>: <reason>' (the pipeline messages usually start with the file name already)."""
    msg = str(exc).strip() or exc.__class__.__name__
    return msg if msg.startswith(filename) else f"{filename}: {msg}"


def preview_file(conn: sqlite3.Connection, path: str | Path, filename: str | None = None) -> dict:
    """Parse one file (no database write) and add the lock test.

    Returns {filename, path, parsed, error, label, account, account_kind, locked_months, locked_rows,
    importable, seeded_rows, seed_cutoff}. `error` is a readable string when the file cannot be read or does not
    tie; then parsed is None. A file with any row in a locked month is not importable (the importers are
    all-or-nothing per file); rows dated on or before the account's seed cutoff are skipped on import, so they
    are counted in `seeded_rows` and never count as locked.
    """
    path = Path(path)
    name = filename or path.name
    item = {
        "filename": name, "path": str(path), "parsed": None, "error": None, "label": "", "account": "",
        "account_kind": None, "locked_months": [], "locked_rows": [], "importable": False,
        "seeded_rows": 0, "seed_cutoff": None,
    }
    try:
        parsed = pipeline.parse_statement(path, filename=name)
    except ValueError as exc:
        item["error"] = error_text(name, exc)
        return item
    except Exception as exc:  # a parser bug must not take the page down
        item["error"] = error_text(name, ValueError(f"cannot read the file ({exc.__class__.__name__}: {exc})"))
        return item
    acct = _account_kinds(conn).get(parsed["account"])
    cutoff = pipeline.seed_cutoff(conn, parsed["account"])
    # lines the seeded history already holds are skipped on import, so they cannot hit a locked month
    live = [r for r in parsed["rows"] if not cutoff or r["date"] > cutoff]
    months = sorted({r["date"][:7] for r in live})
    locked = [m for m in months if close.is_locked(conn, m)]
    item.update(
        parsed=parsed, label=KIND_LABELS.get(parsed["kind"], parsed["kind"]), account=parsed["account"],
        account_kind=acct["kind"] if acct else None, locked_months=locked,
        locked_rows=[r for r in live if r["date"][:7] in locked],
        importable=acct is not None and not locked,
        seed_cutoff=cutoff, seeded_rows=len(parsed["rows"]) - len(live),
    )
    if acct is None:
        item["error"] = f"{name}: account {parsed['account']!r} is not set up in this database"
    return item


def preview_files(conn: sqlite3.Connection, entries: list[tuple[str, str | Path]]) -> list[dict]:
    """entries = [(display name, path)]; one preview dict per file, in order (a bad file does not stop the others)."""
    return [preview_file(conn, path, name) for name, path in entries]


def signed(item: dict, cents: int | None) -> str:
    """Opening / ending shown the way the workbook does: the card as a positive liability."""
    return _money(cents, flip=item.get("account_kind") == "card")


def _money(cents: int | None, *, flip: bool = False) -> str:
    return "n/a" if cents is None else db.cents_to_str(-cents if flip else cents)


def summary_row(item: dict) -> dict:
    """One row of the preview table (B)."""
    p = item["parsed"]
    if p is None:
        return {"File": item["filename"], "Detected as": "not recognised", "Account": "", "Lines": 0,
                "Period": "", "Opening": "", "Ending": "", "Locked?": ""}
    return {
        "File": item["filename"], "Detected as": item["label"], "Account": p["account"], "Lines": len(p["rows"]),
        "Period": f"{p['period_start']} to {p['period_end']}", "Opening": signed(item, p["opening_cents"]),
        "Ending": signed(item, p["ending_cents"]),
        "Locked?": ("yes: " + ", ".join(item["locked_months"])) if item["locked_months"] else "no",
    }


def lines_table(rows: list[dict]) -> list[dict]:
    """Per-line preview rows (UI.md B): Date, Description, Amount, Fee, Source ref."""
    return [
        {"Date": r["date"], "Description": r["description"], "Amount": _money(r["amount_cents"]),
         "Fee": _money(r.get("fee_cents") or 0), "Source ref": r["source_ref"]}
        for r in rows
    ]


# ------------------------------------------------------------------ statement balance found in file (C)
def wise_default_as_of(parsed: dict) -> str:
    """Period end in the file name (..._2026-10-01_2026-10-31.csv) when it has one, else the last row date."""
    m = _PERIOD_IN_NAME.search(parsed["filename"])
    return m.group(2) if m else parsed["period_end"]


def balance_candidates(items: list[dict]) -> list[dict]:
    """Previewed files whose ending balance the page may save itself: Wise CSV with an ending balance."""
    return [i for i in items if i["parsed"] and i["parsed"]["kind"] == "wise_csv" and i["parsed"]["ending_cents"] is not None]


def _account_id(conn: sqlite3.Connection, source_label: str) -> int:
    acct = _account_kinds(conn).get(source_label)
    if acct is None:
        raise ValueError(f"account {source_label!r} is not set up in this database")
    return acct["id"]


def existing_balance(conn: sqlite3.Connection, source_label: str, as_of: str) -> int | None:
    """The statement balance already saved for this account and date (signed asset cents), else None."""
    acct = _account_kinds(conn).get(source_label)
    if acct is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of or ""):
        return None
    for row in reports.tie_out(conn, as_of[:7]):
        if row["account_code"] == acct["code"] and row["as_of"] == as_of and row["statement_cents"] is not None:
            return row["statement_cents"]
    return None


def wise_balance_state(conn: sqlite3.Connection, item: dict, as_of: str) -> dict:
    """{locked, existing_cents, conflict, default_save} for the Wise balance checkbox.

    Unticked by default when the date is in a locked month or a different balance is already saved for it
    (nothing is overwritten silently)."""
    p = item["parsed"]
    locked = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of or "")) and close.is_locked(conn, as_of[:7])
    existing = existing_balance(conn, p["account"], as_of)
    conflict = existing is not None and existing != p["ending_cents"]
    return {"locked": locked, "existing_cents": existing, "conflict": conflict,
            "default_save": not (locked or conflict)}


# ------------------------------------------------------------------ import (D) and result (E)
def import_all(conn: sqlite3.Connection, items: list[dict], wise_saves: dict[str, str] | None = None) -> list[dict]:
    """Import every importable file with pipeline.run_import; never raises.

    wise_saves = {path: 'YYYY-MM-DD'} for the Wise files whose ending balance should be saved at that date
    (close.set_statement_balance). Returns one dict per file:
    {filename, report, error, locked_rows, locked_months, balance_msg, balance_error}.
    Files with an error, or with rows in a locked month, are not sent to run_import.
    """
    wise_saves = wise_saves or {}
    out = []
    for item in items:
        res = {"filename": item["filename"], "report": None, "error": None, "locked_rows": [],
               "locked_months": [], "balance_msg": None, "balance_error": None}
        out.append(res)
        if item["error"] or item["parsed"] is None:
            res["error"] = item["error"] or f"{item['filename']}: not recognised"
            continue
        if item["locked_months"]:
            res["locked_rows"], res["locked_months"] = item["locked_rows"], item["locked_months"]
            continue
        try:
            res["report"] = pipeline.run_import(conn, item["path"], kind=item["parsed"]["kind"])
        except ValueError as exc:  # does not tie, locked month, unknown account: the file was rolled back
            res["error"] = error_text(item["filename"], exc)
            continue
        except Exception as exc:
            res["error"] = error_text(item["filename"], ValueError(f"import failed ({exc.__class__.__name__}: {exc})"))
            continue
        as_of = wise_saves.get(item["path"])
        if as_of:
            p = item["parsed"]
            try:
                close.set_statement_balance(conn, _account_id(conn, p["account"]), as_of, p["ending_cents"], source=WISE_SOURCE)
                res["balance_msg"] = f"Statement balance {db.cents_to_str(p['ending_cents'])} saved for {p['account']} at {as_of}."
            except ValueError as exc:
                res["balance_error"] = f"{item['filename']}: balance not saved: {exc}"
    return out


def review_total(report: dict) -> int:
    return sum((report.get("review_counts") or {}).values())


def seeded_text(n: int, *, future: bool = False) -> str:
    """Plain-words count of lines that the seeded 2026 history already holds ('' for 0)."""
    if not n:
        return ""
    lines = f"{n} line{'s' if n != 1 else ''}"
    if future:
        return f"{lines} already in your seeded 2026 history will be skipped."
    return f"{lines} already in your seeded 2026 history {'were' if n != 1 else 'was'} skipped."


def format_report(res: dict) -> str:
    """The result line (E) for one file, from an `import_all` entry."""
    name = res["filename"]
    if res["error"]:
        return res["error"]
    if res["locked_rows"]:
        return (f"{name} - not imported: {len(res['locked_rows'])} line(s) fall in locked month(s) "
                f"{', '.join(res['locked_months'])}. Re-open the month on Reports first.")
    r = res["report"]
    seeded = seeded_text(r.get("skipped_seeded", 0))
    seeded = f" {seeded}" if seeded else ""
    if r["inserted"] == 0 and r["skipped_duplicate"] > 0 and not r["rejected_locked"]:
        head = f"{name} - 0 inserted, {r['skipped_duplicate']} duplicates - already imported."
        if r["skipped_before_opening"]:
            head += f" {r['skipped_before_opening']} before opening date."
        return head + seeded
    if r["inserted"] == 0 and r.get("skipped_seeded", 0) > 0 and not r["rejected_locked"]:
        head = f"{name} - 0 inserted."
        if r["skipped_before_opening"]:
            head += f" {r['skipped_before_opening']} before opening date."
        return head + seeded
    text = (f"{name} - {r['inserted']} lines inserted, {r['skipped_duplicate']} duplicates skipped, "
            f"{r['skipped_before_opening']} before opening date, {r['rejected_locked']} rejected (locked month). "
            f"{r['ledger_rows']} ledger rows after fee split. Needs review: {review_total(r)}")
    counts = r.get("review_counts") or {}
    if counts:
        text += " (" + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) + ")"
    return text + "." + seeded


def result_level(res: dict) -> str:
    """st.* method name that fits the result: error / warning / success."""
    if res["error"]:
        return "error"
    if res["locked_rows"] or (res["report"] and res["report"]["rejected_locked"]):
        return "warning"
    return "success"


# ------------------------------------------------------------------ manual statement balance
def parse_manual_date(text: str) -> str:
    try:
        return date.fromisoformat((text or "").strip()).isoformat()
    except ValueError:
        raise ValueError(f"Statement date {text!r} is not a date; use YYYY-MM-DD, the closing date printed on the statement") from None


def save_manual_balance(conn: sqlite3.Connection, account_id: int, as_of_text: str, amount_text: str) -> str:
    """Save a statement balance typed by hand; returns the confirmation text, raises a readable ValueError.

    The card is typed as a positive liability (the statement's New Balance) and stored negative (UI.md section 0);
    banks are typed as they are."""
    acct = next((a for a in pipeline.list_accounts(conn) if a["id"] == account_id), None)
    if acct is None:
        raise ValueError("Choose an account")
    as_of = parse_manual_date(as_of_text)
    if not (amount_text or "").strip():
        raise ValueError("Type the statement ending balance")
    try:
        cents = db.to_cents(amount_text.strip())
    except (ValueError, ArithmeticError, TypeError):
        raise ValueError(f"Balance {amount_text!r} is not an amount; type it like 1,456.41") from None
    stored = -cents if acct["kind"] == "card" else cents
    close.set_statement_balance(conn, account_id, as_of, stored, source=MANUAL_SOURCE)
    shown = db.cents_to_str(cents)
    return f"Saved statement balance {shown} for {acct['source_label']} at {as_of}" + (
        " (stored as a negative liability)." if acct["kind"] == "card" else ".")
