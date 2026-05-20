# Bruno borrower seed — cutover from Rain's dev DB to Rasmus's MesiCap clone

`data/bruno.db` carries real financial records (5 lender counterparties, 7
loans, 15 bank movements, 2 amendments, 72 payments, plus portal/principal
users). It is gitignored, so the source tree alone cannot reconstruct it.
This pair of scripts moves that data into Rasmus's MesiCap-clone DB.

## Files

| File | Role |
|---|---|
| `tools/gen_seed_borrower.py` | **Exporter** — run on Rain's machine. Reads the live `data/bruno.db` via the SQLAlchemy models and writes a JSON snapshot with FKs resolved to natural keys. |
| `tools/seed_borrower_data.json` | **Snapshot.** The committed (or transferred) data payload. Schema-versioned (v1). Contains real IBANs, principals, names. |
| `tools/seed_borrower.py` | **Importer** — run on Rasmus's machine. Reads the JSON and populates an empty (or partial) `data/bruno.db`. Idempotent. |
| `tools/seed_pilot.py` | Pre-existing portal-users layer. Already covered by the snapshot — re-running it is a no-op after `seed_borrower.py`. |

## What gets seeded

- `counterparties` (6) — names, IBANs, KYC status, Merit links, notes.
- `loans` (7) — full contract terms, FKs resolved by counterparty name.
- `loan_movements` (15) — bank movements, FK by loan `contract_reference`.
- `loan_amendments` (2) — paper restructures.
- `payments` (72) — scheduled + paid history (the Thirona octoserver schedule).
- `portal_users` (8) — lender-portal logins; auth state (tokens, sessions) stripped.
- `principal_users` (4) — internal admin logins; auth state stripped.

## What is NOT seeded

Runtime / derived / transactional tables — they repopulate on Rasmus's side:

- `interest_accruals` (840 rows on Rain's side) — the daily 05:30 UTC accrual job rebuilds these from scratch on first run.
- `audit_log` — history; not seedable.
- `portal_sessions`, `principal_sessions` — auth runtime state.
- `bank_transactions`, `headroom_inputs`, `merit_balances`, `loan_documents`, `loan_approvals`, `contact_update_requests` — runtime/empty.

## Cutover procedure (one-time)

On Rain's machine — generate the snapshot:

```
python tools/gen_seed_borrower.py
# -> tools/seed_borrower_data.json
```

Move the snapshot to Rasmus's machine (commit + pull, or scp, or any other
trusted channel — the JSON contains private financial data).

On Rasmus's machine — initialize schema (if `data/bruno.db` doesn't exist) and
seed:

```
python -c "from src.borrower.models import init_db; init_db()"
python tools/seed_borrower.py            # populate
python tools/seed_borrower.py --dry-run  # verify: should report all skipped
```

Optional spot-check:

```
sqlite3 data/bruno.db 'SELECT count(*) FROM counterparties'    # expect 6
sqlite3 data/bruno.db 'SELECT count(*) FROM loans'             # expect 7
sqlite3 data/bruno.db 'SELECT count(*) FROM payments'          # expect 72
```

After the cutover Rasmus's `data/bruno.db` is the **production** authoritative
copy; Rain's stays as a dev mirror but is expected to drift (per STATE.md L1
§"Bruno"). A future refresh-from-prod requires re-running the exporter against
the new authoritative source.

## Privacy

`tools/seed_borrower_data.json` contains real financial data — counterparty
names, registration numbers, IBANs, loan principals and rates, payment
references. Treat it like `data/bruno.db` itself: don't share publicly, don't
post in chat logs, don't attach to support tickets.
