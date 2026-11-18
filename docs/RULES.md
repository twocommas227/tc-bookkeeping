# Two Commas LLC bookkeeping tool - RULES ENGINE (v1)

Companion to `docs/SPEC.md` (accounting rules 1-8, import contract) and `src/tc/schema.sql`.
Money is INTEGER cents everywhere. Category and type names are the exact strings seeded in `categories`.

Every number in this file was checked against `tests/fixtures/` on 2026-11-15 with a throw-away
prototype of the pipeline (section 7.5). Nothing here is estimated.

Contents: 1 rule file - 2 pipeline - 3 review queue - 4 idempotency / re-runs - 5 Chase and card quirks -
6 worked examples - 7 test plan - 8 decisions to confirm.

---

## 0. Facts about the inputs the design depends on

1. **The Wise CSV is newest-first.** Nothing in the pipeline may depend on file order; every stage works on
   the whole batch plus the database.
2. **Two Wise fee regimes** (confirmed by the Wise importer):
   - **Regime A** - older exports (`raw/wise_full.csv`, to 2026-09-11). Each fee is its own CSV row with
     id `FEE-<parent id>` and description `Wise Charges for: TRANSFER-...`. The parent amount EXCLUDES the fee
     (3 Sep draw: parent -495.22, `Total fees` 4.78, FEE row -4.78).
   - **Regime B** - the October export (`raw/wise_2026-10.csv`). No FEE rows. A debit INCLUDES the fee
     (-300.00 with `Total fees` 1.13); a credit is NET of the fee (WA wire +5,699.89 with `Total fees` 6.11).
   - The regime is decided **per parent row, not per file**: a parent is regime A if and only if a row with
     `source_ref = 'FEE-' || parent.source_ref` exists in the batch or in the database.
3. **Wise dates can be one day later than Chase dates** for the same transfer (Wise 8 Jan 07:44 vs Chase
   7 Jan; Wise 3 Jul vs Chase 2 Jul; Wise 30 Sep vs Chase 29 Sep) and up to two days earlier (Wise 29 Aug vs
   Chase 31 Aug). Pairing uses a date window, never date equality.
4. **WA's wire reference is unreliable.** Seen in 2026: `INV-000005 ...` (clean), `INV.-000016` (stray dot),
   `INV-0000157 ...` (typo for INV-000017), `/ROC/PO-26-08-2306` (no invoice number), and
   `INV-013 ... INV-014 ...` (two invoices in one wire). WA's payer name has 8 spellings.
5. **Invoice numbers overlap**: Wise `INV-014` and Firstbase `INV-000014` are different invoices. Matching is
   on the literal number string, never on the integer value.
6. **Coverage gap in the fixtures**: `wise_full.csv` ends 2026-09-11 and `wise_2026-10.csv` starts
   2026-10-01. Six answer-key rows dated 2026-09-15 to 2026-09-30 have no raw CSV (section 7.3).

---

## 1. Rule file: `src/tc/rules.yaml`

### 1.1 Shape

```yaml
version: 1
rules:
  - id: wise-to-chase            # required, unique, kebab-case, never reused
    priority: 30                 # required, integer, LOWER wins
    label: "Wise -> Chase transfers"   # optional, Vendor Rules text; defaults to id
    match:                       # required, at least one key; ALL given keys must match (AND)
      account: "USD account 1430"      # optional, exact accounts.source_label; omitted = any account
      raw_type: TRANSFER               # optional, exact (or list = any of)
      description: "..."               # optional, regex, re.search, case-insensitive
      payee: "^Two Commas( LLC)?$"     # optional, regex on the trimmed payee, case-insensitive
      sign: debit                      # optional: debit (amount < 0) | credit (amount > 0)
      amount_cents: -3500              # optional, exact signed cash amount
      amount_min_cents: -200000        # optional, inclusive signed range (either bound may be omitted)
      amount_max_cents: -1
    action: categorize           # required: categorize | follow_parent | needs_invoice
    category: "Bank Transfer"    # required when action = categorize; forbidden otherwise
    type: Transfer               # required when action = categorize; forbidden otherwise
    is_fee_parent: true          # optional, default false - rows matched here are allowed to carry a Wise fee
    fee: split                   # optional: split | keep. Default: keep if category = Owner's Draw, else split
    transfer_role: wise_out      # optional: wise_out | chase_in | card_pay_out | card_pay_in | jar_move
    review: false                # optional, default false - true = set the category but queue for confirmation
    active: true                 # optional, default true
    notes: "..."                 # optional
```

Matching always uses the **cash amount** of the row (the amount as the bank shows it, before any fee split),
so a rule keeps matching the same row on every re-run.

### 1.2 Validation (the loader refuses the file if any of these fail)

- `id` unique; `priority` integer; `category` exists in `categories`; `type` is one of the six type values.
- `action = categorize` requires `category` and `type`; the other two actions forbid them.
- `fee: keep` is allowed only with `category: "Owner's Draw"`, and `category: "Owner's Draw"` forbids
  `fee: split` (this is SPEC rule 4 and the `trg_fee_follows_parent_*` triggers).
- `amount_cents` cannot be combined with `amount_min_cents` / `amount_max_cents`.
- Every regex compiles. `match` is not empty.
- `transfer_role` requires `category: "Bank Transfer"`.

### 1.3 Ordering: first match wins, ties go to review

1. Sort active rules by `(priority, position in file)`.
2. Walk the list; the first rule whose `match` holds is the candidate.
3. Collect every other matching rule **with the same priority**. If any of them gives a different
   `(action, category, type)`, the row is ambiguous: no category is set, reason `ambiguous_rules`.
4. Otherwise the candidate wins. Rules with a higher priority number are never consulted.

There is no automatic "specificity" scoring. Specificity is expressed by priority bands:

| Band | Use | Examples |
|---|---|---|
| 10-19 | Structural lines | Wise fee line, interest, jar moves |
| 20-29 | Vendor + exact amount | Firstbase $35, HTGROWTH $1,250, Chase $15 fee |
| 30-59 | Vendor / payee | Joshua, Maersk, CBP, Sellerboard |
| 60-89 | Broad patterns | WA receipt (`needs_invoice`), any other Firstbase / HTGROWTH charge |
| 90-99 | Reserved for catch-alls (none in v1) | |

This is how SPEC section 7 "row 30 takes priority over row 23" and the Firstbase rows 8/9/10/31 are resolved.

### 1.4 Mapping to the `rules` table

`rules.yaml` is the source of truth and is what the engine evaluates. The `rules` table is a mirror for the
UI and for SQL checks. `tc rules sync` replaces the table in one transaction (delete all, insert all; nothing
references `rules.id`).

| `rules` column | From YAML |
|---|---|
| `priority` | `priority` |
| `label` | `id || ': ' || label` (the id is the stable key the UI shows) |
| `pattern` | `match.description` if present, else `match.payee`, else `'.*'` |
| `match_type` | always `'regex'` |
| `account_id` | id of `match.account`, else NULL |
| `amount_cents` | `match.amount_cents`, else NULL |
| `action` | `action` |
| `category_id`, `type` | `category`, `type` (NULL for `follow_parent` / `needs_invoice`) |
| `active` | `active` |
| `notes` | JSON object with the keys the table has no column for: `field` (`description` or `payee`), `raw_type`, `sign`, `amount_min_cents`, `amount_max_cents`, `is_fee_parent`, `fee`, `transfer_role`, `review`, `notes` |

### 1.5 Initial rule set (complete; this is what the fixtures need)

Patterns are written against the text the fixtures actually contain. `acct` abbreviations: W = `USD account 1430`,
C = `BUS COMPLETE CHK 5200`, K = `J. DECKER 2110`.

