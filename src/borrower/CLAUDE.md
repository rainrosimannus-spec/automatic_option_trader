# CLAUDE.md — Bruno (loan portfolio management)

Read this at the start of any session that touches Bruno code.

## What Bruno is

Bruno is MesiCap Technologies OÜ's internal loan portfolio management system. It tracks loans where MesiCap is the borrower (initially shareholder loans, eventually external private loans in Phase 3). Lives at `/borrower` in the trader dashboard, but is otherwise independent of Maggy/Winston code.

**Source:**
- `src/borrower/models.py` — SQLAlchemy data model
- `src/borrower/accrual.py` — interest accrual engine
- `src/borrower/audit.py` — audit-log helpers (snapshot + write_audit)
- `src/borrower/documents.py` — signed-PDF storage layer
- `src/borrower/collateral.py` — single aggregator for NLV-collateral view (lender portal)
- `src/borrower/headroom.py` — four-metric debt-burden gate
- `src/borrower/quorum.py` — 2-of-N principal-approval logic for ≥ €25k loans
- `src/borrower/deadman.py` — dead-man switch state computer
- `src/borrower/backup_ledger.py` — weekly offline CSV ledger
- `src/borrower/statements.py` — quarterly lender PDF statements (reportlab)
- `src/borrower/lhv_accounts.py` + `src/borrower/lhv_ingest.py` — LHV account registry + CAMT.053 file ingestion
- `src/borrower/merit_export.py` — Merit quarterly CSV for the bookkeeper
- `src/borrower/admin_auth.py` — magic-link auth for `/borrower/*` admin
- `src/lender_portal/` — Phase 3 portal sub-app (`auth.py`, `router.py`, `templates/`)
- `src/web/routes/borrower.py` — admin FastAPI routes under `/borrower`
- `src/web/templates/borrower_*.html` — admin page templates
- `src/scheduler/jobs.py` — daily accrual snapshots (05:30 UTC), weekly backup ledger (Sun 05:45 UTC), quarterly statement PDFs (Apr/Jul/Oct/Jan 2 06:00 UTC), daily dead-man check (06:15 UTC)
- `tools/seed_pilot.py` — pilot user + Hologram OÜ seeder
- `tools/lint_lender_copy.py` — banned-terminology CI gate
- `LEGAL_CONTEXT.md` (this dir) — regulatory perimeter; wins over anything here if they disagree
- `docs/governance.md` — design + operational policy
- `docs/deployment.md` — runbook for standing Bruno up on Rasmus's clone

**Database:**
- `data/bruno.db` — SQLite, separate from Maggy's `trading.db`. Gitignored.
- Tables: counterparties, loans, loan_movements, loan_amendments, interest_accruals, payments, audit_log, **loan_documents**, **bank_transactions**, **portal_users**, **portal_sessions**, **principal_users**, **principal_sessions**, **loan_approvals**, **headroom_inputs**

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

Status: data tracking done. **Document attachment storage built** — `loan_documents` table, upload UI on loan detail (PDF-only, ≤ 10 MB, SHA-256 hashed), tied-document rule enforced (`DRAFT → ACTIVE` requires an attached `agreement`). Lender-side download exposes `agreement` + `amendment` only (no `side_letter` / `other`). Contract generation from templates depends on lawyer-drafted Estonian templates (parallel legal track, not yet started).

### Debt burden control: four-metric framework

LTV alone is insufficient. Bruno's debt-burden gate uses four metrics:

1. **Asset Coverage ≥ 2.0x** (LTV ≤ 50%) — solvency. Loosens to 1.67x (60%) after 12-18 months operational track record.
2. **Liquidity Reserve ≥ 2.0x** of 12-month cash debt service — liquidity timing.
3. **Operating Cash Coverage ≥ 1.5x** (Maggy expected return / cash debt service) — engine capability.
4. **Net Worth** — observability only, not binding. (Was binding in an earlier draft; dropped as double-counting with Asset Coverage.)

A new loan/amendment is acceptable only if all three binding metrics stay green/amber. If any goes red, the form refuses or warns loudly.

**Denominator definition:** Asset Coverage uses **gross unencumbered assets** (cash + market value of positions), NOT net-of-debt. Subordinated debt doesn't reduce the collateral pool because in a wind-down, subordinated creditors stand behind external lenders. This is the bank-style approach.

