# Maggy & Winston — State Document

Last updated: 2026-04-22 (Wed) end-of-day, after Option B + Commit B.

---

## Top Of Mind — Current State

Today's full arc: morning restart + screener run exposed that Piece 2 (yesterday's commit `785d83f`) pointed compound_quality at the WRONG column. Dividend-tier stocks were being ranked on generic FCF/debt quality instead of dividend-specific metrics. Ryan caught it. Today's work fixed it properly.

**Net: dividend tier now ranks on dividend-specific metric. Growth/breakthrough ranks on portfolio_score formula (valuation re-included). Option B + Commit B both live in the running process.**

**But the dashboard still looks growth-heavy.** NOW/TTD/NICE/ZS dominate top 10 because they have active technical buy signals (raw_score=40). Dividend stocks sit at composite 0-18 because none are currently oversold (raw_score=0). That's the 80/20 composite blend working as designed — quality alone doesn't lift composite without timing. Ryan accepted this as correct, with a "watch for 2-3 days" checkpoint: if scores freeze or nothing ever breaks 55 or dividend stocks never surface, revisit.

**What's actually in the running system:**

- Option B live: `dividend_total_return_score` column populated for dividend tier, `_compute_compound_quality` uses tier-specific formulas
- Commit B live: Phase 2b refreshes scores for held stocks not in top-100 during screener runs
- Fix B from this morning: IBKR fundamentals fallback, pence normalization, 2-year cut window
- Fix 0 / Fix P / Fix A narrow from yesterday — all live

---

## Today's Commits (all pushed)

| Hash | What |
|------|------|
| `5738250` | Fix 0: composite_score clobber |
| `8ae93c3` | Fix P: tier proportions unified |
| `d9821da` | Fix A narrow: pool-aware dividend routing |
| `785d83f` | Piece 2: dividend weights 0.00/1.00 (pointed at wrong column — mostly superseded by Option B) |
| `e1d9568` | Dashboard green-box persistence across refreshes |
| `1a8b883` | Fix B: IBKR fundamentals fallback + pence normalization + 2-yr cut window |
| `267d36e` | STATE.md morning version |
| `077aab5` | **Option B**: `dividend_total_return_score` column + tier-aware compound quality (valuation re-included for growth/breakthrough) |
| `c6d5e06` | **Commit B**: Phase 2b refresh scores for held holdings not in top-100 |

---

## How Scoring Actually Works (verified, not speculated)

**DB columns on portfolio_watchlist:**

- `growth_score` — from `_score_growth(fmp)` — revenue YoY, gross margin, margin trend
- `valuation_score` — from `_score_valuation(fmp)` — PEG/PE/ROE
- `quality_score` — from `_score_quality(fmp)` — debt/equity, FCF consistency, FCF margin
- `dividend_total_return_score` — from `_score_dividend_total_return(fmp, growth)` — yield+CAGR+sustainability+price-proxy. **NULL for non-dividend-tier rows by design.**
- `raw_score` — written by analyzer's technical signal (discount from SMA, RSI oversold, etc.). Written by both `_update_watchlist_metrics` and `recalc_scores_from_db`.
- `compound_quality_pct` — within-tier percentile 1-100, computed by `_compute_compound_quality`
- `composite_score` — what dashboard shows. Computed by `recalc_scores_from_db` as `(raw_score - risk_total_penalty) × 0.80 + compound_quality_pct × 0.20`
- `risk_total_penalty` — sum of structural risk flags

**Formulas in `_compute_compound_quality` (per tier, percentile normalized within tier to 1-100):**

- Growth/breakthrough: `raw = 0.40×growth_score + 0.25×valuation_score + 0.35×quality_score`
- Dividend: `raw = dividend_total_return_score`

This was the key fix today. Previously dividend tier used quality_score directly, which held FCF/debt quality — not dividend metrics. Now dividend tier has its own column.

**Which scripts write scores:**

