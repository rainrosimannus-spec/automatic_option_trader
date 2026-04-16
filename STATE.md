# Maggy & Winston — STATE
This file is updated at the end of every session.
It describes the system exactly as it stands RIGHT NOW.
Read this to know what to do next, what's broken, and what to test first.

---

## System Status (April 15, 2026)

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
| Watchlist metrics | FIXED — event loop conflict resolved, non-SMART exchanges updating |
| CC profit-taking | LIVE — OTM profit-take + ITM roll-up in job_check_profit() |
| Portfolio pending orders | WORKING — shows manually placed TWS orders |
| Portfolio price update | FIXED — non-SMART exchanges keep original exchange code |
| Portfolio sync transactions | FIXED — new holdings detected via sync now record PortfolioTransaction |
| Bridge event loop | FIXED — bridge.py imports _ensure_event_loop from connection.py |
| Trade sync position closure | FIXED — BUY_CALL/BUY_PUT fills close matching open position with P&L |
| Compound quality score | NEW — 80/20 price/quality blend in buy strategy |

---

## Current Positions

**Options account (Maggy):**
- TTD: assigned at $26.50, stock at ~$20.53, cost basis $25.52. Wheel scanning for covered calls.
- SHOP, UBER, PANW: assigned March 27, wheel scanning for covered calls
- PANW: covered call rolled from $155 to $162.50 Apr 24 expiry. Realized loss -$474 recorded.
- PG: covered call April expiry

**Portfolio account (Winston):**
- 43 holdings
- Margin at ~80% — no new buys until margin clears
- Market at new highs — no buy signals above 70 threshold currently
- Non-US watchlist prices populating correctly after event loop fix

---

## Top Priority Next Session

1. Chronos live test — run nightly forecast job manually to verify it writes to portfolio_forecasts
2. Trailing stop verification — check suggestions have trailing_stop_pct set
3. Monitor CC profit-taker logs — confirm cc_profit_check_started fires correctly
4. Verify trade sync position closure works on next manual close in TWS

---

## What Changed This Session (April 15, 2026)

**Compound quality score for buy strategy:**
- New field: compound_quality_pct in portfolio_watchlist (1-100, normalized within tier)
- _compute_compound_quality(): tier-specific weights:
  - growth: growth 50% + quality 50%
  - dividend: growth 30% + quality 70%
  - breakthrough: growth 70% + quality 30%
- Normalization: best stock = 100%, worst = 1%, proportional to actual score distance
- 80/20 blend: composite_score = (price_signal * 0.80) + (compound_quality_pct * 0.20)
- Called automatically in recalc_scores_from_db() before every buy scan
- Monthly screener unchanged — only buying strategy affected

**Design rationale:**
- Price signal remains dominant (80%) — timing is primary
- Quality is the differentiator (20%) — when price signals are similar, better companies win
- Max quality boost = 20 points (PLTR/NVDA can't dominate purely on quality)
- Prevents buying mediocre stocks just because they're cheap

**Current buy signal scores (post-recalc):**
- Growth: ULVR 56.4, IMB 54.7, NOW 51.0, TTD 49.7, NICE 49.3
- Dividend: INFY 49.1, RKT 45.5, CEG 21.1
- Breakthrough: ZS 48.7, SNOW 47.5, ENPH 41.8
- No stock above 70 (direct buy threshold) — market at highs, correct behavior

---

## Score Architecture (IMPORTANT)

Two completely separate scoring systems:

1. Screener score (FMP fundamentals) — selects top 100 stocks. Lives in screened_universe.yaml ONLY. Never written to DB.
2. Buy signal score (IBKR price/SMA/RSI) — triggers actual buys. Now blended 80/20 with compound quality:
   - raw_score = pre-penalty IBKR price signal score
   - compound_quality_pct = normalized within-tier fundamental quality (1-100)
   - composite_score = (raw_score * 0.80) + (compound_quality_pct * 0.20) - risk_penalty
   - Written by recalc_scores_from_db() and _update_watchlist_metrics()

---

## Architecture Quick Reference

    Server: rain@37.0.30.34
    Project: ~/automatic_option_trader
    Restart: ~/restart-all.sh
    Dashboard: http://37.0.30.34:8080
    Repo: github.com/rainrosimannus-spec/automatic_option_trader

Key file locations:
- Compound quality: src/portfolio/buyer.py — _compute_compound_quality()
- Buy score blend: src/portfolio/buyer.py — recalc_scores_from_db()
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