Status: **Headroom Calculator built** at `/borrower/headroom`. Logic in `src/borrower/headroom.py`; inputs (gross NLV, cash, expected annual return) live in `headroom_inputs` table, populated either manually or — when Rasmus's clone has `bruno_run_integrations=True` — from a daily IBKR snapshot job (the auto-populate wire is the last unbuilt piece; see "What's next" below). The page surfaces a verdict banner (GO / CAUTION / REFUSE / NA) and an "evaluate hypothetical loan" form that overlays a candidate loan and re-renders the metrics.

### Subordination

All shareholder loans must be formally subordinated to external lenders before Phase 3 launches. Currently `is_subordinated=False` on all loan records — this needs to change via master agreement amendment before any external loan is taken. Flag for action when Phase 3 timing approaches.

### Lender portal privacy (load-bearing invariant)

**Legal authority:** `LEGAL_CONTEXT.md` is the regulatory reference. The rules in this section operationalize it but do not replace it. Where they disagree, LEGAL_CONTEXT.md wins until updated.

**Framing:** the portal is a **courtesy feature** for friends/family/selected lenders — not a regulated product, not a deposit interface, not an investor platform. The design should not feel like one. See `docs/governance.md` §5.0 for the philosophy preamble.

The lender portal at `lenders.mesicap.com` (Phase 3) is **read-only** and lenders see **only their own** counterparty record, loans, payments, and statements. Lenders never see MesiCap's trading data, bank statements, P&L, tax data, other lenders, or any other operational view.

**The single exception:** when a loan is collateralized against MesiCap's brokerage NLV (`loan.is_nlv_collateralized=True`), the lender of that loan sees an aggregated, EOD-snapshot collateral view: total pool NLV, % stocks/cash/other allocation pie, top 5 stock holdings (ticker + € + % of pool), cash position (€ + % of pool), and the asset-coverage ratio on their loan. Snapshot at 05:30 UTC, gated on staleness (banner at 24h, hidden at 72h). No individual option positions, no P&L, no positions ranked 6+, no share counts, no cost basis, no other lender's view of the same pool.

The portal contains no write paths of any kind — no cancel, no withdraw, no instruct. Rights that affect the loan (acceleration, demand for collateral, etc.) exist in the signed agreement and execute out-of-band.

Enforcement is technical, not procedural:
- Portal app runs as a separate FastAPI process; reads `bruno.db` via a read-only SQLite connection
- Every loan-scoped route calls a single `require_loan_owned_by_user(loan_id, user)` helper — bypass requires changing the helper
- Portal process is **forbidden from reading the trading DB** except via one explicit aggregator `collateral_view(loan_id)` that returns the §5.3 aggregates only. Any other read from positions/account/trades is a bug; CI grep should fail builds that introduce one
- Master kill switch `PORTAL_DISABLED=true` (config reload, no deploy) disables every portal route

Full spec: `docs/governance.md` §5 (Lender Portal) and §5.3 (Privacy boundary & collateral disclosure exception).

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

## What's done (status: 2026-05-20)

**Core (Phase 1):**
- Loans index, loan detail, new loan form, new counterparty form + counterparty edit
- Counterparty detail page (per-lender exposure, contact, portal-user admin)
- Movement recording form + edit + delete
- Amendment recording form
- Payment edit form with revert capability
- Interest accrual engine (rate-amendment-aware)
- Daily accrual-snapshot scheduler at 05:30 UTC; 819 historical snapshots backfilled
- Audit log helper (`src/borrower/audit.py`) wired into every mutating route

**Phase 2 (operational gates):**
- Document attachment storage: `loan_documents` table, upload UI, SHA-256 hashing, tied-document rule (DRAFT → ACTIVE requires `agreement`)
- Headroom Calculator (four-metric framework) at `/borrower/headroom` with hypothetical-loan evaluator
- LHV CAMT.053 file ingestion: `bank_transactions` staging table, parser, manual-match UI, ignore action, idempotent on (file, entry_ref)
- LHV account registry (`src/borrower/lhv_accounts.py`) with the two known IBANs
- Merit quarterly CSV exporter at `/borrower/exports/merit-YYYY-Qn.csv`
- Quarterly lender statement PDFs (reportlab); manual-generate buttons on `/borrower/exports`; scheduler at quarter-end + 1 day
- Banned-terminology lint (`tools/lint_lender_copy.py`) gating lender-facing templates

**Phase 2.5 (auth + continuity):**
- Admin magic-link auth on `/borrower/*` (PrincipalUser + PrincipalSession tables, 30-day sessions, rate limit, lockout)
- Lender magic-link auth on `/lenders/*` (PortalUser + PortalSession tables; one email can map to multiple counterparties)
- Offline backup ledger (`src/borrower/backup_ledger.py`) — Sunday 05:45 UTC CSV write
- Dead-man switch (`src/borrower/deadman.py`) — opt-in via env, banner + 423 freeze when no principal logs in for N days

