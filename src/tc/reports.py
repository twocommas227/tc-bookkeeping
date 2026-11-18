"""Reports engine: P&L, bank tie-out, transfers in transit, balance sheet, owner's draws.

Money is integer cents, dates are ISO 'YYYY-MM-DD', months are 'YYYY-MM'. Read-only: nothing here writes.
Signatures and return shapes follow docs/UI.md section 4.4.
"""

from __future__ import annotations

import calendar
import sqlite3

REIMBURSABLE = "Reimbursable Expenses Recoverable"
PAYABLE_WA = "Payable to WA Rubbermate"
OWNER_DRAW = "Owner's Draw"

# P&L labels exactly as on the workbook 'Profit & Loss' tab (golden_pnl.csv); other categories use their name.
_PNL_LABELS = {
    "Service Revenue": "Commission Revenue (cash received)",
    "Other Professional Services": "Professional Services (HTGROWTH retainer, Firstbase fees)",
    "Postage & Delivery": "Postage & Delivery (Firstbase mailbox / registered agent)",
    "Software & Web Services": "Software & Web Services",
    "Bank Charges": "Bank Charges (Wise fees, Chase MSF, FX fees)",
    "Interest Income": "Interest Income",
}
_ADJ_LINE = "Prior-period AR adjustment (non-cash write-off, booked at Sep close)"
_ADJ_SECTIONS = ("revenue", "sga", "other_income")

# Balance-sheet labels exactly as on the workbook 'Balance Sheet' tab.
_BS_LABELS = {
    "WISE_1430": "Wise US Checking *1430",
    "CHASE_5200": "Chase Checking *5200",
    "CARD_2110": "Chase Credit Card *2110",
}
_JARS_LABEL = "Wise jars (Taxes / Rent)"


def _months(year: int) -> list[str]:
    return [f"{year:04d}-{m:02d}" for m in range(1, 13)]


