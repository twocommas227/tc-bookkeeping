-- Two Commas LLC bookkeeping tool - SQLite schema (v1)
-- Money: INTEGER cents, never floats. Dates: ISO 'YYYY-MM-DD' text. Months: 'YYYY-MM'.
-- Sign convention: amount_cents is signed from the account holder's view
--   (+ money in / card payment, - money out / card charge).
--   Balances are signed asset values, so the card *2110 liability is NEGATIVE
--   (statement "balance 1,456.41" is stored as -145641).
-- See docs/SPEC.md for the rule -> table/column/test mapping.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- accounts
CREATE TABLE accounts (
    id                    INTEGER PRIMARY KEY,
    code                  TEXT    NOT NULL UNIQUE,
    source_label          TEXT    NOT NULL UNIQUE,   -- exact "Source" text used in the workbook / importers
    name                  TEXT    NOT NULL,
    kind                  TEXT    NOT NULL CHECK (kind IN ('bank','card','jar')),
    institution           TEXT    NOT NULL,
    opening_date          TEXT    NOT NULL CHECK (opening_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    opening_balance_cents INTEGER NOT NULL,          -- signed asset value (card = negative)
    opening_inferred      INTEGER NOT NULL DEFAULT 0 CHECK (opening_inferred IN (0,1)),
    tie_required          INTEGER NOT NULL DEFAULT 1 CHECK (tie_required IN (0,1)),  -- rule 8: must tie before a month locks
    statement_day         INTEGER CHECK (statement_day BETWEEN 1 AND 31),            -- NULL = calendar month-end
    monthly_fee_cents     INTEGER,                   -- rule 7 (Chase *5200: 1500)
    fee_waiver_min_balance_cents INTEGER,            -- rule 7 (Chase *5200: 200000)
    active                INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    notes                 TEXT,
    seeded_through        TEXT                       -- NULL = no cutoff. Set by `python -m tc.fixtures seed`: import skips rows dated on or before it
);

-- -------------------------------------------------------------- categories
CREATE TABLE categories (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    statement    TEXT NOT NULL CHECK (statement IN ('pnl','balance_sheet','equity','none')),
    section      TEXT NOT NULL CHECK (section IN ('revenue','sga','other_income','asset','liability','owner_draw','transfer')),
    default_type TEXT NOT NULL CHECK (default_type IN ('Income','Expense','Transfer','Personal','Pass-through','Reimbursable')),
    pnl_line     TEXT,           -- P&L row label in the workbook (NULL when not on the P&L)
    sort_order   INTEGER NOT NULL,
    notes        TEXT
);

-- ------------------------------------------------------------ transactions
CREATE TABLE transactions (
    id            INTEGER PRIMARY KEY,
    date          TEXT    NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    description   TEXT    NOT NULL,
    amount_cents  INTEGER NOT NULL,
    category_id   INTEGER REFERENCES categories(id),          -- NULL = uncategorised (blocks month close)
    type          TEXT CHECK (type IN ('Income','Expense','Transfer','Personal','Pass-through','Reimbursable')),
    source_ref    TEXT    NOT NULL,                            -- bank's own id, or importer-built stable key
    parent_id     INTEGER REFERENCES transactions(id),         -- fee row -> the transfer it belongs to (rule 4)
    fee_cents     INTEGER NOT NULL DEFAULT 0 CHECK (fee_cents >= 0),  -- fee carried INSIDE this row (owner draws)
    payee         TEXT,                                        -- payee / payer as normalised by the importer
    raw_type      TEXT,                                        -- bank's own type code (TRANSFER, DIRECT_DEBIT, ...)
    running_balance_cents INTEGER,                             -- as printed by the bank, when available
    notes         TEXT,
    review_status TEXT    NOT NULL DEFAULT 'needs_review'
                  CHECK (review_status IN ('needs_review','auto','reviewed')),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_id, source_ref),                           -- re-import is idempotent
    CHECK (parent_id IS NULL OR parent_id <> id),
    CHECK ((category_id IS NULL) = (type IS NULL))
);
CREATE INDEX idx_tx_date            ON transactions(date);
CREATE INDEX idx_tx_account_date    ON transactions(account_id, date);
CREATE INDEX idx_tx_category_date   ON transactions(category_id, date);
CREATE INDEX idx_tx_parent          ON transactions(parent_id);
CREATE INDEX idx_tx_review          ON transactions(review_status);