- Screener (scheduler.py Phase 2 INSERT/UPDATE): writes all 5 score columns. For dividend-tier: writes `dividend_total_return_score` too. Phase 2b (new from Commit B) also writes for held holdings not in top-100.
- `_update_watchlist_metrics` (buyer.py): writes `raw_score` only (after Fix 0 — used to clobber composite_score)
- `recalc_scores_from_db` (buyer.py): calls `_compute_compound_quality` then writes `composite_score` and also `raw_score`

---

## What's In The Watchlist Right Now (15:38 screener)

- 128 total rows (15+25+60=100 top-N + 28 held holdings that stayed)
- Dividend tier (24 rows): top 15 selected by screener, 9 held holdings without top-15 slot
- Growth tier (~80): top 60 + held holdings
- Breakthrough tier (21): top 25 − some holdings that aren't in it

**Top dividend_total_return_score values:**
1. CNQ 90.2, 2. TD 87, 3. RY 84, 4. MFC 84, 5. BMO 83, 6. CFR 80.9, 7. SPG 80.1, 8. O39 79.3, 9. 2318 79, 10. SU 78.1, 11. MRK 75.9, 12. NEE 75.5, 13. BAC 75.2, 14. AZN 74.2, 15. EOG 74.1

**Holdings without current dividend scores (held but NOT in top-15):** HDB, IBN, BMY, CEG, 0ZQ, ALV, NLY, PBR, SFL. These got `dividend_total_return_score = NULL` because the 15:38 screener didn't run Commit B yet (we restarted AFTER the 15:38 run, not before). Next screener run will fix this — Commit B's Phase 2b is now live.

**Dashboard top 10 as of 19:16:** NOW 50, ZS 49, TTD 49, NICE 48, FSLR 46, ALL 41, SNOW 36, NPN 34, BZG2 32, MC 32.

**Concerning entries:** BZG2 and MC at 32 have quality_score=0, growth_score=0 but raw_score=40 (technical signal from price). Likely stale / off-pool holdings with data gaps. Not necessarily bugs but worth watching.

---

## Critical Architecture Facts (Re-Read Every Session)

1. **FMP is dead for LSE/AEB/HKEX/BM.** Confirmed today — no suffix variant works, `/api/v3/` retired Aug 2025, search by company name returns empty. Even FMP-covered stocks have broken fields: `payout_ratio` almost always 0, `dividend_cagr_*` always None, `dividend_cut` has yield-based false positives.

2. **IBKR ReportsFinSummary is the workhorse for non-US.** XML contains DividendPerShares (annual history), EPSs, TotalRevenues. My parser in `src/portfolio/ibkr_fundamentals.py` reads it. Some LSE stocks still fail with Error 430 (SHEL notably) — genuine data gap, not fixable from our side.

3. **Maggy and Winston are truly separate.** Separate IBKR accounts, separate ports (4001 vs 7496), separate position tables (`positions` for Maggy, `portfolio_holdings` + `portfolio_put_entries` for Winston). Today I wasted time conflating them. Don't.

4. **Options universe is a subset of portfolio universe.** `screen_all()` picks top-100 portfolio, then picks top-50 of those by options_score. Maggy reads `options_universe.yaml` only for "eligible for new trades" — existing positions are managed from the `positions` table regardless. Verified today via `src/strategy/universe.py` trace. Stocks dropping out of options universe mid-cycle do NOT abandon covered calls.

5. **LSE prices come in pence, not pounds.** Every piece of the codebase that consumes GBP prices does `price / 100.0`. The screener wasn't doing this until today (Fix B added it at `_score_stock` price handler).

6. **The running process must be restarted for code changes to take effect.** The restart workflow: `~/restart-all.sh`, 2× phone 2FA, wait 15-20s. Background threads silently die without logging. Every commit is dead until restart proves it live.

---

## Working Rules

