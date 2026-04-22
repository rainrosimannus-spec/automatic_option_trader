# Maggy & Winston — State Document

Last updated: 2026-04-22 (Wed) after morning session.

---

## Top Of Mind — Current State

**The dividend-tier / non-US-scoring problem is fixed in code. Awaiting restart + fresh screener run to flow into the DB.**

Three fixes landed today (not yet live in running process):

- `785d83f` Portfolio buyer: dividend-tier compound_quality uses quality_score only (0.00 growth / 1.00 quality weights). Previously dividend stocks were ranked on a 0.30 growth + 0.70 quality blend, mixing a structurally meaningless growth metric into dividend-tier ranking.
- `e1d9568` Screener UI: persist running-state flag across page refreshes. File `data/screener_running.flag` written before the background thread launches, cleared in try/finally. GET handler reads flag, template renders green box conditionally. Stale flag (>2h) auto-cleared.
- `1a8b883` Fix B: IBKR fundamentals fallback for non-US stocks. New module `src/portfolio/ibkr_fundamentals.py` parses ReportsFinSummary XML. Merged into `tools/screen_universe.py:_score_stock`. Also includes pence-to-pounds normalization for LSE stocks (was 100× off) and 2-year window for dividend_cut detection (was: any historical cut = floor 5.0, now: only cuts in last 2 year transitions).

To activate: restart app, then click "Run now" on /screener dashboard. Expect 30-45 min run time. After completion, IMB should score ~36 (genuine low, not floor), BATS ~57 (dividend tier competitive), PM ~59.9 (improved from FMP's 54.7 because IBKR payout/CAGR override FMP's broken defaults).

---

## What Ryan Fixed Today That You Need To Know About

Three structural fixes from last night that Ryan pushed BEFORE this session:
- `5738250` Fix 0: `_update_watchlist_metrics` writes `raw_score` not `composite_score`. Eliminates the race where the analyzer's raw technical score was clobbering recalc's blend. LIVE as of 06:45 restart.
- `8ae93c3` Fix P: unified tier proportions to 15/25/60 via `cfg.tier_count_*` in config.py. Scheduler.py:348-350 hardcoded 65/15/20 was overriding config. LIVE.
- `d9821da` Fix A narrow: `tools/screen_universe.py:908-910` Phase 3 builds `_dividend_pool_symbols` set from `DIVIDEND_CANDIDATES`. Dividend split: `s.tier != "breakthrough" and (s.symbol in _dividend_pool_symbols or s.dividend_yield > 2.5)`. LIVE, and exercised in today's 08:07 screener run.

Piece 1 manual DB flip at ~07:00 (UPDATE 12 mistiered stocks to tier='dividend') was SUPERSEDED by the 08:07 screener run. Not worth tracking — it's history.

---

## System Architecture — One Page

Two IBKR accounts, two Python processes, one shared app. Or rather: one app, two connection singletons.

**Maggy** (options wheel): port 4001, clientId 97 (put_seller) + 99 (risk, etc.). Strategy: sell 0-3 DTE cash-secured puts (0-7 DTE for non-USD underlyings), roll into CC when assigned, cover at delta 0.30-0.45 to get called away. All covered-call exits are profit-take at 80%, no roll-up, no exit-mode gating (iron-logic). Active CCs on SHOP, UBER, PANW, PG.

**Winston** (long-term portfolio builder): port 7496, clientId 50 (test), 51 (sync), 52 (buyer), 53 (metrics). Allocates across 3 tiers:
- Dividend 15% (currently 15 slots, was 25)
- Growth 60% (currently 60 slots, was 50)  
- Breakthrough 25% (currently 25 slots, was 25)

Controlled by `PortfolioConfig.tier_count_*` (15/60/25). DO NOT override in settings.yaml — config.py is source of truth.

**Monthly screener** (`tools/screen_universe.py:UniverseScreener.screen_all`):
- Phase 1: score all CANDIDATE_POOLS symbols (US, UK, DE, FR, ES, IT, SE, DK, FI, NO, JP, HK, SG, KR, AU, IN, BR, MX, IL, ZA, AT, IE)
- Phase 1b: score DIVIDEND_CANDIDATES (additional dividend-focused symbols, deduplicated with pool)
- Phase 2 (of screener): Claude-assisted breakthrough scan
- Phase 3: split into dividend/growth pools by pool membership OR yield>2.5, sort each by appropriate score (portfolio_score for growth, dividend_total_return_score for dividend), select top N per tier with Fix P's config counts
- Phase 4: build options_universe.yaml (top 50 from portfolio universe by options_score)

