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
| Trade sync position closure | FIXED — BUY_CALL/BUY_PUT fills now close matching open position with P&L |

---

## Current Positions

**Options account (Maggy):**
- TTD: assigned at $26.50, stock at ~$20.53, cost basis $25.52. Wheel scanning for covered calls.
- SHOP, UBER, PANW: assigned March 27, wheel scanning for covered calls
- PANW: covered call rolled from $155 to $162.50 Apr 24 expiry. $155 manually closed, realized loss -$474 recorded manually in DB.
- PG: covered call April expiry

**Portfolio account (Winston):**
- 43 holdings
- Margin at ~80% — no new buys until margin clears
- Non-US watchlist prices now populating correctly after event loop fix

---

## Top Priority Next Session

1. Chronos live test — run nightly forecast job manually to verify it writes to portfolio_forecasts
2. Trailing stop verification — check suggestions have trailing_stop_pct set
3. Monitor CC profit-taker logs — confirm cc_profit_check_started fires correctly
4. Verify trade sync position closure works on next manual close in TWS

---

## What Changed This Session (April 14, 2026)

**Trade sync position closure fix:**
- trade_sync.py: when BUY_CALL or BUY_PUT fill is detected, finds matching open position and closes it
- Calculates realized P&L: (entry_premium - close_price) * quantity * 100 - commission
- Handles both covered_call and short_call position types for BUY_CALL
- Handles short_put for BUY_PUT
- Previously: manually closed options stayed OPEN in DB until next position reconciliation marked them EXPIRED with no P&L
- No double-close risk: reconciliation only touches OPEN positions; fill loop closes first

**PANW $155 call manual DB fix (one-time):**
- Manually marked CLOSED with realized_pnl = -474.0
- This was necessary because the fix wasn't in place when the trade happened
- Future manual closes will be handled automatically by trade_sync

**Event loop fix — portfolio metrics job:**
- scheduler.py: portfolio lock released before update_watchlist_metrics()
- Fixes 'This event loop is already running' for all non-SMART exchange stocks
- JSE/TASE/SEHK/SGX prices now populating correctly

**Bridge event loop fix:**
- bridge.py: local _ensure_event_loop() replaced with import from connection.py

**Portfolio pending orders:**
- refresh_portfolio_pending_orders_cache() using reqAllOpenOrders()
- Shows manually placed TWS orders on portfolio dashboard

**Portfolio price update (non-SMART exchanges):**
- buyer.py and analyzer.py: non-SMART exchanges keep original exchange code
- Blacklist: SEHK, JSE, SGX, TASE, NSE, ASX, BSE, KSE, TWSE, BKK, IDX

**CC profit-taking (April 11):**
- buy_to_close_call(), check_covered_calls(), _close_covered_call(), _roll_call_up()
- job_check_profit() calls both puts and calls profit-taker

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
- Trade sync position closure: src/scheduler/trade_sync.py — BUY_CALL/BUY_PUT handling
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