def _month_end(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{month}-{calendar.monthrange(y, m)[1]:02d}"


def _row(line: str, kind: str, months: dict[str, int]) -> dict:
    return {"line": line, "kind": kind, "months": dict(months), "ytd": sum(months.values())}


def _add(*series: dict[str, int]) -> dict[str, int]:
    return {m: sum(s[m] for s in series) for m in series[0]}


def _sub(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    return {m: a[m] - b[m] for m in a}


# ------------------------------------------------------------------ P&L
def pnl_monthly(conn: sqlite3.Connection, year: int) -> list[dict]:
    """P&L lines in workbook order, each ``{line, kind ('detail'|'total'), months{YYYY-MM: cents}, ytd}``.

    Revenue, expenses and other income are all positive numbers (as on the workbook). The 'Prior-period AR
    adjustment' line is the sum of non-cash P&L adjustments (signed effect on net income). Owner's draws,
    reimbursable, payable and transfers are not in the P&L. All 12 months of the year are returned.
    """
    months = _months(year)
    zero = {m: 0 for m in months}

    cats = conn.execute(
        "SELECT name, section, sort_order FROM categories WHERE statement = 'pnl' ORDER BY sort_order, id"
    ).fetchall()
    sums: dict[str, dict[str, int]] = {c["name"]: dict(zero) for c in cats}
    for r in conn.execute(
        "SELECT yyyy_mm, category, amount_cents FROM v_pnl_monthly WHERE yyyy_mm BETWEEN ? AND ?",
        (months[0], months[-1]),
    ):
        sums[r["category"]][r["yyyy_mm"]] += r["amount_cents"]

    def section(name: str, sign: int) -> list[tuple[str, dict[str, int]]]:
        return [
            (_PNL_LABELS.get(c["name"]) or c["name"], {m: sign * v for m, v in sums[c["name"]].items()})
            for c in cats if c["section"] == name
        ]

    adj = dict(zero)
    for r in conn.execute(
        f"""SELECT substr(date,1,7) AS m, SUM(amount_cents) AS s FROM adjustments
            WHERE non_cash = 1 AND section IN ({','.join('?' for _ in _ADJ_SECTIONS)})
              AND substr(date,1,7) BETWEEN ? AND ? GROUP BY substr(date,1,7)""",
        (*_ADJ_SECTIONS, months[0], months[-1]),
    ):
        adj[r["m"]] = r["s"]

    out: list[dict] = []
    revenue = section("revenue", 1)
    out += [_row(n, "detail", s) for n, s in revenue]
    total_rev = _add(zero, *[s for _, s in revenue])
    out.append(_row("Total Revenue", "total", total_rev))

    sga = section("sga", -1)
    out += [_row(n, "detail", s) for n, s in sga]
    total_sga = _add(zero, *[s for _, s in sga])
    out.append(_row("Total SG&A", "total", total_sga))

    op_income = _sub(total_rev, total_sga)
    out.append(_row("OPERATING INCOME", "total", op_income))

    other = section("other_income", 1)
    out += [_row(n, "detail", s) for n, s in other]
    out.append(_row(_ADJ_LINE, "detail", adj))
    net = _add(op_income, adj, *[s for _, s in other])
    out.append(_row("NET INCOME", "total", net))
    out.append(_row("Net income before the non-cash AR adjustment", "total", _sub(net, adj)))
    return out


def owner_draws(conn: sqlite3.Connection, year: int) -> dict[str, int]:
    """Owner's draws by month as positive cents, plus key 'ytd'. Memo only, never in the P&L."""
    months = _months(year)
    res = {m: 0 for m in months}
    for r in conn.execute(
        """SELECT substr(t.date,1,7) AS m, SUM(t.amount_cents) AS s FROM transactions t
           JOIN categories c ON c.id = t.category_id
           WHERE c.name = ? AND substr(t.date,1,7) BETWEEN ? AND ? GROUP BY substr(t.date,1,7)""",
        (OWNER_DRAW, months[0], months[-1]),
    ):
        res[r["m"]] = -r["s"]
    res["ytd"] = sum(res.values())
    return res


# ------------------------------------------------------------------ tie-out
def _ledger(conn: sqlite3.Connection, account_id: int, as_of: str) -> tuple[int, int, int]:
    """(opening_cents, activity_cents up to as_of, ledger_cents) for one account."""
    opening = conn.execute("SELECT opening_balance_cents FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]
    activity = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) FROM transactions WHERE account_id = ? AND date <= ?",
        (account_id, as_of),
    ).fetchone()[0]
    return opening, activity, opening + activity


def tie_out(conn: sqlite3.Connection, month: str) -> list[dict]:
    """One dict per statement balance dated in ``month`` (an account may have two), plus one per active
    tie-required account without any (``statement_cents``/``diff_cents``/``source`` = None).

    ``opening_cents`` is the account's opening balance and ``activity_cents`` the sum of its transactions up to
    ``as_of``, so ``opening + activity = ledger``. Card liabilities are negative. A pending row is dated at
    the account's statement day (or month-end) and shows the ledger as of that date.
    """
    rows: list[dict] = []
    for r in conn.execute(
        """SELECT sb.account_id, a.code, a.name, sb.as_of, sb.balance_cents, sb.source
           FROM statement_balances sb JOIN accounts a ON a.id = sb.account_id
           WHERE substr(sb.as_of,1,7) = ? ORDER BY a.id, sb.as_of""",
        (month,),
    ):
        opening, activity, ledger = _ledger(conn, r["account_id"], r["as_of"])
        rows.append({
            "account_code": r["code"], "account_name": r["name"], "as_of": r["as_of"],
            "opening_cents": opening, "activity_cents": activity, "ledger_cents": ledger,
            "statement_cents": r["balance_cents"], "diff_cents": ledger - r["balance_cents"],
            "source": r["source"], "_aid": r["account_id"],
        })

    end = _month_end(month)
    for a in conn.execute(
        """SELECT id, code, name, statement_day FROM accounts a
           WHERE tie_required = 1 AND active = 1 AND opening_date <= ?
             AND NOT EXISTS (SELECT 1 FROM statement_balances sb
                              WHERE sb.account_id = a.id AND substr(sb.as_of,1,7) = ?)
           ORDER BY id""",
        (end, month),
    ):
        day = min(a["statement_day"], int(end[8:10])) if a["statement_day"] else int(end[8:10])
        as_of = f"{month}-{day:02d}"
        opening, activity, ledger = _ledger(conn, a["id"], as_of)
        rows.append({
            "account_code": a["code"], "account_name": a["name"], "as_of": as_of,
            "opening_cents": opening, "activity_cents": activity, "ledger_cents": ledger,
            "statement_cents": None, "diff_cents": None, "source": None, "_aid": a["id"],
        })

    rows.sort(key=lambda d: (d["_aid"], d["as_of"]))
    for d in rows:
        del d["_aid"]
    return rows


def transfers_in_transit(conn: sqlite3.Connection, month: str) -> int:
    """Cumulative own-account transfer net (v_transfer_net) through ``month``, cents. 0 = nothing in transit."""
    return conn.execute(
        "SELECT COALESCE(SUM(net_cents), 0) FROM v_transfer_net WHERE yyyy_mm <= ?", (month,)
    ).fetchone()[0]


# ------------------------------------------------------------------ balance sheet
def balance_sheet(conn: sqlite3.Connection, as_of: str) -> list[dict]:
    """Ordered lines ``{section ('assets'|'liabilities'|'equity'), line, amount_cents, kind}`` as of ``as_of``.

    Cash accounts use the ledger balance (opening + transactions up to ``as_of``); the card liability is shown
    positive. Reimbursable (net) = -(ledger rows of that category) + asset adjustments with that label;
    other asset / liability adjustments dated on or before ``as_of`` appear under their own label.
    Equity = total assets - total liabilities.
    """
    bal: dict[str, tuple[str, str, int]] = {}  # code -> (kind, name, ledger)
    for a in conn.execute("SELECT id, code, name, kind FROM accounts ORDER BY id"):
        bal[a["code"]] = (a["kind"], a["name"], _ledger(conn, a["id"], as_of)[2])

    def cat_sum(name: str) -> int:
        return conn.execute(
            """SELECT COALESCE(SUM(t.amount_cents), 0) FROM transactions t
               JOIN categories c ON c.id = t.category_id WHERE c.name = ? AND t.date <= ?""",
            (name, as_of),
        ).fetchone()[0]

    adjs: dict[tuple[str, str], int] = {}
    for r in conn.execute(
        """SELECT section, label, SUM(amount_cents) AS s FROM adjustments
           WHERE section IN ('asset','liability') AND date <= ? GROUP BY section, label ORDER BY MIN(id)""",
        (as_of,),
    ):
        adjs[(r["section"], r["label"])] = r["s"]

    out: list[dict] = []

    def add(section: str, line: str, cents: int, kind: str = "detail") -> int:
        out.append({"section": section, "line": line, "amount_cents": cents, "kind": kind})
        return cents

    # assets
    cash = 0
    banks = [(c, v) for c, v in bal.items() if v[0] == "bank"]
    jars = sum(v[2] for v in bal.values() if v[0] == "jar")
    wise = [(c, v) for c, v in banks if c.startswith("WISE")]
    other_banks = [(c, v) for c, v in banks if not c.startswith("WISE") and c != "CHASE_5200"]
    for c, v in wise:
        cash += add("assets", _BS_LABELS.get(c, v[1]), v[2])
    if any(v[0] == "jar" for v in bal.values()):
        cash += add("assets", _JARS_LABEL, jars)
    for c, v in [(c, v) for c, v in banks if c == "CHASE_5200"] + other_banks:
        cash += add("assets", _BS_LABELS.get(c, v[1]), v[2])
    add("assets", "Total Cash", cash, "total")

    total_assets = cash
    reimb = -cat_sum(REIMBURSABLE) + adjs.pop(("asset", REIMBURSABLE + " (net)"), 0)
    total_assets += add("assets", REIMBURSABLE + " (net)", reimb)
    for (sec, label), cents in list(adjs.items()):
        if sec == "asset":
            total_assets += add("assets", label, cents)
    add("assets", "TOTAL ASSETS", total_assets, "total")

    # liabilities
    total_liab = 0
    for c, v in bal.items():
        if v[0] == "card":
            total_liab += add("liabilities", _BS_LABELS.get(c, v[1]), -v[2])
    payable = cat_sum(PAYABLE_WA)
    if payable:
        total_liab += add("liabilities", PAYABLE_WA, payable)
    for (sec, label), cents in adjs.items():
        if sec == "liability":
            total_liab += add("liabilities", label, cents)
    add("liabilities", "Total Liabilities", total_liab, "total")

    add("equity", "EQUITY (assets - liabilities)", total_assets - total_liab, "total")
    return out
