# CLAUDE.md — Bruno (loan portfolio management)

Read this at the start of any session that touches Bruno code.

## What Bruno is

Bruno is MesiCap Technologies OÜ's internal loan portfolio management system. It tracks loans where MesiCap is the borrower (initially shareholder loans, eventually external private loans in Phase 3). Lives at `/borrower` in the trader dashboard, but is otherwise independent of Maggy/Winston code.

**Source:**
- `src/borrower/models.py` — SQLAlchemy data model (7 tables)
- `src/borrower/accrual.py` — interest accrual engine
- `src/web/routes/borrower.py` — FastAPI routes under `/borrower`
- `src/web/templates/borrower_*.html` — page templates
- `src/scheduler/jobs.py` — `job_record_accruals` runs daily at 05:30 UTC

**Database:**
- `data/bruno.db` — SQLite, separate from Maggy's `trades.db`. Gitignored.
- 7 tables: counterparties, loans, loan_movements, loan_amendments, interest_accruals, payments, audit_log

## Critical context

### Dev/prod separation

Bruno code lives in two places:
- **Rain's codebase (this one):** development environment. Has loan data because Rain entered it for testing. Should not run external integrations against real MesiCap accounts.
- **Rasmus's MesiCap clone:** production environment. Real MesiCap financials live there. External integrations (LHV API, IBKR NLV reads) belong there.

**The gate:** `cfg.app.bruno_run_integrations` in `src/core/config.py`. Default `False` on this codebase. Rasmus sets it `True` in his settings overlay.

When building Bruno features that touch external systems (LHV bank, IBKR NLV, contract generation that emails lenders, etc.), wrap them:

```python
from src.core.config import get_settings
cfg = get_settings()
if not cfg.app.bruno_run_integrations:
    log.info("skipping_external_integration_dev_codebase")
    return
```

Pure DB operations (accrual snapshots, CRUD via forms, reports) don't need the gate — they run identically on both servers.

### Counterparties and tiers

Counterparties have a `tier` field:
- `SHAREHOLDER` — Rain, Rasmus, Lauri's entities. Currently 4 lenders: Thirona, SK4 HoldCo, Waddy, Arvutitugi
- `EXTERNAL_PRIVATE` — Phase 3 external lenders, not yet onboarded
- `BANK` — institutional lenders, future
- `OTHER` — uncategorized

The `tier` distinguishes risk and regulatory treatment. Shareholder loans are subordinated (will be formally — agreement amendment pending). External private debt drives the LTV/headroom math.

### The 7 loans (real data as of May 2026)

1. **Thirona octoserver** (€30k, 11.55%, amortizing, 72 monthly installments) — back-to-back with Thirona's bank loan
2. **Waddy operational facility** (€17,500 max revolving, 5%, capitalizing)
3. **Arvutitugi operational facility** (€17,500 max revolving, 5%, capitalizing, mirrors Waddy)
4. **Thirona trading** (€8,682.90, 5%, capitalizing) — restructured 02.05.2026 from €8,500 at 0%
5. **SK4 HoldCo trading** (€3,592.13, 5%, capitalizing) — restructured 02.05.2026 from €3,200 at 0%
6. **Arvutitugi trading EUR** (€2,285, 5%, capitalizing)
7. **Arvutitugi trading USD** ($11,500, 5%, capitalizing)

All loan principals reconcile exactly to LHV bank statements at €0.00 / $0.00 diff.

## Architectural decisions (locked)

### Contract handling: Path 3 + Option C

- **Path 3:** Build contract template + generation infrastructure (not just data tracking)
- **Option C (hybrid):** Bruno is the source of truth for loan data; contracts are generated from templates filled with Bruno's variables; both parties sign externally; signed PDF uploaded back as the canonical legal artifact attached to the loan record

Status: data tracking done. Contract generation depends on lawyer-drafted Estonian templates (parallel legal track, not yet started). Document attachment storage (`contract_document` table + upload UI) not yet built.

### Debt burden control: four-metric framework

LTV alone is insufficient. Bruno's debt-burden gate uses four metrics:

1. **Asset Coverage ≥ 2.0x** (LTV ≤ 50%) — solvency. Loosens to 1.67x (60%) after 12-18 months operational track record.
2. **Liquidity Reserve ≥ 2.0x** of 12-month cash debt service — liquidity timing.
3. **Operating Cash Coverage ≥ 1.5x** (Maggy expected return / cash debt service) — engine capability.
4. **Net Worth** — observability only, not binding. (Was binding in an earlier draft; dropped as double-counting with Asset Coverage.)

A new loan/amendment is acceptable only if all three binding metrics stay green/amber. If any goes red, the form refuses or warns loudly.

