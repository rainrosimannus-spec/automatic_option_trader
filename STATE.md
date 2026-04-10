# Maggy & Winston — STATE
This file is updated at the end of every session.
It describes the system exactly as it stands RIGHT NOW.
Read this to know what to do next, what's broken, and what to test first.

---

## System Status (April 11, 2026)

Both connections stable. App running. Dashboard accessible at http://37.0.30.34:8080

| Component | Status |
|-----------|--------|
| Options gateway (port 4001) | Running |
| Portfolio gateway (port 7496) | Running |
| Trader app (port 8080) | Running |
| Trailing stop monitor | Active, every 15 min |
| FMP cache | Active, 30-day cache in data/fmp_cache.json |
| Screener | WORKING — last run 2026-04-09 06:21 UTC |
| Accrued interest Flex refresh | FIXED — runs daily 08:00 ET via refresh_accrued_interest_from_flex() |
| Risk assessment | Sonnet, monthly, conservative prompt, 1/2/3 penalties |
| Watchlist metrics | FIXED — all US/EU stocks updating correctly |
| CC profit-taking | NEW — OTM profit-take + ITM roll-up, runs inside job_check_profit() |

---

## Current Positions

**Options account (Maggy):**
- TTD: assigned at $26.50, stock at ~$20.53, cost basis $25.52. Wheel scanning for covered calls.
- SHOP, UBER, PANW: assigned March 27, wheel scanning for covered calls
- PG: covered call April expiry, awaiting fill confirmation

**Portfolio account (Winston):**
- 42 holdings, market value ~$874K, invested $498,514
- Margin at ~80% — no new buys until margin clears

---

## Top Priority Next Session

1. Monitor CC profit-taker in logs — confirm cc_profit_check_started fires on next job_check_profit() cycle
2. Verify non-US price data — 20 stocks (SEHK, JSE, NSE, SGX, TASE, ASX) show 0 price when markets closed — verify they populate when markets open
3. Chronos live test — run nightly forecast job manually to verify it writes to portfolio_forecasts table
4. Trailing stop verification — check suggestions have trailing_stop_pct set

---

## What Changed Last Session (April 11, 2026)

**New features — covered call profit-taking:**

- buy_to_close_call() added to src/broker/orders.py — exact mirror of buy_to_close_put(), uses C right and BUY_CALL trade type
- ProfitTaker.check_covered_calls() added to src/strategy/profit_taker.py:
  - OTM profit-taking: closes call at 75% profit (DTE>14), 65% (DTE>7), 50% (DTE>3). Skip DTE<=3, let expire worthless.
  - ITM roll-up trigger: fires when stock_price > strike * 1.07 AND DTE > 2
  - Uses live IBKR bid/ask via get_option_live_price()
  - Duplicate close-order guard (cancels existing SUBMITTED orders before placing new one)
- ProfitTaker._close_covered_call() — shared close logic used by both OTM profit-take and ITM roll-up
- ProfitTaker._roll_call_up() — full auto roll-up:
  - New strike = max(net_cost_basis * 1.01, current_strike * 1.05)
  - net_cost_basis = stock.cost_basis - (stock.total_premium_collected / quantity) — exact same formula as _write_call() in wheel.py
  - New call screened via screen_calls() with min_strike, delta 0.30-0.45
  - Premium floor guard: new premium must be >= 50% of original premium, else fall back to manual sell_covered_call_review suggestion
  - Net debit guard: net debit must be <= 50% of original premium, else fall back to manual review
  - At DTE <= 2: skip roll, accept assignment
  - If no candidate found: surface sell_covered_call_review suggestion with details
- job_check_profit() in src/scheduler/jobs.py — now calls both check_positions() (puts) and check_covered_calls() (calls)

**Theoretical design decisions recorded:**
- Effective cost basis for wheel = assignment_strike - put_premium - sum(cc_premiums) + fees
- This is already tracked in Position.total_premium_collected and used correctly

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
- Watchlist metrics: src/portfolio/buyer.py — update_watchlist_metrics()
- Analyzer: src/portfolio/analyzer.py — analyze_stock()
- Risk assessment: src/portfolio/scheduler.py — _assess_structural_risks()
- Accrued interest: src/portfolio/scheduler.py — refresh_accrued_interest_from_flex()
