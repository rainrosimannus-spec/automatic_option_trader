# Maggy & Winston — STATE

This file is updated at the end of every session.
It describes the system exactly as it stands RIGHT NOW.
Read this to know what to do next, what's broken, and what to test first.

---

## System Status (April 5, 2026)

Both connections stable. App running. Dashboard accessible at http://37.0.30.34:8080

| Component | Status |
|-----------|--------|
| Options gateway (port 4001) | Running |
| Portfolio gateway (port 7496) | Running |
| Trader app (port 8080) | Running |
| Trailing stop monitor | Active, every 15 min |
| Chronos forecast job | Registered, runs 17:30 ET — NOT YET TESTED ON LIVE DATA |
| Sentiment guard | Active in buyer.py |
| Earnings guard | Active in buyer.py |

---

## Current Positions

**Options account (Maggy):**
- TTD: assigned at $26.50, stock at ~$22.70, cost basis $25.52. Wheel scanning for covered calls. Working by design — TREND_BEARISH, wide strikes.
- SHOP, UBER, PANW, PG: covered calls active
- TTD: assigned at $26.50, stock at ~$22.70, cost basis $25.52 — scanning for covered call (TREND_BEARISH, wide strikes)
- VIX normalizing — new puts expected to resume

**Portfolio account (Winston):**
- 42 holdings, market value ~$874K, invested $498,514
- Unrealized P&L: -11.2% (tariff-related market selloff April 2026)
- Margin at ~94.7% — no new buys until margin clears

---

## Top Priority Next Session

1. **Chronos live test** — run nightly forecast job manually during market hours to verify it fetches real IBKR prices and writes to `portfolio_forecasts` table correctly:
```bash
cd ~/automatic_option_trader && .venv/bin/python3 -c "
from src.portfolio.forecaster import job_portfolio_chronos_forecast
from src.core.config import get_settings
job_portfolio_chronos_forecast(get_settings().portfolio)
"
```

2. **Verify trailing stop suggestions** — check that new sell_stock_review suggestions created after April 5 have `trailing_stop_pct` and `trailing_peak_price` set:
```bash
cd ~/automatic_option_trader && .venv/bin/python3 -c "
from src.core.database import get_db
from src.core.suggestions import TradeSuggestion
with get_db() as db:
    sgs = db.query(TradeSuggestion).filter(TradeSuggestion.trailing_stop_pct != None).all()
    for s in sgs:
        print(s.symbol, s.action, s.trailing_stop_pct, s.trailing_peak_price)
"
```

3. **Watchlist universe review** — review ~50 stocks with strict 10-year quality lens ahead of $5M scaling


---

## What Changed Last Session (April 5, 2026)

**Fixed bugs:**
- Portfolio loans doubled → `connection.py` now uses `TotalCashBalance BASE` only
- Accrued interest showing 0.0 → now reads from `portfolio_account_cache.json` file cache
- File cache overwritten with 0.0 on restart → guard: only write non-zero values
- Misaligned `trailing_stop_pct` in `monthly_growth_thesis_weak` and `monthly_review_reduce` → fixed

**New features:**
- Trailing stop monitor (`job_portfolio_trailing_stop_monitor`, every 15 min)
- `TradeSuggestion.trailing_stop_pct` and `trailing_stop_pct` fields + DB migration
- Earnings guard in `buyer.py` — skips buy/put-entry within 3 days of earnings
- Sentiment module `src/portfolio/sentiment.py` — Finnhub free tier, keyword scoring
- Sentiment guard in `buyer.py` — skips entry if score < -0.3
- Chronos nightly forecast (`job_portfolio_chronos_forecast`, 17:30 ET) — `src/portfolio/forecaster.py`
- `PortfolioForecast` DB table
- Chronos entry guard in `buyer.py`
- Chronos exit suppression in `scheduler.py` via `_get_chronos_trend()` helper
- `chronos-forecasting` installed in both `~/timesfm_env` and `.venv`

---

## Architecture Quick Reference

```
Server: rain@37.0.30.34
Project: ~/automatic_option_trader
Restart: ~/restart-all.sh
Dashboard: http://37.0.30.34:8080
Repo: github.com/rainrosimannus-spec/automatic_option_trader
```

Key file locations:
- Entry guards: `src/portfolio/buyer.py` lines ~361-405
- Chronos forecast job: `src/portfolio/forecaster.py`
- Chronos exit suppression: `src/portfolio/scheduler.py` — `_get_chronos_trend()` + `(not _cu) and create_suggestion(...)`
- Trailing stop monitor: `src/portfolio/scheduler.py` — `job_portfolio_trailing_stop_monitor()`
- Sentiment: `src/portfolio/sentiment.py`
- Loans fix: `src/portfolio/connection.py` lines ~264-270
- Accrued interest cache: `src/portfolio/connection.py` — reads from `_CACHE_FILE`

---

*Last updated: April 5, 2026 — end of session*
*Update this file at the end of every session before committing*
