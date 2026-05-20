# Bruno production deployment

The shortest path from "repo cloned on a fresh Debian/Ubuntu VPS" to
`https://lender.mesicap.com/` serving the lender portal with real magic-link
email delivery.

See also `docs/deployment.md` for the broader operational runbook (backup,
upgrade, health checks, troubleshooting). This README covers only the
**initial stand-up**.

---

## What you need before starting

- A Linux VPS with public IP. 1 vCPU / 1 GB RAM is plenty.
- DNS control of `mesicap.com` (or whichever apex you use).
- A gmail account dedicated to outbound mail, with **2FA enabled** and an
  **app password** generated at https://myaccount.google.com/apppasswords.
  Any other SMTP provider works too — gmail is just cheapest.
- SSH access as a sudo user on the VPS.

---

## Step 0 — DNS

Point both records to the VPS's IP:

```
A      mesicap.com              <vps-ip>
A      lender.mesicap.com       <vps-ip>
```

Wait until both resolve before continuing (so caddy can issue certs):

```bash
dig +short mesicap.com
dig +short lender.mesicap.com
```

---

## Step 1 — VPS provisioning

On the VPS, as sudo:

```bash
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3-pip git caddy
sudo useradd -r -d /var/lib/bruno -m -s /bin/bash bruno
sudo mkdir -p /var/lib/bruno
sudo chown bruno:bruno /var/lib/bruno
```

---

## Step 2 — Clone + install Bruno

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

## Step 3 — Seed bruno.db

```bash
# still as the bruno user inside the repo
.venv/bin/python -c "from src.borrower.models import init_db; init_db()"

# Re-run the upstream borrower seed scripts (they create the 7 known loans +
# 4 shareholder lender counterparties + MesiCap itself).
# Check src/borrower/CLAUDE.md "The 7 loans" for what should appear.

# Then seed pilot principal_users + portal_users:
.venv/bin/python tools/seed_pilot.py
```

Confirm:
```bash
sqlite3 data/bruno.db "SELECT count(*) FROM counterparties;"  # ≥ 6 (incl. Hologram)
sqlite3 data/bruno.db "SELECT count(*) FROM principal_users;" # 4
sqlite3 data/bruno.db "SELECT count(*) FROM portal_users;"    # 8
```

---

## Step 4 — Production env file

```bash
# As root or sudo:
sudo cp deploy/bruno.env.example /etc/bruno.env
sudo chown root:bruno /etc/bruno.env
sudo chmod 0640 /etc/bruno.env
sudo nano /etc/bruno.env   # set SMTP_HOST/USER/PASS/FROM, DEADMAN_EXECUTOR_*, MERIT_API_*
```

---

## Step 5 — systemd unit

```bash
sudo cp deploy/bruno.service /etc/systemd/system/bruno.service
sudo systemctl daemon-reload
sudo systemctl enable --now bruno
sudo systemctl status bruno
```

Expected output: `Active: active (running)`.

Confirm uvicorn is listening on localhost:8000 only:

```bash
sudo ss -tlnp | grep :8000   # should show 127.0.0.1:8000, not 0.0.0.0
curl -sI http://127.0.0.1:8000/borrower/login   # → 200
```

If it fails, check `journalctl -u bruno -e`.

---

## Step 6 — Caddy reverse proxy + TLS

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo systemctl status caddy
```

Caddy will auto-provision Let's Encrypt certs for both hostnames on first
request. First-time cert issuance takes 10–30 seconds.

Verify:

```bash
curl -sI https://mesicap.com/borrower/login            # → 200
curl -sI https://lender.mesicap.com/lenders/login      # → 200
```

---

## Step 7 — Test the SMTP wiring

```bash
# Log into the admin side first to verify magic-link delivery
curl -sX POST https://mesicap.com/borrower/test-mail?to=rain.rosimannus@gmail.com
# Expected: {"smtp_configured": true, "sent": true, "reason": null}
```

If `sent: false`, check `journalctl -u bruno -e` for the smtp_send_failed
line and adjust /etc/bruno.env.

---

## Step 8 — Login as a pilot principal / lender

1. Open `https://mesicap.com/borrower/login` in a browser.
2. Enter `rain.rosimannus@gmail.com` (or whichever email you used in seeding).
3. Click "Send sign-in link."
4. Check the inbox — the email arrives from your SMTP From address.
5. Click the magic link → you land on `/borrower/` with a session cookie.

Repeat with `https://lender.mesicap.com/lenders/login` for the lender side.

---

## Step 9 — Smoke walk

```bash
# admin walk
for p in / loans lender-admin bank-accounts bank-transactions exports headroom audit statements-archive merit-reconcile contact-requests; do
  curl -sI -b /tmp/sess.txt "https://mesicap.com/borrower/$p" | head -1
done

# lender walk
for p in dashboard statements contact; do
  curl -sI -b /tmp/lender-sess.txt "https://lender.mesicap.com/lenders/$p" | head -1
done
```

All `200 OK` once authenticated.

---

## Step 10 — Approach lenders

System is live. Per `docs/governance.md §5`, you can now:

1. Onboard a lender off-portal (signed agreement, basic AML if applicable).
2. Create their Counterparty record at `/borrower/counterparties-new`.
3. Create the loan at `/borrower/loans-new`, status `DRAFT`.
4. Upload the signed PDF via the Documents panel.
5. Get the second principal approval if ≥ €25k.
6. Transition the loan to `ACTIVE`.
7. From the counterparty detail page, add a `portal_user` with the lender's
   email — they receive their magic link automatically the next time they
   request one at `https://lender.mesicap.com/lenders/login`.

---

## Reload after a code deploy

```bash
sudo -u bruno -i
cd /var/lib/bruno/automatic_option_trader
git pull origin main
. .venv/bin/activate
pip install -e . --quiet
.venv/bin/python -c "from src.borrower.models import init_db; init_db()"   # idempotent
exit
sudo systemctl restart bruno
```

Caddy needs no reload unless `/etc/caddy/Caddyfile` itself changed.