| pri | id | acct | match | action / category / type | flags | SPEC s7 row |
|---|---|---|---|---|---|---|
| 10 | wise-fee-line | W | description `^Wise Charges for:` | follow_parent | | 30 (beats 23) |
| 11 | wise-interest | W | raw_type `BALANCE_INTEREST`, credit | Interest Income / Income | | 22 |
| 12 | wise-jar-move | W | raw_type `CONVERSION`, description `^Moved [\d,.]+ USD (to\|from) (Rent & Utilities\|Taxes)$` | Bank Transfer / Transfer | transfer_role jar_move | 21, 32 |
| 20 | firstbase-35 | K | description `FIRSTBASE\.IO`, amount_cents -3500 | Postage & Delivery / Expense | | 9, 31 |
| 21 | htgrowth-retainer | K | description `^HTGROWTH`, amount_cents -125000 | Other Professional Services / Expense | | 7 |
| 22 | chase-monthly-fee | C | description `^MONTHLY SERVICE FEE`, amount_cents -1500 | Bank Charges / Expense | (checked by 5.4) | 24 |
| 30 | wise-to-chase | W | raw_type `TRANSFER`, debit, payee `^Two Commas( LLC)?$` | Bank Transfer / Transfer | is_fee_parent, transfer_role wise_out | 20 |
| 31 | chase-from-wise | C | credit, description `^REAL TIME TRANSFER RECD` | Bank Transfer / Transfer | transfer_role chase_in | 20 |
| 32 | chase-card-payment-out | C | debit, description `Payment to Chase card ending in 2110` | Bank Transfer / Transfer | transfer_role card_pay_out | 25 |
| 33 | card-payment-in | K | credit, description `^Payment Thank You` | Bank Transfer / Transfer | transfer_role card_pay_in | 25 |
| 40 | owner-draw-joshua | W | raw_type `TRANSFER`, debit, payee `^Joshua (F )?Decker$` | Owner's Draw / Personal | is_fee_parent, fee keep | 17 |
| 41 | owner-draw-betterment | any | description `BETTERMENT SEC` | Owner's Draw / Personal | | 16 |
| 42 | owner-draw-irs | W | raw_type `DIRECT_DEBIT`, debit, payee `^IRS$` | Owner's Draw / Personal | | 19 |
| 45 | cbp-direct-debit | W | raw_type `DIRECT_DEBIT`, debit, payee `^US CBP$` | Reimbursable Expenses Recoverable / Reimbursable | | 34 |
| 46 | maersk-wise | W | raw_type `TRANSFER`, debit, payee `^Maersk` | Reimbursable Expenses Recoverable / Reimbursable | is_fee_parent | 3, 28 |
| 47 | card-wa-passthrough | K | description `MAERSK\|PAYCARGO\|PIERPASS\|PORTCHECK` | Reimbursable Expenses Recoverable / Reimbursable | | 3-6, 28 |
| 48 | cbp-tariff-refund | W | raw_type `DEPOSIT`, credit, payee `^CBP\s+TREAS 310` | Payable to WA Rubbermate / Pass-through | | 33 |
| 49 | wa-outbound-wire | W | raw_type `TRANSFER`, debit, payee `^W\.?\s*A\.?\s*RUBBERMATE` | Payable to WA Rubbermate / Pass-through | is_fee_parent, review | 33 |
| 50 | sw-sellerboard | K | description `SELLERBOARD` | Software & Web Services / Expense | | 11 |
| 51 | sw-google | K | description `^GOOGLE \*(Workspace\|SVCS)` | Software & Web Services / Expense | | 12, 26 |
| 52 | sw-myrealprofit | K | description `MYREALPROFIT` | Software & Web Services / Expense | | 13 |
| 53 | sw-anthropic | K | description `ANTHROPIC\|CLAUDE\.AI` | Software & Web Services / Expense | | 14 |
| 54 | sw-openai | K | description `OPENAI` | Software & Web Services / Expense | | 15 |
| 55 | sw-cloudflare | K | description `CLOUDFLARE` | Software & Web Services / Expense | | 29 |
| 56 | card-fx-fee | K | description `^FOREIGN TRANSACTION FEE` | Bank Charges / Expense | | (ledger row 27 Feb; not on the Vendor Rules tab) |
| 60 | wa-receipt | W | raw_type `DEPOSIT`, credit, payee `^W\.?\s*A\.?\s*RUBBERMATE` | needs_invoice | is_fee_parent | 1, 2, 27 |
| 61 | firstbase-other | K | description `FIRSTBASE\.IO` | Other Professional Services / Expense | | 8, 10 |
| 62 | htgrowth-other | K | description `^HTGROWTH` | Other Professional Services / Expense | review | 7 ("extra charges ... confirm") |

Not loaded: SPEC s7 row 18 (superseded). No rule for Wyoming/state fees (`Taxes & Licenses` is not seeded).
The payee regex `^W\.?\s*A\.?\s*RUBBERMATE` matches all 8 payer spellings in `wise_full.csv` and the payee
spelling `W.A. Rubbermate (Thailand) Co., Ltd.`.

---

## 2. Pipeline

`run_import(file)` = stages 1-2 then `run_rules(new_row_ids)`.
`run_rules(row_ids)` = stages 3-8. It is also what "re-run after a rule edit" calls (section 4).
Everything from stage 2 onwards for one file is ONE database transaction; a failure rolls the whole import back.

```
run_import(file):
    backup("import")                               # SPEC section 5
    rows, meta = importer.parse(file)              # import-contract rows + statement meta (5.1)
    rows       = normalize(rows)                   # stage 1
    new_ids    = dedupe_and_insert(rows)           # stage 2
    run_rules(new_ids)                             # stages 3-8
    upsert_statement_balance(meta)                 # 5.1
    return report(inserted, skipped_duplicate, skipped_before_opening, rejected_locked, review_counts)
```

In-memory row object used below (`R`): the import-contract fields plus
`cash` (amount as the bank shows it, never changed), `rule` (matched rule or None), `category`, `type`,
`review` (list of reason codes), `has_fee_row` (bool), `keep_fee` (bool).

### Stage 1 - normalize

```
normalize(rows):
    out = []
    for r in rows:
        assert r.account in accounts.source_label            # else abort the import
        r.description = collapse_whitespace(trim(r.description))
        r.payee       = trim(r.payee) or None                # Wise: Payee Name, else Payer Name, else Merchant
        assert is_int(r.amount_cents) and r.amount_cents != 0 and r.fee_cents >= 0
        if r.date < account(r.account).opening_date:         # e.g. the 2022-2025 rows of wise_full.csv
            count("skipped_before_opening"); continue
        if month(r.date) in month_locks:                     # SPEC rule 8
            count("rejected_locked"); continue
        r.cash = r.amount_cents
        out.append(r)
    return sort(out, key = (date, bank timestamp if any, source_ref))   # oldest first
```

No fee arithmetic happens here. Regime A FEE rows pass through as ordinary rows.
For Wise, also assert the running-balance chain per file: after sorting by timestamp,
`prev.running_balance + cur.amount == cur.running_balance` for every consecutive pair (holds for all 86 rows of
2026 in `wise_full.csv` and all 8 rows of `wise_2026-10.csv`). A break aborts the import: the file is
truncated or edited.

### Stage 2 - dedupe and insert

```
dedupe_and_insert(rows):
    batch_refs = { (r.account, r.source_ref) for r in rows }
    new_ids = []
    for r in rows:
        if exists(transactions, account = r.account, source_ref = r.source_ref):
            count("skipped_duplicate"); continue
        if r.source_ref starts with "FEE-":
            p_ref = r.source_ref[4:]
            if exists(transactions, account = r.account, source_ref = p_ref) and (r.account, p_ref) not in batch_refs:
                # the parent was imported earlier WITHOUT this fee row => it came from a regime B file and the
                # fee is already inside the parent or in a synthetic FEE row (same source_ref, caught above)
                count("skipped_duplicate"); continue
        id = INSERT transactions(date, account_id, description, amount_cents = r.cash, source_ref, fee_cents = r.fee_cents,
                                 payee, raw_type, running_balance_cents,
                                 category_id = NULL, type = NULL, parent_id = NULL, review_status = 'needs_review')
        new_ids.append(id)
    return new_ids
```

