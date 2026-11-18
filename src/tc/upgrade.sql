-- Idempotent additive upgrades. Run by init_db (after schema.sql) and by db.upgrade() on an existing database.
-- Only CREATE ... IF NOT EXISTS here: nothing is dropped, no user data is changed.

-- Rule 8b (continued): invoice matches follow the lock of the month of the receipt they belong to.
CREATE TRIGGER IF NOT EXISTS trg_locked_invpay_ins BEFORE INSERT ON invoice_payments
WHEN EXISTS (SELECT 1 FROM transactions t JOIN month_locks m ON m.yyyy_mm = substr(t.date,1,7)
              WHERE t.id = NEW.transaction_id)
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

CREATE TRIGGER IF NOT EXISTS trg_locked_invpay_upd BEFORE UPDATE ON invoice_payments
WHEN EXISTS (SELECT 1 FROM transactions t JOIN month_locks m ON m.yyyy_mm = substr(t.date,1,7)
              WHERE t.id IN (OLD.transaction_id, NEW.transaction_id))
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

CREATE TRIGGER IF NOT EXISTS trg_locked_invpay_del BEFORE DELETE ON invoice_payments
WHEN EXISTS (SELECT 1 FROM transactions t JOIN month_locks m ON m.yyyy_mm = substr(t.date,1,7)
              WHERE t.id = OLD.transaction_id)
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

-- An invoice that backs a receipt in a locked month cannot be changed or deleted.
CREATE TRIGGER IF NOT EXISTS trg_locked_inv_upd BEFORE UPDATE ON invoices
WHEN EXISTS (SELECT 1 FROM invoice_payments ip JOIN transactions t ON t.id = ip.transaction_id
               JOIN month_locks m ON m.yyyy_mm = substr(t.date,1,7)
              WHERE ip.invoice_id = OLD.id)
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

CREATE TRIGGER IF NOT EXISTS trg_locked_inv_del BEFORE DELETE ON invoices
WHEN EXISTS (SELECT 1 FROM invoice_payments ip JOIN transactions t ON t.id = ip.transaction_id
               JOIN month_locks m ON m.yyyy_mm = substr(t.date,1,7)
              WHERE ip.invoice_id = OLD.id)
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

-- Accounts and categories drive every closed month's balances and P&L: once any month is locked, the columns that
-- do so cannot change and rows cannot be deleted. Notes, seeded_through, fee settings and default_type stay editable.
CREATE TRIGGER IF NOT EXISTS trg_locked_acct_upd
BEFORE UPDATE OF code, source_label, kind, opening_date, opening_balance_cents, tie_required, statement_day, active
ON accounts
WHEN EXISTS (SELECT 1 FROM month_locks)
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked: account settings are frozen'); END;

CREATE TRIGGER IF NOT EXISTS trg_locked_acct_del BEFORE DELETE ON accounts
WHEN EXISTS (SELECT 1 FROM month_locks)
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked: accounts cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS trg_locked_acct_ins BEFORE INSERT ON accounts
WHEN EXISTS (SELECT 1 FROM month_locks WHERE yyyy_mm >= substr(NEW.opening_date,1,7))
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked: a new account cannot open inside a closed month'); END;

CREATE TRIGGER IF NOT EXISTS trg_locked_cat_upd
BEFORE UPDATE OF name, statement, section, pnl_line ON categories
WHEN EXISTS (SELECT 1 FROM month_locks)
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked: category settings are frozen'); END;

CREATE TRIGGER IF NOT EXISTS trg_locked_cat_del BEFORE DELETE ON categories
WHEN EXISTS (SELECT 1 FROM month_locks)
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked: categories cannot be deleted'); END;

-- One Type label for reimbursables: 'Reimbursable' (the workbook mixed it with 'Pass-through'). Idempotent. Rows in
-- locked months keep their old label (the lock triggers refuse the change); only the label changes, never amounts.
UPDATE categories SET default_type = 'Reimbursable'
 WHERE name = 'Reimbursable Expenses Recoverable' AND default_type = 'Pass-through';
UPDATE transactions SET type = 'Reimbursable'
 WHERE type = 'Pass-through'
   AND category_id = (SELECT id FROM categories WHERE name = 'Reimbursable Expenses Recoverable')
   AND substr(date,1,7) NOT IN (SELECT yyyy_mm FROM month_locks);
