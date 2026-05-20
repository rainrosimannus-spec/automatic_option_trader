# Bruno — governance, integrations & lender portal

Draft v0.1 — 2026-05-20. Companion to `src/borrower/CLAUDE.md`.

CLAUDE.md describes *how Bruno is built*. This document describes *how Bruno
is governed and operated*, what it must integrate with, and what the lender
portal looks like. Most of what follows is **policy and architecture**, not
code. The intent is that it stays accurate as we build, so each section calls
out which parts are code-implementable, which belong in the shareholder
agreement, and which live as physical artifacts (safe deposit box, etc).

---

## 1. Authoritative Data Hierarchy

When two systems disagree, the higher one wins. This rule is invoked during
audits, disputes, and reconciliation.

| Level | Source                            | Owner          | Truth concept       |
| ----- | --------------------------------- | -------------- | ------------------- |
| 1     | Signed agreement PDF              | Lender + Borrower | **Legal truth**     |
| 2     | LHV bank transaction (EUR/USD)    | LHV            | **Cash truth**      |
| 3     | Bruno DB                          | MesiCap        | **Operational truth** |
| 4     | IBKR daily snapshot               | IBKR           | **Valuation truth** |
| 5     | Merit Aktiva ledger               | Bookkeeper     | **Statutory truth** |

**Conflict resolution rule:** if Bruno says €30,000 outstanding and the signed
PDF says €25,000, the PDF wins and Bruno must be corrected. If a bank
transaction does not appear in Bruno, Bruno gets a new movement row. If Merit
disagrees with Bruno, the resolution depends on whether the disagreement is on
*cash flow* (LHV wins → Bruno corrected → Merit reposted) or *interpretation*
(read the signed agreement; if still ambiguous, lender + MesiCap agree on an
amendment).

### Reconciliation cadence

Each adjacent layer must be reconciled on a fixed schedule. Without this, the
hierarchy is just words.

| Pair                  | Cadence                       | Owner       | Tooling                          |
| --------------------- | ----------------------------- | ----------- | -------------------------------- |
| Signed PDF ↔ Bruno    | On every amendment + annual   | MesiCap     | Manual spot-check vs PDF archive |
| LHV ↔ Bruno           | Continuous (every disbursement / repayment matched within 7 days) | MesiCap | Bank-tx ingestion + match UI (§4.1) |
| Bruno ↔ IBKR snapshot | Daily 05:30 UTC               | Bruno scheduler | Already in `job_record_accruals` (extend with NLV) |
| Bruno ↔ Merit         | Quarterly (after each VAT period) | Bookkeeper + MesiCap | Bruno quarterly export + reconciliation page (§4.2) |
| Bruno ↔ Lender portal | Real-time (read-only same DB) | Bruno       | Single source, no replication (§5) |

