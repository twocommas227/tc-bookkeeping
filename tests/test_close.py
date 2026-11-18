"""Month close gate, locks, statement balances, backups (SPEC section 5, rule 8; UI.md sections 2-4)."""

import csv
import sqlite3

import pytest

from tc import close, invoices
from tc.close import CloseBlocked, LockedMonthError
from tc.db import insert_transaction
from tc.errors import translate_integrity_error

MONTHS = [f"2026-{m:02d}" for m in range(1, 10)]


@pytest.fixture
def db(golden_db, tmp_path, monkeypatch):
    """golden_db with backups in tmp_path. load_golden writes no invoice_payments, so Service Revenue rows are
    'unbacked' until backfill_payments links them (blocking check revenue_backed)."""
    monkeypatch.setenv("TC_BACKUP_DIR", str(tmp_path))
    invoices.backfill_payments(golden_db)
    golden_db.commit()
    return golden_db


def _n_tx(conn):
    return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def _locked_write(conn, sql, params=()):
    """Run a write; a lock-trigger abort is translated to LockedMonthError (errors.translate_integrity_error)."""
    try:
        conn.execute(sql, params)
    except sqlite3.IntegrityError as exc:
        raise translate_integrity_error(exc) from exc


def test_close_jan_to_sep_in_order(db, tmp_path):
    for m in MONTHS:
        # tail_covered: the card *2110 balance of the next month (28th) covers a month's last days. Only Sep has no
        # later statement in the fixture, so only Sep needs the acknowledgement.
        if m == "2026-09":
            with pytest.raises(CloseBlocked) as e:
                close.close_month(db, m, "note")
            assert [i["key"] for i in e.value.items] == ["tail_covered"]
            assert not close.is_locked(db, m)
            path = close.close_month(db, m, f"close {m}", acknowledge_tail=True)
        else:
            path = close.close_month(db, m, f"close {m}")
        assert path.exists() and path.parent == tmp_path
        assert close.is_locked(db, m)
        assert close.lock_info(db, m)["reason"] == f"close {m}"
    assert {r[0] for r in db.execute("SELECT yyyy_mm FROM month_locks")} == set(MONTHS)


def test_close_writes_csv_next_to_backup(db):
    path = close.close_month(db, "2026-01", "jan", acknowledge_tail=True)
    csv_path = path.with_name(f"{path.stem}_2026-01_transactions.csv")
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    n = db.execute("SELECT COUNT(*) FROM transactions WHERE substr(date,1,7) = '2026-01'").fetchone()[0]
    assert len(rows) == n > 0
    assert all(r["date"].startswith("2026-01") for r in rows)
    # M3: the Invoice Register is written beside it
    inv_path = path.with_name(f"{path.stem}_2026-01_invoices.csv")
    with open(inv_path, newline="", encoding="utf-8") as fh:
        inv_rows = list(csv.DictReader(fh))
    assert len(inv_rows) == db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 18
    assert {"label", "commission_month", "type", "amount", "status", "paid_on"} <= set(inv_rows[0])
    assert any(r["label"] == "Wise INV-013" for r in inv_rows)


def test_close_oct_blocked_names_missing_balances(db, tmp_path):
    with pytest.raises(CloseBlocked) as e:
        close.close_month(db, "2026-10", "oct", acknowledge_tail=True)
    msg = str(e.value)
    assert "Chase *5200" in msg and "card *2110" in msg and "statement balance" in msg
    assert {"balances_entered", "tie_out_zero"} <= {i["key"] for i in e.value.items}
    assert not close.is_locked(db, "2026-10")
    assert db.execute("SELECT COUNT(*) FROM month_locks WHERE yyyy_mm = '2026-10'").fetchone()[0] == 0
    assert list(tmp_path.glob("tc_*_close.db"))  # backup is taken first, even when blocked


def test_checklist_oct_step_order_and_failing_tie_out(db):
    items = close.checklist(db, "2026-10")
    steps = [i["step"] for i in items]
    assert steps == sorted(steps) and set(steps) == set(range(1, 8))
    for i in items:
        assert set(i) == {"key", "step", "label", "ok", "blocking", "detail", "page"}
    by = {i["key"]: i for i in items}
    assert by["tie_out_zero"]["ok"] is False and by["tie_out_zero"]["blocking"] is True
    assert "Chase *5200 pending" in by["tie_out_zero"]["detail"]
    assert by["balances_entered"]["ok"] is False and by["balances_entered"]["blocking"] is True
    assert by["categorized"]["ok"] and by["reviewed"]["ok"]
    assert by["imported"]["blocking"] is False and by["imported"]["ok"] is False  # Chase / card have no Oct rows
    assert by["commission_equals_revenue"]["blocking"] is False and by["commission_equals_revenue"]["ok"]
    assert by["tail_covered"]["blocking"] is False


