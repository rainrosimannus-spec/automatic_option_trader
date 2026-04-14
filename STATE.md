# Maggy & Winston — STATE
This file is updated at the end of every session.
It describes the system exactly as it stands RIGHT NOW.
Read this to know what to do next, what's broken, and what to test first.

---

## System Status (April 14, 2026)

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
| Watchlist metrics | FIXED — event loop conflict resolved, non-SMART exchanges now update |
| CC profit-taking | LIVE — OTM profit-take + ITM roll-up in job_check_profit() |
| Portfolio pending orders | WORKING — shows manually placed TWS orders |
| Portfolio price update | FIXED — non-SMART exchanges keep original exchange code |
| Portfolio sync transactions | FIXED — new holdings detected via sync now record PortfolioTransaction |
| Bridge event loop | FIXED — bridge.py now imports _ensure_event_loop from connection.py |

---

## Current Positions

**Options account (Maggy):**
- TTD: assigned at $26.50, stock at ~$20.53, cost basis $25.52. Wheel scanning for covered calls.
- SHOP, UBER, PANW: assigned March 27, wheel scanning for covered calls
- PG: covered call April expiry

**Portfolio account (Winston):**
- 43 holdings
- Margin at ~80% — no new buys until margin clears
- Non-US watchlist stocks (SEHK/JSE/SGX/TASE/NSE) should now populate prices on next metrics run

---

## Top Priority Next Session

1. Verify non-US watchlist prices populated (JSE/TASE open during EU hours — check after 09:00 CET)
2. Chronos live test — run nightly forecast job manually to verify it writes to portfolio_forecasts
3. Trailing stop verification — check suggestions have trailing_stop_pct set
4. Monitor CC profit-taker logs — confirm cc_profit_check_started fires correctly

---

## What Changed Last Two Sessions (April 11-14, 2026)

**Covered call profit-taking:**
- buy_to_close_call() in orders.py
- ProfitTaker.check_covered_calls() — OTM profit-take at 50/65/75% + ITM roll-up at strike*1.07
- ProfitTaker._close_covered_call() — shared close logic
- ProfitTaker._roll_call_up() — auto roll-up with net_cost_basis, premium floor, net debit guards
- job_check_profit() now calls both check_positions() (puts) and check_covered_calls() (calls)

**Portfolio pending orders:**
- refresh_portfolio_pending_orders_cache() in connection.py using reqAllOpenOrders()
- Catches manually placed TWS orders across all clients
- Wired into job_health_check() in scheduler
- portfolio.py route passes portfolio_pending_orders to template
- portfolio.html pending section now uses real data

**Portfolio price update (non-SMART exchanges):**
- buyer.py update_holdings_prices(): non-SMART exchanges keep original exchange
- analyzer.py: same fix for watchlist metrics
- Blacklist: SEHK, JSE, SGX, TASE, NSE, ASX, BSE, KSE, TWSE, BKK, IDX

**Event loop fix — portfolio metrics job:**
- scheduler.py job_portfolio_update_metrics(): portfolio lock released before update_watchlist_metrics()
- update_watchlist_metrics() makes blocking IBKR calls — must NOT hold portfolio lock
- This was causing event loop conflict for all non-SMART exchange stocks

**Bridge event loop fix:**
- bridge.py had local _ensure_event_loop() — now imports from connection.py
- Critical: bridge transfers real money between two IBKR accounts

**Portfolio sync transaction recording:**
- sync.py: records PortfolioTransaction when new holding detected via IBKR sync
- 3-day dedup window to avoid duplicates

**Dashboard cleanup:**
- Removed Winston Recent Transactions from options dashboard
- Winston transactions shown on portfolio page only

**Known gaps:**
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
- Portfolio metrics job: src/portfolio/scheduler.py — job_portfolio_update_metrics()
- Portfolio pending orders cache: src/portfolio/connection.py — refresh_portfolio_pending_orders_cache()
- Portfolio sync transactions: src/portfolio/sync.py — sync_ibkr_holdings()
- Bridge (inter-account transfers): src/portfolio/bridge.py
- Watchlist metrics: src/portfolio/buyer.py — update_watchlist_metrics()
- Analyzer: src/portfolio/analyzer.py — analyze_stock()
- Risk assessment: src/portfolio/scheduler.py — _assess_structural_risks()
- Accrued interest: src/portfolio/scheduler.py — refresh_accrued_interest_from_flex()