**Tied-document rule.** A loan can be `status=DRAFT` without an attached
agreement PDF, but the transition `DRAFT → ACTIVE` requires
`agreement_document_path` to be non-null. This makes the legal-truth layer a
hard prerequisite for the operational-truth layer. Enforce in the loan status
transition route once Document Attachment Storage (Phase 2 item #5) ships.

---

## 2. Failure Modes & Degraded Operations

What happens when a system breaks. Each row defines: trigger → effect → who
acts → what is locked.

### 2.1 IBKR snapshot unavailable

Tiered freshness:

| Age of last successful snapshot | Effect                                                      |
| ------------------------------- | ----------------------------------------------------------- |
| ≤ 4 hours                       | Normal — use as valuation                                   |
| 4–24 hours                      | Warn banner on Headroom Calculator, valuation cached        |
| 24–72 hours                     | New loan creation **blocked** (renders "valuation stale" error on form) |
| > 72 hours                      | Hard freeze: also block movements + status transitions on existing loans |

A stale snapshot is most dangerous during vol spikes, which is exactly when
IBKR is most likely to be unreachable. Conservative tiers preferred. The
72-hour figure originally proposed is too generous; replace with the table
above.

### 2.2 Bruno DB unavailable

If `bruno.db` is corrupted, the dashboard is offline, or a deploy goes wrong,
operational truth is temporarily lost. Mitigation: **offline backup ledger**.

- Weekly cron emits a CSV of every active loan: `loan_id, lender_name,
  currency, outstanding, accrued_interest, next_payment_date,
  next_payment_amount`.
- CSV is emailed to all three principals + saved to LHV cloud storage (or
  Google Drive backup).
- If Bruno is down, the most recent CSV tells you who is owed what on a given
  date. Stale up to one week, but operationally usable.
- ~30 LOC. Phase 2.5.

### 2.3 LHV unavailable

Less catastrophic since cash truth is event-driven, not state-driven. If LHV
API is down for a day, just skip auto-ingestion that day; manual
reconciliation catches up.

If LHV is down for > 1 week: pause new disbursements (we can't verify them),
allow existing payments to continue per schedule.

### 2.4 Merit unavailable

No operational effect. Quarterly reconciliation skips that quarter or shifts
by a month. Only affects statutory reporting.

### 2.5 Portal compromised

Master kill switch: a single environment variable / config setting that
disables `lender.mesicap.com` entirely (`PORTAL_DISABLED=true` →
all routes return a static "temporarily unavailable" page). Must be settable
without a deploy (config reload sufficient). See §5.5.

### 2.6 Principal availability

This is **shareholder agreement language, not code**. Bruno cannot enforce
who is allowed to do what until an auth layer exists (see §3.3). For the
record, the proposed rules:

| Status                       | Effect                                                        |
| ---------------------------- | ------------------------------------------------------------- |
| 1 of 3 principals unreachable | Other two can execute all operations; emergency authority noted in audit log |
| 2 of 3 principals unreachable | Operations freeze except: scheduled payments, mandatory tax payments, payroll |
| 3 of 3 unreachable           | Dead-man switch fires (§3.2). Designated executor takes custody |

"Unreachable" defined as: no response to a documented contact attempt within
72 hours via two distinct channels.

### 2.7 Valuation unavailable

Covered by §2.1's tiered staleness. The "no valuation → no new loans" rule
kicks in at the 24-hour mark, not 72.

---

## 3. Key Person & Continuity

Most of this is **not Bruno's job**. It is shareholder agreement and physical
artifacts. Bruno *supports* the framework with the audit log, the offline
backup ledger, and (eventually) the dead-man switch and access quorum.

### 3.1 Recovery envelopes (physical)

A sealed envelope held in a safe deposit box at LHV containing:

- Bruno DB backup credentials (encrypted USB)
- Domain registrar credentials
- Hosting provider credentials
- IBKR account access procedure
- LHV account recovery procedure
- Signed shareholder agreement copy
- Executor contact + instructions

Access: any two of three principals. If three unreachable, named executor.

### 3.2 Dead-man switch (Bruno code)

If no principal logs into Bruno for N days (proposed N=30), Bruno:

1. Sends an alert email to all three principals at all known addresses
2. Waits 7 more days
3. Freezes new loan creation, movement creation, status transitions
4. Emails the latest CSV ledger + a "what to do" instruction sheet to the
   designated executor
5. Continues to serve read-only views; scheduled payment reminders still fire

~50 LOC. Phase 2.5. **Depends on auth (§3.3) — without users, "logged in"
has no meaning.**

### 3.3 Access quorum (Bruno code, blocked on auth)

Once auth exists, loans above a threshold require approval from two
principals before transitioning `DRAFT → ACTIVE`. Implementation: an
`approvals` table; the second principal sees a pending loan in their queue
and clicks "approve."

Threshold proposal: any new loan ≥ €25,000 in face value, or any movement
≥ €10,000. Below those, single-principal approval (audit-logged) suffices.

### 3.4 Succession rights (shareholder agreement)

In each principal's estate plan: their MesiCap shares + their lender shares
flow to a designated successor. Bruno's counterparty record gets a
`successor_contact` field for this.

### 3.5 Legal signing fallback (legal artifact)

Each principal grants a limited power of attorney to one of the other
principals (or to a designated lawyer), to be exercised only in the unreachable
scenarios in §2.6. PoA documents sit in the recovery envelope.

### 3.6 Mandatory documentation standards

Every loan record in Bruno must have, before reaching `status=ACTIVE`:

- A non-null `contract_reference`
- A non-null `agreement_document_path` (tied-document rule)
- An `origination_date` matching the first DISBURSEMENT movement
- A `maturity_date`
- A populated `interest_treatment` + `day_count_convention` + initial
  `interest_rate_annual`
- A lender counterparty record with at least `name`, `type`, `tier`, and one
  IBAN (for repayments)

Current Bruno already enforces most of these via `nullable=False` columns or
form validation. The agreement_document_path requirement waits on the
Document Attachment Storage feature.

---

## 4. System Integration Architecture

### 4.1 LHV ↔ Bruno

**Goal:** every bank transaction matched to a Bruno movement (or explicitly
ignored), within 7 days of settlement.

**Approach (recommend MT940/CAMT.053 first; PSD2 API later):**

LHV exposes two interfaces:

- **PSD2 AIS API** — REST, OAuth2, 90-day consent renewal cycle. Real-time but
  operationally heavy (consent expires; needs principal action every quarter).
- **CAMT.053 / MT940 statement files** — daily downloadable files in
  ISO 20022 / SWIFT format. Battle-tested standard, no consent renewal,
  works while you sleep. Less real-time (T+1).

For v1, use CAMT.053. Move to PSD2 only if real-time becomes a requirement.

**Bruno data flow:**

```
LHV → CAMT.053 file (daily) → ingestion job parses XML →
  staging table `bank_transactions` (raw rows) →
  matching engine (amount + value_date + IBAN + reference fuzzy-match) →
    auto-match exact → link to existing LoanMovement OR auto-create
    partial-match → human confirms in match UI
    no-match → row stays in staging with status=unmatched
```

New table needed: `bank_transactions` with columns `id, value_date,
booking_date, amount, currency, counterparty_iban, counterparty_name,
reference_text, raw_xml, matched_movement_id, status (unmatched | matched |
ignored), ingested_at`.

The `LoanMovement.bank_reference` field already exists — that's the join key
for confident matches.

**Gating:** runs only on Rasmus's clone (`bruno_run_integrations=True`).
Rain's codebase ingests no real bank data — uses fixture files for testing
the matcher.

### 4.2 Merit ↔ Bruno

**Principle:** Bruno does **not** generate journal entries automatically.
Accountant owns the chart of accounts and posts journals. Bruno produces
**inputs** (CSV exports) and **reconciles** (diff view).

**Inflows (Bruno → Merit) — quarterly, via CSV:**

For each loan, a row per quarter:
```
loan_id, lender_name, currency,
opening_principal, disbursements_qtr, repayments_qtr,
interest_accrued_qtr, capitalized_qtr (if any),
closing_principal, closing_accrued_interest,
notes
```

Bookkeeper imports the CSV into Merit and posts the journals herself. The
CSV is generated by a "Quarterly export" button on the dashboard.

**Outflows (Merit → Bruno) — quarterly, via Merit API:**

After the bookkeeper posts the quarter, Bruno pulls Merit's balance for the
"Loans from shareholders" account family. Bruno compares Merit's per-lender
closing balance to its own closing outstanding, and renders a reconciliation
page:

```
Lender         Bruno close    Merit close    Diff
Thirona        30,000.00      30,000.00      0.00     ✓
Waddy           4,500.00       4,500.00      0.00     ✓
Arvutitugi      6,200.00       6,180.00     20.00     ✗  → investigate
```

Any non-zero diff blocks the quarter from being "closed" in Bruno (a soft
status: not a DB constraint, just a UI banner).

**Merit API:** REST, OAuth2. Documented at api.merit.ee. Pull only —
Bruno never writes to Merit.

### 4.3 Bruno ↔ Dashboard

Bruno already lives under `/borrower` in the same FastAPI app as
Maggy/Winston. No cross-process integration needed for the admin views.

**Cross-product data flows (planned):**

- **IBKR NLV → Headroom Calculator** — Bruno's headroom logic reads from
  the same `account` table the dashboard reads from. One source of truth
  for NLV.
- **Maggy positions → Asset Coverage** — Bruno's Asset Coverage metric
  needs the market value of all positions. Reads from the same positions
  table.
- **Bruno alerts → Dashboard inbox** — when a payment is overdue, a
  reconciliation diff is non-zero, or the dead-man switch armed, surface as
  a notification in the main dashboard.

**Important caveat:** the dashboard currently has **no user system**.
Whoever can reach the URL can use everything. This is fine while it's three
principals on a private network, but becomes blocking the moment we add the
lender portal (§5), the access quorum (§3.3), or the dead-man switch (§3.2).

**Sequencing:** auth must come before any of those. Proposed slot: between
Headroom Calculator (Phase 2) and Lender Portal (Phase 3). See §6.

### 4.4 IBKR ↔ Bruno

Already in the Phase 2 roadmap (CLAUDE.md item #7). Reads NLV from the same
shared `account` table the dashboard uses. Gated on
`bruno_run_integrations=True`. Daily snapshot at 05:30 UTC alongside the
existing accrual job.

### 4.5 PDF storage layer

Required by the tied-document rule (§1) and the Phase 2 Document Attachment
Storage item.

**Where to store:** local filesystem at `data/contracts/{loan_id}/{filename}`,
backed up to LHV cloud storage daily. Reasoning: simple, no extra service,
file paths stable, no vendor lock-in. Bruno DB stores
`agreement_document_path` (relative path) + `agreement_document_sha256` (so
we can detect tampering between backup runs).

**Upload UI:** small form on loan detail page, gated to ≤ 10 MB per file,
PDF or PNG/JPG only.

### 4.6 Integration sequencing

```
Today:    Bruno + Maggy/Winston, all local, no auth
Phase 2:  Headroom Calc + IBKR NLV + LHV CAMT.053 ingestion
          + Merit quarterly CSV + PDF storage + Document Attachments
Phase 2.5: Auth (multi-user login, roles: admin/lender/readonly)
          + Offline backup ledger
          + Dead-man switch
Phase 3:  Lender portal (§5)
          + Merit API integration (pull-side)
          + Access quorum
```

---

## 5. Lender Portal — lender.mesicap.com

Not previously designed beyond a one-line mention. This section proposes a
v1 design. Existing `borrower_lender_admin.html` page is a stub and stays
that way until this design is approved.

> **Legal authority:** `src/borrower/LEGAL_CONTEXT.md` is the regulatory
> reference. This section is the *implementation* of the portal it
> contemplates. Where the two disagree, LEGAL_CONTEXT.md wins until updated.

### 5.0 Portal philosophy (read first)

MesiCap is a small company that occasionally borrows from friends, family,
and selected others when capital is needed and the conditions suit both
sides. This portal is **a courtesy** to those lenders — a clean
self-service view instead of ad-hoc "what do I owe you" emails. It is **not
a regulated product**, **not a deposit-taking interface**, **not an
investor relations platform**, and the design should not feel like one.

Three implications for everything below:

1. **No friction we don't owe the lender.** No KYC upload form, no document
   verification flow, no "complete your profile." Whatever AML procedure
   applies (tiered to the relationship — see §5.5 for the tier table)
   happens during a private onboarding conversation, recorded in the
   Counterparty record, and the portal just trusts that. Shareholders and
   close circle get no procedure; less familiar / second-round lenders get
   basic AML; higher-risk or larger relationships get more. None of it
   passes through the portal.
2. **No language that drifts into product territory.** Don't say "deposit,"
   "savings," "account," "balance," "fund," "pool," or "investment." Say
   "loan," "principal," "outstanding," "facility," "credit." This is what
   keeps us outside the credit-institution and AIF perimeters in
   LEGAL_CONTEXT.md §1–2.
3. **No editorializing.** Show facts. Don't comment on whether the loan is
   "healthy" or "performing well." We are a record-keeper, not the
   lender's advisor.

### 5.1 Purpose

A read-only web portal where each MesiCap lender (currently 4 shareholders;
eventually a small number of external private lenders if we ever take any)
sees their own loans, payment history, and statements. Goal: replace
ad-hoc emails with a self-service view, and have a single archive of
quarterly statements.

### 5.2 Scope (v1)

**In:**

- Login (magic link to registered email; no passwords)
- Lender dashboard: per-loan outstanding + accrued interest, next scheduled
  payment, last payment received
- Payment history: chronological list of all paid/scheduled payments for
  loans where the lender is the counterparty
- Statements archive: PDF quarterly statements (one per lender per quarter)
- Contact info: lender's current contact data + an "update" link emailing
  Rain (no self-edit yet)
- **Collateral view** (only when the lender's loan is NLV-collateralized;
  see §5.3)

**Out (deliberately):**

- Any write operation of any kind (no cancel, no withdraw, no instruct, no
  edit)
- Any view into MesiCap's trading data — option positions, options P&L,
  strategy signals, Greeks, screener results, Consigliere advice, IBKR
  account number, Maggy/Winston suggestions, controls, scheduler logs
- Any view of MesiCap's bank statements, cash flow, payroll, tax filings,
  or operating expenses
- Any view into other counterparties (lender A never sees lender B's data,
  not even the existence of lender B)
- Any view into other Bruno tables outside the lender's own
  counterparty + their loans + their payments + their statements
- Any view into the audit log, dead-man switch state, system config
- Any transferability or assignment affordance (loan agreements are
  non-transferable per LEGAL_CONTEXT.md rule #5; the portal must not
  suggest otherwise — no "transfer this loan," no "assign to," no "sell
  position")
- Any advisory or evaluative text — no "your loan is healthy," no
  "consider X," no "we recommend Y." Facts only; the signed agreement is
  the binding reference (LEGAL_CONTEXT.md rule #8)
- Any KYC / document-upload UI. KYC, if needed for a new external lender,
  happens off-portal during onboarding (LEGAL_CONTEXT.md rule #10) and is
  recorded in the Counterparty record. The portal trusts that record and
  does not gate, prompt, or re-verify

### 5.3 Privacy boundary & collateral disclosure exception

**Default rule (load-bearing invariant):**

A lender sees **only** their own counterparty record, their own loans, their
own payments, and their own statements. They never see MesiCap's trading data,
bank statements, other lenders, P&L, tax data, or any other operational view.
This is enforced at the query layer — every read in the lender portal joins
through `portal_users.counterparty_id` and rejects any path that resolves to
a different counterparty.

**The collateral exception:**

When a loan is **collateralized against MesiCap's brokerage NLV** (cash +
open positions + long-term stock portfolio), the lender of that loan gets a
restricted, aggregated view of the collateral pool. This is the *only*
exception to the default rule. It exists because the lender has a legitimate
contractual interest in verifying the collateral exists and remains adequate
— otherwise the security on their loan is opaque.

The collateral view shows, **aggregated across the entire MesiCap brokerage
NLV** (Maggy options book + Winston long-term portfolio combined):

| Disclosure                          | Form                                          |
| ----------------------------------- | --------------------------------------------- |
| Total collateral pool NLV           | One number, EUR                               |
| Allocation                          | Pie: % stocks / % cash / % other (options, bonds) |
| Top 5 stock holdings                | Ticker + value in € + % of total NLV          |
| Cash position                       | Value in € + % of total NLV                   |
| Asset coverage on the lender's loan | Pool NLV ÷ loan outstanding (e.g., 4.3×)      |
| Snapshot timestamp                  | "as of YYYY-MM-DD 05:30 UTC" — see refresh rule below |

**What is NOT shown, even with the exception:**

- Individual option positions (just lumped into "other")
- Strategy / signal data
- Realized or unrealized P&L
- Holdings ranked 6 and below (top 5 only)
- Position sizes in shares (only EUR value + % of pool)
- Cost basis, holding period, or tax lots
- Any other lender's exposure or collateral on the same pool
- Any cash-flow data (deposits, withdrawals)

**Refresh cadence:** the collateral view uses the **end-of-day IBKR snapshot
at 05:30 UTC**, not live data. Reasoning: live data swings during the trading
day are noise; the lender's contractual reference point is daily close. Same
snapshot the Headroom Calculator consumes, so the two stay consistent.

**Gating on staleness:** if the snapshot is more than 24h stale, the
collateral view shows a banner ("data as of {date}, snapshot delayed") and
freezes at the last-known values. If > 72h stale, the collateral view is
hidden entirely with a "temporarily unavailable" message; the rest of the
portal (loans, payments, statements) continues to work.

**Per-loan flag:** Loan model gets a new boolean
`is_nlv_collateralized` (or richer `collateral_type` enum). Only loans with
this flag set unlock the collateral view for the lender; loans collateralized
against other assets (real estate, equipment, etc.) get a textual disclosure
through their signed PDF, not a portal view.

**Multi-collateral loans:** if a loan is collateralized against NLV *plus*
something else (e.g., NLV + a piece of real estate), the portal shows only
the NLV portion. Other collateral types disclosed via the signed agreement.

**Read-only enforcement:** the collateral view contains no buttons, no
forms, no links to action paths. There is no "demand collateral", no "freeze
loan", no "withdraw" — those rights, if they exist at all, live in the
signed agreement and execute out-of-band (lawyer + bank).

**Asset coverage ratio framing:** showing the asset coverage ratio
("4.3×") is informationally equivalent to showing pool NLV and loan
outstanding side-by-side, just clearer. With the portal's
courtesy-feature philosophy (§5.0), we *want* to be transparent with
people we know rather than make them do mental math. Default v1: show
the ratio. If the signed agreement specifies a covenant
(e.g., "Asset Coverage shall not fall below 2.0×"), the ratio's
prominence is appropriate. If it doesn't, the displayed ratio is still
just disclosure of facts the lender could derive anyway — no new
contractual obligation is created by the portal showing it.

**Legal framework alignment:** the collateral view is *informational*, not
*contractual*. Per LEGAL_CONTEXT.md rule #9 (truthful disclosure), the
view must be accurate — misrepresentation here creates personal liability
for the principals. The portal's standing footer states the boundary
explicitly: "This view is informational and does not modify the terms of
your signed loan agreement. For binding terms, refer to your contract."

**Flipping the flag:** `loan.is_nlv_collateralized` can only be set to
True if the signed agreement contains an NLV collateralization clause.
Operationally, the principal flipping the flag on the loan detail page
should be doing so after the lawyer-reviewed contract is countersigned
and uploaded — not before. No technical gate enforces this yet (would
require the tied-document rule from §1 to be live first); for now, it's
a procedural discipline backed by the audit log.

### 5.4 Architecture

Separate FastAPI app at `lender.mesicap.com`. Different domain, different
process, different deploy. Shares the read side of `bruno.db` via a SQLAlchemy
session opened with a read-only sqlite connection (`?mode=ro`).

Why separate process: blast-radius isolation. A bug or compromise in the
lender app cannot mutate the borrower DB.

```
lender.mesicap.com         dashboard.mesicap.com (Bruno admin lives here)
       │                                  │
       └──────read-only SQLite────────────┴──── data/bruno.db
```

### 5.5 User model

New table `portal_users` in `bruno.db`:

```
id, counterparty_id (FK), email, magic_link_token, magic_link_expires_at,
last_login_at, locked_at, locked_reason, created_at
```

Counterparty → 0..n portal_users (a lender entity might have multiple humans
who need access — e.g., spouse, accountant). Each portal_user belongs to
exactly one counterparty.

**Login flow:**

1. User enters email
2. Bruno generates a single-use token, emails a link
3. Link → session cookie (HTTP-only, secure, SameSite=Lax), valid 30 days
4. Rate-limit: 3 magic-link requests per email per hour

**Role:** every portal_user has effectively the same role — read-only on
their own counterparty's loans. No admin role on the portal (admin happens
on the main dashboard).

**Onboarding (off-portal):** a new portal_user is created by a MesiCap
principal from the main dashboard's lender-admin page. Creation requires
only that the underlying Counterparty record already exists with a name,
contact info, and an IBAN. Any AML procedure happens during the private
onboarding conversation and is recorded in `counterparty.kyc_status` +
free-text notes. The portal does not check or re-verify it; that's done
off-platform once, by humans.

**AML tiers (LEGAL_CONTEXT.md rule #10):** scaled to the relationship,
not one-size-fits-all. The shareholders and immediate close circle don't
need formal AML — we already know each other. For lenders further out,
basic AML is appropriate; it's also cheap (a Zoom call + an email
exchange), and it protects everyone if questions come up later.

| Counterparty profile                                  | AML floor                                  | `counterparty.kyc_status` value |
| ----------------------------------------------------- | ------------------------------------------ | ------------------------------- |
| Shareholders (tier=`shareholder`) + their entities    | None (Estonian Business Register check sufficient) | `not_required`         |
| Close personal contact, principal knows them well, loan < €25k | None or note-only ("known personally") | `not_required` or `basic` |
| Less familiar contact, or second-round / repeat lender beyond close circle, or loan ≥ €25k | **Basic:** ID copy + source-of-funds note + beneficial-ownership disclosure for entities + one-time sanctions/PEP check | `basic`             |
| Larger or higher-risk relationship (loan ≥ €100k, cross-border source, unfamiliar entity structure) | **Enhanced:** above + verified entity documents + ongoing review | `enhanced`        |
| Anything that triggers a flag (sanctions hit, opaque source, declined to disclose) | **Refuse** the loan, escalate to lawyer        | `flagged`                       |

Light-touch *means* light-touch: a brief written source-of-funds
statement, a copy of a passport or ID, and beneficial-ownership noted in
the Counterparty record. No multi-page questionnaire, no third-party
verification SaaS. The discipline is in *doing* the check and writing it
down, not in the form factor.

`counterparty.kyc_status` is free-text VARCHAR by design — these
conventions can evolve without a schema migration. Suggested values
above; document any new value in this section before using it.

**When the AML decision is non-obvious:** default conservative
(LEGAL_CONTEXT.md's "When in doubt" rule). A 30-minute lawyer call costs
less than a regulatory question costs.

**Lender-count awareness (soft, admin-side):** the main dashboard's lender
admin page shows the active-lender count and flags amber at 18, red at 20
(LEGAL_CONTEXT.md rule #3, ≤ ~20 ceiling). Not a hard block — just a
visible reminder that we're nearing the operational ceiling for staying
outside credit-institution territory. Decision to onboard the 19th, 20th,
or beyond lender requires explicit principal sign-off, not just clicking
"+ Add Counterparty."

### 5.6 Pages

| Path                    | Content                                                              | Always shown? |
| ----------------------- | -------------------------------------------------------------------- | ------------- |
| `/`                     | Login page (email input → magic link)                                | yes           |
| `/login/{token}`        | Token consumer; sets session                                         | yes           |
| `/dashboard`            | Per-loan tiles: outstanding, accrued interest, next payment          | yes           |
| `/loans/{id}`           | Single loan: full term sheet (read-only), payment schedule, movements | yes           |
| `/loans/{id}/collateral` | Collateral view (§5.3) — pool NLV, allocation pie, top 5, coverage  | **only if loan.is_nlv_collateralized** |
| `/statements`           | Quarterly statement archive (PDFs), download links                   | yes           |
| `/contact`              | Current contact info + "request update" button (emails Rain)         | yes           |
| `/logout`               | Clears session                                                       | yes           |

No `/loans/new`, no `/movements`, no admin paths. Hard-deny on the router
if a path doesn't match this list. The `/collateral` route 404s for any loan
that does not have `is_nlv_collateralized=True`, even if the lender owns
that loan — so the route's existence alone does not leak whether other
loans might have it.

### 5.7 Security

- **Kill switch (§2.5):** environment variable `PORTAL_DISABLED=true` → every
  route returns a static maintenance page. Config reload, no deploy.
- **Counterparty isolation (load-bearing):** every query joins through
  `portal_users.counterparty_id`. No path takes a `loan_id` or `payment_id`
  and trusts the URL — always re-verify ownership. Enforced as a single
  helper `require_loan_owned_by_user(loan_id, user)` that every loan-scoped
  route calls; that helper is the only place ownership is checked, so a
  bypass requires changing the helper, not adding a new route.
- **Whitelisted Maggy/Winston access:** the portal app is forbidden from
  reading the trading DB except via one explicit aggregator function
  `collateral_view(loan_id)` returning the exact aggregates listed in §5.3.
  Any other read (positions table, account table, trades table, etc.) from
  the portal process is a bug, not a feature, and CI grep should fail builds
  that introduce one.
- **Banned-terminology lint:** a CI check greps all portal templates,
  generated statement PDFs, and email templates for the words "deposit,"
  "savings," "account," "balance," "fund," "pool," "investment" (case-
  insensitive). A match fails the build. This is a misclassification
  guard, not compliance theater — using those words is what *makes* a
  lawyer reclassify the arrangement under LEGAL_CONTEXT.md §1–2.
- **No advisory output:** statement PDFs and portal pages contain only
  facts — numbers, dates, terms, signed-agreement references. No
  evaluative phrasing ("strong," "healthy," "performing well") and no
  recommendations. If a lender asks "how am I doing?" the answer lives in
  a human conversation, not in the portal.
- **Audit log:** every login + every page view writes to `audit_log` with
  `actor="portal:{user_id}"`, IP, UA, path. So we can later prove who saw
  what when.
- **Rate limiting:** per-IP + per-email on login attempts. 429 on excess.
- **No PII in URLs:** all entity refs are IDs, never names or emails.
- **Session cookie:** HTTP-only, Secure, SameSite=Lax, 30-day rolling
  expiry, regenerated on each successful login.
- **2FA:** optional in v2, mandatory at some lender count (TBD; pick a
  threshold).
- **Compromise procedure:** flip the kill switch, invalidate all sessions
  (truncate `portal_sessions` table), rotate magic-link signing secret,
  audit `audit_log` for anomalies.

### 5.8 Statement generation

Quarterly job (Apr 1, Jul 1, Oct 1, Jan 1, 05:30 UTC) generates per-lender
PDFs from a template:

- Header: MesiCap details, statement period, generation date
- Per-loan table: opening balance, disbursements, repayments, interest
  accrued, closing balance, in loan currency
- EUR equivalents at quarter-end FX (footnote rate source)
- Signed off "Computed by Bruno on {date}. For questions, contact
  rain@mesicap.com."

PDFs stored at `data/statements/{lender_id}/{YYYY-Qn}.pdf`. Portal lists
them; lender can download.

### 5.9 Failure-mode integration

The portal participates in the failure-mode framework (§2):

- **IBKR snapshot stale 24–72h:** loan/payment/statement views unaffected;
  collateral view (§5.3) shows stale banner.
- **IBKR snapshot stale > 72h:** collateral view hidden entirely with
  "temporarily unavailable"; rest of portal unaffected.
- **Bruno DB unavailable:** portal returns the maintenance page (since it
  reads the same DB).
- **Portal compromised:** kill switch flipped manually; lender ledger
  CSV (§2.2) is the fallback statement.
- **Dead-man switch fired (§3.2):** portal continues to serve read-only (so
  lenders can see what they're owed). Login still allowed. No write paths
  exist anyway.

### 5.10 Documentation standards (portal-side)

Each portal_user record requires: `counterparty_id`, `email`, and an
"invited_by" + "invitation_date" stamp (audit). Lockout writes `locked_at`
+ `locked_reason`. We do not delete portal_users — we lock them (so audit
trail remains).

### 5.11 Out of scope for v1, candidates for v2

- Lender self-edit of contact info
- 2FA mandatory
- Statement co-signing (some jurisdictions need lender-confirmed statements)
- Tax forms (e.g., Estonian interest-income summary for the lender)
- Multi-language (currently English only; Estonian planned)
- Mobile app (web responsive is enough)

---

## 6. Roadmap

Current status (2026-05-20): Bruno is functionally feature-complete on the dev
side. Outstanding work falls into three buckets — buildable, blocked on
credentials, blocked on legal.

| Phase | Items                                                                        | Status                                              |
| ----- | ---------------------------------------------------------------------------- | --------------------------------------------------- |
| 1     | Data model · accrual engine · forms · audit log                              | ✅ done                                             |
| 2     | Headroom Calc · LHV CAMT.053 ingestion · Merit quarterly CSV · PDF storage · Document Attachments · Tied-document rule · Lender statement PDFs | ✅ done                                             |
| 2 wire | IBKR NLV auto-populate of `headroom_inputs`                                 | ⏸️ gated to Rasmus's clone (`bruno_run_integrations`) — manual mode works today |
| 2.5   | Admin auth · Lender auth · Multi-entity portal users · Offline backup ledger · Dead-man switch · Tiered IBKR staleness logic | ✅ done                                             |
| 3     | Lender portal · Lender-side signed-doc download · Access quorum · Banned-terminology lint · Pilot user seeding | ✅ done                                             |
| 3 follow | Merit API pull-side reconciliation                                        | 🔨 buildable (creds in `.env`) — not yet wired      |
| 3 prod | SMTP magic-link delivery · Statement-issued notifications                   | 🔨 blocked on SMTP credentials                      |
| 3 legal | Subordination flip on all shareholder loans                                | 📝 master-agreement amendment, then 5-line migration |
| polish | Mobile lender-portal pass · Statement-archive admin view · Audit-log admin view · FX-aware quorum · 2FA · CSP headers | 🔨 buildable, deferred                              |

Where things sit today, in one paragraph: Bruno can be handed to Rasmus's
clone right now and operated end-to-end for the existing 4 shareholders.
Headroom Calculator works with manually-entered NLV/cash/return until the
IBKR wire is flipped on. Magic-link login works for dev-style use (the link
prints to the server log). The system is ready for external-lender
onboarding the moment the master-agreement subordination amendment is signed
and SMTP credentials are configured.

**Subordination reminder:** all 4 shareholder loans currently have
`is_subordinated=False`. Must flip via master-agreement amendment before
Phase 3 launches (any external lender taken in). This is legal work, not
code. See LEGAL_CONTEXT.md rule #7.

**Lender-count reminder:** LEGAL_CONTEXT.md rule #3 caps active lenders
at ~20. Today: 4 active + 1 placeholder (Hologram OÜ, Rasmus, no loans).
The soft counter on `/borrower/lender-admin` flips amber at 18 and red at
20. Approaching either is a decision that warrants a quick legal check-in.

**Deployment:** see `docs/deployment.md` for the runbook to stand Bruno up
on Rasmus's clone (env vars, DB init, seed, scheduler, health checks).

---

## 7. Living document

When any of the following change, update this file in the same PR:

- A new system joins the hierarchy (§1)
- A new failure mode is identified or a tier threshold changes (§2)
- A new continuity tool is built or a recovery envelope content changes (§3)
- A new integration is added or an existing one changes protocol (§4)
- The portal scope expands beyond §5.2's "In" list
- The roadmap sequencing changes (§6)

Read at the start of any session that touches Bruno *governance* (as opposed
to Bruno *code*). For code, CLAUDE.md remains the primary reference.
