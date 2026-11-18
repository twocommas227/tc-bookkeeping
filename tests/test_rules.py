"""Tests for tc.rules: loader, validator, rules-table sync, first-match, stage 3, add_rule.

Expected rule/category per example come from docs/RULES.md section 6 (parent rows only; fee rows,
splits, pairing and invoice matching are later stages).
"""

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from tc import rules as R
from tc.db import insert_transaction
from tc.importers.wise import parse_wise_csv

RAW = Path(__file__).parent / "fixtures" / "raw"
WISE, CHASE, CARD = "USD account 1430", "BUS COMPLETE CHK 5200", "J. DECKER 2110"


def _acct_id(conn, label):
    return conn.execute("SELECT id FROM accounts WHERE source_label = ?", (label,)).fetchone()[0]


def _insert_wise(conn, rows):
    ids = {}
    for r in rows:
        ids[r["source_ref"]] = insert_transaction(
            conn, date=r["date"], account_id=_acct_id(conn, r["account"]), description=r["description"],
            amount_cents=r["amount_cents"], source_ref=r["source_ref"], fee_cents=r["fee_cents"],
            payee=r["payee"], raw_type=r["raw_type"], running_balance_cents=r["running_balance_cents"],
            review_status="needs_review",
        )
    conn.commit()
    return ids


def _tx(conn, tx_id):
    return conn.execute(
        "SELECT t.*, c.name AS category FROM transactions t LEFT JOIN categories c ON c.id = t.category_id "
        "WHERE t.id = ?", (tx_id,)
    ).fetchone()


@pytest.fixture(scope="module")
def rules():
    return R.load_rules()


# ---------------------------------------------------------------- load / validate
def test_load_28_rules_and_valid(rules):
    assert len(rules) == 28
    assert R.validate_rules(rules) == []
    assert [r.id for r in rules][:3] == ["wise-fee-line", "wise-interest", "wise-jar-move"]
    by = {r.id: r for r in rules}
    assert by["owner-draw-joshua"].fee == "keep" and by["wise-to-chase"].fee == "split"
    assert by["wise-fee-line"].label == "wise-fee-line" and by["wise-fee-line"].category is None


def test_validate_with_db_connection(rules, empty_db):
    assert R.validate_rules(rules, empty_db) == []


def _base(**kw):
    d = dict(id="t-rule", priority=50, match={"description": "X"}, action="categorize",
             category="Bank Charges", type="Expense")
    d.update(kw)
    return R.Rule(**d)


@pytest.mark.parametrize(
    "rule, needle",
    [
        (_base(category="Nope"), "not in categories"),
        (_base(type="Bogus"), "type"),
        (_base(priority="high"), "priority must be an integer"),
        (_base(category=None), "requires category"),
        (_base(type=None), "requires type"),
        (_base(action="follow_parent"), "forbids category"),
        (_base(action="needs_invoice", category=None, type="Expense"), "forbids type"),
        (_base(action="bogus"), "action must be one of"),
        (_base(fee="keep"), "fee: keep is allowed only"),
        (_base(category="Owner's Draw", type="Personal", fee="split"), "forbids fee: split"),
        (_base(match={"amount_cents": -1, "amount_min_cents": -5}), "cannot be combined"),
        (_base(match={"description": "(unclosed"}), "not a valid regex"),
        (_base(match={"payee": "["}), "not a valid regex"),
        (_base(match={}), "non-empty mapping"),
        (_base(match={"colour": "red"}), "unknown match key"),
        (_base(match={"account": "No such account", "description": "X"}), "not an accounts.source_label"),
        (_base(match={"sign": "sideways"}), "match.sign"),
        (_base(match={"amount_min_cents": 5, "amount_max_cents": 1}), "greater than"),
        (_base(transfer_role="wise_out"), "requires category"),
        (_base(id="Not Kebab"), "kebab-case"),
    ],
)
def test_validation_failures(rule, needle):
    errors = R.validate_rules([rule])
    assert any(needle in e for e in errors), errors