Rows are inserted uncategorised first; later stages only UPDATE (and INSERT synthetic rows). This keeps the
`UNIQUE(account_id, source_ref)` guarantee as the single source of idempotency.

### Stage 3 - categorize

```
categorize(R_all):
    for R in R_all where R.review_status != 'reviewed':
        R.review = []
        m = match_rules(R)                         # section 1.3, uses R.cash
        if m is AMBIGUOUS:  R.rule = None; R.review += ["ambiguous_rules"]; continue
        if m is None:       R.rule = None; R.review += ["no_rule"];         continue
        R.rule = m
        if m.action == "categorize":
            R.category, R.type = m.category, m.type
            if m.review: R.review += ["confirm_rule"]
        # follow_parent: decided in stage 4.  needs_invoice: decided in stage 7.
        if R.fee_cents > 0 and not m.is_fee_parent and m.action != "follow_parent":
            R.review += ["unexpected_fee"]
```

A `no_rule` row whose payee has never appeared on any earlier row of that account gets the extra reason
`unknown_payee` (it tells the reviewer "new counterparty" rather than "known vendor, new pattern").

### Stage 4 - fee follows parent (two passes)

```
FEE_CATEGORY(parent_category):                    # identical to the CASE in trg_fee_follows_parent_*
    "Owner's Draw"             -> ("Owner's Draw", "Personal")
    "Payable to WA Rubbermate" -> ("Payable to WA Rubbermate", "Pass-through")
    anything else, incl. NULL  -> ("Bank Charges", "Expense")

# pass 1: LINK. Touches no category. Runs over the whole batch before pass 2 starts.
for F in rows where F.rule.action == "follow_parent":          # the 'Wise Charges for:' rows (regime A)
    P = find(account = F.account, source_ref = F.source_ref without the "FEE-" prefix)   # batch, then DB
    if P is None:  F.review += ["orphan_fee"]; continue
    F.parent_id   = P.id
    P.has_fee_row = True
for P in rows where P.fee_cents > 0 and not P.has_fee_row:      # regime B
    P.has_fee_row = exists(transactions, account = P.account, source_ref = "FEE-" || P.source_ref)

# pass 2: ASSIGN. Runs only after every parent in the batch has its rule result from stage 3.
for F in rows where F.parent_id is set:
    P = F.parent
    if P.rule is None:                         # parent uncategorised => fee waits with it
        F.category = F.type = None; F.review += ["parent_uncategorised"]; continue
    F.category, F.type = FEE_CATEGORY(P.category)      # P.category is None for needs_invoice => Bank Charges
```

Why two passes: in a newest-first file a fee row and its parent can arrive in either order and the parent's
category can depend on another row. Pass 1 only links; pass 2 only reads. A WA receipt is still
`needs_invoice` at this point, which is fine: its fee is `Bank Charges` whether the receipt turns out to be
Service Revenue or Reimbursable.

### Stage 5 - fee split (deterministic)

```
for P in rows where P.fee_cents > 0 and P.parent_id is None and P.review_status != 'reviewed':
    if P.has_fee_row:                          # REGIME A: the fee is its own CSV row
        UPDATE P set fee_cents = 0             # the fee is NOT inside this row; amount_cents stays = cash
        continue                               # never split, whatever the category (owner draws included)
    # REGIME B from here: no FEE row exists, the fee is inside P.cash
    if P.rule is None: continue                # uncategorised: leave whole; split happens when it is categorised
    if P.category == "Owner's Draw":           # rule.fee == keep
        P.keep_fee = True                      # ONE row: amount_cents = cash, fee_cents = fee
        continue
    # split (rule.fee == split). Same formula for debits and credits:
    UPDATE P set amount_cents = P.cash + P.fee_cents, fee_cents = 0
        # debit : -120000 + 113 = -119887   (transfer = debit - fee)
        # credit:  569989 + 611 =  570600   (WA receipt booked GROSS)
    cat, typ = FEE_CATEGORY(P.category)
    INSERT transactions(date = P.date, account_id = P.account_id,
                        description = "Wise Charges for: " || P.source_ref,
                        amount_cents = -P.fee_cents(original), source_ref = "FEE-" || P.source_ref,
                        parent_id = P.id, fee_cents = 0, payee = "Wise", raw_type = P.raw_type,
                        category = cat, type = typ, review_status = 'auto')
    assert P.amount_cents + child.amount_cents == P.cash        # parent + child = the CSV cash movement
```

Stored meaning of `fee_cents` after this stage: **> 0 only when the fee is inside the row** (regime B owner
draws). Regime A parents and split parents store 0 and have a child row instead.

Consequences, all intended:
- Regime A owner draw (25 Apr, 3 Sep, ...): two rows, both `Owner's Draw` (parent -495.22, FEE child -4.78).
- Regime B owner draw (21 Oct): one row -600.00, `fee_cents = 113`.
- The synthetic child uses exactly the `source_ref` Wise uses for a real FEE row, so the same transfer imported
  once from each regime can never produce two fee rows (stage 2).

Cash is always recoverable: `cash(P) = P.amount_cents + sum(amount_cents of children with a source_ref of 'FEE-' || P.source_ref created by the split)`.
Section 4 uses this.

### Stage 6 - own-account transfer pairing

Three kinds, selected by `transfer_role`. Pairing never changes an amount or a date; each leg stays on its own
bank date (SPEC rule 5). The link is not stored (the schema has no column for it); it is recomputed on every
run and shown in the UI. Only its failures are persisted, as review reasons.

```
# 6a jar moves: the jar has no statement, so the jar leg is synthesised from the main-account row
for R in new rows where R.rule.transfer_role == "jar_move":
    jar = {"Rent & Utilities": "Wise USD - Rent & Utilities 0000", "Taxes": "Wise USD - Taxes jar"}[jar name in R.description]
    if not exists(transactions, account = jar, source_ref = R.source_ref):
        INSERT transactions(date = R.date, account = jar, amount_cents = -R.cash,
                            description = R.description with "to <jar>" -> "from USD" / "from <jar>" -> "to USD",
                            source_ref = R.source_ref,            # same id, other account: unique per account
                            category = "Bank Transfer", type = "Transfer", review_status = 'auto')

# 6b Wise -> Chase, 6c card payment: greedy one-to-one match, oldest first
PAIRS = [ (out_role = wise_out,     in_role = chase_in,    window = in.date - out.date in [-1, +3] days),
          (out_role = card_pay_out, in_role = card_pay_in, window = in.date - out.date in [-3, +1] days) ]
for (out_role, in_role, window) in PAIRS:
    outs = all rows with role out_role, unpaired, sorted by date
    ins  = all rows with role in_role,  unpaired, sorted by date
    for O in outs:
        cands = [ I in ins if I.amount_cents == -O.amount_cents and window(O, I) and I not yet paired ]
        if len(cands) == 0:
            if other_account_is_covered(O):  O.review += ["unpaired_transfer"]
            else:                            mark O "in transit"          # not a review item
        else:
            pair(O, cands with the smallest |date difference|; tie -> earliest date, then lowest id)
    for I in ins still unpaired:
        if other_account_is_covered(I):  I.review += ["unpaired_transfer"]

other_account_is_covered(X):          # has the other account been imported far enough to contain the missing leg?
    return max(statement_balances.as_of for the other account) >= X.date + 3 days
```

