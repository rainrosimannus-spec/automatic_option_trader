# Maggy & Winston — STATE

This file is updated at the end of every session.
It describes the system exactly as it stands RIGHT NOW.
Read this to know what to do next, what is broken, and what to test first.

---

## System Status (April 10, 2026)

Both connections stable. App running. Dashboard accessible at http://37.0.30.34:8080

| Component | Status |
|-----------|--------|
| Options gateway (port 4001) | Running |
| Portfolio gateway (port 7496) | Running |
| Trader app (port 8080) | Running |
| Trailing stop monitor | Active, every 15 min |
| FMP cache | Active, 30-day cache in data/fmp_cache.json |
| Screener | WORKING - 20/65/15/50, last run 2026-04-09 06:21 UTC |
| Accrued interest Flex refresh | FIXED - runs daily 08:00 ET via refresh_accrued_interest_from_flex() |
| Risk assessment | Sonnet, monthly, conservative prompt, 1/2/3 penalties |
| Watchlist metrics | FIXED - all US/EU stocks updating correctly |

---

## Current Positions

**Options account (Maggy):**
- TTD: assigned at 6.50, stock at ~0.53, cost basis 5.52. Wheel scanning for covered calls.
- SHOP, UBER, PANW: assigned March 27, wheel scanning for covered calls
- PG: covered call April expiry, awaiting fill confirmation

**Portfolio account (Winston):**
- 42 holdings, market value ~74K, invested 98,514
- Margin at ~80% - no new buys until margin clears

---

## Top Priority Next Session

1. Verify non-US price data - 20 stocks (SEHK, JSE, NSE, SGX, TASE, ASX) show 0 price when markets closed - verify they populate when markets open
2. Chronos live test - run nightly forecast job manually to verify it writes to portfolio_forecasts table
3. Trailing stop verification - check suggestions have trailing_stop_pct set

---

## What Changed Last Session (April 9-10, 2026)

**Fixed bugs:**
- CRITICAL: primaryExch AttributeError in analyzer.py - crashed analyze_stock for ALL 135 stocks for 12+ hours
- Screener was writing FMP quality score into composite_score - fixed, screener scores stay in screened_universe.yaml only
- _ensure_event_loop in analyzer.py and buyer.py was creating new loops instead of using portfolio connection loop - now imports from connection.py
- IBKR pacing violation (Error 162) - sleep between historical data requests increased to 2s
- reqAccountUpdates signature was wrong - fixed
- Risk penalties rescaled from 5/10/20 to 1/2/3 (tiebreaker not blocker)

**New features:**
- raw_score field in portfolio_watchlist - stores pre-penalty IBKR buy score
- composite_score = raw_score minus penalties (IBKR analyzer only, never screener)
- refresh_accrued_interest_from_flex() - daily Flex refresh regardless of IBKR connection state
- Monthly risk reassessment via Claude Sonnet - rewrites structural_risks.yaml

---

## Score Architecture (IMPORTANT)

Two completely separate scoring systems:
1. Screener score (FMP fundamentals) - selects top 100 stocks. Lives in screened_universe.yaml ONLY. Never written to DB.
2. Buy signal score (IBKR price/SMA/RSI) - triggers actual buys. Stored as:
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
- Watchlist metrics: src/portfolio/buyer.py - update_watchlist_metrics()
- Analyzer: src/portfolio/analyzer.py - analyze_stock()
- Risk assessment: src/portfolio/scheduler.py - _assess_structural_risks()
- Accrued interest: src/portfolio/connection.py - refresh_accrued_interest_from_flex()

---

*Last updated: April 10, 2026 - end of session*
*Update this file at the end of every session before committing*
