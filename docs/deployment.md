# Bruno — deployment runbook

How to stand Bruno up on a production host (Rasmus's clone). Written so the
operator can work through it sequentially without reverse-engineering the
codebase. Cross-references `src/borrower/CLAUDE.md` for design intent and
`docs/governance.md` for the operational policy that this runbook
implements.

Status: dev-side feature-complete as of `8193370` on `main`. This document
focuses on the host setup, not the code.

---

## 0. Prerequisites

- Python ≥ 3.11
- `git` with read access to `rainrosimannus-spec/automatic_option_trader`
- A host with persistent disk (SQLite + uploaded PDFs)
- DNS control of `mesicap.com` and `lenders.mesicap.com`
- TLS termination (Let's Encrypt via caddy/nginx, or Cloudflare in front)
- An LHV business banking account (for CAMT.053 file exports)
- A Merit Aktiva account (for the quarterly reconciliation pull-side)
- An SMTP relay (gmail app password, postmark, sendgrid — any) for magic-link delivery
- IBKR account with portfolio data available to the existing Maggy/Winston dashboard process on the same host (Bruno reads its `account` table indirectly via the shared connection layer)

---

## 1. Fresh install

```bash
git clone git@github.com:rainrosimannus-spec/automatic_option_trader.git mesicap
cd mesicap

python3.11 -m venv .venv
. .venv/bin/activate
pip install -e .
```

The repo's existing setup scripts (`setup.sh`, `start.sh`, `start-gateway-*.sh`)
remain the source of truth for the Maggy/Winston side. Bruno needs no
additional install steps beyond what's listed here.

---

## 2. Database initialization

Bruno's DB is a single SQLite file at `data/bruno.db`, separate from Maggy's
`data/trading.db`. Both are gitignored.

```bash
# Creates all Bruno tables. Idempotent — safe to re-run on schema additions.
python -c "from src.borrower.models import init_db; init_db()"
```

This creates: `counterparties`, `loans`, `loan_movements`, `loan_amendments`,
`payments`, `interest_accruals`, `audit_log`, `loan_documents`,
`bank_transactions`, `portal_users`, `portal_sessions`, `principal_users`,
`principal_sessions`, `loan_approvals`, `headroom_inputs`.

If `data/` is restored from a backup, skip this step — the backup carries the
schema with it.

---

## 3. Seed data

### 3.1 Lender entities

The 4 shareholder lender entities (Thirona Capital OÜ, SK4 HoldCo OÜ, Waddy
Holding OÜ, Arvutitugi OÜ) plus MesiCap Technologies OÜ as the internal
borrower are seeded by the existing upstream borrower seed scripts. Run
whichever script the codebase uses on first install (check the
`/borrower/loans` page to confirm the 7 known loans are present, see
CLAUDE.md "The 7 loans (real data as of May 2026)").

### 3.2 Pilot principal + portal users

```bash
python tools/seed_pilot.py
```

Idempotent. Creates the Hologram OÜ placeholder counterparty (Rasmus's
shareholder-via-sweat-equity entity) if missing, then seeds:

- `principal_users`: rain.rosimannus@gmail.com, rain.rosimannus@mac.com, rasmus.rosimannus@gmail.com, lauriluik1982@gmail.com
- `portal_users`: same emails mapped to their respective lender entities (Rain × {Thirona, SK4, Waddy}, Lauri × Arvutitugi, Rasmus × Hologram)

To preview without writing:

```bash
python tools/seed_pilot.py --dry-run
```

---

## 4. Environment variables (`.env`)

Bruno reads env vars at process start. The `.env` file in repo root is
gitignored. Minimum production set:

```bash
# Auth: turn OFF dev-mode magic-link console logging (forces real email delivery)
BRUNO_ADMIN_PROD=1
LENDER_PORTAL_PROD=1

# Dead-man switch (governance.md §3.2): turn ON in production
DEADMAN_ENABLED=1
DEADMAN_WARNING_DAYS=30
DEADMAN_FREEZE_DAYS=37
DEADMAN_EXECUTOR_NAME="<full name>"
DEADMAN_EXECUTOR_EMAIL="<executor email>"

# Merit Aktiva API (governance.md §4.2 / pulled by reconciliation job)
MERIT_API_ID=<uuid>
MERIT_API_KEY=<base64-key>

# Access quorum threshold (default 25000 if unset)
QUORUM_THRESHOLD_EUR=25000

# SMTP for magic-link delivery (Phase 3 follow-up; currently TODO)
# Once the auth.py send-helper is wired, these become required:
# SMTP_HOST=...
# SMTP_PORT=587
# SMTP_USER=...
# SMTP_PASS=...
# SMTP_FROM="MesiCap <no-reply@mesicap.com>"
```

The `bruno_run_integrations` flag (controls LHV / IBKR live calls) is set
**in `config/settings.yaml`** under `app.bruno_run_integrations: true`, not
as an env var. This is intentional — it's a config decision, not a
deployment one, and it lives next to the rest of the app config.

---

## 5. Scheduler jobs

Bruno adds five recurring jobs to the shared APScheduler instance
(`src/scheduler/jobs.py`). All times UTC:

| Cron                                | Job                                       | Notes |
| ----------------------------------- | ----------------------------------------- | ----- |
| `05:30` daily                       | `job_record_accruals`                     | snapshot interest accrual for every active loan |
| `05:45` Sundays                     | `job_write_backup_ledger`                 | write `data/backups/ledger-YYYY-Www.csv` (governance.md §2.2) |
| `06:00` on 2nd of Jan/Apr/Jul/Oct   | `job_generate_quarterly_statements`       | per-lender PDFs to `data/statements/{cp_id}/YYYY-Qn.pdf` |
| `06:15` daily                       | `job_deadman_check`                       | log warning/frozen state (no-op when DEADMAN_ENABLED off) |

Verify the scheduler picked them up: tail `logs/app.log` after a restart;
APScheduler prints "Adding job tentatively" or similar at boot.

---

## 6. DNS + TLS

| Hostname                 | Routes to                          | Notes |
| ------------------------ | ---------------------------------- | ----- |
| `mesicap.com` (or sub)   | the main FastAPI process at `/`    | Maggy/Winston dashboard + Bruno admin at `/borrower/*` |
| `lenders.mesicap.com`    | the same FastAPI process at `/lenders/*` | future: split to its own process (governance.md §5.4) |

Both terminate TLS at the reverse proxy. Bruno does not handle TLS itself.

For first pilot it's fine to leave both on the same process; the `/lenders/`
prefix + the admin auth middleware on `/borrower/*` enforce the separation.
Splitting `lenders.mesicap.com` into a separate process is a future step for
blast-radius isolation (the design is in governance.md §5.4 already; the
code change is mostly an `app.mount(...)` move).

---

## 7. First-pilot invitation flow

1. **Log in as a principal:**
   - Visit `https://mesicap.com/borrower/login`
   - Enter your registered email; in production mode the magic link is emailed (when SMTP wires up); in dev mode it prints to the server log
   - Open the magic link → session cookie set → you land on `/borrower/`

2. **Confirm pilot data:**
   - `/borrower/loans` should show the 7 known loans + warning banner for those missing agreement PDFs
   - `/borrower/lender-admin` should show 5 counterparties (4 shareholders with active loans + Hologram with none); active lender count = 4 (well below the 18/20 thresholds)
   - `/borrower/headroom` should show NA verdict (no external debt yet)

3. **Invite a lender:**
   - Open the lender's counterparty detail page (`/borrower/counterparties/{id}`)
   - Use the "Portal users" panel to add their email
   - Tell them to visit `https://lenders.mesicap.com/`, enter their email, click the magic link in their inbox
   - They land on their lender dashboard showing only their own loans

---

## 8. Health checks

Curl these after deploy. All should be quick.

```bash
# Admin login page should be reachable without auth
curl -fsS https://mesicap.com/borrower/login | grep -q "Sign in"

# Lender login page same
curl -fsS https://lenders.mesicap.com/lenders/login | grep -q "Sign in"

# Without auth, /borrower/ redirects to /borrower/login (303)
curl -fsS -o /dev/null -w "%{http_code}" https://mesicap.com/borrower/ # → 303

# /lenders/ unauthed redirects to /lenders/login
curl -fsS -o /dev/null -w "%{http_code}" https://lenders.mesicap.com/lenders/ # → 303

# Banned-terminology lint should pass on lender templates
.venv/bin/python tools/lint_lender_copy.py # → exit 0
```

Healthy log lines (after first scheduler tick):
```
INFO bruno_accruals_recorded created=N skipped=M loans_processed=K
INFO bruno_deadman_ok days_since_last_login=0
```

Unhealthy log lines that need a human:
```
ERROR bruno_deadman_frozen ...
ERROR bruno_accruals_error ...
ERROR loan_status_change_error ...
WARNING bruno_deadman_warning ...
```

---

## 9. Backup + restore

What lives on disk and needs regular backup:

- `data/bruno.db` — the loan DB. Backup hourly during business hours, daily otherwise.
- `data/contracts/{loan_id}/*.pdf` — uploaded signed agreements. **Must** be backed up; losing these is losing the legal-truth layer (governance.md §1).
- `data/statements/{cp_id}/*.pdf` — issued lender statements. Re-generatable from `data/bruno.db` + `src/borrower/statements.py` but easier to back up than recompute.
- `data/backups/ledger-*.csv` — weekly offline ledger. Can be regenerated.

Cloud backup recommendation: rsync to LHV cloud storage or to a second host
nightly. SHA-256 of each contract file is stored in `loan_documents.sha256_hash`,
so a tamper check can compare on-disk to the DB after a restore.

Restore: drop the backed-up files into place, run init_db (no-op if schema
is current), restart. No state lives outside `data/`.

---

## 10. Upgrade procedure

```bash
cd /path/to/mesicap
git pull origin main
. .venv/bin/activate
pip install -e .  # picks up any new deps (e.g. reportlab in Tier H)
python -c "from src.borrower.models import init_db; init_db()"   # idempotent
# restart your supervisor / systemd unit / whatever runs uvicorn
sudo systemctl restart mesicap-app   # or your equivalent
# smoke-check
curl -fsS https://mesicap.com/borrower/login | grep -q "Sign in"
```

If a deploy introduces a schema-breaking change (a column type swap, not just
an additive column), the commit message will say so explicitly. Default
expectation is additive-only.

---

## 11. Coordination with Rain's dev clone

Per `MEMORY.md`'s upstream relationship note: this repo is the father
original; Rain's dev clone is `mesicap` remote → son's clone. Bruno changes
flow father → son (surgical ports, never merges). Rasmus pulls on his side
when he wants new Bruno features.

`data/bruno.db` does **not** sync via git (it's gitignored). When Rasmus
first sets up, he runs the seed scripts and `tools/seed_pilot.py` to
populate his own DB. From that point on the two DBs are independent
operational states.

---

## 12. Troubleshooting

**Magic links don't arrive in email.**
- Until SMTP delivery is wired, check the app's stdout/journalctl for the
  `[bruno-admin] magic link for X: /borrower/magic/...` line. Copy-paste the
  URL into the browser.
- Once SMTP is wired and `BRUNO_ADMIN_PROD=1` is set, check the SMTP
  provider's outbound log and the recipient's spam folder.

**Dead-man freeze in effect, can't write anything.**
- Log in via `/borrower/login`. Successful login updates
  `PrincipalUser.last_login_at`, which rearms the timer to "normal" on the
  next request.
- If no principal can log in (e.g. account lockout), `DEADMAN_ENABLED=0` in
  the env temporarily disables the freeze. Re-enable after the cause is
  resolved.

**`Cannot activate this loan: no signed agreement on file`.**
- Open the loan detail page, upload the signed PDF via the Documents panel
  with type=`agreement`. Then retry the DRAFT → ACTIVE transition.

**`Quorum: this loan's face value is at or above €25,000`.**
- The loan needs approval from a second principal before activation. The
  pending loan appears on the other principal's `/borrower/` landing page in
  the "Pending approvals" card.

**`Tied-document rule` banner won't go away despite uploads.**
- The banner counts active loans without an `agreement` document. If a loan
  has only `amendment` / `side_letter` / `other` types, it still counts as
  missing. Upload with type=`agreement` specifically.

**LHV CAMT.053 ingestion: `Unknown account` skipped count > 0.**
- The file contains transactions on an IBAN not in
  `src/borrower/lhv_accounts.py`. Either add the IBAN to that file
  (deliberate code change) or confirm the transactions don't need ingesting.

**Banned-terminology lint fails the build.**
- `tools/lint_lender_copy.py` found "deposit" / "savings" / "account" /
  "balance" / "fund" / "pool" / "investment" in a lender-facing template
  under `src/lender_portal/templates/` or `data/statements/templates/`.
  Replace with neutral terms (loan / credit / principal / facility /
  outstanding). The hit's exact location is in the lint output.

**Headroom Calculator shows "manual" source forever.**
- The IBKR auto-populate job is the unbuilt piece (governance.md §6
  "Phase 2 wire"). Today: a principal edits the inputs by hand at
  `/borrower/headroom` after the daily IBKR snapshot is reviewed.

---

## 13. Quick reference — paths

- Admin: `https://mesicap.com/borrower/`
- Lender portal: `https://lenders.mesicap.com/`
- Loan list: `/borrower/loans`
- Headroom: `/borrower/headroom`
- Bank transactions (CAMT.053 staging): `/borrower/bank-transactions`
- Exports (Merit CSV + statement generate): `/borrower/exports`
- Lender admin (count + KYC overview): `/borrower/lender-admin`
- Logs: see your supervisor's log location (default `logs/app.log`)
- DB: `data/bruno.db` (SQLite, use `sqlite3 data/bruno.db` for ad-hoc queries)

See `src/borrower/CLAUDE.md` for design intent and conventions.