-- ------------------------------------------------------------------- rules
-- Vendor rules (workbook tab "Vendor Rules"). Lowest priority number wins.
CREATE TABLE rules (
    id           INTEGER PRIMARY KEY,
    priority     INTEGER NOT NULL DEFAULT 100,
    label        TEXT    NOT NULL,                 -- "Vendor / Pattern" text from the workbook
    pattern      TEXT    NOT NULL,                 -- matched against description / payee
    match_type   TEXT    NOT NULL DEFAULT 'contains' CHECK (match_type IN ('contains','exact','regex')),
    account_id   INTEGER REFERENCES accounts(id),  -- NULL = any account
    amount_cents INTEGER,                          -- NULL = any amount (e.g. -3500 for Firstbase $35)
    action       TEXT    NOT NULL DEFAULT 'categorize'
                 CHECK (action IN ('categorize','follow_parent','needs_invoice')),
                 -- follow_parent = rule 4; needs_invoice = WA receipts (rule 1 decides from the invoice)
    category_id  INTEGER REFERENCES categories(id),
    type         TEXT CHECK (type IN ('Income','Expense','Transfer','Personal','Pass-through','Reimbursable')),
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    notes        TEXT,
    CHECK (action <> 'categorize' OR (category_id IS NOT NULL AND type IS NOT NULL))
);
CREATE INDEX idx_rules_active_priority ON rules(active, priority);

