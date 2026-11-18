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

-- One Type label for reimbursables: 'Reimbursable' (the workbook mixed it with 'Pass-through'). Idempotent. Rows in
-- locked months keep their old label (the lock triggers refuse the change); only the label changes, never amounts.
UPDATE categories SET default_type = 'Reimbursable'
 WHERE name = 'Reimbursable Expenses Recoverable' AND default_type = 'Pass-through';
UPDATE transactions SET type = 'Reimbursable'
 WHERE type = 'Pass-through'
   AND category_id = (SELECT id FROM categories WHERE name = 'Reimbursable Expenses Recoverable')
   AND substr(date,1,7) NOT IN (SELECT yyyy_mm FROM month_locks);