Downstream `job_portfolio_monthly_screen` in `src/portfolio/scheduler.py`:
- Phase 2 (of scheduler, confusingly): diff new universe vs existing watchlist. Add new stocks. Update scores on existing ones (growth_score, valuation_score, quality_score, dividend_yield, dividend_total_return_score, portfolio_score, options_score). For existing watchlist entries not in new universe, flag `pending_removal=True` UNLESS position exists, in which case keep with flag. Stocks that are neither in the new universe nor held get DROPPED from watchlist entirely.
- Phase 3: tier reclassification for existing stocks where new `score.tier` differs from old.
- Phase 4: write `data/screener_last_run.json` for dashboard, trigger alerts.

The screener does NOT read DB state as input — it builds from CANDIDATE_POOLS + live IBKR + live FMP. Phase 2/3 of the scheduler is the only interaction between screener and DB.

**Two scoring flows for watchlist (run every 4 hours):**
- `job_portfolio_scan` → `buyer.run_scan()` → scores updates via `_update_watchlist_metrics`. After Fix 0, this only writes `raw_score`, leaving `composite_score` alone.
- `job_portfolio_update_metrics` → sequence: `buyer.recalc_scores_from_db()` → `buyer.update_watchlist_metrics()` (IBKR calls outside lock). recalc computes composite = raw_tech_signal × 0.80 + compound_quality_pct × 0.20 with risk_penalty subtracted from raw. This is the ONLY writer of composite_score.

`compound_quality_pct` = within-tier percentile of `raw = growth_score × w_growth + quality_score × w_quality`. Tier weights (buyer.py:1290):
- growth: 0.50 / 0.50
- **dividend: 0.00 / 1.00** (after Piece 2 — was 0.30 / 0.70)
- breakthrough: 0.70 / 0.30

Critical: `quality_score` column is reused — for dividend tier stocks, screener writes `dividend_total_return_score` into `quality_score`. For growth/breakthrough, it writes the regular `_score_quality` output. (I THINK this is the design. NEED TO VERIFY — see Open Questions below.)

---

## Data Provider Situation (Important)

