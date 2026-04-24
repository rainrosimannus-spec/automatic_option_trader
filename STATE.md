# Maggy & Winston — State Document

Last updated: 2026-04-23 (Thu) midday — metrics-order fix + breakthrough filter + logger fix + stale-flag + cross-tier dedup.

---

## Top Of Mind — Current State

Two days of focused fixes. Most pushed and live in repo, but ALL require **restart** to activate, and breakthrough/dedup ALSO require a fresh monthly screener run to take effect.

**Wednesday (2026-04-22) — Option B + Commit B + metrics-order fix:**

- Dividend tier ranks on dedicated dividend_total_return_score column (Option B). Tier-aware compound_quality formulas: growth/breakthrough use 0.40*growth + 0.25*valuation + 0.35*quality; dividend uses dividend_total_return_score directly.
- Held holdings not in top-100 get score refresh in Phase 2b (Commit B). 9 dividend holdings still NULL on dividend_total_return_score (HDB, IBN, BMY, CEG, 0ZQ, ALV, NLY, PBR, SFL) until next screener run.
- Late evening: discovered ~58/126 watchlist rows had composite=0 because scheduler ran recalc_scores_from_db BEFORE update_watchlist_metrics. Fix (commit 2a95c7b): swap order. New stocks now get fresh discount/rsi BEFORE compound_quality computation. Holdings were unaffected because they had populated metrics from prior cycles.
- XXII (22nd Century — $15M tobacco penny stock with 1-for-15 reverse split pending) made breakthrough tier at composite 55. Confirmed Claude's breakthrough scan returned it; no post-LLM filter rejected it.

**Thursday (2026-04-23) morning — four targeted fixes:**

1. **Breakthrough quality filters** (commit 3c744c6) — `_check_breakthrough_eligibility` rejects ETFs (FMP isEtf=true), market_cap < $500M (uses FMP profile mktCap as backup), and any reverse split in last 18 months. Adds 2 FMP calls per breakthrough candidate (~60/run). Quota-safe.

2. **Logger routing** (commit 65a9dec) — switched structlog from PrintLoggerFactory to stdlib.LoggerFactory. Application events (portfolio_score_saved, portfolio_metrics_updated, etc.) now land in trader.log instead of stdout-only. Fixes the debugging blind-spot from Wednesday night where tmux buffer rolled and we had to guess at what happened.

3. **Stale-metrics flagging** (commit 06cb459) — added metrics_stale (bool) and last_metrics_success (datetime) columns. Inner _update_watchlist_metrics writes timestamp on success. Outer loop scans at end and flips metrics_stale=True for stocks with last_metrics_success older than 24h. Dashboard shows yellow ⏱ icon next to stale symbols. No extra IBKR calls. First run after deployment will flag 100+ stocks (NULL timestamps); self-corrects within 4h.

4. **Cross-tier dedup** (commit 23f6f72) — symbols can appear in CANDIDATE_POOLS (scored as growth/dividend) AND in Claude's breakthrough scan. Without dedup, Phase 2 of screener reclassifies the same symbol twice in one run (growth→breakthrough, then breakthrough→growth). TSLA, NVDA, PLTR all flip-flopped in latest screenshot. Fix: dedup before tier slicing — breakthrough wins because it's the more specific thesis assignment.

---

## All Recent Commits (yesterday + today, all pushed)

- 5738250 Fix 0: composite_score clobber in _update_watchlist_metrics
- 8ae93c3 Fix P: tier proportions unified
- d9821da Fix A narrow: pool-aware dividend routing
- 785d83f Piece 2 (partially superseded by Option B)
- e1d9568 Dashboard green-box persistence
- 1a8b883 Fix B: IBKR fundamentals fallback + pence normalization
- 267d36e STATE.md (Wed morning)
- 077aab5 Option B: dividend_total_return_score column + tier-aware compound quality
- c6d5e06 Commit B: Phase 2b refresh scores for held holdings not in top-100
- bf39145 STATE.md (Wed mid-evening)
- 2a95c7b Fix metrics job order: fetch metrics before recalc
- a05cee7, 8ab4b65, c8ec353 Wed-night STATE.md updates
- **3c744c6 Breakthrough tier: reject ETFs, sub-500M caps, reverse splits**
- **65a9dec Logger: route structlog through stdlib so events land in trader.log**
- **06cb459 Watchlist: flag stocks with stale metrics**
- **23f6f72 Screener: dedup symbols across tiers, breakthrough wins**

---

## How Scoring Works (verified end-of-Thursday)

Columns on portfolio_watchlist (now 38 total):

- growth_score, valuation_score, quality_score — FMP/IBKR fundamentals
- dividend_total_return_score — dividend-specific. NULL for non-dividend tier.
- raw_score — analyzer's technical signal (SMA discount + RSI). Written by both _update_watchlist_metrics and recalc_scores_from_db.
- compound_quality_pct — within-tier 1-100 percentile from _compute_compound_quality
- composite_score — dashboard value. Written ONLY by recalc_scores_from_db. Formula: (raw - penalty) * 0.80 + compound_quality_pct * 0.20
- discount_pct, rsi_14, sma_200, current_price — written by _update_watchlist_metrics
- metrics_stale, last_metrics_success — staleness flag and last-success timestamp (NEW Thursday)

