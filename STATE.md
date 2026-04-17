# Maggy & Winston — STATE

This file is updated at the end of every session.
It describes the system exactly as it stands RIGHT NOW.
Read this to know what to do next, what's broken, and what to test first.

---

## System Status (April 17, 2026)

Both connections stable. App running. Dashboard at <http://37.0.30.34:8080>.

**Auto-execution is OFF** after today's incident. Do not re-enable without market-open sanity watch.

| Component | Status |
| --- | --- |
| Options gateway (port 4001) | Running |
| Portfolio gateway (port 7496) | Running |
| Trader app (port 8080) | Running |
| Auto-execute | **DISABLED (suggestion mode)** |
| Trailing stop monitor | Active, every 15 min |
| CC profit-taking | LIVE (see new safeguards) |
| CC roll-up | LIVE (see new safeguards) |
| BS-derived order pricing | **ELIMINATED everywhere** |

---

## Current Positions

**Options account (Maggy):**
- TTD: assigned at $26.50, cost basis $25.52. Short CC $26 May 8 @ $0.86 (synced by IBKR).
- SHOP: 100 shares + short CC $135 May 15 @ $9.20 (manually sold today after incident).
- UBER: wheel scanning for covered calls.
- PANW: short CC $162.50 Apr 24 @ $5.47.
- PG: 100 shares + short CC $146 Apr 24 @ $2.74. Stock ~$147 at session end; real buyback ~$3.02.

**Portfolio account (Winston):** 43 holdings, margin ~75%, no new buys until margin clears.

---

## TODAY'S INCIDENT — April 17, 2026

**Damage:** $202 loss on SHOP from duplicate buy-to-close fills at bad prices.

**Root causes (in order of contribution):**

1. **`current_price = pos.strike + live_ask`** — the roll-up derived "stock price" from `strike + call_ask` instead of calling for real stock price. When call ask snapped up momentarily, system believed stock was at $144.50 when reality was ~$133 all day.
2. **Bid-as-ask fallback in `check_covered_calls`** — when live ask unavailable, substituted live bid. For a deep-ITM call: bid was $1.19, real ask was $3.02. System thought PG was at 57% profit when it was at a loss. Fired 15 buy orders in one day.
3. **BS-derived prices reaching real orders** — multiple paths (screener, put profit-taker, portfolio put-entry, hedge) fell back to Black-Scholes theoretical prices when live IBKR quotes failed, then used those prices as real order limits.
4. **No naked-buy guard** — buy-to-close fired a second time on SHOP even after the first had already closed the short position.
5. **No roll-up re-entry guard** — previous roll's SELL leg sitting unfilled did not prevent a new roll from starting.

---

## Eight commits shipped today

| Phase | Commit | Change |
| --- | --- | --- |
| 2 | `706bd43` | `check_covered_calls`: removed bid-as-ask fallback AND intrinsic-value fallback. No live ask → skip cycle, log `cc_check_no_live_price`. |
| 3a | `8bbb3b9` | Put profit-taker: removed BS fallback for buy-to-close price. No live ask → return False. Dropped unused greeks import. |
| 3f | `685851b` | CC roll-up trigger: `current_price` now from `get_stock_live_price()` instead of `strike + call_ask`. No live price → skip. |
| 3b+3c | `f15d82c` | Both screeners (`screen_puts`, `screen_calls`): removed BS fallback for order prices. Now iterate **top 2 candidates by score**, return first with valid live quote AND passing fee floor. If both fail → return None. |
| 3d | `43d1084` | Hedge: removed BS fallback for SPY put ask. No live → return None. Dropped unused greeks import. |
| 3e | `54f04c0` | Portfolio put-entry (`buyer.py`): added `reqMktData` snapshot + `_ensure_market_data_type()`. Replaced `greeks.bid` with live bid. No live → return False. Deleted dead `_bs_put_price` helper + unused `math` import. |
| 5 | `307e970` | `buy_to_close_call` / `buy_to_close_put`: pre-flight check via new `_verify_short_position()` helper. Calls `ib.positions()`, verifies matching (symbol, expiry, strike, right) with `position.position < 0 AND abs(position) >= quantity`. If no match → log `buy_to_close_{call,put}_BLOCKED_no_matching_short` and return None. |
| 6 | `0eef4a6` | `_roll_call_up`: at function top, scans `ib.openTrades()` for any pending SELL_CALL on same symbol (PreSubmitted/Submitted/PendingSubmit). If found → log `cc_rollup_skipped_prior_sell_pending` and return False. |

**Core principle enforced:** Black-Scholes is allowed for selection/ranking/delta-filtering, never for a price that becomes an order limit or a profit-take comparison.

---

## DB CLEANUP PERFORMED

During today's session:
- All SUBMITTED Trade rows manually marked CANCELLED (PG phantom + SHOP phantom $149 rolls + pre-incident churn).
- SHOP $135 covered_call Position row marked CLOSED.
- SHOP position now reconciled via IBKR trade_sync (real positions: 100 shares + new short $135 May 15 @ $9.20).

---

## CONCURRENCY REVIEW (post-patch)

Verified: today's commits do NOT introduce new lock/event-loop issues.
- All new IBKR calls run inside existing `_scan_lock` (scheduler-level) or `get_ib_lock` (which is an `RLock` — reentrant, deadlock-safe for nested acquires).
- Phase 3b+3c's top-2 iteration can block up to 4s (vs previous 2s) on one scan cycle. Well within scheduler timeouts (60-300s).
- Portfolio side uses `get_ib_lock` only, no `_scan_lock` — consistent with pre-existing architecture.