**Phase 3 (external-lender-ready):**
- Lender portal sub-app at `/lenders/` — login, dashboard, loan detail, payment history, statements list with downloads, collateral view scaffold, contact, logout
- Counterparty isolation via single `_require_loan_owned_by_user` helper; collateral_view aggregator is the only allowed Maggy/Winston cross-product read
- Lender-side signed-doc download (`agreement` / `amendment` only; `side_letter` / `other` never lender-visible)
- Access quorum (`src/borrower/quorum.py`): loans ≥ €25k face value need 2 distinct principal approvals before DRAFT → ACTIVE; approve/revoke routes + inbox panel on `/borrower/` landing
- Hologram OÜ placeholder counterparty for Rasmus (shareholder via sweat equity); pilot portal users seeded for Rain × 2 emails, Rasmus, Lauri

All committed and pushed through `8193370` on `main`.

## What's next

Buildable today:
1. **Merit API pull-side reconciliation** (`docs/governance.md §4.2`) — creds are in `.env`; build the per-lender balance diff page + a CSV-import path (LHV-style: file first, live API gated for Rasmus's side).
2. **Production deployment runbook** (`docs/deployment.md`) — done in this same batch as the docs refresh.

Blocked on credentials / external state:
3. **SMTP magic-link delivery** — currently dev mode logs the magic URL to stdout. Replace with real outbound email once SMTP credentials are provided. The send-helper has a clean injection point (`auth.DEV_LOG_MAGIC_LINKS`).
4. **IBKR NLV auto-populate** for the Headroom Calculator + Collateral view — gated to Rasmus's clone via `bruno_run_integrations`. Today the `HeadroomInputs` row is filled manually; on Rasmus's side a job reads from his existing `account` table and writes the row daily.

Blocked on legal:
5. **Subordination flip** — master-agreement amendment is legal work, not code. Once signed, the 4 shareholder loans get `is_subordinated=True` via a small migration.

Polish (nice-to-have, not load-bearing):
6. Mobile pass on lender portal templates (admin side already passed).
7. Statement-archive admin view (currently can regenerate but no per-issued-PDF history view).
8. Audit-log admin view (data is in `audit_log` table, no UI yet).
9. FX-aware quorum threshold (v1 is currency-naive; rare edge case).
10. Lender-count soft-gate on counterparty-new form when approaching ~20 active lenders.
11. 2FA on the lender portal; CSP headers; tighter session inactivity.

See `docs/governance.md §6` for the same roadmap from a design angle.

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
- Don't add any read path in the lender portal (Phase 3, `lenders.mesicap.com`) that touches Maggy/Winston tables directly. The only allowed cross-product read is via the single `collateral_view(loan_id)` aggregator returning the `docs/governance.md` §5.3 aggregates. Adding a second cross-product read path is a privacy breach, not a feature.
- Don't add any write path of any kind in the lender portal — no buttons, no forms, no action links. The portal is read-only by architectural decision.
- Don't use "deposit", "savings", "account", "balance", "fund", "pool", or "investment" in any lender-facing text — templates, statement PDFs, emails, anything that reaches a lender. Use "loan", "credit", "principal", "facility", "outstanding". This is a misclassification guard (`LEGAL_CONTEXT.md` §1–2), not a stylistic choice. CI grep should fail builds.
- Don't add advisory or evaluative copy to the lender portal or its statement PDFs. No "your loan is healthy," no "consider X," no "recommended Y." We're a record-keeper, not the lender's advisor (`LEGAL_CONTEXT.md` rule #8). Facts only, signed agreement as the binding reference.
- Don't add transferability or assignment UI to the portal. Loan agreements are non-transferable (`LEGAL_CONTEXT.md` rule #5). No "transfer," no "assign," no "sell position" affordance — not even disabled-state buttons.
- Don't add a KYC / document-upload UI to the portal. AML, where it applies, is tiered to the relationship (`docs/governance.md` §5.5 has the table): shareholders + close circle = none, less familiar / second-round = basic (ID + source-of-funds + beneficial ownership for entities + sanctions/PEP check), larger/higher-risk = enhanced. All of it happens off-portal during a private onboarding conversation and is recorded in `counterparty.kyc_status` + notes. The portal trusts what's there and doesn't gate, prompt, or re-verify.
- Don't onboard the 19th, 20th, or beyond lender without an explicit principal sign-off. `LEGAL_CONTEXT.md` rule #3 caps active lenders at ~20; the soft counter on the admin lender page should be amber at 18 and red at 20.