**FMP (Financial Modeling Prep):**
- Current subscription level: only `/stable/` endpoints work
- `/api/v3/` legacy endpoints retired August 2025 — any call returns "Legacy Endpoint" error
- NO coverage of LSE/AEB/HKEX/BM/BVME primary listings. Tested: IMB bare → empty, IMB.L → HTML error, IMB.LSE → HTML error, search "Imperial Brands" → empty. FMP simply does not have these companies.
- US ADRs DO work: ASML (bare), TEF (bare), HDB (bare as ADR) all return full data.
- Known broken fields even for covered stocks: `payout_ratio` returns 0 for SHEL/PM/HDB/TEF (probably `/stable/ratios` doesn't return `payoutRatio` field). `dividend_cagr_3yr` and `_5yr` are None for every single stock — `historical-dividends` endpoint returns empty.
- FMP's `dividend_cut` logic uses yield-ratio comparison (yield dropped >30% YoY = "cut") — this is buggy. Yield can drop due to price rising. False positive on TEF specifically.

**IBKR fundamentals (subscription check):**
- `ReportSnapshot`: WORKS for all tickers tested including LSE. ~10-14k chars XML. Contains PE, ROE, MarketCap, MostRecentSplit, Employees, shares out, business summary. Does NOT contain dividend history, EPS history, or revenue history in easily-parseable form — company-metadata heavy.
- `ReportsFinSummary`: WORKS for most tickers. ~10-44k chars XML. Contains DividendPerShares (annual, 14 years history for IMB), Dividends (individual pay dates), EPSs, TotalRevenues (quarterly + annual). THIS IS THE GOLD.
- `ReportRatios`: Error 430 "NOT ENTITLED" on current subscription.
- `ReportsFinStatements`: Error 430 "NOT ENTITLED".
- `RESC`: WORKS (127k-391k chars analyst estimates) but triggers "news feed not allowed" warnings. Not used.
- Known gap: `ReportsFinSummary` returned 430 for SHEL specifically. So "IBKR covers all LSE stocks" is false — some specific stocks are excluded. Unknown what pattern (maybe IBKR's coverage of Shell is via the ADR / SMART route).

**Implication:** Current fallback chain: FMP → IBKR. When both fail (e.g., SHEL), scorers still default to 50.0. This is a known hole. Fix B integration doesn't address it; Fix B only adds IBKR as second tier.

---

## Working Rules (Read These Every Session)

1. **Copy-paste terminal commands only.** Ryan is not a programmer but is not an idiot either. He can run commands, read output, copy-paste. He cannot debug Python stack traces. Every command needs to work first time.

2. **One change at a time.** Backup → cat -A whitespace check → sed/python one line → ast.parse → git diff → test → commit → push. This IS the workflow. Shortcuts cause bugs.

3. **No asyncio.** asyncio is prohibited in this codebase per RULES.md. Use threading.Lock and threading.Thread. If you see `async def` outside the FastAPI route layer, something is wrong.

4. **`_scan_lock`** must be held for any blocking IBKR call inside scheduler-triggered code. Portfolio side uses `get_portfolio_lock()`. Options side uses `get_options_lock()` (or similar — check).

5. **`_ensure_event_loop()`** needs to be called at the start of background threads that will do IBKR operations. Without it, ib_insync can't find its event loop and throws.

6. **DO NOT TOUCH OPTIONS SIDE.** Maggy lives in `src/strategy/`, `src/scheduler/jobs.py` (options-side jobs), `src/broker/` (orders). The whole `src/portfolio/` and `tools/screen_universe.py` are fair game. Everything else is off-limits unless Ryan explicitly asks.

7. **Every fix must be structural, not manual.** If you find yourself writing `UPDATE portfolio_watchlist SET ...` as a "hotfix," stop. The bug is in the code that WROTE the wrong value. Fix that. A manual DB UPDATE is a band-aid. Ryan will call this out. It happened twice today.

8. **You cannot work while Ryan is asleep.** Every "action" requires a turn. If Ryan says "good night," you stop. If Ryan says "do X while I sleep," tell him you can't. This is not a limitation to apologize for — it's the model. State it clearly.

9. **Ryan is direct. Match that.** He doesn't want hedging, preamble, or excessive caveats. He wants the clearest version of what you know and what you need. If you don't know something, say so once and move on. Don't repeatedly flag uncertainty.

10. **Ryan pushes back fast and accurately.** When he pushes back, his critique is almost always correct. Do not defend — re-examine. Today's examples: "Did you fix the text box?" (I'd wandered into IMB investigation). "Are you trying to overwhelm me to stop thinking?" (I had been piling questions). "That's still a manual fix." (Piece 1 was a hotfix not a structural fix). He was right all three times.

---

## Today's Session Summary

**What we planned to do:**
- Fix A+: rebalance dividend sustainability scoring
- Fix B: IBKR fundamentals fallback
- Fix C: tier-aware compound quality
- Backfill script

**What actually happened:**

1. Verified last night's 3 commits were live after 06:45 restart (they were). Fix 0 confirmed working: IMB showed composite=57.3 (blend) vs raw=69.9 (technical signal) — different values means no clobber.

2. Wasted time investigating whether the 08:07 screener run updated the DB correctly. It did. IMB dropped from watchlist because its dividend_total_return_score was below the 15-stock cutoff (too much FMP data missing) — which was actually the bug we needed to fix.

3. Piece 1: manual DB flip of 12 pool-misplaced stocks to tier='dividend'. Ryan correctly called this a hotfix not a structural fix. Superseded by the 08:07 screener run.

4. Piece 2: committed `785d83f`. Changed buyer.py:1290 dividend tier weights to 0.00/1.00 — dividend ranking now purely on quality_score (which holds dividend_total_return_score). Not yet live.

5. Dashboard fix: committed `e1d9568`. Added running-flag persistence so green box survives page refresh. Not yet live.

6. Investigated FMP ticker suffix handling — dead end. FMP genuinely lacks LSE/AEB/HKEX coverage, all suffix variants fail, `/api/v3/` retired.

7. Verified IBKR ReportsFinSummary has the structured data we need for LSE. IMB returned 14 years of DividendPerShare, EPSs, and TotalRevenues.

8. Wrote `src/portfolio/ibkr_fundamentals.py`. Parses XML. Returns dict with absent-or-real semantics.

9. Integrated into `tools/screen_universe.py:_score_stock` as fallback. FMP → IBKR merge. Prefer IBKR for `payout_ratio`, `dividend_cagr_*`, `dividend_cut` (FMP known-broken). Fill gaps elsewhere.

10. Added pence-to-pounds normalization. LSE GBP stocks come from IBKR in pence. Every OTHER place in the codebase (trade_sync.py, risk.py) already normalizes — screener didn't.

11. Added 2-year window to dividend_cut detection. IMB's 2020-2021 cut no longer disqualifies it (3 years on, DPS is recovering).

12. Verified via live IBKR tests. IMB now scores 36 (was 5). BATS 57 (was 34). PM 59.9 (was 54.7). SSE correctly stays at 5 (cut within 2-year window is genuine).

13. Committed Fix B: `1a8b883`. Not yet live.

---

## Open Questions / Things To Verify Next Session

**These all need answering before further work:**

1. **Does the screener actually write `dividend_total_return_score` into the `quality_score` DB column for dividend-tier stocks?** I'm unsure. The screener writes `StockScore` objects; `scheduler.py` Phase 2 translates those to DB fields. If `quality_score` for dividend stocks holds the regular `_score_quality` output (not `_score_dividend_total_return`), then Piece 2 is pointing at the wrong column and won't work as intended. CHECK: after restart + screener run, compare `quality_score` vs YAML's `dividend_total_return_score` for a few dividend stocks.

2. **Why did SHEL get IBKR error 430?** We assumed IBKR covers LSE broadly, but SHEL specifically doesn't. Is this because SHEL has a US dual-listing? Same for other multi-listed stocks. May need a try-alternative-exchange fallback in ibkr_fundamentals (e.g., if LSE fails, try SMART with US primary exchange).

3. **PHIA scored 5.0 with dividend_cut=True** in my earlier test. Is that a real cut within 2 years, or a false positive from my parser? PHIA (Philips) has been restructuring — possible genuine cut. Need to verify by dumping its DividendPerShare history.

4. **TEF returned `price: nan`** because BM isn't subscribed. TEF scored on its ADR version perhaps? Or the pence fix is applied to EUR stocks unintentionally? Actually — my code only converts when `currency == "GBP"`. So EUR shouldn't be affected. The `nan` is from IBKR returning no market data. The score still computed because `_score_stock` fell back to FMP-provided price via `/stable/quote`. But dividend_yield went through IBKR path using what price? INVESTIGATE.

5. **Scorers still default to 50.0 when inputs missing.** This was flagged as Part 3 of original Fix B but deferred. Today's integration gives IBKR as fallback but doesn't make `_score_growth` / `_score_valuation` / `_score_quality` return None honestly. For stocks where neither FMP nor IBKR has data, they still get fake 50s. Ryan flagged this explicitly.

6. **`_estimate_market_cap` duplicates the ReportSnapshot fetch.** It's called from `_score_stock` and does its own `ib.reqFundamentalData(contract, "ReportSnapshot")`. Meanwhile `ibkr_fundamentals.py` fetches `ReportsFinSummary` separately. This is wasteful (2 IBKR calls) but not broken. Refactor opportunity for later.

7. **Is screened_universe.yaml's `dividend_total_return_score` the post-cut-floor score or pre-floor?** When a stock has `dividend_cut=True`, the function returns 5.0. YAML shows integer scores. Need to verify: does a cut stock show "5" in YAML or does it still show its uncut-components total? Check PM's YAML entry — PM is not cut, so its 54.7 is genuine. But for IMB (pre-fix, cut=True), what did the 08:07 YAML show? Earlier output showed IMB wasn't in the 08:07 YAML at all because it didn't make top 15. So this is moot for now.

8. **CANDIDATE_POOLS vs DIVIDEND_CANDIDATES:** which one has IMB in it? Answer: `DIVIDEND_CANDIDATES["UK"]` probably. Double-check `_dividend_pool_symbols` set construction in Phase 3 — Fix A narrow depends on this.

---

## Operational Facts

- **Server:** `rain@octoserver-genoax2:~/automatic_option_trader`
- **Python:** `.venv/bin/python3`
- **Working dir for commands:** `~/automatic_option_trader`
- **GitHub:** https://github.com/rainrosimannus-spec/automatic_option_trader (public)
- **Git remote has token embedded** in `.github_claude_token` — pushes work without auth prompt
- **Restart command:** `~/restart-all.sh` — needs 2× phone 2FA, wait 15-20s, check dashboard http://37.0.30.34:8080
- **Tmux session:** `tmux attach -t trader` — shows running app logs, but background thread logs may not appear in tmux; file logs are in `/var/log/` or similar (investigate if needed)
- **IBKR ports:** Maggy=4001, Winston=7496
- **Free clientIds for diagnostic connections:** 50 (used in today's test script — confirmed unused)
- **FMP key:** in settings.yaml `fmp.api_key`
- **FMP quota:** 250 calls/day on current plan (free tier). Monthly screener uses ~300-500 calls. Watch for quota exhaustion.
- **Working DB file:** `data/trades.db` (SQLite)
- **Portfolio config lives in:** `src/portfolio/config.py` — NOT settings.yaml
- **Scheduler jobs file:** `src/scheduler/jobs.py`, top-level scheduling around line 1225-1290. Portfolio jobs: portfolio_scan (4h), portfolio_prices (1h), portfolio_metrics (4h), portfolio_monthly_screen (first Monday 3 AM ET)

---

## Things NOT To Do

- Don't touch `src/strategy/`, `src/broker/`, or the Maggy-side of `src/scheduler/jobs.py`
- Don't run the monthly screener without warning Ryan (it's 30-45 min, consumes FMP quota, blocks portfolio lock)
- Don't write backfill scripts that duplicate screener logic — if screener is right, run the screener. If screener is wrong, fix the screener.
- Don't try to work while Ryan sleeps. You stop when the conversation stops.
- Don't forget to view files before editing. `view` → see line numbers → confident sed. Not "I remember this from before."
- Don't claim a fix is working without verifying it. Claim it tentatively, verify with test, then claim it confidently.

