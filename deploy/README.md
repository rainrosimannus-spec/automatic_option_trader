# Bruno production deployment — `lender.mesicap.com` on octoserver

Architecture (confirmed 2026-05-20):

```
                   ┌──────────────────────────── octoserver (37.0.30.34) ────────────────────────────┐
                   │                                                                                  │
DNS A records      │  Caddy (port 80/443)                                                             │
  lender.mesicap.com ─┤      ├── lender.mesicap.com → proxy 127.0.0.1:8001 ──▶ bruno-lender.service   │
  mesicap.com ────────┤      ├── mesicap.com         → 308 redirect to lender.mesicap.com             │
  mesicap.ee ─────────┤      └── mesicap.ee          → 308 redirect to lender.mesicap.com             │
                     │                                                                                │
                     │  (the existing trading dashboard process keeps running 'as it is currently'.   │
                     │   Bruno admin at /borrower/* lives there. Caddy is NOT routing the public      │
                     │   web to it — principals reach it via whatever URL/VPN they use today.)        │
                     │                                                                                │
                     │  Shared:  data/bruno.db   (SQLite WAL allows concurrent processes)             │
                     │           data/contracts/ (signed PDFs)                                        │
                     │           data/statements/ (quarterly lender PDFs)                             │
                     └──────────────────────────────────────────────────────────────────────────────────┘
```

Why two processes: blast-radius isolation (governance.md §5.4). A bug or
compromise in the lender-facing app can't mutate loans / movements /
counterparties — those routes don't exist in the standalone app.

This README covers **standing up the new lender process** + **updating
the existing trading-dashboard host's bruno.db schema**. The trading
dashboard process itself is not touched.

See also `docs/deployment.md` for the broader operational runbook.

---

## Step 0 — DNS

Confirm both records point to octoserver:

```bash
dig +short lender.mesicap.com   # → 37.0.30.34
dig +short mesicap.com          # → 37.0.30.34
dig +short mesicap.ee           # → 37.0.30.34
```

Wait until all three resolve before continuing — caddy needs them to issue
certs.

---

## Step 1 — Caddy + bruno user on octoserver

If they're not already there:

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv git caddy

# The 'bruno' user can be shared with the existing trading-dashboard process
# if it already runs under a system user with access to data/. If it doesn't
# exist yet:
sudo useradd -r -d /var/lib/bruno -m -s /bin/bash bruno
```

If the trading dashboard already runs from a different repo checkout under a
different user, just confirm both have read+write access to the same
`data/bruno.db` (set the file's group ownership appropriately or use ACLs).

---

## Step 2 — Repo checkout

```bash
sudo -u bruno -i
cd /var/lib/bruno
git clone https://github.com/rainrosimannus-spec/automatic_option_trader.git
cd automatic_option_trader
python3.11 -m venv .venv
. .venv/bin/activate
pip install -e .
```

---

## Step 3 — Schema migrations (only if bruno.db already exists)

If this is a brand-new host with no existing `data/bruno.db`:

```bash
.venv/bin/python -c "from src.borrower.models import init_db; init_db()"
.venv/bin/python tools/seed_pilot.py
```

If the existing trading-dashboard process already maintains `data/bruno.db`
on this host (likely on octoserver), bring it up to current schema:

```bash
# From the *running* repo (or this one — they share the DB):
.venv/bin/python tools/migrate_db.py
```

`migrate_db.py` is idempotent: re-runs cost nothing if the DB is already
current.

---

## Step 4 — Env file `/etc/bruno.env`

```bash
sudo cp deploy/bruno.env.example /etc/bruno.env
sudo chown root:bruno /etc/bruno.env
sudo chmod 0640 /etc/bruno.env
sudo nano /etc/bruno.env
```

Fill in:
- `BRUNO_ADMIN_PROD=1`  (forces real SMTP for admin magic links — admin process reads this too)
- `LENDER_PORTAL_PROD=1` (forces real SMTP for lender magic links)
- `LENDER_BASE_URL=https://lender.mesicap.com` (used in magic-link email body)
- `ADMIN_BASE_URL=https://...` (whatever URL principals use to reach the admin side)
- `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER=<gmail>`, `SMTP_PASS=<app-password>`, `SMTP_FROM="MesiCap <…>"`
- `DEADMAN_ENABLED=1` + `DEADMAN_EXECUTOR_*` per governance.md §3.2
- `MERIT_API_ID` / `MERIT_API_KEY` (you already have these in your local `.env`)

