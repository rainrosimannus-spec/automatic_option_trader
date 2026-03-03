# Options Trader — Paper Trading Setup Guide

## Step 1: Configure TWS for API Access

Open TWS and log in to your **paper trading** account:

1. In TWS menu: **Edit → Global Configuration → API → Settings**
2. Enable these settings:
   - ✅ **Enable ActiveX and Socket Clients**
   - ✅ **Allow connections from localhost only**
   - Set **Socket port** to `7497` (this is the paper trading default)
   - ✅ **Read-Only API** — UNCHECK this (we need to place orders)
   - Set **Master API client ID** to blank or leave default
3. Click **Apply** then **OK**
4. You may need to restart TWS after changing these settings

### How to switch to Paper Trading in TWS:
- When logging in, click the **gear icon** next to your username
- Select **Paper Trading** mode
- Or: File → Log In → check "Use paper trading account"

Your paper trading account username is usually your real username prefixed with a `D` (e.g., if your account is `U1234567`, paper is `DU1234567`).

---

## Step 2: Install the Project

Open Terminal and run:

```bash
# Navigate to your project directory
cd ~/automatic_option_trader

# Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install the project and all dependencies
pip install -e .

# Create data directory for the database
mkdir -p data

# Create your .env file from template
cp config/.env.example config/.env
```

---

## Step 3: Configure for Paper Trading

Edit `config/.env`:

```bash
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=1
IBKR_ACCOUNT=
TRADING_MODE=paper
WEB_HOST=0.0.0.0
WEB_PORT=8000
```

> Leave `IBKR_ACCOUNT` blank — it will auto-detect your paper account.

The `config/settings.yaml` is already configured for paper trading with safe defaults. No changes needed.

---

## Step 4: Launch

Make sure TWS is running and logged into **paper trading**, then:

```bash
cd ~/automatic_option_trader
source .venv/bin/activate
python -m src.main
```

You should see output like:
```
starting_options_trader  mode=paper port=8000
database_initialized     path=data/trades.db
ibkr_connected          account=['DU1234567']
scheduler_started        jobs=[...]
```

---

## Step 5: Open the Dashboard

Open Safari and go to:

```
http://localhost:8000
```

You'll see the dashboard with:
- IBKR connection status
- VIX level and SPY MA trend
- Daily trade counter
- Open positions and trade history

---

## Step 6: Monitor for 30 Days

The system will automatically:
- Scan your 50-stock universe every 30 minutes during US market hours (9:30–16:00 ET)
- Sell puts on qualifying stocks (0–2 DTE, delta 0.20–0.30)
- Check for assignments at 10:00 AM and 3:30 PM ET
- Write covered calls on any assigned stock
- Respect all risk limits (VIX > 30 halt, SPY MA gate, 10/day max, 5% position size)

### Keeping it running:
- TWS must stay open and connected
- Your Mac must stay on (or use a headless IB Gateway instead)
- The Python process must keep running

### To run in background (optional):
```bash
nohup python -m src.main > logs/trader.log 2>&1 &
```

### To stop:
```bash
# If running in foreground: Ctrl+C
# If running in background:
kill $(pgrep -f "src.main")
```

---

## Troubleshooting

| Problem | Solution |
|---|---|
| "Connection refused" on port 7497 | TWS not running or API not enabled |
| "No market data" | Paper account may need market data subscriptions (free for paper) |
| No trades happening | Check VIX (must be < 30), check market hours, check logs |
| Dashboard not loading | Make sure port 8000 isn't used by another app |

### Check logs:
The app logs to stdout. Look for:
- `vix_gate_triggered` — VIX too high
- `risk_blocked` — a risk rule prevented a trade
- `put_sold` — successful trade
- `ibkr_error` — connection issues

### Market data for paper trading:
IBKR paper accounts may need delayed market data activated. In TWS:
- **Account → Settings → Market Data** → Subscribe to free delayed data if needed
- For options Greeks, you may need: **US Securities Snapshot and Futures Value Bundle** (free for paper)