-- ---------------------------------------------------------------- invoices
CREATE TABLE invoices (
    id               INTEGER PRIMARY KEY,
    number           TEXT    NOT NULL,             -- 'INV-014', 'INV-000014'
    source_label     TEXT    NOT NULL,             -- 'Wise' | 'Firstbase' | ... never omit (numbers overlap)
    issue_date       TEXT    NOT NULL CHECK (issue_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    due_date         TEXT    CHECK (due_date IS NULL OR due_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    commission_month TEXT    CHECK (commission_month IS NULL OR commission_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    type             TEXT    NOT NULL CHECK (type IN ('Commission','Reimbursable','Voided')),
    boxes            INTEGER NOT NULL DEFAULT 0 CHECK (boxes >= 0),
    rate_cents       INTEGER NOT NULL DEFAULT 0 CHECK (rate_cents >= 0),
    amount_cents     INTEGER NOT NULL CHECK (amount_cents >= 0),
    status           TEXT    NOT NULL CHECK (status IN ('Pending','Paid','Voided')),
    replaces_invoice_id INTEGER REFERENCES invoices(id),   -- reissue of a voided invoice
    notes            TEXT,
    UNIQUE (source_label, number),
    -- RULE 1 (ONE RULE): a Commission invoice is a single line, boxes x $0.50, for one named month.
    CONSTRAINT ck_invoice_commission_single_line CHECK (
        type <> 'Commission'
        OR (rate_cents = 50 AND boxes > 0 AND amount_cents = boxes * rate_cents AND commission_month IS NOT NULL)
    ),
    CONSTRAINT ck_invoice_voided_status CHECK ((type = 'Voided') = (status = 'Voided'))
);
CREATE INDEX idx_invoices_status ON invoices(status);
CREATE INDEX idx_invoices_month  ON invoices(commission_month);

-- -------------------------------------------------------- invoice_payments
-- Many invoices per transaction (one wire can pay several invoices) - rule 2.
CREATE TABLE invoice_payments (
    id             INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    invoice_id     INTEGER NOT NULL REFERENCES invoices(id),
    amount_cents   INTEGER NOT NULL CHECK (amount_cents > 0),   -- GROSS amount of this invoice settled by the wire
    UNIQUE (transaction_id, invoice_id)
);
CREATE INDEX idx_invpay_invoice ON invoice_payments(invoice_id);
CREATE INDEX idx_invpay_tx      ON invoice_payments(transaction_id);

-- ------------------------------------------------------ statement_balances
CREATE TABLE statement_balances (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    as_of         TEXT    NOT NULL CHECK (as_of GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    balance_cents INTEGER NOT NULL,                -- signed asset value (card liability negative)
    source        TEXT,                            -- 'Wise CSV', 'Chase PDF', ...
    entered_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_id, as_of)
);

-- ------------------------------------------------------------- adjustments
-- Manual, non-bank entries (e.g. the 30 Sep 2026 AR write-off, +156286, other_income, non-cash).
CREATE TABLE adjustments (
    id           INTEGER PRIMARY KEY,
    date         TEXT    NOT NULL CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    label        TEXT    NOT NULL,                 -- P&L / balance-sheet row label
    section      TEXT    NOT NULL CHECK (section IN ('revenue','sga','other_income','asset','liability','equity')),
    amount_cents INTEGER NOT NULL,                 -- signed effect on net income (pnl sections) or on the BS line
    non_cash     INTEGER NOT NULL DEFAULT 1 CHECK (non_cash IN (0,1)),
    category_id  INTEGER REFERENCES categories(id),
    reason       TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_adjustments_date ON adjustments(date);

-- ------------------------------------------------------------- month_locks
CREATE TABLE month_locks (
    yyyy_mm   TEXT PRIMARY KEY CHECK (yyyy_mm GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    locked_at TEXT NOT NULL DEFAULT (datetime('now')),
    reason    TEXT NOT NULL
);

-- =================================================================== views
-- Rule 8: ledger balance vs statement balance at every statement date.
CREATE VIEW v_tie_out AS
SELECT sb.account_id,
       a.code               AS account_code,
       sb.as_of,
       substr(sb.as_of,1,7) AS yyyy_mm,
       a.opening_balance_cents
         + COALESCE((SELECT SUM(t.amount_cents) FROM transactions t
                      WHERE t.account_id = sb.account_id AND t.date <= sb.as_of), 0) AS ledger_cents,
       sb.balance_cents     AS statement_cents,
       a.opening_balance_cents
         + COALESCE((SELECT SUM(t.amount_cents) FROM transactions t
                      WHERE t.account_id = sb.account_id AND t.date <= sb.as_of), 0)
         - sb.balance_cents AS diff_cents
FROM statement_balances sb
JOIN accounts a ON a.id = sb.account_id;

-- Rule 4: fee rows whose category does not follow the parent (must be empty).
CREATE VIEW v_fee_parent_mismatch AS
SELECT f.id AS fee_id, f.parent_id, fc.name AS fee_category, pc.name AS parent_category
FROM transactions f
JOIN transactions p       ON p.id = f.parent_id
LEFT JOIN categories fc   ON fc.id = f.category_id
LEFT JOIN categories pc   ON pc.id = p.category_id
WHERE COALESCE(fc.name,'') <> CASE
        WHEN pc.name = 'Owner''s Draw'             THEN 'Owner''s Draw'
        WHEN pc.name = 'Payable to WA Rubbermate'  THEN 'Payable to WA Rubbermate'
        ELSE 'Bank Charges' END;

-- Rules 1+2: Service Revenue rows not fully backed by Commission invoices (must be empty).
CREATE VIEW v_revenue_unbacked AS
SELECT t.id AS transaction_id, t.date, t.amount_cents,
       COALESCE((SELECT SUM(ip.amount_cents) FROM invoice_payments ip
                   JOIN invoices i ON i.id = ip.invoice_id
                  WHERE ip.transaction_id = t.id AND i.type = 'Commission'), 0) AS commission_backed_cents
FROM transactions t
JOIN categories c ON c.id = t.category_id
WHERE c.name = 'Service Revenue'
  AND t.amount_cents <> COALESCE((SELECT SUM(ip.amount_cents) FROM invoice_payments ip
                                    JOIN invoices i ON i.id = ip.invoice_id
                                   WHERE ip.transaction_id = t.id AND i.type = 'Commission'), 0);

-- Rule 5: own-account transfers by month (cumulative total must be 0 apart from in-transit items).
CREATE VIEW v_transfer_net AS
SELECT substr(t.date,1,7) AS yyyy_mm, SUM(t.amount_cents) AS net_cents, COUNT(*) AS n_rows
FROM transactions t
JOIN categories c ON c.id = t.category_id
WHERE c.name = 'Bank Transfer'
GROUP BY substr(t.date,1,7);

-- Monthly P&L by category (cash basis: the transaction date is the recognition date - rule 2).
CREATE VIEW v_pnl_monthly AS
SELECT substr(t.date,1,7) AS yyyy_mm, c.name AS category, c.section, SUM(t.amount_cents) AS amount_cents
FROM transactions t
JOIN categories c ON c.id = t.category_id
WHERE c.statement = 'pnl'
GROUP BY substr(t.date,1,7), c.id;

-- Invoice display label: always "<source> <number>" (e.g. "Wise INV-014" vs "Firstbase INV-000014").
CREATE VIEW v_invoice_labels AS
SELECT id, source_label || ' ' || number AS label, type, status, amount_cents FROM invoices;

-- ================================================================ triggers
-- Rule 1: Service Revenue may only be linked to Commission invoices, and vice versa.
CREATE TRIGGER trg_one_rule_invoice_payment
BEFORE INSERT ON invoice_payments
BEGIN
    SELECT RAISE(ABORT, 'ONE RULE: Service Revenue can only settle a Commission invoice')
     WHERE (SELECT c.name FROM transactions t JOIN categories c ON c.id = t.category_id
             WHERE t.id = NEW.transaction_id) = 'Service Revenue'
       AND (SELECT type FROM invoices WHERE id = NEW.invoice_id) <> 'Commission';
    SELECT RAISE(ABORT, 'ONE RULE: a Reimbursable invoice receipt must be Reimbursable Expenses Recoverable')
     WHERE (SELECT type FROM invoices WHERE id = NEW.invoice_id) = 'Reimbursable'
       AND COALESCE((SELECT c.name FROM transactions t JOIN categories c ON c.id = t.category_id
                      WHERE t.id = NEW.transaction_id), 'Reimbursable Expenses Recoverable')
           <> 'Reimbursable Expenses Recoverable';
    SELECT RAISE(ABORT, 'A voided invoice cannot be paid')
     WHERE (SELECT type FROM invoices WHERE id = NEW.invoice_id) = 'Voided';
END;

-- Rule 4: a fee row (parent_id set) takes its category from the parent.
CREATE TRIGGER trg_fee_follows_parent_ins
BEFORE INSERT ON transactions
WHEN NEW.parent_id IS NOT NULL AND NEW.category_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'Fee must be in the same account as its parent')
     WHERE (SELECT account_id FROM transactions WHERE id = NEW.parent_id) <> NEW.account_id;
    SELECT RAISE(ABORT, 'Fee follows the parent: wrong category for this fee row')
     WHERE (SELECT name FROM categories WHERE id = NEW.category_id) <> CASE
             (SELECT c.name FROM transactions p JOIN categories c ON c.id = p.category_id WHERE p.id = NEW.parent_id)
             WHEN 'Owner''s Draw'            THEN 'Owner''s Draw'
             WHEN 'Payable to WA Rubbermate' THEN 'Payable to WA Rubbermate'
             ELSE 'Bank Charges' END;
END;

CREATE TRIGGER trg_fee_follows_parent_upd
BEFORE UPDATE OF category_id, parent_id ON transactions
WHEN NEW.parent_id IS NOT NULL AND NEW.category_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'Fee follows the parent: wrong category for this fee row')
     WHERE (SELECT name FROM categories WHERE id = NEW.category_id) <> CASE
             (SELECT c.name FROM transactions p JOIN categories c ON c.id = p.category_id WHERE p.id = NEW.parent_id)
             WHEN 'Owner''s Draw'            THEN 'Owner''s Draw'
             WHEN 'Payable to WA Rubbermate' THEN 'Payable to WA Rubbermate'
             ELSE 'Bank Charges' END;
END;

-- Rule 8a: a month may only be locked when every tie-required account has a statement
-- balance in that month, every tie-out diff is 0, and nothing is uncategorised.
CREATE TRIGGER trg_month_lock_requires_tie_out
BEFORE INSERT ON month_locks
BEGIN
    SELECT RAISE(ABORT, 'Cannot close month: a tie-required account has no statement balance')
     WHERE EXISTS (SELECT 1 FROM accounts a
                    WHERE a.tie_required = 1 AND a.active = 1
                      AND NOT EXISTS (SELECT 1 FROM statement_balances sb
                                       WHERE sb.account_id = a.id AND substr(sb.as_of,1,7) = NEW.yyyy_mm));
    SELECT RAISE(ABORT, 'Cannot close month: ledger balance <> statement balance')
     WHERE EXISTS (SELECT 1 FROM v_tie_out WHERE yyyy_mm = NEW.yyyy_mm AND diff_cents <> 0);
    SELECT RAISE(ABORT, 'Cannot close month: uncategorised transactions')
     WHERE EXISTS (SELECT 1 FROM transactions
                    WHERE substr(date,1,7) = NEW.yyyy_mm AND category_id IS NULL);
END;

-- Rule 8b: closed months are locked (transactions, adjustments, statement balances).
CREATE TRIGGER trg_locked_tx_ins BEFORE INSERT ON transactions
WHEN EXISTS (SELECT 1 FROM month_locks WHERE yyyy_mm = substr(NEW.date,1,7))
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

CREATE TRIGGER trg_locked_tx_upd BEFORE UPDATE ON transactions
WHEN EXISTS (SELECT 1 FROM month_locks WHERE yyyy_mm IN (substr(OLD.date,1,7), substr(NEW.date,1,7)))
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

CREATE TRIGGER trg_locked_tx_del BEFORE DELETE ON transactions
WHEN EXISTS (SELECT 1 FROM month_locks WHERE yyyy_mm = substr(OLD.date,1,7))
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

CREATE TRIGGER trg_locked_adj_ins BEFORE INSERT ON adjustments
WHEN EXISTS (SELECT 1 FROM month_locks WHERE yyyy_mm = substr(NEW.date,1,7))
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

CREATE TRIGGER trg_locked_adj_upd BEFORE UPDATE ON adjustments
WHEN EXISTS (SELECT 1 FROM month_locks WHERE yyyy_mm IN (substr(OLD.date,1,7), substr(NEW.date,1,7)))
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

CREATE TRIGGER trg_locked_adj_del BEFORE DELETE ON adjustments
WHEN EXISTS (SELECT 1 FROM month_locks WHERE yyyy_mm = substr(OLD.date,1,7))
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

CREATE TRIGGER trg_locked_stmt_ins BEFORE INSERT ON statement_balances
WHEN EXISTS (SELECT 1 FROM month_locks WHERE yyyy_mm = substr(NEW.as_of,1,7))
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

CREATE TRIGGER trg_locked_stmt_upd BEFORE UPDATE ON statement_balances
WHEN EXISTS (SELECT 1 FROM month_locks WHERE yyyy_mm IN (substr(OLD.as_of,1,7), substr(NEW.as_of,1,7)))
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

CREATE TRIGGER trg_locked_stmt_del BEFORE DELETE ON statement_balances
WHEN EXISTS (SELECT 1 FROM month_locks WHERE yyyy_mm = substr(OLD.as_of,1,7))
BEGIN SELECT RAISE(ABORT, 'Month is closed and locked'); END;

-- =================================================================== seeds
-- Accounts: source_label is the exact Transactions "Source" text in Two_Commas_Accounting_2026.xlsx.
-- Openings are from the Bank Tie-out tab. Jar openings are INFERRED (no jar statements on file):
-- jars are confirmed $0 and their 2026 ledger rows sum to -293.00 / -950.00.
INSERT INTO accounts (id, code, source_label, name, kind, institution, opening_date, opening_balance_cents,
                      opening_inferred, tie_required, statement_day, monthly_fee_cents, fee_waiver_min_balance_cents, notes) VALUES
 (1, 'WISE_1430',  'USD account 1430',                 'Wise USD *1430 (main balance)',  'bank', 'Wise',  '2026-01-01', 1044457, 0, 1, NULL, NULL, NULL,
     'Opening 1 Jan 2026 = $10,444.57 (after 30 Dec 2025 PayCargo debit)'),
 (2, 'CHASE_5200', 'BUS COMPLETE CHK 5200',            'Chase Business Checking *5200',  'bank', 'Chase', '2026-01-01',  252956, 0, 1, NULL, 1500, 200000,
     'Opening 1 Jan 2026 = $2,529.56 (Jan statement). $15 monthly fee waived if daily balance >= $2,000'),
 (3, 'CARD_2110',  'J. DECKER 2110',                   'Chase Ink card *2110',           'card', 'Chase', '2025-12-28', -148992, 0, 1, 28,   NULL, NULL,
     'Liability: opening = 28 Dec 2025 statement balance $1,489.92, stored negative. Statement closes on the 28th'),
 (4, 'WISE_JAR_RENT', 'Wise USD - Rent & Utilities 0000', 'Wise jar - Rent & Utilities', 'jar',  'Wise',  '2026-01-01',   29300, 1, 0, NULL, NULL, NULL,
     'Opening inferred; jar is $0 and no longer used'),
 (5, 'WISE_JAR_TAX',  'Wise USD - Taxes jar',             'Wise jar - Taxes',            'jar',  'Wise',  '2026-01-01',   95000, 1, 0, NULL, NULL, NULL,
     'Opening inferred; jar is $0 and no longer used');

-- Categories: the 10 categories used on the Transactions tab (235 rows).
INSERT INTO categories (id, name, statement, section, default_type, pnl_line, sort_order, notes) VALUES
 (1,  'Service Revenue',                   'pnl',           'revenue',      'Income',       'Commission Revenue (cash received)', 10,
      'ONLY single-line $0.50/box commission invoices, on the date WA pays'),
 (2,  'Other Professional Services',       'pnl',           'sga',          'Expense',      'Professional Services (HTGROWTH retainer, Firstbase fees)', 20, NULL),
 (3,  'Postage & Delivery',                'pnl',           'sga',          'Expense',      'Postage & Delivery (Firstbase mailbox / registered agent)', 30, 'Firstbase $35'),
 (4,  'Software & Web Services',           'pnl',           'sga',          'Expense',      'Software & Web Services', 40, NULL),
 (5,  'Bank Charges',                      'pnl',           'sga',          'Expense',      'Bank Charges (Wise fees, Chase MSF, FX fees)', 50, NULL),
 (6,  'Interest Income',                   'pnl',           'other_income', 'Income',       'Interest Income', 60, NULL),
 (7,  'Owner''s Draw',                     'equity',        'owner_draw',   'Personal',     NULL, 70,
      'Transfers to Joshua, Betterment, IRS federal income tax. Never on the P&L'),
 (8,  'Bank Transfer',                     'none',          'transfer',     'Transfer',     NULL, 80,
      'Own-account moves: Wise->Chase, card payments, jar moves. Nets to zero'),
 (9,  'Reimbursable Expenses Recoverable', 'balance_sheet', 'asset',        'Reimbursable', NULL, 90,
      'Everything fronted for / recovered from WA that is not commission. Never revenue or expense'),
 (10, 'Payable to WA Rubbermate',          'balance_sheet', 'liability',    'Pass-through', NULL, 100,
      'CBP tariff refund held for and paid on to WA, incl. the Wise fee on that wire');