`O.amount_cents` for a Wise leg is the amount AFTER the stage 5 split (-1,198.87, not -1,200.00): the Chase
credit equals the transfer net of the Wise fee in both regimes (8 Jan -1,500.00 / +1,500.00; 30 Jun -898.87 /
+898.87; 29 Aug -2,498.87 / +2,498.87).

Check: with Chase imported to 30 Sep, the only unpaired legs are the 2 Oct (-298.87) and 29 Oct (-1,198.87)
Wise legs, "in transit" = -1,497.74, the figure in SPEC rule 5 / section 6. Card windows observed: card 29 Jan
/ checking 30 Jan, 1 Mar / 2 Mar, 2 Jul / 3 Jul, 31 Jul / 3 Aug, same day otherwise.

### Stage 7 - invoice matching for WA receipts (ONE RULE)

Runs for every row whose rule action is `needs_invoice`, **oldest first** (an invoice can be consumed only
once, so order matters and the newest-first CSV order must not leak in).

```
match_wa_receipt(R):
    gross = R.amount_cents          # already gross after stage 5 in both regimes
                                    # (regime A: CSV amount is gross; regime B: cash + fee)
    open  = invoices where type != 'Voided' and status != 'Paid' and issue_date <= R.date

    # 1. by reference: literal number strings found in the description
    tokens = unique, in order: "INV-" || digits  for digits in re.findall(r"INV[.\s]*-\s*(\d+)", R.description)
    by_ref = [ i for t in tokens for i in open if i.number == t ]         # literal string equality
    voided = [ t for t in tokens if an invoice with number t has type 'Voided' ]

    if by_ref and sum(i.amount_cents for i in by_ref) == gross:
        matched, how = by_ref, "reference"
    else:
        # 2. by amount: exactly one open invoice whose amount equals the whole wire
        by_amt = [ i for i in open if i.amount_cents == gross ]
        if len(by_amt) == 1:  matched, how = by_amt, "amount"
        else:                 matched, how = [], None

    if not matched:
        R.category, R.type = "Reimbursable Expenses Recoverable", "Reimbursable"     # never revenue by default
        R.review += ["invoice_amount_mismatch"] if by_ref else ["no_invoice_match"]
        if voided: R.review += ["voided_invoice_referenced"]
        return

    if all(i.type == 'Commission' for i in matched):
        R.category, R.type = "Service Revenue", "Income"
    else:                                              # any non-commission invoice in the wire
        R.category, R.type = "Reimbursable Expenses Recoverable", "Reimbursable"
        if any(i.type == 'Commission' for i in matched): R.review += ["mixed_invoice_wire"]
    if how == "amount": R.review += ["invoice_matched_by_amount"]

    UPDATE transactions set category, type where id = R.id              # BEFORE the inserts (trigger reads it)
    for i in matched:
        INSERT invoice_payments(transaction_id = R.id, invoice_id = i.id, amount_cents = i.amount_cents)   # gross
        UPDATE invoices set status = 'Paid' where id = i.id
    assert sum(invoice_payments.amount_cents for R) == R.amount_cents   # SPEC rule 2
```

Rules of the matcher:
- **Revenue needs proof.** `Service Revenue` is set only when every matched invoice is a `Commission` invoice
  and the invoice amounts add up to the gross wire exactly. Everything else is
  `Reimbursable Expenses Recoverable`. `v_revenue_unbacked` must stay empty.
- The Wise fee is never part of the match (gross = net + fee; invoices are gross).
- Step 2 never combines invoices. A multi-invoice wire is recognised only by reference.
- `issue_date <= R.date`: a wire cannot pay an invoice that did not exist yet. The payment date is always
  `transactions.date`, never the Invoice Register "Date Paid" (SPEC section 9 point 1).
- Result on the 2026 fixtures (13 WA receipts with raw CSV): 11 matched by reference, 2 by amount
  (18 Jun `INV-0000157` -> Firstbase INV-000017; 31 Aug `/ROC/PO-26-08-2306` -> Firstbase INV-000019),
  0 unmatched, 0 mixed.

### Stage 8 - review queue (finalise)

```
for R in rows touched by this run where R.review_status != 'reviewed':
    if R.category is None:  R.review_status = 'needs_review'     # type stays NULL too (schema CHECK)
    elif R.review:          R.review_status = 'needs_review'     # category proposed, human confirms
    else:                   R.review_status = 'auto'
    R.notes = replace_tag(R.notes, "[review:" || ",".join(R.review) || "]" if R.review else "")
    R.notes = replace_tag(R.notes, "[rule:" || R.rule.id || "]" if R.rule else "")
```

The schema has no `review_reason` / `rule_id` column, so both are kept as bracket tags at the start of
`transactions.notes` (`[rule:wa-receipt][review:invoice_matched_by_amount] free text...`). `replace_tag`
replaces an existing tag of the same kind and leaves the user's free text alone.

---

## 3. Review queue

A row is in the queue when `review_status = 'needs_review'`. A month cannot close while any row in it is in
the queue (SPEC section 5).

| Reason code | Set by | Category on the row | Why a human is needed |
|---|---|---|---|
| `no_rule` | stage 3 | NULL | Nothing matched |
| `unknown_payee` | stage 3 | NULL | `no_rule` and the payee has never been seen on this account |
| `ambiguous_rules` | stage 3 | NULL | Two rules of equal priority disagree; the reviewer must fix the rule file |
| `confirm_rule` | stage 3 | proposed | The rule has `review: true` (any wire TO WA; HTGROWTH charge that is not the $1,250 retainer) |
| `unexpected_fee` | stage 3 | proposed | Fee on a row whose rule is not `is_fee_parent` (e.g. a fee on a direct debit) |
| `orphan_fee` | stage 4 | NULL | `Wise Charges for:` row whose parent is neither in the file nor in the database |
| `parent_uncategorised` | stage 4 | NULL | Fee row waiting for its parent; clears itself when the parent is categorised |
| `no_invoice_match` | stage 7 | Reimbursable (default) | WA receipt with no usable reference and no single invoice of that amount |
| `invoice_amount_mismatch` | stage 7 | Reimbursable (default) | Referenced invoices exist but do not add up to the gross wire (short pay, over pay, part payment) |
| `invoice_matched_by_amount` | stage 7 | per invoice type | Reference missing or mistyped; matched on amount only |
| `mixed_invoice_wire` | stage 7 | Reimbursable | One wire pays commission and reimbursable invoices together |
| `voided_invoice_referenced` | stage 7 | Reimbursable (default) | WA quoted a voided number (e.g. Wise INV-011 / INV-012) |
| `unpaired_transfer` | stage 6 | Bank Transfer | The other leg should be there (other account imported past the window) and is not |
| `unexpected_bank_fee` | 5.4 | Bank Charges | Chase $15 fee in a statement period where a waiver applied (balance >= $2,000 or Ink activity >= $2,000) |
| `possible_duplicate` | 4.1 | as categorised | Same account, date and amount as an existing row with a different `source_ref` |

What the reviewer can do (each action is one transaction and sets `review_status = 'reviewed'` on the row):

1. **Confirm** - accept the proposed category as is.
2. **Assign category** - pick category + type. If the row has fee children, they are re-derived with
   `FEE_CATEGORY`; if the new category flips a regime B row between "keep" and "split", the row is merged or
   split (section 4.3). The UI refuses `Service Revenue` unless the invoice links make `v_revenue_unbacked`
   empty for that row.
3. **Link / unlink invoices** (WA receipts) - choose the invoices; the sum must equal the row amount; category
   follows the ONE RULE from the chosen invoices.
4. **Split a mixed wire** - replace one receipt by N rows (`source_ref` = original, then `original#2`,
   `original#3`), amounts adding up to the original, each linked to its own invoices.