Compound quality formulas (Option B, per tier):

- Growth / breakthrough: raw = 0.40 * growth + 0.25 * valuation + 0.35 * quality
- Dividend: raw = dividend_total_return_score

Composite-write chain (after Wed metrics-order fix):

1. job_portfolio_update_metrics runs every 4 hours
2. update_watchlist_metrics loops every watchlist stock, IBKR calls, writes price/sma/rsi/raw + last_metrics_success
3. End of loop: scan for stocks with last_metrics_success older than 24h, set metrics_stale=True
4. recalc_scores_from_db loops again with populated discount/rsi, calls _compute_compound_quality, writes composite_score
5. Logs portfolio_metrics_updated (with newly_stale count) and portfolio_scores_recalced

---

## Critical Architecture Facts

1. FMP is dead for LSE/AEB/HKEX/BM. Use IBKR ReportsFinSummary via src/portfolio/ibkr_fundamentals.py.
2. LSE prices come in pence, not pounds. Fix B divides by 100 in _score_stock.
3. Maggy and Winston are separate IBKR accounts. Ports 4001 vs 7496. Separate tables (positions vs portfolio_holdings/portfolio_put_entries).
4. Options universe is a subset of portfolio universe. Stocks dropping out mid-cycle do NOT abandon covered calls — Maggy reads positions table directly.
5. Running process must be restarted for code changes to take effect.
6. Logging: structlog routes through stdlib → trader.log captures everything (as of Thursday).
7. 3-consecutive-failure exchange skip in update_watchlist_metrics STILL in code. Dormant. Leave alone.
8. Never use heredoc for Python patches with complex strings — write to /tmp/*.py if needed.
9. Breakthrough tier enforces $500M cap + no ETFs + no recent reverse splits at code level.
10. Watchlist staleness now visible — yellow ⏱ icon on dashboard.
11. Symbols in both CANDIDATE_POOLS and breakthrough scan deduplicate to breakthrough.

---

## Working Rules

1. Copy-paste terminal commands only.
2. One change at a time. Backup, verify, patch, syntax check, diff, test, commit, push.
3. View files before editing. Memory is stale.
4. Fixes structural, not manual.
5. Don't touch Maggy-side code (src/strategy/, src/broker/, options-side of src/scheduler/jobs.py).
6. Don't conflate PortfolioPutEntry (Winston's CSPs) with Position (Maggy's wheel).
7. When Ryan pushes back, re-examine, don't defend.
8. Never claim a fix works without proof: restart, test, verify DB values, then claim.
9. Composite=0 for valid stocks is NEVER correct.
10. Every action needs a turn. Stop when Ryan stops.
11. If Ryan asks a question before pasting output, answer the question first. Don't assume output that wasn't there.
12. Cross-check STATE.md after writing. Empty pattern matches mean nothing changed; verify content actually updated.

---

## Open Items / TODO Next Session

### To verify after restart
- Stale flag populates correctly (first run: most flagged; subsequent runs: only failures)
- Logger writes structlog events to trader.log
- Breakthrough filter activates on next screener run (XXII / micro-caps drop out)
- Dedup eliminates flip-flop reclassifications

### Wait-and-see
- BZG2 / MC composite 32 with Q=0, G=0 — stale data, watch if next cycle resolves
- SHEL IBKR Error 430 — some LSE stocks uncovered by IBKR; consider US ADR fallback
- MA / V signal_type cosmetic stale — shows 52w_low despite raw_score=0
- Dashboard SMA display vs tier-specific discount — cosmetic inconsistency
- 9 dividend holdings still NULL on dividend_total_return_score — Phase 2b populates on next screener run

### Lower priority
- Scorer fail-closed behavior — screener side largely self-corrects, metrics side now via staleness. Defer.
- 3-consecutive-failure exchange skip — dormant, leave alone unless it bites again.
- Dashboard log redundancy from logger fix — cosmetic, can clean later.

---

## Operational Facts

- Server: rain@octoserver-genoax2:~/automatic_option_trader
- Python: .venv/bin/python3
- GitHub: https://github.com/rainrosimannus-spec/automatic_option_trader (token rotated Wed)
- Restart: ~/restart-all.sh (2x phone 2FA, wait 15-20s)
- Dashboard: http://37.0.30.34:8080
- IBKR ports: Maggy 4001, Winston 7496
- FMP quota: 250/day (screener uses ~150-300, +60 today for breakthrough filter checks)
- check_interval_hours: 4 (config/settings.yaml)
- First metrics run after restart: startup + 120 seconds

---

## Don'ts

- Don't touch Maggy-side code
- Don't run monthly screener without warning (20-40 min, FMP quota, lock held)
- Don't rewrite screener logic in backfill scripts
- Don't try to work while Ryan sleeps
- Don't assume pushed == live
- Don't conflate dashboard looks wrong with is broken
- Don't assume a fix worked — restart and verify
- Don't use complex heredoc for patches
- Don't assume STATE.md write succeeded — verify with head/tail of the file