---

## TOP PRIORITY — START OF NEXT SESSION

1. **Sanity watch after re-enabling auto-exec.** Monday open. Watch first 2-3 cycles per ticker. Confirm:
   - `cc_profit_check_done acted=[]` fires cleanly
   - No `buy_to_close_*_BLOCKED_no_matching_short` on legitimate positions
   - `cc_check_no_live_price` fires rarely (if it's firing every cycle, data feed is wrong)
   - `call_candidates_exhausted_no_live` fires rarely
2. **Investigate trade_sync position reconciliation.** Today's incident showed trade_sync can leave DB trades SUBMITTED when IBKR has moved on. Needs a periodic "reconcile SUBMITTED trades against actual IBKR order state" job.
3. **Clean up STATE.md.** It's been accumulating. Tighter summary, less sprawl.

---

## STRATEGIC ASSESSMENT — TEST RUN Feb 20 to Apr 17, 2026

**NLV return chart:** steady +5-10% through Feb 20 to Mar 18 (low VIX, sideways-up market). Crashed to -16% around Mar 24-29 (Iran war shock). Recovered to +10% by Apr 17. Above 24% annualized target line at session end.

**What worked:** strategy performs reliably in calm markets. Steady premium income, low drawdown, consistent above-target pacing during Feb 20-Mar 18.

**What didn't work:** entering turbulence. Drawdown wasn't the HALT (VIX>30) kicking in — it was damage accumulating as VIX rose from 15 to 25 while the system was still trading aggressively. By the time HALT triggered, losses were locked in. Recovery was market-shape-dependent, not system-driven.

### Candidate improvements for turbulence handling (backlog, not this week)

Ordered by impact/simplicity:

1. **Smooth VIX throttle, not just HALT.** Current: aggressive <20, moderate 20-30, halt >30. Better: progressive reduction of delta, DTE, and position count as VIX moves from 15 → 25. By VIX=25 system should be nearly flat, not still trading.
2. **VIX rate of change.** `dVIX/dt` matters more than absolute level. Fast spike (14→22 in two days) should trigger defensive mode before any level threshold.
3. **Slower SPY trend filter.** Current TREND_BEARISH uses SPY MA10 < MA20 — noisy. Add a SPY-below-MA50 check for regime detection that's less jumpy.
4. **Sector concentration check.** Don't stack 3+ short puts in the same sector. Geopolitical shocks hit correlated names together.
5. **Earnings week derisking.** Count watchlist names reporting in the next 5 trading days. Above threshold → scan defensively.
6. **Assignment-rate feedback.** Clustering of recent assignments = market moving against you. Auto-reduce aggressiveness.
7. **Realized-vol-scaled position sizing.** After a -5% day, next day's scan cuts position size automatically.

**Avoid:** ML-based regime detection, complex vol-of-vol triggers, option-pricing-model regime inference. Simple rules that degrade gracefully beat clever rules that fail unpredictably.

### Strategic question raised today but not resolved

**CC strategy mismatch with "exit ASAP" goal.** Current CC playbook (delta 0.30-0.45, OTM profit-take, ITM roll-up) is optimized for *premium-maximization on held stock*. But the stated goal for wheel-assigned stocks is *exit at zero damage ASAP* — which calls for higher delta (0.40-0.55), let-expire-rather-than-profit-take, and no roll-up (assignment is the win).

Proposed design: per-position `wheel_exit_mode` flag, gating profit-take + roll-up checks. Default on for wheel-assigned stocks, off for stocks intentionally held. Not implemented yet. Worth building if assignment cycles continue to tie up capital at 6-8% margin interest.

### Capital efficiency note

15k account accruing $100+ interest today while shares sit. Margin interest ~6-8% annualized. Every day a wheel-assigned stock sits uncalled is opportunity cost vs. fresh put-selling. Quantifies why "exit ASAP" is the right frame for CC writing on wheel-assigned shares.

---

## Architecture Quick Reference (unchanged)

```
Server: rain@37.0.30.34
Project: ~/automatic_option_trader
Restart: ~/restart-all.sh
Dashboard: http://37.0.30.34:8080
```

Key files:
- CC profit-taker + roll-up + duplicate guard: `src/strategy/profit_taker.py`
- Live stock price: `src/broker/market_data.py — get_stock_live_price()`
- Call/put screener (top-2 iteration, live-only): `src/strategy/screener.py`
- Compound quality + buy blend: `src/portfolio/buyer.py` (now live-bid-only)
- Trade sync closure: `src/scheduler/trade_sync.py`
- Wheel / CC writing: `src/strategy/wheel.py`
- Naked-buy guard: `src/broker/orders.py — _verify_short_position()`

---

## Known Bugs / Not Fixed Yet

1. **NLV staleness 16:00-20:00 ET** — `accountValues()` push stops after idle. Not investigated.
2. **Structlog not writing to `logs/trader.log`** — only stdlib logging writes to file. Low priority. (Today's session confirmed this is still true — we used `tmux capture-pane` throughout.)
3. **TimesFM GPU device bug** — workaround in place, Chronos preferred.
4. **Trade sync leaves SUBMITTED rows when IBKR orders die.** Seen today — phantom $149 SHOP rows required manual cleanup. Needs a periodic reconciliation job.
5. **CC strategy not aligned with exit-ASAP goal** (see Strategic Assessment above).
