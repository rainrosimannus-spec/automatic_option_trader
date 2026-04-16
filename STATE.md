# Maggy & Winston — STATE
This file is updated at the end of every session.
It describes the system exactly as it stands RIGHT NOW.
Read this to know what to do next, what's broken, and what to test first.

---

## System Status (April 16, 2026)

Both connections stable. App running. Dashboard accessible at http://37.0.30.34:8080

| Component | Status |
|-----------|--------|
| Options gateway (port 4001) | Running |
| Portfolio gateway (port 7496) | Running |
| Trader app (port 8080) | Running |
| Trailing stop monitor | Active, every 15 min |
| CC profit-taking | LIVE — OTM profit-take + ITM roll-up working |
| CC roll-up | TESTED — SHOP rolled from $116 to $135 May 15 successfully today |
| Compound quality score | LIVE — 80/20 price/quality blend |
| Trade sync position closure | FIXED — BUY_CALL/BUY_PUT fills close matching position |
| Portfolio price update | FIXED — non-SMART exchanges correct |
| Bridge event loop | FIXED |

---

## Current Positions

**Options account (Maggy):**
- TTD: assigned at $26.50, cost basis $25.52. Wheel scanning for covered calls.
- SHOP: rolled from $116 Apr17 to $135 May15. Realized loss on $116 call: -$798.
- UBER: wheel scanning for covered calls
- PANW: covered call $162.50 Apr 24
- PG: covered call $146 Apr 24

**Portfolio account (Winston):**
- 43 holdings, margin ~75%, no new buys until margin clears

---

## Top Priority Next Session

1. Investigate why trade sync didn't auto-close SHOP $116 position after BUY_CALL fill — had to close manually. Check if `ib.fills()` loses fills after restart.
2. Chronos live test
3. Trailing stop verification

---

## What Changed This Session (April 16, 2026)

**CC roll-up fully working — SHOP proved it:**
- SHOP $116 call (stock at $128) rolled automatically to $135 May15 at $7.15 premium
- Roll benefit: ($135-$116) - ($12.90-$7.15) - fees = $13.22/share = $1,322/contract

**get_stock_live_price() added to market_data.py:**
- Uses reqMktData for intraday price (get_stock_price returns yesterday's close — wrong for ITM calls)
- Used in CC profit checker fallback when get_option_live_price returns None

**screen_calls() upgraded:**
- stock_price_override parameter — passes live price to Black-Scholes instead of stale close
- max_dte_override parameter — roll-up uses 14-day cap (was going to 29 days)
- If no candidate within 14 days → skip roll, let original call expire, wheel writes new one

**SHOP currency fix:**
- options_universe.yaml: SHOP currency CAD → USD (was causing Error 200 on option lookup)

**CC roll-up DTE guard:**
- DTE <= 2 AND deeply ITM (>5% above strike) → still evaluates roll
- Fixes case where stock runs late in option's life

**CC roll-up profitability guard:**
- roll_benefit = (new_strike - current_strike) - (buyback - new_premium) - fees
- Must be > $50/contract to auto-execute, else skip entirely

**Known issues:**
- Trade sync doesn't close covered_call positions after restart (fills lost from session)
- UBER, PANW, TTD, PG not in options_universe.yaml — get_option_live_price uses defaults
- These stocks rely on get_stock_live_price fallback for roll-up trigger

---

## CC Roll-Up Logic (Complete)

Trigger: stock > strike * 1.07 AND (DTE > 2 OR stock > strike * 1.05)
Stock price: get_option_live_price first, fallback to get_stock_live_price (reqMktData)
New strike: max(net_cost_basis * 1.01, current_strike * 1.05)
New expiry: screen_calls() with max_dte=14, live stock price
Profitability: (new_strike - current_strike) - (buyback - new_premium) - fees > $50/contract
No candidate within 14 days → skip, let expire, wheel writes fresh call
Runs every 5 min inside job_check_profit()

---

## Score Architecture

1. Screener score — selects watchlist. screened_universe.yaml only. Never in DB.
2. Buy signal score — 80/20 blend:
   - raw_score = IBKR price signal
   - compound_quality_pct = normalized within-tier fundamental quality
   - composite_score = (raw_score * 0.80) + (compound_quality_pct * 0.20) - penalty

---

## Architecture Quick Reference

    Server: rain@37.0.30.34
    Project: ~/automatic_option_trader
    Restart: ~/restart-all.sh
    Dashboard: http://37.0.30.34:8080

Key files:
- CC profit-taker + roll-up: src/strategy/profit_taker.py
- Live stock price: src/broker/market_data.py — get_stock_live_price()
- Call screener: src/strategy/screener.py — screen_calls()
- Compound quality + buy blend: src/portfolio/buyer.py
- Trade sync closure: src/scheduler/trade_sync.py
- Wheel / CC writing: src/strategy/wheel.py