5. **Create rule** - prefilled from the row (account, payee or description, sign; exact amount optional).
   Appends to `rules.yaml` with a priority chosen from the band table, runs `tc rules sync`, then offers
   "apply to the other N rows in the queue that this rule matches" (a scoped re-run, section 4.2).
6. **Mark as duplicate** - delete the row (only for `possible_duplicate`, only in an unlocked month).

The reviewer cannot change `amount_cents`, `date`, `account` or `source_ref` of a bank row.

---

## 4. Idempotency and re-run semantics

### 4.1 Re-import

- Importing the same file twice inserts 0 rows: every row is skipped by `UNIQUE(account_id, source_ref)`;
  synthetic fee rows and jar mirror rows have deterministic `source_ref`s and are skipped the same way.
- Overlapping files (a full export and a monthly statement covering the same days) are safe for the same
  reason, including across fee regimes (stage 2, FEE guard).
- Chase rows have no bank id; their `source_ref` is the import-contract key
  `date|amount_cents|normalised description|n`. Two identical lines on one day are distinct (`n`); the same
  statement imported twice is skipped.
- Soft check after insert: a new row with the same account, date and amount as an existing row but a different
  `source_ref` gets `possible_duplicate` (it catches a jar statement imported after the jar leg was synthesised).
- Rows before the account's `opening_date` and rows in locked months are never inserted.
- Stages 3-8 run only on the newly inserted rows, plus existing rows they link to (a parent already in the
  database, an unpaired leg, an open invoice).

### 4.2 Re-run after a rule edit

`tc rules rerun [--from YYYY-MM] [--rule ID]` calls `run_rules` on every row that satisfies ALL of:
`review_status IN ('auto','needs_review')`, month not locked, and (if `--rule`) matched by that rule before or
after the edit.

```
rerun(scope):
    backup("rerun")
    for R in scope:  R.cash = cash(R)                      # stage 5 formula; undoes nothing in the database yet
    old = snapshot(category, type, review tags) per row
    delete invoice_payments of scoped WA receipts; set those invoices back to 'Pending'
    stages 3, 4, 5', 6, 7, 8                               # 5' = reconcile, section 4.3
    return diff(old, new)                                  # shown to the user: N rows changed, list
```

A re-run with unchanged rules changes nothing (the test `test_rerun_is_noop` asserts an empty diff and an
unchanged table checksum).

### 4.3 Stage 5 on a re-run (reconcile instead of split)

For a regime B parent (no real FEE row; a synthetic child may exist):

| Stored state | New category | Action |
|---|---|---|
| split (child exists) | still not Owner's Draw | set child category = `FEE_CATEGORY(new)`; nothing else |
| split (child exists) | Owner's Draw | merge: `parent.amount_cents += child.amount_cents`, `parent.fee_cents = -child.amount_cents`, delete child |
| whole, `fee_cents > 0` | Owner's Draw | nothing |
| whole, `fee_cents > 0` | not Owner's Draw | split as in stage 5 |

Regime A parents are never split or merged; only the FEE row's category is re-derived.
If the parent or the child is `reviewed`, nothing is merged or split and the pair is reported as a conflict.

### 4.4 Manual overrides are never overwritten

- `review_status = 'reviewed'` is the override marker. No stage updates `category_id`, `type`, `amount_cents`,
  `fee_cents`, `parent_id` or the invoice links of a reviewed row, on import or on re-run.
- One exception, required by the schema: when a human changes a parent's category, its **auto** fee children
  follow (`trg_fee_follows_parent_upd` and `v_fee_parent_mismatch` demand it). A fee child that was itself
  reviewed is left alone and the mismatch is reported.
- Invoice links created or confirmed by a human (`reviewed` receipt) are not deleted by a re-run, and their
  invoices are not available to the matcher.
- Locked months are untouched by everything (the `trg_locked_tx_*` triggers would abort anyway; the engine
  filters them out first so a re-run never fails half-way).

---

## 5. Chase and card quirks

### 5.1 Chase *5200 statement periods are not calendar months

Periods seen: Jan 1-Jan 30, Jan 31-Feb 27, Feb 28-Mar 31, May 1-May 29, May 30-Jun 30.

- The importer returns statement meta: `account, period_start, period_end, opening_balance_cents,
  closing_balance_cents`. The pipeline never assumes a statement equals a month.
- Transactions keep their own posting date; the P&L month is the month of `transactions.date`, whatever
  statement the line was printed on (a 31 Jan line on the February statement is January).
- Import check (before commit): `ledger balance at period_start - 1 day == opening_balance_cents` and
  `ledger balance at period_end == closing_balance_cents`. A failure aborts the import.
- Tie-out row: `statement_balances(account, as_of = period_end, balance_cents = closing_balance_cents)`.
  `period_end` always falls inside the calendar month it closes, which satisfies
  `trg_month_lock_requires_tie_out`. `v_tie_out` then compares the ledger at `period_end`, which is the true
  tie. The workbook (and `tests/fixtures/statement_balances.csv`) uses the calendar month-end date instead;
  the two agree whenever no *5200 line is dated between `period_end` and month-end, which is the case for
  Jan-Sep 2026. If a month-end row already exists for that month, the importer keeps it and adds nothing,
  after asserting the ledger at month-end equals it.
- A month whose statement has not been imported cannot close (October 2026 today).

### 5.2 Card *2110 liability is stored negative

- Charges are negative, payments and refunds positive (SPEC sign convention). The importer flips the
  statement's signs once; the rules engine never negates anything.
- "New Balance $1,492.97" is stored as `-149297`; opening balance `-148992` at 2025-12-28.
- The cycle closes on the 28th (`accounts.statement_day = 28`): tie-out row `as_of = YYYY-MM-28`. A charge
  dated the 29th-31st belongs to the next statement but to its own calendar month in the P&L.
- A refund (3 Sep Firstbase +149.00) matches the same vendor rule as the charge (rules without `sign` are
  sign-agnostic) and nets the category to zero.

### 5.3 Card payment = two legs

- Checking leg: rule `chase-card-payment-out` (`card_pay_out`), negative on *5200, `Bank Transfer`.
- Card leg: rule `card-payment-in` (`card_pay_in`), positive on *2110, `Bank Transfer`.
- Paired by stage 6c: equal and opposite amounts, card date - checking date in [-3, +1] days.
- Each leg stays on its own date, so a pair can straddle a month (card 31 Jul, checking 3 Aug): the monthly
  `v_transfer_net` is then non-zero for both months and the cumulative total is zero.
- A leg whose partner's statement is not imported yet is "in transit", not a review item (card cycle
  29 Jul-28 Aug is missing from the fixtures; see MANIFEST).

### 5.4 Chase $15 monthly service fee

Rule `chase-monthly-fee` categorises it (`Bank Charges`). A check then runs once per imported *5200 statement.

**The statements show two waivers, not one.** Each *5200 statement prints both tests:
"$2,000 Minimum Daily Ending Balance" and "$2,000 Chase Ink Business Card Activity". The fee is charged only
when BOTH fail. SPEC rule 7 names only the balance test; April 2026 shows why that is not enough (balance
fell to $1,983.45, Ink activity $3,142.79, fee waived).

| Statement period | Lowest daily balance (printed) | Ink activity (printed) | Fee |
|---|---|---|---|
| Jan 1-Jan 30 | 2,213.65 | 1,693.86 | waived |
| Jan 31-Feb 27 | 2,095.78 | 1,693.86 | waived |
| Feb 28-Mar 31 | 2,083.45 | 1,493.08 | waived |
| Apr 1-Apr 30 | **1,983.45** | **3,142.79** | waived (Ink test) |
| May 1-May 29 | 1,947.58 | 1,611.53 | **$15** |
| May 30-Jun 30 | 2,601.05 | 1,512.86 | waived |
| Jul 1-Jul 31 | 2,087.06 | 1,512.97 | waived |
| Aug 1-Aug 31 | 1,912.96 | 1,892.18 | **$15** |
| Sep 1-Sep 30 | 2,404.65 | 1,456.41 | waived |