def test_duplicate_id_and_transfer_role_ok():
    ok = _base(id="a", category="Bank Transfer", type="Transfer", transfer_role="jar_move")
    assert R.validate_rules([ok]) == []
    errs = R.validate_rules([ok, _base(id="a")])
    assert any("duplicate id" in e for e in errs)


def test_load_rejects_bad_structure(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text("version: 2\nrules: []\n")
    with pytest.raises(R.RulesError):
        R.load_rules(p)
    p.write_text("version: 1\nrules:\n  - id: a\n    priority: 1\n    match: {description: x}\n    action: categorize\n    bogus: 1\n")
    with pytest.raises(R.RulesError, match="unknown key"):
        R.load_rules(p)


# ---------------------------------------------------------------- sync
def _dump_rules_table(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM rules ORDER BY id")]


def test_sync_twice_is_identical(rules, empty_db):
    assert R.sync_rules_table(empty_db, rules) == 28
    first = _dump_rules_table(empty_db)
    assert R.sync_rules_table(empty_db, rules) == 28
    assert _dump_rules_table(empty_db) == first
    assert len(first) == 28


def test_sync_mapping(rules, empty_db):
    R.sync_rules_table(empty_db, rules)
    row = empty_db.execute("SELECT * FROM rules WHERE label LIKE 'firstbase-35:%'").fetchone()
    assert row["label"] == "firstbase-35: firstbase-35"
    assert row["pattern"] == r"FIRSTBASE\.IO" and row["match_type"] == "regex"
    assert row["account_id"] == _acct_id(empty_db, CARD) and row["amount_cents"] == -3500
    assert row["action"] == "categorize" and row["priority"] == 20 and row["active"] == 1
    cat = empty_db.execute("SELECT name FROM categories WHERE id = ?", (row["category_id"],)).fetchone()[0]
    assert (cat, row["type"]) == ("Postage & Delivery", "Expense")
    fee = empty_db.execute("SELECT * FROM rules WHERE label LIKE 'wise-fee-line:%'").fetchone()
    assert fee["action"] == "follow_parent" and fee["category_id"] is None and fee["type"] is None
    draw = json.loads(empty_db.execute("SELECT notes FROM rules WHERE label LIKE 'owner-draw-joshua:%'").fetchone()[0])
    assert draw["fee"] == "keep" and draw["is_fee_parent"] is True and draw["field"] == "payee"
    assert draw["raw_type"] == "TRANSFER" and draw["sign"] == "debit"
    # pattern fallbacks: payee pattern, and '.*' is never needed by the shipped file
    assert empty_db.execute("SELECT pattern FROM rules WHERE label LIKE 'owner-draw-joshua:%'").fetchone()[0] == "^Joshua (F )?Decker$"
    assert empty_db.execute("SELECT account_id FROM rules WHERE label LIKE 'owner-draw-betterment:%'").fetchone()[0] is None


def test_sync_refuses_invalid_rules(empty_db):
    with pytest.raises(R.RulesError):
        R.sync_rules_table(empty_db, [_base(category="Nope")])
    assert empty_db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 0


# ---------------------------------------------------------------- match_rule
def test_match_rule_priority_bands(rules):
    firstbase = {"account": CARD, "description": "FIRSTBASE.IO, INC.", "amount_cents": -3500}
    rule, reason = R.match_rule(rules, firstbase)
    assert (rule.id, reason) == ("firstbase-35", None)
    rule, _ = R.match_rule(rules, {**firstbase, "amount_cents": -14900})
    assert rule.id == "firstbase-other"
    # a +149.00 refund is not the $35 rule either
    rule, _ = R.match_rule(rules, {**firstbase, "amount_cents": 14900})
    assert rule.id == "firstbase-other"
    rule, reason = R.match_rule(rules, {"account": CARD, "description": "HTGROWTH LLC", "amount_cents": -99900})
    assert (rule.id, reason) == ("htgrowth-other", "confirm_rule")
    rule, reason = R.match_rule(rules, {"account": CARD, "description": "HTGROWTH LLC", "amount_cents": -125000})
    assert (rule.id, reason) == ("htgrowth-retainer", None)


def test_match_rule_no_rule_and_account_scope(rules):
    assert R.match_rule(rules, {"account": CARD, "description": "SOME NEW VENDOR", "amount_cents": -100}) == (None, "no_rule")
    # a Firstbase-looking line on another account matches nothing
    assert R.match_rule(rules, {"account": CHASE, "description": "FIRSTBASE.IO", "amount_cents": -3500}) == (None, "no_rule")
    # betterment has no account restriction
    rule, _ = R.match_rule(rules, {"account": CHASE, "description": "BETTERMENT SEC TRANSFER", "amount_cents": -50000})
    assert rule.id == "owner-draw-betterment"


def test_match_rule_ambiguous_and_ties():
    a = _base(id="a", priority=10, match={"description": "ACME"})
    b = _base(id="b", priority=10, match={"description": "ACME"}, category="Software & Web Services")
    same = _base(id="c", priority=10, match={"description": "ACME"})
    later = _base(id="d", priority=11, match={"description": "ACME"}, category="Software & Web Services")
    row = {"description": "acme corp", "amount_cents": -1}
    assert R.match_rule([a, b], row) == (None, "ambiguous_rules")
    assert R.match_rule([a, same], row)[0].id == "a"            # same outcome at equal priority: first wins
    assert R.match_rule([later, a], row)[0].id == "a"           # lower priority number wins regardless of order
    assert R.match_rule([a, replace(b, active=False)], row)[0].id == "a"  # inactive ignored


def test_match_rule_conditions():
    rule = _base(match={"description": "^X", "sign": "credit", "amount_min_cents": 100, "amount_max_cents": 500})
    ok = {"description": "  x   y ", "amount_cents": 300}
    assert R.match_rule([rule], ok)[0] is rule                  # whitespace collapsed, case-insensitive
    for bad in ({"amount_cents": 99}, {"amount_cents": 501}, {"amount_cents": -300}, {"description": "Y"}):
        assert R.match_rule([rule], {**ok, **bad}) == (None, "no_rule")
    multi = _base(match={"raw_type": ["A", "B"]})
    assert R.match_rule([multi], {"raw_type": "CREDIT/B"})[0] is multi
    assert R.match_rule([multi], {"raw_type": "C"}) == (None, "no_rule")


def test_unexpected_fee_when_rule_is_not_fee_parent(rules):
    row = {"account": WISE, "raw_type": "DEBIT/DIRECT_DEBIT", "payee": None, "description": "Paid to IRS",
           "amount_cents": -141100, "fee_cents": 50}
    rule, reason = R.match_rule(rules, row)
    assert (rule.id, reason) == ("owner-draw-irs", "unexpected_fee")
    assert R.match_rule(rules, {**row, "fee_cents": 0}) == (rule, None)


def test_wise_raw_type_and_merchant_adaptation(rules):
    """Importer values ('DEBIT/TRANSFER', 'JAR_MOVE:DEBIT/CONVERSION', payee None on direct debits)."""
    jar = {"account": WISE, "raw_type": "JAR_MOVE:DEBIT/CONVERSION", "payee": None, "amount_cents": -29300,
           "description": "Moved 293.00 USD to Rent & Utilities"}
    assert R.match_rule(rules, jar)[0].id == "wise-jar-move"
    # a jar move to a jar that has no rule (pre-opening 'Owner Payout') matches nothing
    assert R.match_rule(rules, {**jar, "description": "Moved 500.00 USD to Owner Payout"}) == (None, "no_rule")
    cbp = {"account": WISE, "raw_type": "DEBIT/DIRECT_DEBIT", "payee": None, "amount_cents": -551014,
           "description": "Paid to US CBP"}
    assert R.match_rule(rules, cbp)[0].id == "cbp-direct-debit"
    # the Merchant fallback is for direct debits only
    assert R.match_rule(rules, {**cbp, "raw_type": "DEBIT/CARD"}) == (None, "no_rule")


# ---------------------------------------------------------------- worked examples (section 6)
OCT_EXPECT = {
    # source_ref: (rule id, category, type, review_status); category None = decided by a later stage
    "BALANCE_INTEREST-817956ec-2940-4ea3-2a38-15268ba52481": ("wise-interest", "Interest Income", "Income", "auto"),  # ex 1
    "TRANSFER-2406973811": ("wise-to-chase", "Bank Transfer", "Transfer", "auto"),                                   # ex 2
    "TRANSFER-2460784094": ("wise-to-chase", "Bank Transfer", "Transfer", "auto"),                                   # ex 2
    "TRANSFER-2441134368": ("wa-receipt", None, None, "needs_review"),                                              # ex 3 (needs_invoice)
    "TRANSFER-2445337879": ("owner-draw-joshua", "Owner's Draw", "Personal", "auto"),                                # ex 4
    "TRANSFER-2459989965": ("owner-draw-joshua", "Owner's Draw", "Personal", "auto"),                                # ex 5
    "DIRECT_DEBIT-40502598": ("cbp-direct-debit", "Reimbursable Expenses Recoverable", "Reimbursable", "auto"),     # ex 6
    "TRANSFER-2451302590": ("maersk-wise", "Reimbursable Expenses Recoverable", "Reimbursable", "auto"),            # ex 7
}


def test_october_wise_examples_via_match_rule(rules):
    for r in parse_wise_csv(RAW / "wise_2026-10.csv"):
        rule, reason = R.match_rule(rules, r)
        assert rule is not None and rule.id == OCT_EXPECT[r["source_ref"]][0], r["source_ref"]
        assert reason is None, (r["source_ref"], reason)  # fees sit on is_fee_parent rules


def test_october_wise_examples_categorize(empty_db):
    rows = parse_wise_csv(RAW / "wise_2026-10.csv")
    assert len(rows) == 8
    ids = _insert_wise(empty_db, rows)
    out = R.categorize(empty_db)
    assert out == {"rows": 8, "categorized": 7, "review_counts": {}}
    for ref, (rule_id, cat, typ, status) in OCT_EXPECT.items():
        t = _tx(empty_db, ids[ref])
        assert (t["category"], t["type"], t["review_status"]) == (cat, typ, status), ref
        assert t["notes"] == f"[rule:{rule_id}]", ref
    # stage 3 does not touch money
    assert [(_tx(empty_db, i)["amount_cents"], _tx(empty_db, i)["fee_cents"]) for i in ids.values()] == [
        (r["amount_cents"], r["fee_cents"]) for r in rows
    ]
    # repeat is a no-op
    before = [tuple(r) for r in empty_db.execute("SELECT * FROM transactions ORDER BY id")]
    assert R.categorize(empty_db)["rows"] == 1  # only the still-uncategorised needs_invoice row is in scope
    assert [tuple(r) for r in empty_db.execute("SELECT * FROM transactions ORDER BY id")] == before
    assert R.categorize(empty_db, list(ids.values()))["rows"] == 8
    assert [tuple(r) for r in empty_db.execute("SELECT * FROM transactions ORDER BY id")] == before


def test_earlier_wise_examples_via_wise_full(rules):
    by_ref = {r["source_ref"]: r for r in parse_wise_csv(RAW / "wise_full.csv")}
    cases = [
        # (source_ref, rule id, reason)
        ("TRANSFER-2236910892", "wa-outbound-wire", "confirm_rule"),   # ex 8 parent: Payable to WA, queued
        ("FEE-TRANSFER-2236910892", "wise-fee-line", None),            # ex 8 fee row: follow_parent
        ("BALANCE-4695954807", "wise-jar-move", None),                 # ex 9
        ("TRANSFER-2349562344", "owner-draw-joshua", None),            # S1 parent
        ("FEE-TRANSFER-2349562344", "wise-fee-line", None),            # S1 fee row
        ("TRANSFER-2341692850", "wa-receipt", None),                   # S2 (needs_invoice)
        ("TRANSFER-1909616554", "wise-to-chase", None),                # S3
    ]
    for ref, rule_id, reason in cases:
        rule, got = R.match_rule(rules, by_ref[ref])
        assert (rule.id, got) == (rule_id, reason), ref
    ex8 = next(r for r in rules if r.id == "wa-outbound-wire")
    assert (ex8.category, ex8.type) == ("Payable to WA Rubbermate", "Pass-through")
    assert {r.id: r.action for r in rules}["wise-fee-line"] == "follow_parent"


def test_example_8_categorize_confirm_rule(empty_db):
    by_ref = {r["source_ref"]: r for r in parse_wise_csv(RAW / "wise_full.csv")}
    ids = _insert_wise(empty_db, [by_ref["TRANSFER-2236910892"], by_ref["FEE-TRANSFER-2236910892"]])
    out = R.categorize(empty_db)
    assert out == {"rows": 2, "categorized": 1, "review_counts": {"confirm_rule": 1}}
    parent = _tx(empty_db, ids["TRANSFER-2236910892"])
    assert (parent["category"], parent["type"], parent["review_status"]) == (
        "Payable to WA Rubbermate", "Pass-through", "needs_review")
    assert parent["notes"] == "[rule:wa-outbound-wire][review:confirm_rule]"
    fee = _tx(empty_db, ids["FEE-TRANSFER-2236910892"])  # follow_parent: category waits for stage 4
    assert (fee["category"], fee["type"], fee["review_status"], fee["notes"]) == (None, None, "needs_review", "[rule:wise-fee-line]")


def test_all_2026_wise_rows_have_a_rule(empty_db, rules):
    rows = [r for f in ("wise_full.csv", "wise_2026-10.csv") for r in parse_wise_csv(RAW / f) if r["date"] >= "2026-01-01"]
    _insert_wise(empty_db, rows)
    out = R.categorize(empty_db)
    assert out["rows"] == len(rows)
    assert out["review_counts"] == {"confirm_rule": 1}
    kinds = {k: v for k, v in empty_db.execute(
        "SELECT substr(notes, 1, instr(notes, ']')), COUNT(*) FROM transactions GROUP BY 1")}
    assert kinds["[rule:wa-receipt]"] == 13 and kinds["[rule:wise-fee-line]"] == 34


def test_card_and_chase_examples(rules):
    # ex 10
    assert R.match_rule(rules, {"account": CARD, "description": "FIRSTBASE.IO, INC.", "amount_cents": -3500})[0].category == "Postage & Delivery"
    assert R.match_rule(rules, {"account": CARD, "description": "FIRSTBASE.IO, INC.", "amount_cents": -14900})[0].category == "Other Professional Services"
    # ex 11
    rule, reason = R.match_rule(rules, {"account": CHASE, "description": "MONTHLY SERVICE FEE", "amount_cents": -1500})
    assert (rule.id, rule.category, rule.type, reason) == ("chase-monthly-fee", "Bank Charges", "Expense", None)
    # ex 12 (both legs, and the Chase line posted 08/03)
    leg_in = {"account": CARD, "description": "Payment Thank You-Mobile", "amount_cents": 149297}
    leg_out = {"account": CHASE, "description": "Payment to Chase card ending in 2110 08/03", "amount_cents": -149297}
    r_in, r_out = R.match_rule(rules, leg_in)[0], R.match_rule(rules, leg_out)[0]
    assert (r_in.id, r_in.transfer_role, r_in.category, r_in.type) == ("card-payment-in", "card_pay_in", "Bank Transfer", "Transfer")
    assert (r_out.id, r_out.transfer_role) == ("chase-card-payment-out", "card_pay_out")
    # S3 Chase leg
    chase_in = R.match_rule(rules, {"account": CHASE, "amount_cents": 150000,
                                    "description": "REAL TIME TRANSFER RECD FROM ABA/CONTR BNK-021000021 FROM: T"})[0]
    assert (chase_in.id, chase_in.transfer_role) == ("chase-from-wise", "chase_in")


# ---------------------------------------------------------------- categorize behaviour
def test_manual_overrides_untouched(empty_db):
    acct = _acct_id(empty_db, WISE)
    sw = empty_db.execute("SELECT id FROM categories WHERE name = 'Software & Web Services'").fetchone()[0]
    mk = lambda ref, status, cat, typ, notes: insert_transaction(
        empty_db, date="2026-10-01", account_id=acct, description="Interest payment", amount_cents=4513,
        source_ref=ref, raw_type="CREDIT/BALANCE_INTEREST", category_id=cat, type=typ, review_status=status, notes=notes)
    reviewed = mk("A", "reviewed", sw, "Expense", "my note")       # human chose Software, rule would say Interest
    reviewed_blank = mk("B", "reviewed", None, None, None)          # reviewed but left uncategorised
    fresh = mk("C", "needs_review", None, None, "free text")
    empty_db.commit()
    snap = lambda i: tuple(_tx(empty_db, i))
    before = (snap(reviewed), snap(reviewed_blank))
    assert R.categorize(empty_db) == {"rows": 1, "categorized": 1, "review_counts": {}}
    assert (snap(reviewed), snap(reviewed_blank)) == before
    # an explicit re-run over every id leaves overrides alone too
    assert R.categorize(empty_db, [reviewed, reviewed_blank, fresh])["rows"] == 1
    assert (snap(reviewed), snap(reviewed_blank)) == before
    t = _tx(empty_db, reviewed)
    assert (t["category"], t["type"], t["review_status"], t["notes"]) == ("Software & Web Services", "Expense", "reviewed", "my note")
    f = _tx(empty_db, fresh)
    assert (f["category"], f["review_status"], f["notes"]) == ("Interest Income", "auto", "[rule:wise-interest] free text")


def test_no_rule_unknown_payee_and_ambiguity_flow(empty_db):
    acct = _acct_id(empty_db, WISE)
    mk = lambda ref, date, payee, notes=None: insert_transaction(
        empty_db, date=date, account_id=acct, description="Sent money to Someone New", amount_cents=-5000,
        source_ref=ref, raw_type="DEBIT/TRANSFER", payee=payee, notes=notes)
    first = mk("N1", "2026-10-02", "Someone New")
    second = mk("N2", "2026-10-09", "Someone New", notes="remember to ask")
    nopayee = mk("N3", "2026-10-10", None)
    empty_db.commit()
    out = R.categorize(empty_db)
    assert out == {"rows": 3, "categorized": 0, "review_counts": {"no_rule": 3, "unknown_payee": 1}}
    t1, t2, t3 = (_tx(empty_db, i) for i in (first, second, nopayee))
    assert (t1["category"], t1["type"], t1["review_status"]) == (None, None, "needs_review")
    assert t1["notes"] == "[review:no_rule,unknown_payee]"
    assert t2["notes"] == "[review:no_rule] remember to ask"
    assert t3["notes"] == "[review:no_rule]"
    # idempotent: no tag growth on repeat
    R.categorize(empty_db)
    assert _tx(empty_db, second)["notes"] == "[review:no_rule] remember to ask"


def test_rerun_recategorizes_and_replaces_tags(empty_db):
    """A row categorised earlier is re-decided on an explicit re-run; old tags are replaced, text kept."""
    acct = _acct_id(empty_db, CARD)
    cat = empty_db.execute("SELECT id FROM categories WHERE name = 'Bank Charges'").fetchone()[0]
    tid = insert_transaction(empty_db, date="2026-10-15", account_id=acct, description="FIRSTBASE.IO, INC.",
                             amount_cents=-3500, source_ref="F1", category_id=cat, type="Expense", review_status="auto",
                             notes="[rule:old-rule][review:no_rule] keep me")
    empty_db.commit()
    assert R.categorize(empty_db)["rows"] == 0           # categorised rows are out of scope for the default run
    out = R.categorize(empty_db, [tid])
    assert out == {"rows": 1, "categorized": 1, "review_counts": {}}
    t = _tx(empty_db, tid)
    assert (t["category"], t["type"], t["review_status"], t["notes"]) == (
        "Postage & Delivery", "Expense", "auto", "[rule:firstbase-35] keep me")


def test_unexpected_fee_in_categorize(empty_db):
    acct = _acct_id(empty_db, WISE)
    tid = insert_transaction(empty_db, date="2026-10-20", account_id=acct, description="Paid to IRS", amount_cents=-100000,
                             source_ref="DD-1", fee_cents=75, raw_type="DEBIT/DIRECT_DEBIT")
    empty_db.commit()
    out = R.categorize(empty_db)
    assert out["review_counts"] == {"unexpected_fee": 1} and out["categorized"] == 1
    t = _tx(empty_db, tid)
    assert (t["category"], t["review_status"], t["notes"]) == ("Owner's Draw", "needs_review", "[rule:owner-draw-irs][review:unexpected_fee]")


def test_categorize_skips_locked_months(empty_db):
    acct = _acct_id(empty_db, WISE)
    tid = insert_transaction(empty_db, date="2026-01-01", account_id=acct, description="Interest payment",
                             amount_cents=100, source_ref="LOCK-1", raw_type="CREDIT/BALANCE_INTEREST")
    # locking needs tie-out data in the real schema; emulate by dropping the guard trigger for this test only
    empty_db.execute("DROP TRIGGER trg_month_lock_requires_tie_out")
    empty_db.execute("INSERT INTO month_locks (yyyy_mm, reason) VALUES ('2026-01', 'test')")
    empty_db.commit()
    assert R.categorize(empty_db, [tid])["rows"] == 0
    assert _tx(empty_db, tid)["category_id"] is None


# ---------------------------------------------------------------- add_rule
@pytest.fixture
def rules_copy(tmp_path, monkeypatch):
    dst = tmp_path / "rules.yaml"
    shutil.copy(R.RULES_PATH, dst)
    monkeypatch.setattr(R, "RULES_PATH", dst)
    return dst


def test_add_rule_appends_validates_and_syncs(rules_copy, empty_db):
    original = rules_copy.read_text(encoding="utf-8")
    sw = empty_db.execute("SELECT id FROM categories WHERE name = 'Software & Web Services'").fetchone()[0]
    card = _acct_id(empty_db, CARD)
    rid = R.add_rule(empty_db, label="Notion", pattern="NOTION LABS", category_id=sw, type="Expense",
                     account_id=card, priority=57)
    text = rules_copy.read_text(encoding="utf-8")
    assert text.startswith(original)                       # existing text and order preserved
    loaded = R.load_rules(rules_copy)
    assert len(loaded) == 29 and loaded[-1].id == "notion"
    assert R.validate_rules(loaded, empty_db) == []
    new = loaded[-1]
    assert (new.priority, new.category, new.type, new.action) == (57, "Software & Web Services", "Expense", "categorize")
    assert new.match == {"account": CARD, "description": "NOTION\\ LABS"}
    row = empty_db.execute("SELECT * FROM rules WHERE id = ?", (rid,)).fetchone()
    assert row["label"] == "notion: Notion" and row["account_id"] == card and row["priority"] == 57
    assert empty_db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] == 29
    assert R.match_rule(loaded, {"account": CARD, "description": "NOTION LABS  SAN FRANCISCO", "amount_cents": -1000})[0].id == "notion"
    # the default loader now sees it; the original file in src is untouched elsewhere
    assert len(R.load_rules()) == 29


def test_add_rule_match_types_and_amount(rules_copy, empty_db):
    pf = empty_db.execute("SELECT id FROM categories WHERE name = 'Postage & Delivery'").fetchone()[0]
    R.add_rule(empty_db, label="Exact ship", pattern="USPS 1", category_id=pf, type="Expense",
               match_type="exact", amount_cents=-995)
    R.add_rule(empty_db, label="Regex ship", pattern=r"^UPS\s+\d+", category_id=pf, type="Expense", match_type="regex")
    loaded = R.load_rules(rules_copy)
    exact, rx = loaded[-2], loaded[-1]
    assert exact.match == {"description": "^USPS\\ 1$", "amount_cents": -995}
    assert rx.match == {"description": r"^UPS\s+\d+"} and rx.priority == 100
    assert R.match_rule(loaded, {"description": "usps 1", "amount_cents": -995})[0].id == "exact-ship"
    assert R.match_rule(loaded, {"description": "usps 1", "amount_cents": -996}) == (None, "no_rule")


def test_add_rule_refuses_duplicates_and_bad_input(rules_copy, empty_db):
    sw = empty_db.execute("SELECT id FROM categories WHERE name = 'Software & Web Services'").fetchone()[0]
    R.add_rule(empty_db, label="Notion", pattern="NOTION", category_id=sw, type="Expense")
    snapshot = rules_copy.read_text(encoding="utf-8")
    table = _dump_rules_table(empty_db)
    bad_calls = [
        dict(label="Notion", pattern="OTHER", category_id=sw, type="Expense"),          # same id
        dict(label="Notion again", pattern="NOTION", category_id=sw, type="Expense"),   # same match + outcome
        dict(label="Bad cat", pattern="X", category_id=9999, type="Expense"),
        dict(label="Bad type", pattern="X", category_id=sw, type="Nope"),
        dict(label="Bad regex", pattern="(", category_id=sw, type="Expense", match_type="regex"),
        dict(label="Bad acct", pattern="X", category_id=sw, type="Expense", account_id=999),
        dict(label="Bad prio", pattern="X", category_id=sw, type="Expense", priority="x"),
        dict(label="Bad mt", pattern="X", category_id=sw, type="Expense", match_type="glob"),
        dict(label="!!!", pattern="X", category_id=sw, type="Expense"),
        dict(label="Owner draw wrong", pattern="X", category_id=7, type="Personal", amount_cents=True),
    ]
    for kw in bad_calls:
        with pytest.raises(R.RulesError):
            R.add_rule(empty_db, **kw)
        assert rules_copy.read_text(encoding="utf-8") == snapshot, kw
        assert _dump_rules_table(empty_db) == table, kw
    assert len(R.load_rules(rules_copy)) == 29


def test_add_rule_to_file_without_trailing_newline(tmp_path, monkeypatch, empty_db):
    p = tmp_path / "rules.yaml"
    p.write_text(R.RULES_PATH.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
    monkeypatch.setattr(R, "RULES_PATH", p)
    sw = empty_db.execute("SELECT id FROM categories WHERE name = 'Software & Web Services'").fetchone()[0]
    R.add_rule(empty_db, label="Zed", pattern="ZED", category_id=sw, type="Expense")
    assert [r.id for r in R.load_rules(p)][-2:] == ["htgrowth-other", "zed"]


def test_shipped_rules_yaml_untouched_by_tests():
    assert len(R.load_rules(Path(R.__file__).with_name("rules.yaml"))) == 28
