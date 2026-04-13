# Maggy & Winston — STATE
This file is updated at the end of every session.
It describes the system exactly as it stands RIGHT NOW.
Read this to know what to do next, what's broken, and what to test first.

---

## System Status (April 13, 2026)

Both connections stable. App running. Dashboard accessible at http://37.0.30.34:8080

| Component | Status |
|-----------|--------|
| Options gateway (port 4001) | Running |
| Portfolio gateway (port 7496) | Running |
| Trader app (port 8080) | Running |
| Trailing stop monitor | Active, every 15 min |
| FMP cache | Active, 30-day cache in data/fmp_cache.json |
| Screener | WORKING — last run 2026-04-09 06:21 UTC |
| Accrued interest Flex refresh | FIXED — runs daily 08:00 ET |
| Risk assessment | Sonnet, monthly, conservative prompt, 1/2/3 penalties |
| Watchlist metrics | WORKING |
| CC profit-taking | LIVE — OTM profit-take + ITM roll-up in job_check_profit() |
| Portfolio pending orders | FIXED — now shows manually placed TWS orders |
| Portfolio price update | FIXED — non-SMART exchanges keep original exchange code |
| Portfolio sync transactions | FIXED — new holdings detected via sync now record PortfolioTransaction |

---

## Current Positions

**Options account (Maggy):**
- TTD: assigned at $26.50, stock at ~$20.53, cost basis $25.52. Wheel scanning for covered calls.
- SHOP, UBER, PANW: assigned March 27, wheel scanning for covered calls
- PG: covered call April expiry

**Portfolio account (Winston):**
- 43 holdings (IONQ recently assigned, not in PortfolioTransaction — sync fix will catch next ones)
- Margin at ~80% — no new buys until margin clears
- 20 non-US watchlist stocks (SEHK/JSE/SGX/TASE/NSE) should now get prices on next hourly update when their markets are open

---

## Top Priority Next Session

1. Verify non-US watchlist prices populate on next market open (JSE/TASE open during EU hours)
2. Verify portfolio pending orders show correctly after next health check cycle
3. Chronos live test — run nightly forecast job manually to verify it writes to portfolio_forecasts
4. Trailing stop verification — check suggestions have trailing_stop_pct set
5. Monitor CC profit-taker logs — confirm cc_profit_check_started fires correctly

---

## What Changed Last Session (April 13, 2026)

**Covered call profit-taking (April 11 work, confirmed live):**
- buy_to_close_call() in orders.py
- ProfitTaker.check_covered_calls() — OTM profit-take at 50/65/75% + ITM roll-up at strike*1.07
- job_check_profit() now calls both check_positions() and check_covered_calls()

**Portfolio pending orders fix:**
- refresh_portfolio_pending_orders_cache() in connection.py using reqAllOpenOrders()
- Catches manually placed TWS orders across all clients (read-only connection)
- Wired into job_health_check() in scheduler
- portfolio.py route passes portfolio_pending_orders to template
- portfolio.html pending section now uses real data instead of hardcoded empty list

**Portfolio price update fix (non-SMART exchanges):**
- buyer.py update_holdings_prices(): stocks on SEHK/JSE/SGX/TASE/NSE/ASX etc. now keep
  their original exchange instead of being forced to SMART
- SMART override only applies to US/EU exchanges
- Blacklist: SEHK, JSE, SGX, TASE, NSE, ASX, BSE, KSE, TWSE, BKK, IDX

**Portfolio sync transaction recording:**
- sync.py: when a new holding is detected via IBKR sync, records a PortfolioTransaction
  with action=put_assigned, 3-day dedup window to avoid duplicates on repeated syncs
- Fixes gap where IONQ assignment appeared in holdings but not in transaction history

**Dashboard cleanup:**
- Removed Winston Recent Transactions section from options dashboard (dashboard.html)
- Winston transactions correctly shown on portfolio page only

**Known gap:**
- IONQ assignment (April 11) has no PortfolioTransaction record — predates sync fix

---

## Score Architecture (IMPORTANT)

Two completely separate scoring systems:

1. Screener score (FMP fundamentals) — selects top 100 stocks. Lives in screened_universe.yaml ONLY. Never written to DB.
2. Buy signal score (IBKR price/SMA/RSI) — triggers actual buys. Stored as:
   - raw_score = pre-penalty IBKR score
   - composite_score = raw_score minus penalties
   - Both fields in portfolio_watchlist DB

---

## Architecture Quick Reference

    Server: rain@37.0.30.34
    Project: ~/automatic_option_trader
    Restart: ~/restart-all.sh
    Dashboard: http://37.0.30.34:8080
    Repo: github.com/rainrosimannus-spec/automatic_option_trader

Key file locations:
- CC profit-taker: src/strategy/profit_taker.py — check_covered_calls(), _close_covered_call(), _roll_call_up()
- Put profit-taker: src/strategy/profit_taker.py — check_positions()
- Buy to close call: src/broker/orders.py — buy_to_close_call()
- Wheel / covered call writing: src/strategy/wheel.py — write_covered_calls(), _write_call()
- Call screener: src/strategy/screener.py — screen_calls()
- Portfolio price update: src/portfolio/buyer.py — update_holdings_prices()
- Portfolio pending orders cache: src/portfolio/connection.py — refresh_portfolio_pending_orders_cache()
- Portfolio sync transactions: src/portfolio/sync.py — sync_ibkr_holdings()
- Watchlist metrics: src/portfolio/buyer.py — update_watchlist_metrics()
- Analyzer: src/portfolio/analyzer.py — analyze_stock()
- Risk assessment: src/portfolio/scheduler.py — _assess_structural_risks()
- Accrued interest: src/portfolio/scheduler.py — refresh_accrued_interest_from_flex()