1. Copy-paste terminal commands only. Ryan runs them, pastes output, I react.
2. One change at a time. Backup → verify whitespace → patch → syntax check → diff → test → commit → push.
3. View files before editing. Line numbers shift, memory is stale.
4. Fixes must be structural, not manual. If you catch yourself writing `UPDATE …` as a hotfix, the real bug is upstream.
5. Don't touch Maggy (`src/strategy/`, `src/broker/`, options-side of `src/scheduler/jobs.py`). Portfolio side (`src/portfolio/`, `tools/screen_universe.py`) is fair game.
6. Don't conflate `PortfolioPutEntry` (Winston's CSPs) with `Position` (Maggy's wheel). Completely separate tables, separate accounts.
7. Verify don't assume. When Ryan pushes back on reasoning, he's almost always right. Re-examine, don't defend.
8. Never claim a fix is working without proof. Restart → test → verify values in DB → then claim.
9. Don't panic over dashboard appearance. Quality stocks ranking low can be correct behavior — the 80/20 blend needs both quality AND buy-timing signals.
10. Ryan cannot have you "work while he sleeps." Every action requires a turn. Stop when he stops.

---

## Open Questions / TODO Next Session

1. **Verify dashboard updates correctly tomorrow.** If scores freeze, if nothing breaks 55, if dividend stocks NEVER surface — the system isn't reacting to market data. Checkpoint: by Friday.

2. **BZG2 and MC at composite 32 with Q=0, G=0** — check if these are stale data or legit off-pool holdings with data gaps. May need either removal or explicit `_score_stock` refresh.

3. **`_compute_compound_quality` is called by `recalc_scores_from_db`** — but only on stocks with `raw_score > 0`? Verify that it runs for ALL watchlist including those with raw=0, otherwise held stocks never get their compound_quality computed.

4. **The 9 NULL `dividend_total_return_score` rows** (HDB, IBN, BMY, CEG, etc.) — Commit B's Phase 2b is now live, next screener run will populate them. Trigger a run to verify?

5. **SHEL with IBKR 430** — some specific LSE stocks aren't covered by IBKR either. Worth a second fallback: try US ADR / SMART route for dual-listed names. Not urgent.

6. **Scorer fail-closed behavior still deferred.** When both FMP AND IBKR fail, scorers still default to 50.0. Should return None, and composite should handle None honestly. Ryan flagged this last night.

7. **STATE.md should mention** that token was rotated mid-session due to accidental paste exposure. Old token revoked, new one in `~/.github_claude_token` and the git remote URL. Not the cause of the mid-afternoon GitHub TCP block (separate transient network issue, resolved itself).

---

## Operational Facts

- **Server:** `rain@octoserver-genoax2:~/automatic_option_trader`
- **Python:** `.venv/bin/python3`
- **GitHub:** https://github.com/rainrosimannus-spec/automatic_option_trader (token rotated today)
- **Restart command:** `~/restart-all.sh` (2× phone 2FA)
- **Dashboard:** http://37.0.30.34:8080
- **IBKR ports:** Maggy 4001, Winston 7496
- **Test clientId for diagnostics:** 50
- **FMP quota:** 250/day. A full screener run uses ~150-300 calls.
- **Scheduler job cadences:**
  - portfolio_scan: 4h
  - portfolio_prices: 1h
  - portfolio_metrics: 4h (recalc + update_watchlist_metrics)
  - portfolio_monthly_screen: first Monday of month, 3 AM ET (or manual via dashboard button)

---

## Don'ts

- Don't touch Maggy-side code (`src/strategy/`, `src/broker/`, `src/scheduler/jobs.py` Maggy sections)
- Don't run the monthly screener without warning Ryan (20-40 min, FMP quota, holds portfolio lock)
- Don't rewrite screener logic in "backfill scripts" — if screener is right, run screener; if wrong, fix screener
- Don't try to work while Ryan sleeps. You stop when conversation stops.
- Don't assume restart happened just because commits are pushed. Pushed ≠ live.
- Don't conflate dashboard "looks wrong" with "is broken." Verify the logic with actual values before claiming bugs.