def test_checklist_sep_all_blocking_ok(db):
    items = close.checklist(db, "2026-09")
    assert all(i["ok"] for i in items if i["blocking"])
    assert next(i for i in items if i["key"] == "tail_covered")["ok"] is False  # card closes on the 28th


def test_commission_vs_revenue_is_warning_only(db):
    db.execute("DELETE FROM invoice_payments WHERE transaction_id IN (SELECT id FROM transactions WHERE date LIKE '2026-06%')")
    # unbacked June revenue blocks June, not January
    june = {i["key"]: i for i in close.checklist(db, "2026-06")}
    assert june["revenue_backed"]["ok"] is False and june["revenue_backed"]["blocking"] is True
    assert june["commission_equals_revenue"]["ok"] is False and june["commission_equals_revenue"]["blocking"] is False
    with pytest.raises(CloseBlocked) as e:
        close.close_month(db, "2026-06", "jun", acknowledge_tail=True)
    assert "revenue_backed" in {i["key"] for i in e.value.items}
    close.close_month(db, "2026-01", "jan", acknowledge_tail=True)


def test_uncategorised_and_needs_review_block(db):
    acct = db.execute("SELECT id FROM accounts WHERE code = 'WISE_1430'").fetchone()[0]
    insert_transaction(db, date="2026-02-10", account_id=acct, description="mystery", amount_cents=-100,
                       source_ref="x1")  # category NULL, review_status needs_review
    db.commit()
    by = {i["key"]: i for i in close.checklist(db, "2026-02")}
    assert not by["categorized"]["ok"] and by["categorized"]["blocking"]
    assert not by["reviewed"]["ok"] and by["reviewed"]["blocking"]
    with pytest.raises(CloseBlocked) as e:
        close.close_month(db, "2026-02", "feb", acknowledge_tail=True)
    assert {"categorized", "reviewed"} <= {i["key"] for i in e.value.items}


def test_close_requires_reason_and_rejects_double_close(db):
    with pytest.raises(ValueError):
        close.close_month(db, "2026-01", "  ", acknowledge_tail=True)
    with pytest.raises(ValueError):
        close.close_month(db, "2026-1", "x", acknowledge_tail=True)
    close.close_month(db, "2026-01", "jan", acknowledge_tail=True)
    with pytest.raises(LockedMonthError):
        close.close_month(db, "2026-01", "again", acknowledge_tail=True)


def test_locked_month_rejects_changes_and_changes_nothing(db):
    close.close_month(db, "2026-01", "jan", acknowledge_tail=True)
    acct = db.execute("SELECT id FROM accounts WHERE code = 'WISE_1430'").fetchone()[0]
    tx_id, amount = db.execute("SELECT id, amount_cents FROM transactions WHERE date LIKE '2026-01%' LIMIT 1").fetchone()
    before = _n_tx(db)
    snapshot = db.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()[:]

    with pytest.raises(LockedMonthError):
        _locked_write(db, "INSERT INTO transactions (date, account_id, description, amount_cents, source_ref) "
                          "VALUES ('2026-01-15', ?, 'late', -5, 'late-1')", (acct,))
    with pytest.raises(LockedMonthError):
        _locked_write(db, "UPDATE transactions SET amount_cents = amount_cents + 1 WHERE id = ?", (tx_id,))
    with pytest.raises(LockedMonthError):
        _locked_write(db, "DELETE FROM transactions WHERE id = ?", (tx_id,))

    assert _n_tx(db) == before
    assert db.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()[:] == snapshot
    assert snapshot[4] == amount


def test_reopen_unlocks_and_backup_opens(db, tmp_path):
    close.close_month(db, "2026-01", "jan", acknowledge_tail=True)
    n = _n_tx(db)
    with pytest.raises(ValueError):
        close.reopen_month(db, "2026-01", "")
    assert close.is_locked(db, "2026-01")
    path = close.reopen_month(db, "2026-01", "typo in a category")
    assert not close.is_locked(db, "2026-01") and close.lock_info(db, "2026-01") is None
    assert path.parent == tmp_path and path.name.endswith("_reopen.db")
    with sqlite3.connect(str(path)) as other:
        assert other.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n
        assert other.execute("SELECT COUNT(*) FROM month_locks WHERE yyyy_mm = '2026-01'").fetchone()[0] == 1
    # unlocked again: writes work
    tx_id = db.execute("SELECT id FROM transactions WHERE date LIKE '2026-01%' LIMIT 1").fetchone()[0]
    db.execute("UPDATE transactions SET notes = 'edited' WHERE id = ?", (tx_id,))
    with pytest.raises(ValueError):
        close.reopen_month(db, "2026-01", "not locked any more")


