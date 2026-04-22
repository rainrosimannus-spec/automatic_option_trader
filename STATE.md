# Maggy & Winston — State Document

Last updated: 2026-04-22 (Wed) end-of-day, after metrics-order fix.

---

## Top Of Mind — Current State

Today's full arc took a sharp turn in the evening. Morning and early afternoon established Option B (dividend-specific ranking column) and Commit B (Phase 2b score refresh for held holdings). Both went live. Dashboard looked growth-heavy but designed — Ryan accepted with a 2-3 day checkpoint.

Then Ryan noticed the real bug: about 58 of 126 watchlist rows had composite_score=0 sitting between stocks with real scores. Not a visual artifact, not a blend-ratio issue. Actual zeros.

Root cause: scheduler.py job_portfolio_update_metrics ran two calls in the wrong order. It ran recalc_scores_from_db FIRST, then update_watchlist_metrics AFTER. When the monthly screener INSERTs new stocks into portfolio_watchlist, they get discount_pct=NULL and rsi_14=NULL. The next metrics job ran recalc first; recalc's top-of-loop guard at line 1359 skipped every new stock (continue on NULL discount/rsi). Then update_watchlist_metrics populated discount/rsi successfully — but no second recalc ran, so composite stayed at 0 from INSERT default.

Why holdings looked OK and non-holdings did not: Holdings were in the DB from previous cycles with discount/rsi populated. Phase 2 of the screener updates fundamental scores but does NOT touch discount/rsi. So holdings survived with populated metrics. Newly-selected top-100 stocks (ABT, RTX, GE, CRM, SOFI, etc. — not held) were fresh INSERTs with NULL discount/rsi, skipped by recalc, composite=0.

Fix (commit 2a95c7b): Swap the call order. Fetch metrics first, then recalc. Not yet live in the running process — needs restart.

---

## Today's Commits

All pushed to origin except 2a95c7b.

- 5738250 Fix 0: composite_score clobber in _update_watchlist_metrics
- 8ae93c3 Fix P: tier proportions unified
- d9821da Fix A narrow: pool-aware dividend routing
- 785d83f Piece 2 (partially superseded by Option B)
- e1d9568 Dashboard green-box persistence
- 1a8b883 Fix B: IBKR fundamentals fallback + pence normalization
- 267d36e STATE.md morning version
- 077aab5 Option B: dividend_total_return_score column + tier-aware compound quality
- c6d5e06 Commit B: Phase 2b refresh scores for held holdings not in top-100
- bf39145 STATE.md end-of-day (pre metrics-order fix)
- 2a95c7b LOCAL ONLY: Metrics job order fix

GitHub push is currently TCP-blocked (transient). 2a95c7b pushes when network heals.

---

## How Scoring Works (verified end-of-day)

Columns on portfolio_watchlist:

- growth_score, valuation_score, quality_score — FMP/IBKR fundamentals
- dividend_total_return_score — dividend-specific metric. NULL for non-dividend tier.
- raw_score — analyzer's technical signal (SMA discount, RSI). Written by _update_watchlist_metrics and recalc_scores_from_db.
- compound_quality_pct — within-tier 1-100 percentile from _compute_compound_quality
- composite_score — dashboard value. Written ONLY by recalc_scores_from_db. Formula: (raw - penalty) * 0.80 + compound_quality_pct * 0.20
- discount_pct, rsi_14, sma_200, current_price — written by _update_watchlist_metrics

Compound quality formulas (Option B, per tier, 1-100 within-tier normalization):

- Growth / breakthrough: raw = 0.40 * growth + 0.25 * valuation + 0.35 * quality
- Dividend: raw = dividend_total_return_score

Composite-write chain (after metrics-order fix):

1. job_portfolio_update_metrics runs every 4 hours.
2. update_watchlist_metrics loops every watchlist stock, calls IBKR, writes current_price, sma_200, discount_pct, rsi_14, buy_signal, signal_type, raw_score.
3. recalc_scores_from_db loops again with populated discount/rsi, calls _compute_compound_quality, writes composite_score per stock.
4. Logs portfolio_metrics_updated (stdlib) and portfolio_scores_recalced (structlog).

---

## Logging Architecture (learned this evening)

stdlib logger (apscheduler, third-party) writes to logs/trader.log. Format: 2026-04-22 18:43:32,249 [INFO] ...

structlog (application events like portfolio_score_saved, portfolio_metrics_updated) uses PrintLoggerFactory in src/core/logger.py:32 — writes to STDOUT only, goes to /dev/pts/3 (tmux pane). NOT in trader.log.

