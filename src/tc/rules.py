"""Rules engine, part 1: rule file loader, validator, `rules` table mirror, first-match
matching (docs/RULES.md 1.1-1.4) and stage 3 (categorize).

`src/tc/rules.yaml` is the source of truth; the `rules` table is a mirror for the UI.
Money is integer cents. Stages 4-8 (fee follows parent, fee split, transfer pairing,
invoice matching, review finalise) are NOT here: `categorize` only decides the rule and,
for `categorize` rules, the category/type, and writes the `[rule:..][review:..]` tags.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from tc.db import connect, init_db

RULES_PATH = Path(__file__).with_name("rules.yaml")  # tests monkeypatch this

TYPES = ("Income", "Expense", "Transfer", "Personal", "Pass-through", "Reimbursable")
ACTIONS = ("categorize", "follow_parent", "needs_invoice")
FEE_MODES = ("split", "keep")
TRANSFER_ROLES = ("wise_out", "chase_in", "card_pay_out", "card_pay_in", "jar_move")
SIGNS = ("debit", "credit")
MATCH_KEYS = (
    "account", "raw_type", "description", "payee", "sign",
    "amount_cents", "amount_min_cents", "amount_max_cents",
)
RULE_KEYS = (
    "id", "priority", "label", "match", "action", "category", "type", "is_fee_parent",
    "fee", "transfer_role", "review", "active", "notes",
)
OWNERS_DRAW = "Owner's Draw"
OVERRIDE_STATUSES = ("reviewed", "manual")  # RULES.md 4.4 (schema only has 'reviewed')
_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_JAR_PREFIX = "JAR_MOVE:"


class RulesError(ValueError):
    """rules.yaml is structurally unreadable, or a rule change was refused."""


@dataclass
class Rule:
    """One rule, with every key of RULES.md 1.1. `fee` is resolved to its default when omitted."""

    id: str
    priority: int
    match: dict = field(default_factory=dict)
    action: str = "categorize"
    label: str | None = None
    category: str | None = None
    type: str | None = None
    is_fee_parent: bool = False
    fee: str | None = None  # default: keep if category = Owner's Draw, else split
    transfer_role: str | None = None
    review: bool = False
    active: bool = True
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.label is None:
            self.label = self.id
        if self.fee is None:
            self.fee = "keep" if self.category == OWNERS_DRAW else "split"


# ------------------------------------------------------------------ load / validate
def load_rules(path: str | Path | None = None) -> list[Rule]:
    """Read rules.yaml (default RULES_PATH) into Rule objects, in file order.

    Raises RulesError for an unreadable structure (bad version, unknown keys, non-mapping
    entries). Semantic checks are `validate_rules`.
    """
    target = Path(RULES_PATH if path is None else path)
    data = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "rules" not in data:
        raise RulesError(f"{target}: expected a mapping with a 'rules' list")
    if data.get("version") != 1:
        raise RulesError(f"{target}: unsupported version {data.get('version')!r} (expected 1)")
    entries = data["rules"]
    if not isinstance(entries, list):
        raise RulesError(f"{target}: 'rules' must be a list")
    out: list[Rule] = []
    for n, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise RulesError(f"{target}: rule #{n} is not a mapping")
        unknown = sorted(set(entry) - set(RULE_KEYS))
        if unknown:
            raise RulesError(f"{target}: rule #{n} ({entry.get('id')!r}) has unknown key(s) {unknown}")
        kwargs = {k: entry[k] for k in RULE_KEYS if k in entry and k != "match"}
        kwargs.setdefault("id", None)
        kwargs.setdefault("priority", None)
        kwargs.setdefault("action", None)
        kwargs["match"] = entry.get("match") if "match" in entry else {}
        out.append(Rule(**kwargs))
    return out


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _reference_lookups(conn: sqlite3.Connection | None) -> tuple[set[str], set[str]]:
    """(category names, account source_labels). Without a connection use the seeds of schema.sql."""
    own = conn is None
    if own:
        conn = connect(":memory:")
        init_db(conn)
    try:
        cats = {r[0] for r in conn.execute("SELECT name FROM categories")}
        accts = {r[0] for r in conn.execute("SELECT source_label FROM accounts")}
    finally:
        if own:
            conn.close()
    return cats, accts


def validate_rules(rules: list[Rule], conn: sqlite3.Connection | None = None) -> list[str]:
    """RULES.md 1.2 checks (plus key/enum sanity). Returns error strings; [] = valid.

    `conn` supplies `categories` and `accounts`; without it the schema.sql seeds are used.
    """
    cats, accts = _reference_lookups(conn)
    errors: list[str] = []
    seen: set = set()
    for i, r in enumerate(rules, start=1):
        tag = r.id if isinstance(r.id, str) and r.id else f"rule #{i}"

        def err(msg: str, tag=tag) -> None:
            errors.append(f"{tag}: {msg}")

        # id / priority
        if not isinstance(r.id, str) or not r.id:
            err("id is required")
        else:
            if not _ID_RE.match(r.id):
                err("id must be kebab-case (a-z, 0-9, '-')")
            if r.id in seen:
                err("duplicate id")
            seen.add(r.id)
        if not _is_int(r.priority):
            err(f"priority must be an integer, got {r.priority!r}")

        # action / category / type
        if r.action not in ACTIONS:
            err(f"action must be one of {list(ACTIONS)}, got {r.action!r}")
        if r.category is not None and r.category not in cats:
            err(f"category {r.category!r} is not in categories")
        if r.type is not None and r.type not in TYPES:
            err(f"type {r.type!r} is not one of {list(TYPES)}")
        if r.action == "categorize":
            if r.category is None:
                err("action categorize requires category")
            if r.type is None:
                err("action categorize requires type")
        elif r.action in ACTIONS:
            if r.category is not None:
                err(f"action {r.action} forbids category")
            if r.type is not None:
                err(f"action {r.action} forbids type")

        # fee mode (SPEC rule 4 / trg_fee_follows_parent_*)
        if r.fee not in FEE_MODES:
            err(f"fee must be one of {list(FEE_MODES)}, got {r.fee!r}")
        elif r.fee == "keep" and r.category != OWNERS_DRAW:
            err(f"fee: keep is allowed only with category {OWNERS_DRAW!r}")
        elif r.fee == "split" and r.category == OWNERS_DRAW:
            err(f"category {OWNERS_DRAW!r} forbids fee: split")

        # flags
        for flag in ("is_fee_parent", "review", "active"):
            if type(getattr(r, flag)) is not bool:
                err(f"{flag} must be true or false")
        if r.transfer_role is not None:
            if r.transfer_role not in TRANSFER_ROLES:
                err(f"transfer_role must be one of {list(TRANSFER_ROLES)}, got {r.transfer_role!r}")
            if r.category != "Bank Transfer":
                err("transfer_role requires category: \"Bank Transfer\"")
        if r.notes is not None and not isinstance(r.notes, str):
            err("notes must be text")
        if not isinstance(r.label, str) or not r.label.strip():
            err("label must be text")

        # match
        m = r.match
        if not isinstance(m, dict) or not m:
            err("match must be a non-empty mapping")
            continue
        unknown = sorted(set(m) - set(MATCH_KEYS))
        if unknown:
            err(f"unknown match key(s) {unknown}")
        if "account" in m and m["account"] not in accts:
            err(f"match.account {m['account']!r} is not an accounts.source_label")
        if "raw_type" in m:
            rt = m["raw_type"]
            ok = isinstance(rt, str) and rt or (
                isinstance(rt, list) and rt and all(isinstance(x, str) and x for x in rt)
            )
            if not ok:
                err("match.raw_type must be text or a non-empty list of text")
        for key in ("description", "payee"):
            if key in m:
                if not isinstance(m[key], str) or not m[key]:
                    err(f"match.{key} must be a non-empty regex")
                else:
                    try:
                        re.compile(m[key], re.IGNORECASE)
                    except re.error as exc:
                        err(f"match.{key} is not a valid regex: {exc}")
        if "sign" in m and m["sign"] not in SIGNS:
            err(f"match.sign must be one of {list(SIGNS)}, got {m['sign']!r}")
        for key in ("amount_cents", "amount_min_cents", "amount_max_cents"):
            if key in m and not _is_int(m[key]):
                err(f"match.{key} must be an integer number of cents")
        if "amount_cents" in m and ("amount_min_cents" in m or "amount_max_cents" in m):
            err("match.amount_cents cannot be combined with amount_min_cents / amount_max_cents")
        if (
            _is_int(m.get("amount_min_cents")) and _is_int(m.get("amount_max_cents"))
            and m["amount_min_cents"] > m["amount_max_cents"]
        ):
            err("match.amount_min_cents is greater than amount_max_cents")
    return errors


def _check(rules: list[Rule], conn: sqlite3.Connection | None) -> None:
    errors = validate_rules(rules, conn)
    if errors:
        raise RulesError("invalid rules: " + "; ".join(errors))


# ------------------------------------------------------------------ rules table mirror
def _rule_notes_json(r: Rule) -> str:
    m = r.match
    payload = {
        "field": "payee" if "description" not in m and "payee" in m else "description",
        "raw_type": m.get("raw_type"),
        "sign": m.get("sign"),
        "amount_min_cents": m.get("amount_min_cents"),
        "amount_max_cents": m.get("amount_max_cents"),
        "is_fee_parent": r.is_fee_parent,
        "fee": r.fee,
        "transfer_role": r.transfer_role,
        "review": r.review,
        "notes": r.notes,
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def sync_rules_table(conn: sqlite3.Connection, rules: list[Rule]) -> int:
    """Replace the `rules` table with `rules` in one transaction (RULES.md 1.4). Returns rows written.

    Rows are inserted in file order, so ids are 1..n and a second sync gives an identical table.
    """
    _check(rules, conn)
    cat_ids = {name: i for i, name in conn.execute("SELECT id, name FROM categories")}
    acct_ids = {label: i for i, label in conn.execute("SELECT id, source_label FROM accounts")}
    try:
        conn.execute("DELETE FROM rules")
        for r in rules:
            m = r.match
            conn.execute(
                "INSERT INTO rules (priority, label, pattern, match_type, account_id, amount_cents, "
                "action, category_id, type, active, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r.priority,
                    f"{r.id}: {r.label}",
                    m.get("description") or m.get("payee") or ".*",
                    "regex",
                    acct_ids.get(m["account"]) if "account" in m else None,
                    m.get("amount_cents"),
                    r.action,
                    cat_ids[r.category] if r.category is not None else None,
                    r.type,
                    1 if r.active else 0,
                    _rule_notes_json(r),
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rules)


# ------------------------------------------------------------------ matching
@lru_cache(maxsize=None)
def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE)


def _norm(text: str | None) -> str:
    return " ".join((text or "").split())


def _raw_type_tokens(raw: str | None) -> set[str]:
    """Values a rule's `raw_type` may equal.

    The Wise importer stores 'DEBIT/TRANSFER', 'CREDIT/DEPOSIT' and 'JAR_MOVE:DEBIT/CONVERSION'
    (Transaction Type / Details Type), while rules.yaml names the bare code (TRANSFER, DEPOSIT,
    CONVERSION). A rule matches the whole value or any '/'-part, after dropping the JAR_MOVE: marker.
    """
    value = raw or ""
    if value.startswith(_JAR_PREFIX):
        value = value[len(_JAR_PREFIX):]
    return {p for p in {value, *value.split("/")} if p}


_PAID_TO = re.compile(r"^Paid to (.+)$")


def _effective_payee(row: dict) -> str:
    """Trimmed payee. Wise direct debits carry the counterparty only in the Merchant column, which the
    importer does not copy to `payee`; for those ('Paid to <merchant>') use the description text."""
    payee = (row.get("payee") or "").strip()
    if payee:
        return payee
    if "DIRECT_DEBIT" in _raw_type_tokens(row.get("raw_type")):
        m = _PAID_TO.match(_norm(row.get("description")))
        if m:
            return m.group(1).strip()
    return ""


def _matches(rule: Rule, row: dict) -> bool:
    m = rule.match
    if not isinstance(m, dict) or not m:
        return False
    amount = row.get("amount_cents")
    if "account" in m and row.get("account") != m["account"]:
        return False
    if "raw_type" in m:
        wanted = [m["raw_type"]] if isinstance(m["raw_type"], str) else list(m["raw_type"])
        if not set(wanted) & _raw_type_tokens(row.get("raw_type")):
            return False
    if "description" in m and not _rx(m["description"]).search(_norm(row.get("description"))):
        return False
    if "payee" in m:
        payee = _effective_payee(row)
        if not payee or not _rx(m["payee"]).search(payee):
            return False
    if "sign" in m:
        if amount is None or (m["sign"] == "debit" and not amount < 0) or (m["sign"] == "credit" and not amount > 0):
            return False
    if "amount_cents" in m and amount != m["amount_cents"]:
        return False
    if "amount_min_cents" in m and (amount is None or amount < m["amount_min_cents"]):
        return False
    if "amount_max_cents" in m and (amount is None or amount > m["amount_max_cents"]):
        return False
    return True


def _outcome(r: Rule) -> tuple:
    return (r.action, r.category, r.type)


def _decide(rules: list[Rule], row: dict) -> tuple[Rule | None, list[str]]:
    """RULES.md 1.3 + stage 3: (rule or None, review reasons known without the database)."""
    active = sorted((r for r in rules if r.active), key=lambda r: r.priority)  # stable = file order
    hits = [r for r in active if _matches(r, row)]
    if not hits:
        return None, ["no_rule"]
    cand = hits[0]
    if any(h.priority == cand.priority and _outcome(h) != _outcome(cand) for h in hits[1:]):
        return None, ["ambiguous_rules"]
    reasons: list[str] = []
    if cand.action == "categorize" and cand.review:
        reasons.append("confirm_rule")
    if (row.get("fee_cents") or 0) > 0 and not cand.is_fee_parent and cand.action != "follow_parent":
        reasons.append("unexpected_fee")
    return cand, reasons


def match_rule(rules: list[Rule], row: dict) -> tuple[Rule | None, str | None]:
    """First match by (priority, file position); equal-priority disagreement is ambiguous.

    Returns (rule, None) for a clean match; (None, 'no_rule') or (None, 'ambiguous_rules') when no
    category can be set; (rule, 'confirm_rule' | 'unexpected_fee') when the rule matched but the row
    must be confirmed (the first reason; `categorize` records all of them).
    """
    rule, reasons = _decide(rules, row)
    return rule, (reasons[0] if reasons else None)


# ------------------------------------------------------------------ notes tags
_TAG_RE = re.compile(r"\s*\[(rule|review):([^\]]*)\]")


def _split_tags(notes: str | None) -> tuple[dict[str, str], str]:
    rest = notes or ""
    tags: dict[str, str] = {}
    while True:
        m = _TAG_RE.match(rest)
        if not m:
            break
        tags[m.group(1)] = m.group(2)
        rest = rest[m.end():]
    return tags, rest.strip()


def _join_tags(tags: dict[str, str], rest: str) -> str | None:
    head = "".join(f"[{k}:{tags[k]}]" for k in ("rule", "review") if tags.get(k))
    out = f"{head} {rest}".strip() if rest else head
    return out or None


def _retag(notes: str | None, *, rule: str | None, reasons: list[str] | None) -> str | None:
    """Replace the [rule:] tag, and the [review:] tag when `reasons` is not None; free text is kept."""
    tags, rest = _split_tags(notes)
    tags["rule"] = rule or ""
    if reasons is not None:
        tags["review"] = ",".join(reasons)
    return _join_tags(tags, rest)


# ------------------------------------------------------------------ stage 3
def _chunks(seq: list, n: int = 500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def categorize(conn: sqlite3.Connection, row_ids: list[int] | None = None) -> dict:
    """Stage 3. Apply rules.yaml to rows and write category/type/review_status and the notes tags.

    Scope: rows that are not manual overrides (review_status 'reviewed'/'manual') and not in a locked
    month; with row_ids=None only uncategorised rows (category_id IS NULL), with row_ids exactly those
    ids (a re-run). Per row:
    - `categorize` rule: category/type from the rule; 'auto', or 'needs_review' with reasons.
    - no rule / ambiguous: category and type NULL, 'needs_review', reasons no_rule[,unknown_payee] or
      ambiguous_rules.
    - `follow_parent` / `needs_invoice`: category and status are left to stages 4 / 7; only the
      [rule:<id>] tag is written.
    Tags go at the start of notes: [rule:<id>][review:<a,b>] free text. Commits.
    Returns {'rows': n, 'categorized': n, 'review_counts': {reason: n}}.
    """
    rules = load_rules()
    _check(rules, conn)
    cat_ids = {name: i for i, name in conn.execute("SELECT id, name FROM categories")}

    marks = ",".join("?" for _ in OVERRIDE_STATUSES)
    base = (
        "SELECT t.id, t.date, t.description, t.amount_cents, t.payee, t.raw_type, t.fee_cents, t.notes, "
        "t.category_id, t.type, t.review_status, t.account_id, a.source_label AS account "
        "FROM transactions t JOIN accounts a ON a.id = t.account_id "
        f"WHERE t.review_status NOT IN ({marks}) "
        "AND substr(t.date,1,7) NOT IN (SELECT yyyy_mm FROM month_locks) "
    )
    if row_ids is None:
        rows = conn.execute(base + "AND t.category_id IS NULL ORDER BY t.date, t.id", OVERRIDE_STATUSES).fetchall()
    else:
        rows = []
        for chunk in _chunks(sorted(set(row_ids))):
            q = ",".join("?" for _ in chunk)
            rows += conn.execute(base + f"AND t.id IN ({q})", (*OVERRIDE_STATUSES, *chunk)).fetchall()
        rows.sort(key=lambda r: (r["date"], r["id"]))

    review_counts: dict[str, int] = {}
    categorized = 0
    try:
        for r in rows:
            row = dict(r)
            rule, reasons = _decide(rules, row)
            if rule is None and "no_rule" in reasons:
                payee = _effective_payee(row)
                if payee and not conn.execute(
                    "SELECT 1 FROM transactions WHERE account_id = ? AND id <> ? "
                    "AND (payee = ? OR (payee IS NULL AND description = ?)) "
                    "AND (date < ? OR (date = ? AND id < ?)) LIMIT 1",
                    (r["account_id"], r["id"], payee, f"Paid to {payee}", r["date"], r["date"], r["id"]),
                ).fetchone():
                    reasons.append("unknown_payee")

            if rule is not None and rule.action != "categorize":
                # follow_parent (stage 4) / needs_invoice (stage 7) decide the category later.
                notes = _retag(r["notes"], rule=rule.id, reasons=None)
                if notes != r["notes"]:
                    conn.execute("UPDATE transactions SET notes = ? WHERE id = ?", (notes, r["id"]))
                continue

            if rule is not None:
                cat_id, typ = cat_ids[rule.category], rule.type
                categorized += 1
            else:
                cat_id, typ = None, None
            status = "needs_review" if (reasons or cat_id is None) else "auto"
            notes = _retag(r["notes"], rule=rule.id if rule else None, reasons=reasons)
            if (cat_id, typ, status, notes) != (r["category_id"], r["type"], r["review_status"], r["notes"]):
                conn.execute(
                    "UPDATE transactions SET category_id = ?, type = ?, review_status = ?, notes = ? WHERE id = ?",
                    (cat_id, typ, status, notes, r["id"]),
                )
            for reason in reasons:
                review_counts[reason] = review_counts.get(reason, 0) + 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"rows": len(rows), "categorized": categorized, "review_counts": review_counts}


# ------------------------------------------------------------------ add_rule
def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:60].strip("-")


def add_rule(
    conn: sqlite3.Connection,
    *,
    label: str,
    pattern: str,
    category_id: int,
    type: str,
    match_type: str = "contains",
    account_id: int | None = None,
    amount_cents: int | None = None,
    priority: int = 100,
) -> int:
    """Append a `categorize` rule on the description to RULES_PATH, re-validate, sync the table.

    The rule id is the kebab-case slug of `label`. match_type: contains (escaped, anywhere),
    exact (escaped, anchored) or regex (as given). Existing file text and order are preserved; the
    file is restored if the result does not validate. Refuses duplicates (same id, or same match and
    outcome as an existing rule). Returns the new `rules` row id.
    """
    if match_type not in ("contains", "exact", "regex"):
        raise RulesError(f"match_type must be contains, exact or regex, got {match_type!r}")
    if not isinstance(pattern, str) or not pattern.strip():
        raise RulesError("pattern is required")
    rule_id = _slug(label or "")
    if not rule_id:
        raise RulesError("label must contain letters or digits")
    cat = conn.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
    if cat is None:
        raise RulesError(f"category_id {category_id!r} does not exist")
    match: dict = {}
    if account_id is not None:
        acct = conn.execute("SELECT source_label FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if acct is None:
            raise RulesError(f"account_id {account_id!r} does not exist")
        match["account"] = acct[0]
    match["description"] = {
        "contains": re.escape(pattern),
        "exact": "^" + re.escape(pattern) + "$",
        "regex": pattern,
    }[match_type]
    if amount_cents is not None:
        match["amount_cents"] = amount_cents

    new = Rule(
        id=rule_id, priority=priority, label=label.strip(), match=match,
        action="categorize", category=cat[0], type=type,
    )
    path = Path(RULES_PATH)
    existing = load_rules(path)
    for r in existing:
        if r.match == new.match and (r.category, r.type) == (new.category, new.type):
            raise RulesError(f"duplicate of existing rule {r.id!r}")
    errors = validate_rules([*existing, new], conn)
    if errors:
        raise RulesError("invalid rule: " + "; ".join(errors))

    original = path.read_text(encoding="utf-8")
    lines = ["", f"  # added by add_rule: {' '.join(label.split())}", f"  - id: {rule_id}", f"    priority: {int(priority)}",
             f"    label: {json.dumps(label.strip(), ensure_ascii=False)}", "    match:"]
    if "account" in match:
        lines.append(f"      account: {json.dumps(match['account'], ensure_ascii=False)}")
    lines.append(f"      description: {json.dumps(match['description'], ensure_ascii=False)}")
    if amount_cents is not None:
        lines.append(f"      amount_cents: {int(amount_cents)}")
    lines += ["    action: categorize", f"    category: {json.dumps(cat[0], ensure_ascii=False)}", f"    type: {type}"]
    text = original if original.endswith("\n") else original + "\n"
    path.write_text(text + "\n".join(lines) + "\n", encoding="utf-8")
    try:
        reloaded = load_rules(path)
        errors = validate_rules(reloaded, conn)
        if errors or [r.id for r in reloaded] != [*(r.id for r in existing), rule_id]:
            raise RulesError("rules file failed re-validation after append: " + "; ".join(errors))
        sync_rules_table(conn, reloaded)
    except Exception:
        path.write_text(original, encoding="utf-8")
        raise
    return conn.execute("SELECT id FROM rules WHERE label = ?", (f"{rule_id}: {new.label}",)).fetchone()[0]