**Denominator definition:** Asset Coverage uses **gross unencumbered assets** (cash + market value of positions), NOT net-of-debt. Subordinated debt doesn't reduce the collateral pool because in a wind-down, subordinated creditors stand behind external lenders. This is the bank-style approach.

Status: Headroom Calculator not yet built. Depends on LHV cash reads + IBKR NLV reads (both gated, run on Rasmus's clone).

### Subordination

All shareholder loans must be formally subordinated to external lenders before Phase 3 launches. Currently `is_subordinated=False` on all loan records — this needs to change via master agreement amendment before any external loan is taken. Flag for action when Phase 3 timing approaches.

## Coding conventions (Bruno-specific)

### Money handling

- All amounts stored as `Float` in original currency
- Currency per loan, not converted at storage time
- Multi-currency views compute EUR equivalents at display time
- All amounts stored as positive numbers; direction comes from movement_type (DISBURSEMENT = in, PRINCIPAL_REPAYMENT = out, PRINCIPAL_RESTRUCTURE = signed adjustment)

### Idempotency

Bruno operations should be safely re-runnable:
- Snapshot recorder: idempotent by `(loan_id, accrual_date)` uniqueness check
- Seed scripts: check-before-insert pattern
- Movement form: each movement is a new row; no implicit deduplication
- Payment edit form: mutates existing payment row, not idempotent by design (revert exists for that)

### Display labels (Bruno vocabulary)

When data and contracts use different words for the same concept, the contract wins:
- "Cash disbursed + paper restructure" → reframed as "Original capital + earned premium added at restructure" (Rain's correction May 17 — "premium" is real money, not paper)
- "Outstanding" = principal balance after disbursements + restructures − repayments
- "Total amount owed today" = outstanding + accrued interest
- "Headroom" only meaningful for revolving facilities (Waddy/Arvutitugi operational); bullet/amortizing loans show "— bullet —"

### Accrual math

Three methods, dispatched by `loan.interest_treatment`:
- **CAPITALIZING** — daily compounding, walks merged principal+rate segment timeline
- **AMORTIZING** — interest portion derived from amortization schedule (uses paid Payment records)
- **PAID_PERIODICALLY** (simple) — linear accrual since last payment date, walks rate segments

Rate changes (via LoanAmendment with `field_changed='interest_rate_annual'`) are honored — initial implementation missed this and over-accrued by ~€100 on loans 4-5. Fix in `_rate_segments` + `_merge_timelines`.

If you touch accrual math, verify against hand calculations on at least one loan of each type before deploying.

## What's done

- Loans index, loan detail, new loan form, new counterparty form
- Movement recording form (disbursements, repayments, restructures)
- Payment edit form with revert capability
- Interest accrual engine (correct, with rate amendments)
- Daily snapshot scheduler at 05:30 UTC
- 819 historical snapshots backfilled
- Config gating mechanism
- All committed and pushed (commits `c7af8d0` and `ec16325`)

## What's next (rough priority)

1. **Mobile UX pass** on loan detail page (Sunday May 17 frustration: tables overflow, key facts cramped)
2. **Counterparty detail page** — equivalent to loan detail but for counterparties; shows their loans, total exposure, contact info, eventual KYC documents
3. **Headroom Calculator** with the four-metric framework — needs LHV cash + IBKR NLV (gated, runs on Rasmus's clone with real data; here can run on manual inputs for testing the math)
4. **Contract template engine + PDF generation** — blocked on lawyer-drafted Estonian templates
5. **Document attachment storage** — `contract_document` table + upload UI, can also retrofit signed PDFs for existing 7 loans
6. **LHV API integration** — gated, production-only
7. **IBKR NLV reads for Bruno** — gated, production-only
8. **Lender portal** at lenders.mesicap.com — Phase 3, separate app

## Coordination with Rasmus's clone

When Bruno code is committed and pushed, Rasmus picks it up on his next pull. But his `data/bruno.db` is separate — it doesn't sync via git.

When Rasmus starts using Bruno operationally (he hasn't yet), his database needs population. Options:
- Re-run the seed scripts on his side (same content, fresh DB)
- One-time SQL dump from Rain's `bruno.db` → secure transfer → import on Rasmus's side

This is a coordination item, not urgent until Rasmus is ready.

## Don'ts

- Don't write Bruno code that assumes Maggy/Winston DB schema or tables. Bruno's DB is separate.
- Don't sneak external system calls into Bruno code paths without the `bruno_run_integrations` gate.
- Don't mark up loan amounts with FX conversions at storage time. Store original currency, convert at display time only.
- Don't break the idempotency of the snapshot recorder or seed scripts.
- Don't add fields to the Counterparty or Loan tables without considering whether they need to round-trip through the New * forms (currently nothing prevents adding orphan fields that no form populates).
