# Options Trader — Automated Wheel Strategy

Automated options trading system that sells short-dated puts on a curated 50-stock universe (40 growth + 10 dividend) via Interactive Brokers. If assigned, it runs the wheel strategy by writing covered calls.

## Strategy Summary

| Parameter | Value |
|---|---|
| **Universe** | 40 growth + 10 dividend stocks |
| **Put DTE** | 0–2 days |
| **Put Delta** | 0.20–0.30 (moderate OTM) |
| **Position Size** | 1 contract per stock |
| **Profit Taking** | Let expire worthless |
| **VIX Gate** | Pause all trading when VIX > 25 |
| **Wheel** | On assignment → sell covered calls (5–14 DTE, 0.25–0.35 delta) |
| **Mode** | Paper / Live (toggle in config) |

## Setup

### 1. Prerequisites
- Python 3.11+
- Interactive Brokers TWS or Gateway running
- TWS API enabled (File → Global Configuration → API → Settings)

### 2. Install
```bash
cd options-trader
pip install -e .
```

### 3. Configure
```bash
cp config/.env.example config/.env
# Edit config/.env with your IBKR settings
# Edit config/settings.yaml for strategy parameters
# Edit config/watchlist.yaml to customize your stock universe
```

### 4. Run
```bash
# Paper trading (default)
python -m src.main

# Or with explicit mode
TRADING_MODE=paper python -m src.main

# Live trading (requires port 7496 or 4001)
TRADING_MODE=live IBKR_PORT=7496 python -m src.main
```

### 5. Dashboard
Open `http://localhost:8000` to view:
- Real-time position overview
- Trade history
- P&L tracking
- Manual pause/resume controls

## Architecture

```
src/
├── main.py              # Entry point — starts broker, scheduler, web
├── core/                # Config, database, models, logging
├── broker/              # IBKR connection, market data, orders, account
├── strategy/            # Universe, screening, risk, put selling, wheel
├── scheduler/           # APScheduler job definitions
└── web/                 # FastAPI dashboard with Jinja2 templates
```

## Key Risk Controls

- **VIX Gate**: No new trades when VIX > 25
- **Position Limit**: Max 50 simultaneous positions
- **Sector Cap**: Max 30% in one sector
- **Buying Power**: Never use > 60%
- **Cash Reserve**: Always keep $10K free
- **No Duplicates**: One put per stock at a time

## Configuration Reference

All settings in `config/settings.yaml`. Environment variables in `config/.env` override YAML values.

| Env Var | Override |
|---|---|
| `TRADING_MODE` | `app.mode` |
| `IBKR_HOST` | `ibkr.host` |
| `IBKR_PORT` | `ibkr.port` |
| `IBKR_CLIENT_ID` | `ibkr.client_id` |
| `IBKR_ACCOUNT` | `ibkr.account` |

## ⚠️ Disclaimer

This software is for educational purposes. Options trading involves significant risk of loss. Always paper trade first. The authors are not responsible for any financial losses.
