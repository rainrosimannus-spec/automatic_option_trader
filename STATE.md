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
| CC profit-taking | LIVE — OTM profit-take + ITM roll-up in job_check_profit() |
| CC roll-up profitability guard | IMPROVED — strike improvement - net debit - fees > $50 |
| CC roll-up DTE guard | IMPROVED — allows roll at DTE<=2 if deeply ITM (>5% above strike) |
| Compound quality score | LIVE — 80/20 price/quality blend in buy strategy |
| Trade sync position closure | FIXED — BUY_CALL/BUY_PUT fills close matching open position with P&L |
| Portfolio price update | FIXED — non-SMART exchanges keep original exchange code |
| Bridge event loop | FIXED |

---

## Current Positions

**Options account (Maggy):**
- TTD: assigned at $26.50, cost basis $25.52. Wheel scanning for covered calls.
- SHOP: covered call at $116 expiry Apr 17. Manual order cancelled — watching for auto roll-up trigger.
- PANW: covered call rolled to $162.50 Apr 24. Realized loss -$474.
- UBER: wheel scanning for covered calls
- PG: covered call April expiry

**Portfolio account (Winston):**
- 43 holdings, margin ~80%, no new buys until margin clears

---

## Top Priority Next Session

1. Verify CC roll-up trigger fired for SHOP (watching ~3h15m window after manual order cancelled)
2. Chronos live test
3. Trailing stop verification

---

## What Changed This Session (April 16, 2026)

**CC roll-up profitability guard:**
- Single formula: roll_benefit = (new_strike - current_strike) - (buyback - new_premium) - fees
- Auto-execute only if > $50/contract, reject below
- Fees estimated $2.60/contract (both legs)

**CC roll-up DTE guard:**
- Now allows roll at DTE<=2 if stock deeply ITM (>5% above strike)
- Fixes SHOP/PANW case where roll was blocked on expiry day

**CC regime awareness:** decided NOT to implement — roll-up trigger is the right mechanism

**Compound quality (April 15):** 80/20 price/quality blend, compound_quality_pct in DB

---

## CC Roll-Up Logic

Trigger: stock > strike * 1.07 AND (DTE > 2 OR stock > strike * 1.05)
New strike: max(net_cost_basis * 1.01, current_strike * 1.05)
Profitability: (new_strike - current_strike) - (buyback - new_premium) - fees > $50
Runs every 5 min inside job_check_profit()

---

## Architecture Quick Reference

    Server: rain@37.0.30.34
    Project: ~/automatic_option_trader
    Restart: ~/restart-all.sh
    Dashboard: http://37.0.30.34:8080

Key files:
- CC profit-taker + roll-up: src/strategy/profit_taker.py
- Compound quality + buy blend: src/portfolio/buyer.py
- Trade sync closure: src/scheduler/trade_sync.py
- Wheel / CC writing: src/strategy/wheel.py
- Analyzer: src/portfolio/analyzer.py