def test_backup_memory_connection_and_unique_names(db, tmp_path):
    a = close.backup(db, "import")
    b = close.backup(db, "import")
    assert a != b and a.exists() and b.exists()
    assert a.name.startswith("tc_") and a.name.endswith("_import.db")
    with sqlite3.connect(str(a)) as other:
        assert other.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 235


def test_backup_dir_defaults_to_repo_backups(golden_db, monkeypatch):
    monkeypatch.delenv("TC_BACKUP_DIR", raising=False)
    assert close._backup_dir().name == "backups"
    assert (close._backup_dir().parent / "src" / "tc" / "schema.sql").exists()


def test_set_statement_balance_upsert_and_lock(db):
    chase = db.execute("SELECT id FROM accounts WHERE code = 'CHASE_5200'").fetchone()[0]
    close.set_statement_balance(db, chase, "2026-10-31", 123456, source="Chase PDF")
    close.set_statement_balance(db, chase, "2026-10-31", 123457)
    rows = db.execute("SELECT balance_cents, source FROM statement_balances WHERE account_id = ? AND as_of = '2026-10-31'",
                      (chase,)).fetchall()
    assert [tuple(r) for r in rows] == [(123457, "Chase PDF")]  # one row, source kept when not given
    close.close_month(db, "2026-01", "jan", acknowledge_tail=True)
    before = db.execute("SELECT balance_cents FROM statement_balances WHERE account_id = ? AND as_of = '2026-01-31'",
                        (chase,)).fetchone()[0]
    with pytest.raises(LockedMonthError) as e:
        close.set_statement_balance(db, chase, "2026-01-31", before + 1)
    assert e.value.month == "2026-01"
    assert db.execute("SELECT balance_cents FROM statement_balances WHERE account_id = ? AND as_of = '2026-01-31'",
                      (chase,)).fetchone()[0] == before
    with pytest.raises(ValueError):
        close.set_statement_balance(db, chase, "2026-10", 1)
    with pytest.raises(ValueError):
        close.set_statement_balance(db, chase, "2026-10-31", 12.5)
    with pytest.raises(ValueError):
        close.set_statement_balance(db, 999, "2026-10-31", 1)


def test_entering_missing_balances_does_not_tie_oct_when_ledger_differs(db):
    chase = db.execute("SELECT id FROM accounts WHERE code = 'CHASE_5200'").fetchone()[0]
    card = db.execute("SELECT id FROM accounts WHERE code = 'CARD_2110'").fetchone()[0]
    close.set_statement_balance(db, chase, "2026-10-31", 1)
    close.set_statement_balance(db, card, "2026-10-28", 1)
    by = {i["key"]: i for i in close.checklist(db, "2026-10")}
    assert by["balances_entered"]["ok"] is True
    assert by["tie_out_zero"]["ok"] is False  # diff <> 0 still blocks
    with pytest.raises(CloseBlocked):
        close.close_month(db, "2026-10", "oct", acknowledge_tail=True)


def test_expected_statement_dates(db):
    d = {r["code"]: r for r in close.expected_statement_dates(db, "2026-02")}
    assert set(d) == {"WISE_1430", "CHASE_5200", "CARD_2110"}
    assert d["WISE_1430"]["default_as_of"] == "2026-02-28"
    assert d["CHASE_5200"]["default_as_of"] == "2026-02-28"
    assert d["CARD_2110"]["default_as_of"] == "2026-02-28"  # statement_day 28 = Feb month-end
    d = {r["code"]: r for r in close.expected_statement_dates(db, "2026-09")}
    assert d["CARD_2110"]["default_as_of"] == "2026-09-28" and d["WISE_1430"]["default_as_of"] == "2026-09-30"
    assert {"account_id", "code", "name", "kind", "default_as_of"} <= set(d["CARD_2110"])
    with pytest.raises(ValueError):
        close.expected_statement_dates(db, "2026-13")


def test_is_locked_and_lock_info_open_month(db):
    assert close.is_locked(db, "2026-03") is False
    assert close.lock_info(db, "2026-03") is None
    assert close.CloseBlocked is CloseBlocked and close.LockedMonthError is LockedMonthError


def test_backup_commits_pending_writes_instead_of_hanging(db):
    db.execute("UPDATE transactions SET notes = 'pending' WHERE id = (SELECT MIN(id) FROM transactions)")
    path = close.backup(db, "pending")
    with sqlite3.connect(str(path)) as other:
        assert other.execute("SELECT COUNT(*) FROM transactions WHERE notes = 'pending'").fetchone()[0] == 1