The Chase importer adds two optional fields to the statement meta, read from those two printed lines:
`lowest_daily_balance_cents`, `ink_activity_cents`.

```
INK_WAIVER_CENTS = 200000                        # constant; the schema has no column for it

check_chase_fee(meta):
    acct = account(meta.account)                 # monthly_fee_cents = 1500, fee_waiver_min_balance_cents = 200000
    fee_rows = rows in [period_start, period_end] matched by chase-monthly-fee
    low = meta.lowest_daily_balance_cents
          or min over each day d in [period_start, period_end] of
                 ledger_balance(acct, d) excluding fee_rows        # end-of-day, posting dates, before the fee itself
    ink = meta.ink_activity_cents                # None if the importer could not read it
    expected = low < acct.fee_waiver_min_balance_cents and (ink is None or ink < INK_WAIVER_CENTS)
    if fee_rows and not expected:  fee_rows[0].review += ["unexpected_bank_fee"]     # dispute it with Chase
    if expected and not fee_rows:  report warning "fee expected but not charged"     # no row to flag
    if len(fee_rows) > 1:          fee_rows[1:].review += ["unexpected_bank_fee"]
    if meta.lowest_daily_balance_cents is not None:
        assert ledger-computed low == meta.lowest_daily_balance_cents               # else warn: a date is off
```

On the nine statements above the check gives "expected" for May and August only, which are the only two fee
rows in the ledger (29 May, 31 Aug). `test_rule7_chase_fee_only_when_balance_below_2000` must use the
statement period and both tests, otherwise April fails.

---

## 6. Worked examples

Row format: one line per expected `transactions` row.
`parent` = `source_ref` of the parent row. `review` = `review_status`. `key` = comparison with
`tests/fixtures/transactions.csv` on (date, account, amount, category): `match`, or `diff` with the reason
stated under the table.

Descriptions are the bank's text (the answer key has hand-written descriptions; description is not compared).
Account names: W = `USD account 1430`.

### October 2026 Wise (regime B) - examples 1-7 cover all 8 CSV rows and all 12 ledger rows

**Example 1 - interest.** CSV: `BALANCE_INTEREST-817956ec-2940-4ea3-2a38-15268ba52481`, 01-10-2026, +45.13, fee 0.00.
Rule `wise-interest`.

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-01 | USD account 1430 | Interest payment | 4513 | Interest Income | Income | BALANCE_INTEREST-817956ec-2940-4ea3-2a38-15268ba52481 | | 0 | auto | match |

**Example 2 - Wise -> Chase transfer including its fee (both October transfers).**
CSV: `TRANSFER-2406973811`, 02-10-2026, -300.00, fee 1.13; `TRANSFER-2460784094`, 29-10-2026, -1200.00, fee 1.13.
Rule `wise-to-chase`; no FEE row exists -> split: -30000 + 113 = -29887 and -120000 + 113 = -119887.
Stage 6: no Chase October statement -> both legs "in transit" (-149774 in total), not in the review queue.

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-02 | USD account 1430 | Sent money to Two Commas LLC | -29887 | Bank Transfer | Transfer | TRANSFER-2406973811 | | 0 | auto | match |
| 2026-10-02 | USD account 1430 | Wise Charges for: TRANSFER-2406973811 | -113 | Bank Charges | Expense | FEE-TRANSFER-2406973811 | TRANSFER-2406973811 | 0 | auto | match |
| 2026-10-29 | USD account 1430 | Sent money to Two Commas LLC | -119887 | Bank Transfer | Transfer | TRANSFER-2460784094 | | 0 | auto | match |
| 2026-10-29 | USD account 1430 | Wise Charges for: TRANSFER-2460784094 | -113 | Bank Charges | Expense | FEE-TRANSFER-2460784094 | TRANSFER-2460784094 | 0 | auto | match |

**Example 3 - one WA wire paying Wise INV-013 + Wise INV-014, with fee.**
CSV: `TRANSFER-2441134368`, 19-10-2026, +5699.89, fee 6.11, reference
`INV-013 PO-26-10-2950 USD.3,016 COMMISSION FOR AUG 2026 INV-014 PO-26-10-2948 USD.2,690 COMMISSION FOR SEP 2026`.
Rule `wa-receipt` (needs_invoice). Split: 569989 + 611 = 570600 gross. Tokens `INV-013`, `INV-014` -> Wise
INV-013 (301600) + Wise INV-014 (269000) = 570600 = gross, both `Commission` -> Service Revenue.
`INV-014` does not match Firstbase `INV-000014` (literal string; and that one is already Paid).
`invoice_payments`: (tx, Wise INV-013, 301600), (tx, Wise INV-014, 269000). Both invoices -> Paid.

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-19 | USD account 1430 | Received money from W.A. RUBBERMATE (THAILAND) CO., LTD with reference INV-013 PO-26-10-2950 USD.3,016 COMMISSION FOR AUG 2026 INV-014 PO-26-10-2948 USD.2,690 COMMISSION FOR SEP 2026 | 570600 | Service Revenue | Income | TRANSFER-2441134368 | | 0 | auto | match |
| 2026-10-19 | USD account 1430 | Wise Charges for: TRANSFER-2441134368 | -611 | Bank Charges | Expense | FEE-TRANSFER-2441134368 | TRANSFER-2441134368 | 0 | auto | match |

**Example 4 - owner draw including its fee (not split).**
CSV: `TRANSFER-2445337879`, 21-10-2026, -600.00, fee 1.13, payee `Joshua Decker`.
Rule `owner-draw-joshua` (`fee: keep`); no FEE row exists -> one row, fee recorded inside.

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-21 | USD account 1430 | Sent money to Joshua Decker | -60000 | Owner's Draw | Personal | TRANSFER-2445337879 | | 113 | auto | match |

**Example 5 - owner draw with no fee.**
CSV: `TRANSFER-2459989965`, 29-10-2026, -300.00, fee 0.00, payee `Joshua F Decker`. Rule `owner-draw-joshua`.

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-29 | USD account 1430 | Sent money to Joshua F Decker | -30000 | Owner's Draw | Personal | TRANSFER-2459989965 | | 0 | auto | match |

**Example 6 - CBP direct debit.**
CSV: `DIRECT_DEBIT-40502598`, 23-10-2026, -5510.14, fee 0.00, merchant `US CBP`. Rule `cbp-direct-debit`.
Never an expense (SPEC rule 6). Not linked to an invoice: `invoice_payments` records receipts only.

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-23 | USD account 1430 | Paid to US CBP | -551014 | Reimbursable Expenses Recoverable | Reimbursable | DIRECT_DEBIT-40502598 | | 0 | auto | match |

**Example 7 - Maersk payment with fee.**
CSV: `TRANSFER-2451302590`, 24-10-2026, -136.13, fee 1.13. Rule `maersk-wise`; split: -13613 + 113 = -13500.
The payment is balance sheet; its fee is a real cost (`Bank Charges`).

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-10-24 | USD account 1430 | Sent money to Maersk Customs Services USA Inc | -13500 | Reimbursable Expenses Recoverable | Reimbursable | TRANSFER-2451302590 | | 0 | auto | match |
| 2026-10-24 | USD account 1430 | Wise Charges for: TRANSFER-2451302590 | -113 | Bank Charges | Expense | FEE-TRANSFER-2451302590 | TRANSFER-2451302590 | 0 | auto | match |