Implication: For debugging application events, use tmux capture-pane -t trader or attach to tmux. grep on trader.log misses all structlog events. Tmux buffer is ~800 lines, roughly the last 30-45 minutes of activity.

Worth adding: structlog file writer as secondary sink for durability.

---

## Watchlist Composition (as of 20:28)

- 126 total rows (100 top-N + 26 held holdings not in top-N)
- Dividend: 24 rows. 15 in top-15, 9 held without top-15 slot.
- Growth: 78 rows.
- Breakthrough: 24 rows.

9 held dividend stocks still NULL on dividend_total_return_score: HDB, IBN, BMY, CEG, 0ZQ, ALV, NLY, PBR, SFL. Phase 2b will populate on next screener run.

Concerning: BZG2 and MC show composite 32 with Q=0, G=0 — stale data or uncovered non-US. Watch whether they clear on next cycle.

---

## Critical Architecture Facts

1. FMP is dead for LSE/AEB/HKEX/BM. No suffix variants work. /api/v3/ retired Aug 2025. Use IBKR ReportsFinSummary via src/portfolio/ibkr_fundamentals.py.
2. LSE prices come in pence, not pounds. Fix B divides by 100 in _score_stock.
3. Maggy and Winston are separate IBKR accounts. Ports 4001 vs 7496. Separate tables (positions vs portfolio_holdings/portfolio_put_entries). Don't conflate.
4. Options universe is a subset of portfolio universe. screen_all picks top-100 then top-50. Stocks dropping out of options universe mid-cycle do NOT abandon covered calls — Maggy reads positions table directly for management.
5. Running process must be restarted for code changes to take effect. ~/restart-all.sh, 2x phone 2FA, wait 15-20s.
6. Logging splits. apscheduler to trader.log. structlog to stdout/tmux only. 800-line buffer.
7. 3-consecutive-failure exchange skip in update_watchlist_metrics is STILL in the code. Bug diagnosed April 10 and April 14, never structurally fixed. Not today's root cause but will resurface.
8. Never use heredoc for Python patches with complex strings. Use separate Python scripts or str_replace patterns.

---

## Working Rules

1. Copy-paste terminal commands only.
2. One change at a time. Backup, verify, patch, syntax check, diff, test, commit, push.
3. View files before editing. Memory is stale.
4. Fixes structural, not manual.
5. Don't touch Maggy-side code.
6. Don't conflate PortfolioPutEntry (Winston's CSPs) with Position (Maggy's wheel).
7. When Ryan pushes back, re-examine, don't defend.
8. Never claim a fix works without proof.
9. Don't panic over dashboard appearance. But composite=0 for valid stocks is NEVER correct.
10. Every action needs a turn. Stop when Ryan stops.

---

## Open Questions / TODO Next Session

1. Restart the app to activate metrics-order fix. After restart, wait 2 minutes, verify ABT/RTX/GE/CRM/SOFI have composite > 0. ABT should blend to ~74.9.
2. Push commit 2a95c7b when GitHub TCP unblocks. Currently local-only.
3. Optional: trigger a screener run to populate the 9 NULL dividend_total_return_score rows via Phase 2b.
4. Still open:
   - BZG2/MC composite 32 with Q=0, G=0 — investigate data source
   - SHEL IBKR Error 430 — some LSE stocks uncovered; consider ADR fallback
   - 3-consecutive-failure exchange skip in update_watchlist_metrics — remove or raise to 20
   - Scorer fail-closed: return None on dual-source failure instead of 50 default
5. Consider: add structlog file writer for debug durability.
6. Today's token rotation (chat-side disclosure, already handled).

---

## Operational Facts

- Server: rain@octoserver-genoax2:~/automatic_option_trader
- Python: .venv/bin/python3
- GitHub: https://github.com/rainrosimannus-spec/automatic_option_trader (token rotated today)
- Restart: ~/restart-all.sh (2x phone 2FA)
- Dashboard: http://37.0.30.34:8080
- IBKR ports: Maggy 4001, Winston 7496
- FMP quota: 250/day
- check_interval_hours: 4 (config/settings.yaml)
- First run after restart: startup + 120 seconds

---

## Don'ts

- Don't touch Maggy-side code
- Don't run monthly screener without warning (20-40 min, FMP quota, lock held)
- Don't rewrite screener logic in backfill scripts
- Don't try to work while Ryan sleeps
- Don't assume pushed == live
- Don't conflate dashboard looks wrong with is broken
- Don't assume a fix worked — restart and verify