For gmail app password: enable 2FA on the dedicated gmail account, then visit
https://myaccount.google.com/apppasswords, generate a 16-char password, paste
it as `SMTP_PASS`.

---

## Step 5 — systemd unit for the lender portal

```bash
sudo cp deploy/bruno-lender.service /etc/systemd/system/bruno-lender.service
sudo systemctl daemon-reload
sudo systemctl enable --now bruno-lender
sudo systemctl status bruno-lender
```

Verify uvicorn is listening on 127.0.0.1:8001 only:

```bash
sudo ss -tlnp | grep :8001       # → 127.0.0.1:8001, not 0.0.0.0
curl -sI http://127.0.0.1:8001/healthz       # → 200
curl -sI http://127.0.0.1:8001/lenders/login # → 200
curl -sI http://127.0.0.1:8001/borrower/login # → 404  (admin lives elsewhere)
```

---

## Step 6 — Caddy reverse-proxy + auto-TLS

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo systemctl status caddy
journalctl -u caddy -e   # check Let's Encrypt cert issuance
```

Verify the public URLs:

```bash
curl -sI https://lender.mesicap.com/lenders/login   # → 200
curl -sI https://lender.mesicap.com/healthz          # → 200
curl -sI https://mesicap.com/                        # → 308, Location: https://lender.mesicap.com/
curl -sI https://mesicap.ee/                         # → 308, Location: https://lender.mesicap.com/
```

---

## Step 7 — SMTP test

```bash
# Hit the test-mail endpoint via the admin process (Bruno admin lives on
# the trading-dashboard side — adjust the URL to wherever that is). Or
# verify via the lender side with a magic-link request:
curl -sX POST https://lender.mesicap.com/lenders/login \
    -d "email=rain.rosimannus@gmail.com" -i | head -20

# Then check the gmail inbox for the rain.rosimannus@gmail.com account — the
# magic link should arrive within ~5 seconds. If not, check the logs:
sudo journalctl -u bruno-lender -e | grep smtp
```

---

## Step 8 — First lender login

1. Open `https://lender.mesicap.com/lenders/login` in a browser.
2. Enter `rain.rosimannus@gmail.com` (already seeded).
3. Click "Send sign-in link."
4. Open the magic link from the email inbox.
5. You should land on `/lenders/dashboard` showing the 4 loans across Thirona / SK4 / Waddy.

Repeat with `lauriluik1982@gmail.com` (Arvutitugi) and `rasmus.rosimannus@gmail.com` (Hologram — empty state, expected).

---

## Step 9 — Approach lenders

System is live. Per `docs/governance.md §5`, you can now:

1. Onboard a new lender off-portal (signed agreement, basic AML if applicable).
2. From the trading-dashboard admin side (`/borrower/*`), create their Counterparty + Loan + upload signed PDF + transition to ACTIVE (with quorum if ≥ €25k).
3. From the counterparty detail page, add a `portal_user` with the lender's email.
4. Tell them: visit `https://lender.mesicap.com/lenders/login`, enter their email, click the magic link in their inbox.

---

## Reload after a code deploy

```bash
sudo -u bruno -i
cd /var/lib/bruno/automatic_option_trader
git pull origin main
. .venv/bin/activate
pip install -e . --quiet
.venv/bin/python tools/migrate_db.py   # idempotent schema updates
exit
sudo systemctl restart bruno-lender
# The trading-dashboard process restarts via its own mechanism.
```

Caddy needs no reload unless `/etc/caddy/Caddyfile` itself changed.

---

## File map

| Path                               | What                                                     |
| ---------------------------------- | -------------------------------------------------------- |
| `deploy/Caddyfile`                 | Reverse-proxy + TLS + CSP/HSTS headers                   |
| `deploy/bruno-lender.service`      | systemd unit for the standalone lender process           |
| `deploy/bruno.env.example`         | Template for `/etc/bruno.env` (SMTP, dead-man, etc.)     |
| `src/lender_portal/standalone_app.py` | Lender-only FastAPI app (`create_lender_app()`)       |
| `tools/migrate_db.py`              | Idempotent schema-delta runner                           |
| `tools/seed_pilot.py`              | Pilot user + Hologram OÜ seeder                          |