**October check.** The 12 rows of examples 1-7 sum to
4513 - 29887 - 113 - 119887 - 113 + 570600 - 611 - 60000 - 30000 - 551014 - 13500 - 113 = **-230125** cents
= 1,334,465 - 1,564,590 (31 Oct balance 13,344.65 - 30 Sep balance 15,645.90).
P&L for October from these rows: revenue 570600, bank charges 950 (113 x 3 + 611), interest 4513, net 574163
(SPEC golden G9).

### Earlier months - examples 8-12

**Example 8 - tariff-refund wire to WA with fee -> Payable to WA (regime A).**
CSV: `TRANSFER-2236910892`, 08-07-2026, -18339.44, `Total fees` 36.13, and the separate row
`FEE-TRANSFER-2236910892`, -36.13, `Wise Charges for: TRANSFER-2236910892`.
Parent: rule `wa-outbound-wire` (`review: true`). A FEE row exists -> parent NOT split, stored `fee_cents` 0.
Fee row: rule `wise-fee-line`, linked in pass 1, category from `FEE_CATEGORY("Payable to WA Rubbermate")` in pass 2.
With the 29 Jun refund (+1837557) the liability nets to 1837557 - 1833944 - 3613 = 0.

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-07-08 | USD account 1430 | Sent money to W.A. Rubbermate (Thailand) Co., Ltd. (fee: 36.13 USD) | -1833944 | Payable to WA Rubbermate | Pass-through | TRANSFER-2236910892 | | 0 | needs_review | match |
| 2026-07-08 | USD account 1430 | Wise Charges for: TRANSFER-2236910892 | -3613 | Payable to WA Rubbermate | Pass-through | FEE-TRANSFER-2236910892 | TRANSFER-2236910892 | 0 | auto | match |

The parent is `needs_review` with reason `confirm_rule` (category already correct; one click to confirm).

**Example 9 - jar move.**
CSV: `BALANCE-4695954807`, 26-01-2026, -293.00, `Moved 293.00 USD to Rent & Utilities`, type `CONVERSION`.
Rule `wise-jar-move`; stage 6a synthesises the jar leg (same `source_ref`, jar account). Net 0.

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-01-26 | USD account 1430 | Moved 293.00 USD to Rent & Utilities | -29300 | Bank Transfer | Transfer | BALANCE-4695954807 | | 0 | auto | match |
| 2026-01-26 | Wise USD - Rent & Utilities 0000 | Moved 293.00 USD from USD | 29300 | Bank Transfer | Transfer | BALANCE-4695954807 | | 0 | auto | match |

**Example 10 - Firstbase $35 on the card (and why it is not the $149 rule).**
Card *2110 lines "FIRSTBASE.IO, INC." -35.00 on 15 Jan and -149.00 on 1 Jan.
-3500 matches `firstbase-35` (priority 20, exact amount) before `firstbase-other` (priority 61).
`source_ref` is the import-contract key built by the card importer (shown with the description as the answer
key has it).

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-01-15 | J. DECKER 2110 | FIRSTBASE.IO, INC. | -3500 | Postage & Delivery | Expense | 2026-01-15\|-3500\|firstbase.io, inc.\|0 | | 0 | auto | match |
| 2026-01-01 | J. DECKER 2110 | FIRSTBASE.IO, INC. | -14900 | Other Professional Services | Expense | 2026-01-01\|-14900\|firstbase.io, inc.\|0 | | 0 | auto | match |

**Example 11 - Chase $15 monthly service fee.**
Chase *5200 statement "May 01, 2026 through May 29, 2026", fee line 05/29 "Monthly Service Fee" $15.00.
Rule `chase-monthly-fee`. Check 5.4: lowest daily balance 194758 (< 200000) and Ink activity 161153
(< 200000) -> both waivers fail -> the fee is expected -> no review reason.

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-05-29 | BUS COMPLETE CHK 5200 | MONTHLY SERVICE FEE | -1500 | Bank Charges | Expense | 2026-05-29\|-1500\|monthly service fee\|0 | | 0 | auto | match |

**Example 12 - card payment pair across a month end.**
Card line 31 Jul "Payment Thank You-Mobile" +1,492.97; Chase *5200 line posted 08/03
"07/31 Payment To Chase Card Ending IN 2110" 1,492.97.
Rules `card-payment-in` and `chase-card-payment-out`; stage 6c pairs them (card date - checking date = -3 days).
July `v_transfer_net` +149297 from this pair, August -149297, cumulative 0.

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-07-31 | J. DECKER 2110 | Payment Thank You-Mobile | 149297 | Bank Transfer | Transfer | 2026-07-31\|149297\|payment thank you-mobile\|0 | | 0 | auto | match |
| 2026-08-03 | BUS COMPLETE CHK 5200 | Payment to Chase card ending in 2110 08/03 | -149297 | Bank Transfer | Transfer | 2026-08-03\|-149297\|payment to chase card ending in 2110 08/03\|0 | | 0 | auto | match |

### Supplementary cases (not counted in the twelve)

**S1 - regime A owner draw with a separate FEE row. Deliberate difference from the answer key.**
CSV: `TRANSFER-2349562344`, 03-09-2026, -495.22, `Total fees` 4.78, plus `FEE-TRANSFER-2349562344`, -4.78.
A FEE row exists -> not split, not merged; the fee row follows the parent into `Owner's Draw`.

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-09-03 | USD account 1430 | Sent money to Joshua F Decker (fee: 4.78 USD) | -49522 | Owner's Draw | Personal | TRANSFER-2349562344 | | 0 | auto | diff |
| 2026-09-03 | USD account 1430 | Wise Charges for: TRANSFER-2349562344 | -478 | Owner's Draw | Personal | FEE-TRANSFER-2349562344 | TRANSFER-2349562344 | 0 | auto | diff |

Difference: the answer key has ONE row for this draw (-500.00, "Incl. Wise fee $4.78"). The engine keeps the
two bank rows, exactly as the key itself does for the seven Jan-Aug draws (e.g. 25 Apr: -289.38 and -3.62,
both `Owner's Draw`). Total, category, P&L and tie-out are identical (-49522 - 478 = -50000). The test
compares draws after collapsing the fee into the parent on both sides (section 7.2).

**S2 - WA receipt whose reference has no invoice number (regime A).**
CSV: `TRANSFER-2341692850`, 31-08-2026, +2812.50, reference `/ROC/PO-26-08-2306`, plus
`FEE-TRANSFER-2341692850`, -6.11. No token -> step 2: exactly one open invoice of 281250 issued on or before
31 Aug (Firstbase INV-000019, Commission) -> Service Revenue, reason `invoice_matched_by_amount`.

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-08-31 | USD account 1430 | Received money from W.A. RUBBERMATE (THAILAND) CO LTD with reference /ROC/PO-26-08-2306 | 281250 | Service Revenue | Income | TRANSFER-2341692850 | | 0 | needs_review | match |
| 2026-08-31 | USD account 1430 | Wise Charges for: TRANSFER-2341692850 | -611 | Bank Charges | Expense | FEE-TRANSFER-2341692850 | TRANSFER-2341692850 | 0 | auto | match |

**S3 - Wise -> Chase in regime A, Chase leg dated the day before.**
CSV: `TRANSFER-1909616554`, 08-01-2026, -1500.00, `Total fees` 1.13, plus `FEE-TRANSFER-1909616554`, -1.13.
Not split (FEE row exists). Stage 6b pairs the Wise leg with the Chase credit of 7 Jan (date difference -1).

| date | account | description | amount_cents | category | type | source_ref | parent | fee_cents | review | key |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-01-08 | USD account 1430 | Sent money to Two Commas LLC (fee: 1.13 USD) | -150000 | Bank Transfer | Transfer | TRANSFER-1909616554 | | 0 | auto | match |
| 2026-01-08 | USD account 1430 | Wise Charges for: TRANSFER-1909616554 | -113 | Bank Charges | Expense | FEE-TRANSFER-1909616554 | TRANSFER-1909616554 | 0 | auto | match |
| 2026-01-07 | BUS COMPLETE CHK 5200 | REAL TIME TRANSFER RECD FROM ABA/CONTR BNK-021000021 FROM: T | 150000 | Bank Transfer | Transfer | 2026-01-07\|150000\|real time transfer recd from aba/contr bnk-021000021 from: t\|0 | | 0 | auto | match |

---

## 7. Test plan (`tests/test_pipeline.py`)

### 7.1 The comparison

```
engine = run pipeline on an empty DB seeded with accounts, categories, rules.yaml and invoices.csv:
             import raw/wise_full.csv, then raw/wise_2026-10.csv
key    = tests/fixtures/transactions.csv

tuple(row) = (date, source, amount_cents, category)         # source = accounts.source_label
assert Counter(tuple(r) for r in canon(engine_rows_in_scope)) == Counter(tuple(r) for r in canon(key_rows_in_scope))
```

It is a multiset comparison (two identical rows must appear twice on both sides). Description, type, notes,
`source_ref`, parent link and review status are NOT part of this comparison; they have their own tests (7.4).
The requirement is 100%: both differences `engine - key` and `key - engine` are empty.

### 7.2 Canonical form `canon()` (applied to both sides)

One transformation only: **an `Owner's Draw` fee row is folded into its draw** (amount added to the parent,
fee row dropped).
- Engine side: rows with `parent_id` set and category `Owner's Draw` (8 rows: the seven Jan-Aug draw fees
  and 3 Sep).
- Key side: rows on `USD account 1430` with category `Owner's Draw` and description
  `Wise fee - TRANSFER-<id>`; the parent is the row whose notes contain `Wise TRANSFER-<id>` (7 rows, Jan-Aug).

This is how the "Jan-Aug draw-fee separate rows" inconsistency (SPEC section 3 notes, section 9 point 4) is
treated: the key shows the same economic event in two shapes (two rows Jan-Aug, one row from Sep); the engine
has one deterministic shape per fee regime; folding makes all of them comparable without changing a total.

### 7.3 Scope

| Key rows | Count | Treatment |
|---|---|---|
| `USD account 1430`, dated <= 2026-09-11 or >= 2026-10-01 | 97 (90 after `canon`) | compared, must match 100% |
| Jar rows that mirror a main-account jar move (`Bank Transfer` on the two jar accounts) | 6 | compared (engine synthesises them, stage 6a) |
| Jar rows for draws paid out of the jar (`Owner's Draw` on the Rent & Utilities jar) | 8 | excluded: they come from a jar statement that is not in the fixtures |
| `USD account 1430`, dated 2026-09-12 to 2026-09-30 | 6 | excluded and listed by the test: no raw Wise CSV covers these dates (15 Sep +550.00 and -6.11; 25 Sep -500.00; 30 Sep -200.00, -1,198.87, -1.13) |
| `BUS COMPLETE CHK 5200`, `J. DECKER 2110` | 118 | not in this test (importer tests, once the PDFs are parsed) |

Expected figures: engine produces 104 rows (98 on `USD account 1430`, 6 jar legs); after `canon` 96 rows on
each side; both differences empty. The excluded rows are asserted by count (8 and 6) so that a change in the
fixtures is noticed. When the September statement is supplied, the 6-row exclusion is deleted.

### 7.4 Other assertions in the same file

- `test_wise_tie_out`: opening 1044457 + engine rows on `USD account 1430` = statement balance at each month
  end Jan-Aug (1100178, 1073276, 1522865, 781632, 1341058, 3635952, 1793278, 1745508); October rows sum to
  -230125 on an opening of 1564590 -> 1334465.
- `test_rule4_wise_debit_split_sums_to_csv_amount`: for every split parent, parent + child = CSV amount.
- `test_fee_views_empty`: `v_fee_parent_mismatch` and `v_revenue_unbacked` are empty.
- `test_rule2_one_wire_many_invoices`: example 3 links (301600, 269000) and the -611 fee row.
- `test_review_queue_exact`: after the two imports exactly three rows are `needs_review`:
  18 Jun +320100 (`invoice_matched_by_amount`), 8 Jul -1833944 (`confirm_rule`),
  31 Aug +281250 (`invoice_matched_by_amount`).
- `test_reimport_is_noop`, `test_rerun_is_noop`, `test_reviewed_rows_survive_rerun`,
  `test_cross_regime_reimport` (import a regime B copy of a transfer already loaded from regime A and the
  reverse: no new rows).
- `test_worked_examples`: parses the tables in section 6 of this file and asserts each row against the
  engine output (all columns).

### 7.5 Known workbook inconsistencies and how the comparison treats them

| Inconsistency | Treatment |
|---|---|
| Jan-Aug draw fees are separate rows, Sep-Oct draws are single rows (SPEC s9 point 4) | `canon()` folds draw fees on both sides (7.2). No row is excluded |
| Wise -> Chase fee "on top" Jan-Aug vs "taken out" from Sep (SPEC s3 notes) | Not an inconsistency once the two CSV regimes are known: the engine reproduces both from the CSV. No special handling |
| `Reimbursable Expenses Recoverable` used two types in the workbook (SPEC s9 point 2) | One label now: every row of that category is `Reimbursable` (the loader maps the workbook's `Pass-through` rows; the category default and the Wise payment rules use it). `Pass-through` stays for `Payable to WA Rubbermate`. `type` is still not compared |
| Invoice Register "Date Paid" differs from the ledger for INV-000016 / 017 / 018 (SPEC s9 point 1) | The engine never reads "Date Paid". Revenue month = `transactions.date`; the comparison is on the ledger date, so nothing to adjust |
| Commission Tracker serial-number date (SPEC s9 point 3) | Not an input to the pipeline |
| Answer-key descriptions are hand-written | Description is not compared |
| Chase 30 Aug card payment: key dates the checking leg 30 Aug, Chase posted it 31 Aug | Outside this test. The engine uses the posting date (import contract); the Chase importer test must allow this one row to differ by a day. Month and balance are unaffected |

Verification run on 2026-11-15 (prototype of sections 1-2 against the fixtures): 104 engine rows; 96 = 96
after `canon`; 0 only-in-engine; 0 only-in-key; month-end balances Jan-Aug equal the statement balances;
October 12 rows, sum -230125.

---

## 8. Decisions to confirm

1. **Mixed wire** (commission + reimbursable invoices in one payment): booked whole as `Reimbursable Expenses
   Recoverable` and queued (`mixed_invoice_wire`) until the reviewer splits it. This follows the ONE RULE
   literally; the alternative is an automatic split per invoice.
2. **Amount-only invoice match** sets the category (so revenue is recognised in the right month) but stays in
   the queue for a one-click confirmation. 2 of the 13 receipts tested.
3. **Any wire TO WA** is proposed as `Payable to WA Rubbermate` and always queued (`review: true`).
4. **Regime A owner draws stay as two rows** (draw + fee, both `Owner's Draw`); only regime B draws are one
   row with `fee_cents`. This differs from the answer key for one row pair (3 Sep).
5. **Type on Reimbursable rows**: every `Reimbursable Expenses Recoverable` row is `Reimbursable` (WA receipts, card charges and Wise payments alike); `Pass-through` is only for `Payable to WA Rubbermate`.
6. **Schema gaps worked around** (no schema change made): review reason and matched rule id live as tags in
   `transactions.notes`; transfer pairs are recomputed, not stored. Dedicated columns would be cleaner.
7. **Chase fee has a second waiver** ($2,000 Ink card activity in the statement period, section 5.4). SPEC
   rule 7 and its test name only the balance test; April 2026 contradicts that. SPEC needs the correction.
8. **Missing inputs**: Wise CSV for 12-30 Sep 2026 (6 key rows untestable); the jar statements (8 key rows).
