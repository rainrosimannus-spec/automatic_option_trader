# Maggy & Winston — State Document

Last updated: 2026-05-10 (Sun) — Augmentation pipeline validated end-to-end + Commit E + breakthrough infrastructure (5 commits).

---


## Sunday (2026-05-10) — Augmentation pipeline validated end-to-end + Commit E + breakthrough infrastructure (5 commits)

**Long debugging-then-shipping evening.** Five real commits, all pushed. Augmentation pipeline produced its first acceptances ever (May 8 work was committed but never executed end-to-end — discovered tonight). Commit E shipped. Breakthrough prompt v4 deployed. Breakthrough history persistence layer built. Plus a 5-day-old portfolio dashboard drift cleared up along the way.

### Augmentation pipeline — found and fixed NameError (commit 80f08c3)

First augmentation live test (AUGMENTATION_ENABLED = True, manual run via dashboard) produced 16 audit rows, every single one with accepted=False. Eight scoring_failed, one duplicate from growth tier, the rest duplicate from dividend tier. Looked like a prompt or data problem at first.

Audit-table notes column told the real story: NameError: name "_score_stock" is not defined. Every proposal raised this exception, was silently caught at line 1048's bare except Exception, and audited as scoring_failed with score=0.0.

Root cause: _score_stock is a method on UniverseScreener (line 2385, defined yesterday), but _process_augmentation_proposal called it as a bare function. Whoever wrote the code yesterday (Claude in the May 8 session) assumed module scope. The defensive except swallowed it. Pre-flight ast.parse doesnt catch undefined names at runtime, only importlib.import_module in a fresh process would have — and even then only if the scoring path actually executes, which it didnt until first real run.

Also discovered a second bug masked by the first: even after fixing the NameError, the call passed region=region as a kwarg, which _score_stock doesnt accept. Both fixed in one commit. Added screener parameter to _process_augmentation_proposal, defensive RuntimeError if screener missing, call screener._score_stock(symbol, exchange=exchange, currency=currency) without region, pass screener=self at both PHASE 2.5 call sites.

**Important lesson for future work:** on-disk fixes dont activate in long-running services until restart. After committing the fix, the second test run produced *identical* errors — same NameError every row. The dashboards web process was holding stale bytecode in memory from before the fix landed. ~/restart-all.sh (2x phone 2FA) picked up the fix, third test run was clean.

### Augmentation re-test post-restart — pipeline validated

Fourth manual run (with NameError fix loaded): 6 of 8 growth proposals accepted. IDXX 82.5 (veterinary diagnostics, clean compounder well above 55.6 cutoff), MPWR 73.0 (analog semis, compute-adjacent), SPGI 65.8 (credit ratings duopoly), TDG 65.6 (aerospace aftermarket), EW 64.2 (structural heart), WST 60.0 (pharma packaging). Plus 2 below cutoff (ROP 52.8, MCHP 39.2) — proves scoring works, names just didnt beat threshold.

Dividend tier: 0 acceptances, 4 duplicates (correct — already in DIVIDEND_CANDIDATES), 3 below 77.8 cutoff, 1 scoring failure (LMI = Lendlease, Australian REIT, fell to international-handling edge case in augmentation prompts — see known limitations below).

After Commit E and second restart, fifth manual run: 4 more growth acceptances (different names — augmentation working under new portfolio_score). Discovered pool grew 0 to 6 to 10 over the evening. Pipeline genuinely validated across multiple runs.

### Portfolio dashboard merge-mode fix (commit 2faa4df)

Found 5-day-old uncommitted drift in src/web/routes/portfolio.py while preparing the augmentation re-test. Two call sites of get_total_invested_usd() were filtering by cfg.portfolio.ibkr_account — pre-merge artifact that under-counted post-April-28 (when both Maggy and Winston run against U17562704). Filter removed at both sites; cfg/get_settings import dropped from _build_portfolio_performance since no other use remained. Other functions in the file still import get_settings independently (lines 302, 442) — unrelated, untouched.

### Commit E — portfolio_score = forward_growth_score (commit e958fc1)

Per May 8 architectural plan: replaced 0.40*growth + 0.25*valuation + 0.35*quality with forward_growth_score directly at line 2484. Old growth/valuation/quality scores remain on StockScore for diagnostic visibility, no longer drive ranking. options_score derivation unchanged — it now propagates forward_growth_score downstream automatically.

May 8 plan called for 2-3 observation runs before flipping; we had only one full run with forward_growth_score visible (todays earlier dashboard data). Decision to ship: todays results were coherent (Buffett-style ranking working, picks-and-shovels rising, NVDA correctly tempered to 80.5), augmentation re-test produced exactly the kind of names forward_growth_score is meant to surface (IDXX 82.5 ranking like a top-tier compounder), and US markets closed Sunday so behavior wouldnt go live until Monday regardless.

**Verification post-restart:** composite_score field in portfolio_watchlist reflects the analyzers downstream 30/70 blend (0.30*raw_score + 0.70*compound_quality_pct), not the screeners portfolio_score directly. NVDA composite=65.0 = 0.30*0 + 0.70*92.9 (within rounding). Architecture confirmed: screener writes portfolio_score, scheduler stores it, analyzer blends with technical signal, result lands as composite_score. Commit E correctly changed step 1; downstream propagation works as designed.

### Breakthrough prompt v4 (commit f855338)

Targeted gaps surfaced over recent runs: memory/HBM names absent, data-center electrical/cooling under-represented, non-USD listings producing only 1 (target was 5+), Korea and Japan particularly absent.

v4 adds COMPUTE BUILDOUT THESIS section calling out four under-represented sub-themes within AI/compute infrastructure megatrend. Memory (HBM3E, NAND/DRAM): Micron, SK Hynix (000660 KSE), Samsung (005930 KSE), Kioxia (285A TSEJ), WDC. Data center electrical and cooling: nVent (NVT), Eaton (ETN), Schneider (SU.PA), Munters (MTRS.SBF). Semicap beyond ASML/AMAT/KLAC: Tokyo Electron (8035 TSEJ), Disco (6146 TSEJ), Advantest (6857 TSEJ), BE Semiconductor (BESI AEB), Lasertec (6920 TSEJ). Power generation for compute load: beyond CCJ, geothermal pure-plays, SMR developers.

Geographic spread tightened: 6+ non-USD (was 5), 4+ underweighted markets (was 3), explicit requirement of at least 1 Korean (KSE) AND at least 1 Japanese (TSEJ) listing. Concrete examples cited per sub-theme to anchor Claudes selection.

No code changes; orchestration in _get_breakthrough_candidates() unchanged. Template grew from 8744 to 10433 chars. Effect activated at restart 2 — first v4 run was tonights final manual run.

### Breakthrough history persistence (commit c098919) — Step B of three-part design

**Architectural framing:** growth and dividend tiers each have a stable hand-curated base (CANDIDATE_POOLS, DIVIDEND_CANDIDATES) plus an augmentation overlay (discovered_pool.yaml). Breakthrough has neither — its pure ephemeral output, every run from scratch. Tonights commit gives breakthrough the same shape as the other two tiers.

Adds new top-level breakthrough: key in tools/discovered_pool.yaml. Each entry tracks a name that has passed _check_breakthrough_eligibility, with these fields: symbol, exchange, currency, name, megatrend, thesis_latest, first_seen and last_seen (YYYY-MM strings, calendar-month bucketing), appearance_count.

**Insert/update semantics.** New name on this run inserts with count=1, first_seen=last_seen=YYYY-MM. Existing name in a different last_seen month bumps count and updates last_seen. Existing name in same month (multiple manual runs) does not bump count, just refreshes thesis.

Hook fires inside the breakthrough loop at line 2419, immediately after breakthrough_scores.append(score). Captures only what the system actually accepts (post-eligibility, post-scoring), not what Claude proposed. Best-effort: any persistence failure logged but does not interrupt the screener.

**Eviction (_evict_breakthrough_overflow) is stubbed** with detailed TODO. Locked design: protect last 6 calendar months (recency dominates count for new names — a single fresh appearance can be a real new gem worth 6 cycles of observation), beyond window evict by lowest appearance_count then oldest last_seen until pool <= 75. Pool may exceed 75 transiently when all names are within protection window. Cap = 75 = 3*25 (three runs worth of names).

**Why eviction stubbed tonight.** With a fresh pool and ~22 names per run, eviction logic isnt exercisable until pool grows beyond 75 over multiple months. Stub keeps tonights scope small (insert + update only, fully testable) while preserving the design for tomorrow.

**Why Step B tonight (vs deferred).** Without persistence, Item 3 (anchored 30-vs-25 selection prompt) has no anchor list to operate on. Capturing tonights manual-run breakthrough names as the seed lets Item 3 be developed against real data on its first run. Without this, Item 3s first run would have no history to anchor against.

### International handling — investigated, not systemic

LMI scoring failure in augmentation prompted a check: are non-USD listings widely broken in the regular screener?

Currency breakdown of regular screener output: USD 99 symbols (avg fgs 54.5), EUR 11 (51.9), CAD 11 (46.3), JPY 3 (48.1), AUD 3 (45.1), ZAR 2 (56.4), ILS 2 (51.5), GBP 2 (55.2), SGD 1 (50.6), HKD 1 (41.4) — 0 NULL forward_growth_score across all 135 symbols.

Regular screener handles internationals fine — explicit per-symbol exchange/currency from yaml configs flow correctly through _score_stock, IBKR fundamentals fallback chain catches FMP gaps for international names. Issue is **augmentation-only**: when Claude proposes a non-US name with default SMART/USD (because the augmentation prompts dont tell Claude to specify exchange/currency for international names like breakthrough v3 does), qualifyContracts fails and _score_stock returns None.

LMI is the only case in tonights runs. Logged as known limitation — fixable in a future session by extending _build_growth_augmentation_prompt and _build_dividend_augmentation_prompt to instruct Claude on IBKR exchange codes for non-US listings.

### Other observations from tonight

data/screener_next_run.txt is stale — says "Next screener run: May 4" but dashboard correctly shows June 1. The text file is leftover documentation; actual schedule lives elsewhere. Not a bug, just outdated. Future housekeeping.

Breakthrough output was ephemeral — until tonights Step B, no run history retained anywhere. Each run overwrote the previous. Todays runs 1-2 breakthrough lists are gone forever. From tonight onward, breakthrough history starts being preserved.

Stale .bak-2026-05-04* files — dozen of them in untracked state. Future housekeeping.

### Pending for next session

**Breakthrough Step B completion (eviction logic).** Implement _evict_breakthrough_overflow() per locked design: protect last 6 calendar months, beyond window evict by (appearance_count ASC, last_seen ASC) until pool <= 75. Allow transient overflow when all names protected. Test by inserting synthetic old entries to force eviction.

**Breakthrough Item 3 — anchored 30-to-25 selection prompt.** Redesign breakthrough prompt to (a) propose 30 fresh names independently, (b) merge with last runs 25 anchor list from breakthrough_history pool, (c) Claude reasons over 55 candidates and selects top 25 by "market view vs prompt fit." Selection step: single Claude call, prompt contains 30 fresh + 25 anchor + selection rubric. First run with empty anchor: skip anchor section, return 25 fresh. Implements cross-run conviction stability (recurring names get protection) without losing fresh-idea generation (independent first step).

**Augmentation international handling.** Patch _build_growth_augmentation_prompt and _build_dividend_augmentation_prompt to instruct Claude on IBKR exchange codes for non-US listings (similar to breakthrough v4). Resolves LMI-class scoring_failed audit rows for non-US augmentation proposals.

**Carried over from earlier sessions.** RULES.md merge-mode update — still describes pre-merge architecture (Maggy 4001 / Winston 7496 separate). Add "Current operating mode (merged, since 2026-04-28)" section noting suggestion mode + auto-approve OFF, plus backup file references for re-split when new options account arrives. Sons clone Asia/EU options scan check — if Mondays Asian/European market scans on sons clone (U23886415) produce zero put suggestions, son needs to determine whether real bug (universe filter, market label, gates firing only on US data, live-quote gate) or current criteria too strict. Diagnostic drafted. Optional Commit N — --dry-run-augmentation flag for prompt iteration without API spend, nice-to-have.

### Tonights commits (5)

- 80f08c3 — augmentation: fix NameError, call screener._score_stock as method, drop invalid region kwarg
- 2faa4df — portfolio dashboard: remove per-account filter on total_invested_usd (merge-mode fix)
- e958fc1 — screener: Commit E, portfolio_score = forward_growth_score (Buffett-style fair-price)
- f855338 — screener: breakthrough prompt v4, compute buildout thesis + tightened geographic spread
- c098919 — screener: breakthrough_history persistence — Step B (insert+update only, eviction stubbed)

## Monday (2026-05-05) — Capital injections deposit-proof graphs + margin interest investigation

**Options graph formula fix (commit 3b407ff):**

Ryan reported: adding €15K capital injection on options account caused the options graph to jump +100% that day (bullshit). The formula was (current_nlv - first_nlv) / first_nlv × 100 — pure NLV diff, treated capital deposits as growth. Portfolio side had the correct pattern; options side never copied it.

Five-file atomic commit fixed it:

1. src/core/database.py — add migration for portfolio_capital_injections.account_id (VARCHAR(20)) 
2. src/portfolio/models.py — add account_id field to PortfolioCapitalInjection model
3. src/portfolio/capital_injections.py — add get_total_invested_usd(account_id=None) parameter filtering; sync_injections_from_ibkr(account_id=None) tags new rows with account_id
4. src/web/routes/dashboard.py — replace buggy (nlv - first_nlv) / first_nlv formula with (nlv / total_invested - 1) × 100, anchored to first-point-zero; reads options_account from cfg.ibkr.account; calls get_total_invested_usd(account_id=options_account)
5. src/web/routes/portfolio.py — update call sites to pass account_id (reverted later; see below)

Post-split readiness: when new options account arrives, options graph will automatically filter deposits to that account only. No cross-account interference.

**Backfill issue discovered at restart (commit 65178bd + 10ef0a4):**

The backfill UPDATE in risk_backfills loop failed with "name 'text' is not defined" error, breaking the account_snapshot job. The SQL query `UPDATE portfolio_capital_injections SET account_id = 'U17562704' WHERE account_id IS NULL` was malformed or being evaluated in wrong scope.

Rather than debug the backfill, removed it entirely (commit 10ef0a4). The migration itself creates the column with no DEFAULT, so existing rows get NULL. That's fine — they're historical. New rows from trade_sync will have account_id set. Post-split, the new options account's Flex sync will populate its own rows correctly.

**Margin interest investigation (no code change):**

User asked: does IBKR's NetLiquidation already include accrued margin interest, or is it shown separately?

Research from IBKR docs: NetLiquidation = TotalCashValue + AccruedInterest . Interest accrues daily and posts monthly. The accrued amount shown is interest that has NOT YET been charged to cash — it's a liability shown separately. Once posted at month-end, it reverses and moves from "Accrued Interest" to "Total Cash Value".

Conclusion: NetLiquidation already includes accrued interest (as a separate line), so your graph is correct as-is. The margin interest cost is already reflected. The strike-bumping heuristic in wheel.py (line 305, interest_surcharge) operationally tries to recover the interest cost through higher premiums. No additional graph adjustment needed.

Portfolio side shows accrued interest via fetch_accrued_interest_usd() which reads Flex data. This is informational — the interest is already baked into NLV.

**Web server went blank after restart:**

The five-file commit broke something at runtime. Both dashboards were blank/error. Root cause: the backfill UPDATE was failing silently, triggering exception handling that masked a Python import error downstream.

Restart after removing the backfill (10ef0a4) brought both dashboards back up. Options side shows the new capital-aware formula. Portfolio side unaffected.

**Pending issue: portfolio.py still has account_id filtering:**

Patch 5/5 modified portfolio.py to call `get_total_invested_usd(account_id=cfg.portfolio.ibkr_account)`. After the revert, this broke because the function signature was reverted too. Quick fix applied: changed both call sites back to `get_total_invested_usd()` with no args. Portfolio dashboard returned. Not pushed yet because we're in cleanup mode post-incident.

**Commits:**
- 3b407ff capital_injections: per-account deposit tracking + deposit-aware graphs (5 files)
- 65178bd database: fix account_id backfill — move DEFAULT to migration (attempted fix, caused web server error)
- 66d4d79 Revert "database: fix account_id backfill..." (reverted the problematic commit)
- 10ef0a4 database: remove problematic account_id backfill (proper fix, both dashboards back)

**Architecture fact for post-split:**

When new options account arrives and is configured for Flex sync, the options graph will read only that account's deposits automatically via the account_id filter. No manual intervention needed.

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


**Friday (2026-04-24) pre-market — wheel.py covered-call expiry bug:**

Investigating dashboard showing PG, PANW, UBER calls as expired pre-close, system trying to write new calls on positions still open in IBKR. Found wheel.py:439 used `expiry <= today` to mark covered calls EXPIRED — fired pre-market on expiry day while shares were still held. trade_sync (every 15 min) saw IBKR still had the contracts, reopened them. Repeating cycle. Window between flip-out and reopen exposed `wheel.write_covered_calls()` to thinking the lot was uncovered → phantom new calls.

First commit (dd16ec6) changed `<=` to `<` — would have skipped early-exercise detection same-day. **Reverted via better fix (b41e39a)**: keep `<=` to catch same-day events, but only mark `called_away` when IBKR confirms via shares dropping below covered amount. Otherwise defer to trade_sync (the proper authority for "is contract still alive at IBKR"). Mirrors the put-side defensive pattern at wheel.py:99 which already does this correctly.

After restart: PG/PANW/UBER stay OPEN through expiry day. trade_sync detects assignment or worthless expiry from IBKR's portfolio state at/after market close.

**Cleanup applied live:** the buggy expiry handler had set realized_pnl on the three OPEN positions (PG=274, UBER=132, PANW=543) — phantom values matching premium collected. Cleared to 0.0 via direct UPDATE. Schema enforces NOT NULL so 0.0 not NULL. Dashboard wasn't displaying these because realized P&L queries filter to status IN (CLOSED, EXPIRED, ASSIGNED), but the dirty values would have caused incorrect accounting on actual close.

**Trade_sync reopen logic still incomplete:** at trade_sync.py:625-630 the reopen flips status and clears closed_at but does NOT clear realized_pnl. With wheel.py fixed, reopen shouldn't trigger for valid OPEN positions anymore — but if it ever does fire, the same bug returns. Defense-in-depth fix would be a one-line addition (set realized_pnl=0 on reopen). Deferred.

**Cleanup applied live:** the buggy expiry handler had set realized_pnl on the three OPEN positions (PG=274, UBER=132, PANW=543) — phantom values matching premium collected. Cleared to 0.0 via direct UPDATE. Schema enforces NOT NULL so 0.0 not NULL. Dashboard wasn't displaying these because realized P&L queries filter to status IN (CLOSED, EXPIRED, ASSIGNED), but the dirty values would have caused incorrect accounting on actual close.

**Trade_sync reopen logic still incomplete:** at trade_sync.py:625-630 the reopen flips status and clears closed_at but does NOT clear realized_pnl. With wheel.py fixed, reopen shouldn't trigger for valid OPEN positions anymore — but if it ever does fire, the same bug returns. Defense-in-depth fix would be a one-line addition (set realized_pnl=0 on reopen). Deferred.

---


**Saturday (2026-04-25) early morning — realized_pnl on covered-call assignments:**

Dashboard showed Realized P&L = -$36,779.24 after PG/PANW/UBER assignments at expiry. Investigation found the loss was approximately the sum of stock sale proceeds at strike — meaning sale proceeds were being recorded as a loss instead of offsetting the assignment cost basis.

**Three accounting bugs found:**

1. **trade_sync.py:580-595** stock-close formula included ASSIGNMENT and CALLED_AWAY trade types in the realized P&L sum. These are IBKR accounting markers alongside the underlying BUY_STOCK/SELL_STOCK rows — not separate cash flows. Including them double-counted on the cost side and off-by-strike on the proceeds side.

2. **trade_sync.py timing race:** the code marked stock CLOSED in the sync that detected the IBKR position disappear, but the matching SELL_STOCK trade often arrived in a *later* sync. Realized got computed with only BUY_STOCK present, freezing at `-cost_basis`. Never recomputed afterward.

3. **wheel.py `_handle_called_away`** wrote its own realized_pnl with formula `(sale - cost + total_premium)`. But `cost_basis` was already net of put premium, AND `total_premium_collected` had already been realized when each option closed. Triple-counted on the premium side, undercounted on the put-strike side.

**Fix (commit 2e9708c):**
- trade_sync.py: sum only BUY_STOCK and SELL_STOCK (commission inclusive). Defer marking the position CLOSED until a matching SELL_STOCK trade is present in the DB. Single sweep handles both timing and accounting.
- wheel.py: stop writing realized_pnl in `_handle_called_away`. Just mark CLOSED. trade_sync owns the calculation. Single source of truth.

**Accounting model confirmed (per Ryan):**
- Total wheel-cycle realized = collected_premium + (call_strike − put_strike) × 100 − fees
- Stored on call positions (EXPIRED): the collected_premium portion (net of any roll buy-backs)
- Stored on stock positions (CLOSED): the strike-difference portion only

**Manual cleanup applied to today's three positions:**
- PG stock (id 130): -$421.24 (sold 146, bought 150, plus -$21 14-share roundtrip)
- UBER stock (id 134): -$100.00 (sold 74, bought 75)
- PANW stock (id 135): $0.00 (sold 162.5, bought 162.5)
- Call positions (id 131, 138, 139): kept at $539/$242/$543 — those were already correct
- Total historical realized after cleanup: $1,708.26

---


**Monday (2026-04-27) — put_seller stuck + IPO scanner silently broken:**

Ryan noticed put_seller making no suggestions despite 24% margin used and Asian/EU markets open. Two issues found:

**1. position_limit double-counting** (commit 3cb2930) — `check_position_limit` counted ALL Position rows including covered_call entries. A wheel cycle (stock + covered call) consumed 2 of 4 slots when it should consume 1. Account at NLV $15.5k has cap=4; with 2 wheels open the cap appeared full. Fix: count only `short_put` and `stock` types. Covered calls are bound to existing stock positions, don't take a slot.

**2. risk check order** (same commit) — position_limit was 5th in the check list. The first 4 checks made IBKR/FMP calls (~7s each), so a position-limit block took ~28s. The put_seller scanner classified any None-result that took >10s as a "connection failure" and aborted after 3 in a row (`scan_aborted_connection_dead`). Reorder: cheap DB-read checks (position_limit, duplicate, daily_limit, vix_gate) run FIRST. Slow IBKR/FMP checks only run if cheap ones pass. Risk-blocks now return in <1s, no more false connection-dead aborts.

**3. IPO scanner timedelta UnboundLocalError** (commit 5607adc) — `scan_ipo_calendar()` had `from datetime import datetime, timedelta` at module level (line 18) AND a redundant `from datetime import timedelta` inside a nested if-branch (line 168). Python's scope rules mark `timedelta` as a local variable for the entire function, masking the module-level binding. Line 92 (which uses timedelta unconditionally near top of function) errors before line 168 ever runs.

Result: IPO Date Calendar Scan job (8 AM ET daily) had been failing for at least 2 days with `cannot access local variable 'timedelta'`. Finnhub data never reached `expected_date` column on `ipo_watchlist`. IPO Ticker Scan loop's `if not ipo.expected_date: continue` guard skipped every row. Scan completed in 10ms doing nothing, every 5 minutes. Fix: remove the redundant local import.

**Post-restart verification (10:30 UTC scan):**
- Position-limit reorder works: scan completes in seconds with honest blocking reasons
- Current put_seller correctly blocked by cash-reserve constraint ($1,355 cash vs $2,328 reserve floor) and sector-concentration limit (Communications 100% > 88%)
- These are correct constraints for the current account state, not bugs
- IPO date scan will run at 8 AM ET (12:00 UTC) tomorrow — should populate expected_date for Kraken (KRKN), Lambda (LMDA), Dataiku (DIKU), Xanadu (XNDU) all with expected_date='2026-06-01' and now ~35 days out (still beyond the 1-day scan trigger window, but data flow restored)

**Discussion outcome on scoring (no code change):**
- Quality formula has been silently broken since FMP rename — valuation_score=50.0 for all 127 stocks because PE/PEG fields renamed in /ratios. Fixed Friday (commit 1905e04). Effect on next screener run.
- Composite formula 0.80×raw + 0.20×CQ% means max possible composite = 76. 70+ requires both timing AND quality alignment — rare by design.
- HCLTech surfaced at top despite 50/50/50 fundamentals. Backfilled fundamentals_complete=False on 13 NSE Indian stocks + 1 FWB2 dividend. Dashboard ⚠ icon now shows them as unverified.
- Ryan's $11M deployment concern: scoring is calibrated for "buy good companies on dips" (wheel-friendly). Not for steady accumulation of quality at fair value. Separate accumulate-signal would need different scoring (60-70% quality, 20-30% own-history valuation, 10-20% timing). Deferred.

---

**Monday (2026-04-27) afternoon — DTE bypass fix in _evaluate_symbol:**

Ryan noticed put_seller had filled four orders at wrong DTE: NVDA May 6 (9 DTE), PLTR/RKLB/DXCM May 8 (11 DTE). Config says 0-3 DTE for USD at low/mid VIX — these were way out of bounds. QCOM order also tried, canceled by IBKR.

**Root cause** (commit 63f68c6): `_evaluate_symbol` called `screen_puts()` without `dte_min`/`dte_max`. `screen_puts()` then fell back to `getattr(cfg, 'dte_min', 5)` and `getattr(cfg, 'dte_max', 14)` — but current `settings.yaml` has no `dte_min`/`dte_max` keys (only `.bak` does), so the hardcoded defaults 5-14 were used. That window perfectly covers May 6 (9 DTE) and May 8 (11 DTE).

`_process_symbol` had been correct all along — it called `_resolve_dte()` and passed both values into `screen_puts`. Only `_evaluate_symbol` was buggy. Fix mirrors `_process_symbol`: resolve DTE via `_resolve_dte(currency)`, halt on VIX-halt return, pass `dte_min=dte_min, dte_max=dte_max` into `screen_puts`. Both scan paths now correctly enforce DTE.

**Margin guard verified during investigation:** initial suspicion was margin guard had failed because account hit 68% margin used. False alarm. Logs showed ranks 1-6 passed with legitimate headroom ($12,171 down to $1,776), ranks 8-20 correctly rejected with `expired_no_margin`. The 4 fills consumed margin proportionally; the 68% used is the correct downstream result of those approved orders, not a guard failure.

**check_position_size cosmetic note (no code change):** variable named `estimated_margin` is misleading — it's notional concentration (price × 100), not margin. Real margin enforcement happens via `get_whatif_margin` in put_seller. Docstring already says this. Rename deferred to a future session.

**SHOP fully closed:** Ryan manually bought back $135 May15 call and sold 100 shares earlier today. Frees Maggy capacity once the four mistaken puts close.

**The 4 mistaken puts kept open:** NVDA May 6, PLTR/RKLB/DXCM May 8. Bug was upstream — these are filled, premium collected, accounting fine. Decision: let them ride. With DTE fix in place, no new violations from next scan onward. Monitor as expirations approach; close early if margin pressure forces it.

**Tuesday (2026-04-28) — account merge: this server now runs both Maggy and Winston on portfolio account:**

Test account U23886415 was decommissioned on this server and migrated to a clone codebase running on the same machine under user `nexbit` (Ryan's son). This server now runs Maggy and Winston code against U17562704 only, on the portfolio gateway (port 7496). Suggestion mode kept on for both, auto-approve toggle OFF for options (Ryan flipped it off Monday after the 4 unintended NVDA/PLTR/RKLB/DXCM fills).

**Config-only changes (no application code touched):**

- `config/settings.yaml` ibkr block: `port: 4001` → `7496`, `account: "U23886415"` → `"U17562704"`. Comments updated to flag merged mode.
- `~/restart-all.sh`: dropped the `tmux new-session -d -s options ...` block + 35s sleep. Added a "MERGED MODE" banner. Defensive `tmux kill-session -t options` in the kill block left in place (harmless cleanup).
- `~/watchdog-trader.sh`: commented out the options-gateway respawn block with a DISABLED note. Cron still runs the watchdog every 5 min, just skips options now. Portfolio + trader checks unchanged.

Both `~/restart-all.sh` and `~/watchdog-trader.sh` live outside the repo (in `$HOME`); `config/settings.yaml` is gitignored due to API keys. Backups stored as `~/restart-all.sh.pre-merge-2026-04-28` and `~/watchdog-trader.sh.pre-merge-2026-04-28` for clean revert when the new dedicated options account arrives.

**Migration sequence (executed):**

1. settings.yaml patched (matches found: 1, written)
2. restart-all.sh patched (matches found: 1, written)
3. ~/restart-all.sh executed — one 2FA tap on phone, portfolio gateway came up on 7496
4. Trader app started, both Maggy code and Winston code connected to U17562704 (clientId 12 and 97 respectively)
5. Son started clone server, took over U23886415

**Hiccup during cutover — son couldn't log in:**

Watchdog cron (`*/5 * * * *`) respawned the killed options tmux session within 3 minutes, holding the U23886415 IBKR session and locking son out. Patched watchdog to skip the options check, killed the respawned options session, son immediately logged in to U23886415 on his clone. 2FA prompts to son's phone (from this server's repeated respawn attempts) stopped after the watchdog patch.

**Open issues from the merged setup (non-blocking, deferred to next session):**

- `'Position' object has no attribute 'unrealized_pnl'` — fires on every put_seller scan (ISRG, CRWD, CNR, AVGO, SOFI, NFLX, etc.). Maggy code expects an attribute the U17562704 Position objects don't have. Likely a 2-line `getattr(pos, 'unrealized_pnl', 0)` fix once we read the code.
- `trade_sync_fetch_error 'This event loop is already running'` + `'There is no current event loop in thread Thread-2'` — asyncio contention. Two ib_insync clients (id 12 + 97) on one Python process amplifies the existing event-loop fragility.
- `reconcile_submitted_trades_skipped_ib_error "name 'get_ib_lock' is not defined"` — missing import surfaced by the merge.
- `portfolio_account_updates_failed` (TimeoutError on `reqAccountUpdates`) — fired once at startup. May be one-time, watch for recurrence.

None of these block the merged setup operationally because Maggy is in suggestion mode with auto-approve OFF — at worst, no options suggestions reach the dashboard. Winston (read-only) is unaffected.

**Architecture note for next dedicated options account:**

To re-split: revert the three patches above (port back to 4001, account back to new ID, restart-all.sh options block restored, watchdog options block uncommented), point at new gateway, run restart-all.sh. All three backup files preserved on disk for direct comparison.

**Tuesday (2026-04-28) evening — dashboard authentication + localhost lockdown:**

Dashboard at http://37.0.30.34:8080/ was publicly accessible with no authentication. Anyone with the URL could see positions, NLV, suggestions. Closed via Caddy reverse proxy with HTTP basic auth + self-signed HTTPS cert.

**Architecture:**

- Caddy 2.11.2 installed from official repo (apt) — public-facing reverse proxy on ports 80 and 443
- Self-signed TLS cert auto-generated for 37.0.30.34 (caddy `tls internal`) — browser warns once per device, click through, remembered after
- Basic auth user `maggycian` (bcrypt cost 14, hash stored in /etc/caddy/Caddyfile)
- HTTP (80) redirects to HTTPS (443)
- Caddy reverse-proxies authenticated requests to localhost:8080 (the trader app)
- Trader app web binding moved from `0.0.0.0:8080` to `127.0.0.1:8080` — localhost only — so the world cannot bypass Caddy by hitting `:8080` directly
- ufw firewall: ports 80 and 443 added (were missing — caused initial "can't connect" from Safari until added)

**Files modified:**

- `/etc/caddy/Caddyfile` — caddy config (root-owned, sudo to edit). Backup at `/etc/caddy/Caddyfile.default-2026-04-28`
- `/var/log/caddy/dashboard-access.log` — access log (caddy:caddy ownership)
- `config/settings.yaml` web block: `host: "0.0.0.0"` to `"127.0.0.1"` with merge-mode comment

**New URL:** https://37.0.30.34/ — username `maggycian`, password set via `caddy hash-password`.
**Old URL dead from outside:** http://37.0.30.34:8080/ — connection refused for non-localhost. Caddy still uses it internally.

**Verification confirmed:**

- `ss -tlnp` shows `127.0.0.1:8080` (python), `*:443` (caddy), `*:80` (caddy)
- `curl -k -i https://37.0.30.34/` without credentials returns `HTTP/2 401 Unauthorized` (auth enforced)
- `curl http://37.0.30.34:8080/` from outside times out (lockdown enforced)
- Authenticated access via Safari + curl works end to end

**Known cosmetic issue:** Safari may hang on first visit to the self-signed cert page until cache is cleared or page is reopened. Other browsers behave normally.

**Wednesday (2026-04-29) — KSPI fill-claiming clarification + watchlist metrics asyncio fix (Bug B + Stages 1-5):**

KSPI buy-back triggered a discovery: post-merge fill-claiming. Then a deeper investigation revealed the watchlist metrics job had been silently failing every 4h since the merge. Five commits address it.

**KSPI fill-claiming (architectural note, no code change):**

Ryan placed a manual buy-back limit order on a long-standing Winston cash-secured put (KSPI 70 strike, June 18 expiry). Order filled at 0.88. Phone notified, but the buy-back showed only on Maggy's trade history, not Winston's transactions or trade-history pages. Winston's open-position counter correctly went 53 to 52, but the realized P&L of 888 landed in positions (Maggy's table) instead of portfolio_put_entries (Winston's table).

Root cause: post-merge, all IBKR fills arrive on the same shared connection with no strategy tag. Whichever sync code runs first claims the fill. Maggy's trade_sync runs every ~5 min; Winston's runs every 4 hours. Maggy almost always wins. The KSPI position was originally placed manually in IBKR (not via dashboard), so Winston never had a portfolio_put_entries record — only the open-counter knew about it. Maggy's sync ran first after the merge, found the open IBKR position with no DB match, created a fresh positions row claiming it as Maggy's. Today's buy-back closed Maggy's record cleanly. Display split is annoying, accounting is correct.

Decision: not fixing now. Real fix would tag fills by strategy at sync time — non-trivial. Listed in deferred bugs. Will be moot once the new options account arrives and the strategies separate again.

**Bug B fixed — get_ib_lock missing import in trade_sync (commit 877426d):**

reconcile_submitted_trades() at line 414 of src/broker/trade_sync.py called "with get_ib_lock():" but the import at line 14 only imported get_ib and is_connected. NameError on every reconcile run, swallowed by except as reconcile_submitted_trades_skipped_ib_error. Surfaced post-merge because trade_sync now runs more frequently against the shared gateway.

Fix: added get_ib_lock to the import. Verified clean by absence of the error in 14:25, 14:40 reconcile cycles after restart.

**Watchlist metrics investigation:**

User asked to verify update_watchlist_metrics was running on schedule. Found it WAS — every 4h at 00:43, 04:52, 08:54, 12:54 — but the last 3 of those failed catastrophically: failed=124 updated=0. All 124 watchlist symbols failing in lockstep meant a connection-wide issue. Confirmed via "event loop is already running", "no current event loop in thread Thread-2", spy_ma_fetch_error, price_fetch_error errors clustering around the failed runs.

Root cause: post-merge, two ib_insync clients (Maggy clientId 12, Winston clientId 97) hit the same gateway through the same Python process. Pre-merge they had separate gateways; collisions were physically impossible. Post-merge, every overlapping IBKR call is a race.

Maggy already had _ib_lock infrastructure used consistently. Winston had _portfolio_lock defined but barely used — only at 2 sites in connection.py and 6 sites in scheduler.py. Roughly 26 IBKR call sites across connection.py, buyer.py, analyzer.py, forecaster.py, sync.py, ibkr_fundamentals.py, bridge.py were unlocked.

Two-layer fix architecture:
- Layer 1 (Stages 1-3): Wrap every Winston IBKR call site with get_portfolio_lock(). Winston serializes its own calls.
- Layer 2 (Stages 4-5): For the merge period, get_portfolio_lock() returns a supervisor that acquires Maggy's ib_lock FIRST, then Winston's _portfolio_lock. Cross-strategy serialization without touching Maggy code.

**Stage 1 (commit ff631e2) — connection.py:** Wrapped refresh_portfolio_account_cache_from() accountValues() and refresh_brkb_history() reqHistoricalData(). Connection-setup code at lines 115/144 intentionally not wrapped (no contention possible).

**Stage 2 (commit baa533d) — buyer.py:** 21 IBKR call sites wrapped as 16 lock blocks (per logical operation). Sites: VIX/SPY regime fetch, option chain discovery, option qualify+live bid sequence, place put order, assignment check, place stock buy, cash park sequence, three account-value queries, holdings update loop.

**Stage 3 (commit 59b3197) — analyzer.py, forecaster.py, sync.py, ibkr_fundamentals.py, bridge.py:** 8 lock blocks across 5 files. After Stage 3, every Winston IBKR call site holds the portfolio lock during the call.

**Stage 4+5 (commit 392369b) — Cross-strategy supervisor:** Replaced get_portfolio_lock() with a context-manager-returning function. When merged (detected once at module import by comparing settings.ibkr.host/port/account vs settings.portfolio.ibkr_host/ibkr_port/ibkr_account), it acquires Maggy's ib_lock FIRST, then _portfolio_lock. When split, returns plain _portfolio_lock. Lock acquisition order is fixed (ib_lock then portfolio_lock) and Maggy never acquires portfolio_lock, so no deadlock. Logged at startup as portfolio_lock_mode merged=True.

When the new options account arrives and ports diverge, _detect_merged_with_options() returns False automatically. Supervisor becomes a no-op without code change. Removal instructions for permanent re-split are inline in connection.py under the MERGE-ONLY header.

**Verification:** Restart at 19:46:10 logged portfolio_lock_mode merged=True confirming supervisor is active. Next watchlist metrics run is at ~23:46. If failed=0 updated=124, asyncio race is dead.

**Open issues (still deferred):**

- "Position object has no attribute unrealized_pnl" in put_seller — Maggy code expects an attribute U17562704 Position objects don't have. Harmless (suggestion mode + auto-approve off), but log noise.
- KSPI-style fill claiming — first-sync-wins behavior across strategies. Real fix is per-strategy fill tagging. Will be moot post-re-split.
- portfolio_account_updates_failed (TimeoutError on reqAccountUpdates) — still seen at startup. May or may not be lock-related.

**Saturday (2026-05-03) — reconnect-race fix + scoring rebalance + pending orders fix:**

Three lines of work today, all pushed.

**1. Reconnect-race fix (commit b734cf7):**

Yesterday's lock work proved itself: 03:49 watchlist metrics run logged failed=0 updated=124. Asyncio race against the metrics job is dead.

But discovered a related bug overnight. IBC restarted the gateway at midnight UTC; trader reconnected cleanly at 00:01, ran 03:49 metrics fine, then connection dropped around 05:46. Reconnect attempts started failing with "This event loop is already running" every 5/10/20s, looped for 47 min until manual restart at 06:35. 195 failed-reconnect log entries.

Root cause: _connect() in src/broker/connection.py calls ib.connect() which internally invokes asyncio.get_event_loop().run_until_complete(...). If a Winston thread is mid-call holding _ib_lock via the merge-period supervisor, the new asyncio task can't run. Yesterday's lock work protected Winston's CALLS but not Maggy's RECONNECT against Winston's calls.

Fix: wrapped ib.connect() plus the post-connect setup (RequestTimeout, reqMarketDataType, sleep) in "with _ib_lock:". Lock is RLock so safe even if called from a thread already holding it. Reconnect now waits for any in-flight Winston operation to release the lock before grabbing the event loop.

**2. Scoring rebalance — Buffett-style (commits a25ccb1, 45e6d72, f65b9d4):**

User concern: dashboard top sat at 45-65 score range, never reaching 70+ direct-buy threshold except in panic. 75 of 124 stocks at raw_score=0, 25 at exactly 40. Top stable for days. System effectively "panic-buy or never."

Architecture investigation revealed: the screener (tools/screen_universe.py) already does Buffett-style work properly — _score_growth (40%, revenue + gross margin level + trend), _score_valuation (25%, PEG-first with PE fallback), _score_quality (35%, D/E + FCF consistency + FCF margin trend). Composite 0.40*growth + 0.25*valuation + 0.35*quality. Already calibrated for "wonderful business at fair price." OFF-LIMITS to changes per explicit user guardrail.

The downstream scoring was the problem. analyzer.py:_compute_composite_score was purely a panic detector (SMA-discount + RSI-oversold + 52w-low gates). If no gate fired, returned 0. Most quality stocks at fair valuation hit no gates, scored 0. The composite blend was 80% raw + 20% quality, so quality couldn't lift them. And analyzer.py was setting composite_score = score directly without ANY blend — discrepancy with recalc_scores_from_db which used 80/20.

Three changes restored Buffett-style behavior:

a25ccb1 — Two simultaneous changes:
- Added a fair-price base of 0-24 points to _compute_composite_score, scaled across discount_pct from -5% (above SMA, 0pts) to +5% (below SMA, 24pts). Saturates exactly where the existing gated SMA signal takes over. Stocks at fair valuation now have a foot in the door even without panic-level signals. Anti-chase guard at -20% still blocks deeply overpriced stocks.
- Composite blend: 80% raw + 20% quality to 30% raw + 70% quality. Applied symmetrically in _evaluate_symbol (was no blend at all — discrepancy fixed) and recalc_scores_from_db.

45e6d72 — Composite floor: stocks below MIN_COMPOSITE_FOR_ACTION = 40.0 don't get buy_signal=True. Filters out fair-priced stocks with weak quality (whose composite was lifted only by fair-price base). At score=0, the 0.70*quality_pct term means floor=40 is roughly quality_pct >= 57. Below that, watchlist-only, no CSP suggestion.

f65b9d4 — Direct-buy threshold bumped from 70 to 75. Under the new 30/70 blend, composite=70 was reachable by top-quality stock at exact-SMA price (zero technical signal). Bumping to 75 ensures every direct-buy candidate has raw_score >= 15 — some real price-side reason to act, not pure quality lift.

**Resulting action mapping:**
- Below 40: watchlist member, no action
- 40-75: sell CSP at target strike (get paid to wait — most fair-priced quality stocks land here)
- Above 75: direct buy (rare, requires both real signal AND high quality)
- Override gates (deep_discount > 15%, RSI < 20, volume_surge + trend_healthy) still promote to direct_buy regardless

**3. Pending orders dashboard fix (commit 2c161a8):**

User wanted Pending Orders view to reflect IBKR state in near-real-time, with order lifecycle (Submitted, PartiallyFilled, Filled disappears to Holdings, Cancelled disappears entirely) handled cleanly.

Audit revealed most of the lifecycle infrastructure already exists:
- refresh_portfolio_pending_orders_cache() captures all the right fields (status, filled, remaining, order_id, etc.)
- Uses reqAllOpenOrders() so it sees orders from all clients including manually placed TWS orders
- trade_sync handles fills (moves to Holdings/Open Options) and ghost detection (rejected to CANCELLED)
- Dashboard renders the table with status column

Two real issues found:
1. Template variable mismatch: {{o.quantity}} in portfolio.html, but cache stores it as 'qty'. Qty column silently empty on dashboard.
2. Cache refreshed only every 15 min (inline with _job_trade_sync). Freshly placed orders invisible for up to 15 minutes.

Fixes (commit 2c161a8):
- Template: {{o.quantity}} to {{o.qty}}
- Trigger refresh_portfolio_pending_orders_cache() immediately after each placeOrder + sleep block. Three sites: CSP (line 695), direct buy (line 1074), cash park (line 1149). Wrapped in try/except so dashboard issues never break order placement.

Result: newly placed orders appear on the dashboard within ~2 seconds.

**Architectural state confirmation (no change, but worth recording):**

Portfolio IBKR connection (clientId 97) remains in **read-only mode at IBKR level** — even if app-level suggestion_mode is flipped off, IBKR rejects placeOrder at the protocol level. This is a deliberate two-layer safety:
- IBKR side: read-only (protocol-level lockout)
- App side: suggestion_mode + auto-approve OFF

For Winston to ever execute, BOTH switches need to be flipped deliberately. Today's scoring/lifecycle work prepares for that future state but doesn't enable it.

**Deferred (still on the list):**

- DB-write timing in buyer.py: holdings/transactions row written at submission time, before fill confirmation. If IBKR rejects, DB has phantom row until trade_sync's next reconcile cycle (15 min). Real fix is to write only on fill. Not blocking under current read-only/suggestion-mode operation. Worth fixing before any auto-approve live mode.
- Maggy unrealized_pnl AttributeError on U17562704: every put scan on this server errors at src/strategy/risk.py:1224 reading pos.unrealized_pnl from IBKR Position objects. The Position model in src/core/models.py has no such column — code expects IBKR's unrealizedPNL attribute on objects from ib.positions(), but U17562704 (post-merge portfolio gateway, read-only) doesn't populate it. Same code wrote 4 puts successfully against old options account U23886415 on April 27 — proves the bug is account-type/permission-specific, not a code defect. When Maggy gets a new independent options account configured like U23886415 (standard options trading account with real-time market data subscription), the existing code should work without patching. If the new account also fails, fix is a one-line `getattr(pos, 'unrealizedPNL', 0.0)` at risk.py:1224. Until then, Maggy on this server scans uselessly every cycle and generates zero suggestions — cosmetic-only on this server because Winston is the active strategy here. Son's clone runs the real Maggy on U23886415 and is unaffected.
- KSPI-style fill claiming — first-sync-wins post-merge. Per-strategy fill tagging needed. Will be moot post-re-split.

**Verification:**

- 21:22 trader restart logged clean: ibkr_connected, portfolio_connection_established, portfolio_ibkr_ready. Lock supervisor still active (merged=True from yesterday).
- First metrics + recalc cycle after the scoring change runs at ~01:22 UTC, then 05:22, 09:22, 13:22 etc.
- Tomorrow's first verification: sqlite3 query on portfolio_watchlist ordered by composite_score DESC, expected top 60-90 range (vs yesterday 45-65), quality leaders dominate, possibly 0-3 direct-buy candidates if any pass the 75 threshold. Pending Orders dashboard view: when a manual order is placed in IBKR by Ryan, should appear within ~2 seconds, Qty column showing actual quantity.

---

## All Recent Commits (last 4 days, all pushed)

**Today (2026-05-05):**
- 10ef0a4 database: remove problematic account_id backfill
- 3b407ff capital_injections: per-account deposit tracking + deposit-aware graphs

**Previous (2026-05-03 to 2026-05-04):**

- 2c161a8 pending orders: fix qty template var + trigger refresh after placeOrder
- f65b9d4 scoring: bump direct-buy threshold from 70 to 75
- 45e6d72 scoring: add composite floor of 40 for buy_signal trigger
- a25ccb1 scoring: rebalance composite to 30% raw + 70% quality, add fair-price base
- b734cf7 fix: hold _ib_lock during ib.connect() to avoid reconnect-vs-Winston race
- 392369b Stage 4+5: cross-strategy lock supervisor for merged accounts
- 59b3197 Stage 3: lock IBKR calls in analyzer, forecaster, sync, fundamentals, bridge
- baa533d Stage 2: lock IBKR calls in portfolio/buyer.py
- ff631e2 Stage 1: lock IBKR calls in portfolio/connection.py
- 877426d Fix: import get_ib_lock in trade_sync.py
- 63f68c6 Fix: pass DTE range to screen_puts in _evaluate_symbol (was using hardcoded 5-14 fallback)
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
- **dd16ec6 Wheel: don't mark calls EXPIRED on expiry day (incomplete fix, superseded)**
- **b41e39a Wheel: defer call-expired status to trade_sync, detect early exercise via shares drop**

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

### Augmentation feature — next steps
- **Live test (highest priority)** — flip `AUGMENTATION_ENABLED = True` in `tools/screen_universe.py`, manually trigger one screener run via dashboard "Run now". Cost ~$0.40 Anthropic API + ~50 FMP calls. First time the prompt exercises real data. Watch for: prompt issues, JSON parse edge cases, scoring-time exceptions on proposed symbols. Verify `augmentation_audit` table fills with rows; verify `discovered_pool.yaml` gets accepted symbols.
- **Optional Commit N** — `--dry-run-augmentation` flag: invoke Claude proposals + scoring without persistence/audit writes. Useful for prompt iteration without polluting audit/yaml. Nice-to-have, not required.

### Carry-over from forward-growth work
- **Commit E** — flip `portfolio_score` formula to use `forward_growth_score`. Currently the field is computed and stored but `portfolio_score` still uses old `40g + 25v + 35q`. Deliberate observation period — accumulate runs to compare old vs new ranking before flipping. Earliest sensible: after 2-3 more screener runs.

### Other
- **Breakthrough prompt v4 — geographic spread fix.** Today's screener returned only 1 non-USD listing (Sony 6758) vs target 5+. Need stricter geographic distribution constraint in the prompt.
- **Watchlist dividend NULL backfill** — 9 dividend holdings still NULL on `dividend_total_return_score`. Phase 2b populates on next screener run; verify after augmentation live test.

### Lower priority
- Scorer fail-closed behavior — screener side largely self-corrects, metrics side now via staleness. Defer.
- 3-consecutive-failure exchange skip — dormant, leave alone unless it bites again.
- GBP centralization — 8+ separate sites in codebase divide by 100 for GBP. Surgical fix landed at analyzer.py on May 7; centralization deferred.

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

## Thursday (2026-05-07) — earnings gate, LSE pence, JSON history export, two open items for son's clone

**Four commits pushed, restart pending:**

1. `974b538` — Added `EarningsCache` model (symbol PK, status, next_earnings_date, fetched_at) for 24h cache backing the earnings gate. Auto-creates via `Base.metadata.create_all` at next startup.
2. `4cf7630` — Added `get_next_earnings_date(ib, contract)` in `src/portfolio/ibkr_fundamentals.py`. Mirrors existing `ReportsFinSummary` pattern — same lock acquisition (`get_portfolio_lock`), same XML parse, same exception shape. Calls `ib.reqFundamentalData(contract, "CalendarReport")`. Returns `EarningsResult(next_date, status)` with three explicit states: `found`, `none_scheduled`, `fetch_failed`. Tries `<EarningsAnnouncement Date="...">` first, falls back to `<EPSDate>` children if format differs.
3. `58c6a55` — Replaced the always-False `has_upcoming_earnings()` stub in `src/broker/market_data.py` with real implementation. **Fail-CLOSED on missing data** (no IB / qualify failure / fetch failure / parse failure all return True = block). 24h DB cache; cached entries auto-invalidated when their date passes. Three states from `get_next_earnings_date` map to: `found` + within 3 days → block, `found` + outside window → allow, `none_scheduled` → allow, `fetch_failed` → block.
4. `95f7d67` — LSE pence normalization at source in `src/portfolio/analyzer.py`. IBKR returns LSE prices in pence; analyzer was storing raw pence into `analysis.current_price`, `sma_*`, `52w_high/low`. Now normalized once where `closes`/`highs`/`lows` are extracted from bars, before any computation. All downstream metrics inherit correct units. AZN was the symptom (showed 13552 instead of 135.52); fix is universal for any GBP symbol. **Eight other GBP-handling sites** in the codebase exist (screener.py:35, put_seller.py:440, trade_sync.py:321, etc.) — surgical fix at this site, no centralization. Centralization deferred.

**Architecture note: earnings gate is now fail-CLOSED, opposite to most gates.** Rationale: earnings is the single most predictable cause of overnight gap risk on a CSP. Better to skip a trade than mis-trade through earnings. VIX gate, MA gate, and most others remain fail-OPEN.

**JSON history export built (not committed to repo, lives at /tmp/options_history_export.json + ~/options_history_export.json):**

`tools/export_options_history.py` (drafted, run from /tmp): exports pre-merge Maggy-side data from your DB for handoff to son's clone. Window 2026-02-22 to 2026-04-28, FILLED trades only. Trades scoped by (symbol, strike, expiry) match against in-window positions because `Trade.position_id` was unpopulated on most historical rows (only 7 of 92 had FK link). Output: 37 positions, 92 trades, 127 events with running realized P&L, total $2,964.76. JSON file ~126 KB, ready to hand off to son.

Import script (Script B) **not yet written** — waiting for son's schema diagnostic + existing-rows snapshot. He will run two read-only checks on his clone, paste output, then we write the import tailored to his actual schema (since his fork may have diverged).

**Investigations that did not become commits:**

- **Asia/EU put scan question (deferred to son's clone)**: This server's diagnostic shows scans run correctly, hit AEB and LSE, evaluate ASM/ASML/AZN — but every symbol gets blocked by `Position limit reached: 23/15`. Not a bug per se on this server, but the cross-strategy position-counting on the merged Maggy+Winston `positions` table makes the diagnostic meaningless for the real options-trader account. Son's clone (clean U23886415, separate `positions` table) is the only place this can be validated. Diagnostic prepared and ready to forward.
- **NVDA realized P&L = 0 on son's dashboard (deferred to son's clone)**: His DB has only `BUY_PUT @ 0.0` (the IBKR expiry-recognition row), no SELL_PUT, because the original April 27 sale fell in your server's gateway session and never reached his `ib.fills()` after cutover. trade_sync's expiry handler queried the Trade ledger, summed to 0, wrote 0. Once written, no recovery path. Possible defensive fixes (defer-marking-EXPIRED-when-no-SELL, fallback to total_premium_collected, defer-position-synthesis-when-avg_cost-zero) discussed and rejected: would not have prevented son's specific case (no SELL row anywhere) and would risk corrupting your already-correct data. JSON history import is the right path for son. **No code change made on your server for this.**
- **Watchlist staleness alarm**: Investigated. All 129 rows have `metrics_stale=0`, last update 2.9h ago — well within 4h cycle. The "looks stale" feeling is the new May 3 30/70 scoring blend producing stable scores dominated by slow-moving quality (70% weight), correctly per design. The 06:56 and 12:13 partial-failure metrics cycles were restart artifacts (manual restarts that day), not regressions of the asyncio race fix.
- **Six historical positions with realized_pnl=0, total_premium_collected>0**: Surfaced during CRWV review. PANW/UBER/SHOP/TTD ASSIGNED puts on March 29 (predate April 25 commit 2e9708c stock-close fix), PANW stock CLOSED on April 25 (possibly correct at break-even), COIN covered_call EXPIRED on March 9 (pre-everything). ~$1,339 in unrecognized P&L on the dashboard. **Decision: do not fix on this server.** Merged-mode data is mixed pre-merge Maggy + post-merge Winston; manual UPDATEs now would risk correcting numbers that should be on the other side of the future re-split. Wait for new options account, separate the data, re-evaluate.
- **CRWV id=152 stuck OPEN despite May 6 buy-back**: Identified but not pursued; same merged-mode-data caveat applies.

**Two open items for son's clone (priority order for next session):**

1. **Asia/EU put scan validation**: needs his diagnostic on U23886415 to confirm whether scans produce zero suggestions due to legitimate market mechanics (live-quote gate, position limits, etc.) or due to a real bug (universe filter mismatch, market label issue, gate firing only against US data).
2. **NVDA P&L recovery**: needs JSON history import (Script B, to be written after his schema diagnostic). Will rewrite his NVDA Position row with realized_pnl=196 from your export.

**Bundle for son contains:**
- `~/options_history_export.json` (126 KB, the JSON export)
- Schema-check + existing-rows diagnostic (drafted, ready to send)
- Asia/EU scan diagnostic (drafted, ready to send)
- Note that import script Script B is coming once he sends his schema output

**Verification queue when restart happens:**
- earnings_cache table auto-creates ✓ check `sqlite_master`
- `has_upcoming_earnings('DDOG')` returns True via real CalendarReport ✓ check verification block
- DDOG cache row populated with valid status ✓ check earnings_cache
- AZN current_price normalizes to ~135.52 on next 4h metrics cycle (not immediately at restart)
- Watchlist log entries should show clean cycles after restart, no asyncio race

## Friday (2026-05-08) — Forward-growth scoring landed + augmentation pipeline complete (22 commits)

**Big day. Two major themes:** built the 5-component forward-growth scoring system (Path A refactor) and the full Claude-driven augmentation pipeline. Plus son's clone JSON import script delivered.

### Forward-growth scoring (commits f55d8b2 → 940507a, ed76fad)

Replaces the old `40g + 25v + 35q` portfolio score formula with a Buffett-style composite weighted across 5 sub-components: revenue durability (25%), compounding quality (25%), operating leverage (20%), innovation investment (15%), capital efficiency (15%). **Hard cap at 30** if a name has 3+ years negative net income AND 3+ years negative FCF over a 5-year window.

Implementation in 4 commits (Path A):
- `d5dedf6` — Commit A: extended `_get_fmp_fundamentals()` to extract 5-year history fields (operating_margin, R&D intensity, share dilution, ROIC sustained, goodwill stability, FCF trend, neg-NI/neg-FCF year counts). NO new API calls — all derived from existing income/balance/key-metrics responses.
- `f285358` — Commit B: 5 sub-scorer functions with explicit-value sector lookup for 23 distinct sectors observed in watchlist. Score breakdowns documented in code comments.
- `449402a` — Commit C: `_score_forward_growth(fmp, sector)` aggregator. Smoke test: NVDA=80.5, MSFT=77.5, AAPL=68.0, JNJ=59.5, XOM=33.0.
- `940507a` — Commit D: wired `forward_growth_score` into screener flow. Stored on StockScore. **Does NOT yet replace `portfolio_score` formula** — that's Commit E (deferred to observation period).

Plus `699900b` (Commit O) — preserve all 5 sub-scores on StockScore so the augmentation prompt can show them per-name. Without this, augmentation would prompt with all-zeros for sub-scores.

**Screener run after these landed (May 8, dashboard "Run now"):** 133 rows populated, range 11.8–89.2, avg 52.5. Top-20 by forward_growth_score: MA 89.2, ASML 88.8 (+21 vs old), LLY 86.6, META 86.5, KLAC 85.7, ANET 84.5, TSM 83.5, GOOG 83.0, NFLX 80.7, MSFT 80.5, NVDA 80.5 (-10.5 vs old, dilution + cyclical risk tempers), ISRG 80.5, RACE 79.8, ABNB 79.5, NVO 79.0, BKNG 76.2, V 75.9, CDNS 75.3, FSLR 73.0. Picks-and-shovels representation jumped from zero to ~10 names.

### Augmentation pipeline — full feature complete (commits 6a9ba4a → 49b6785)

Goal: monthly screener invokes Claude to propose 5–10 high-conviction names beyond the hand-coded universe, scores them, accepts those that beat the rank-60/rank-15 cutoff. **All gated behind `AUGMENTATION_ENABLED = False` — default OFF, no API calls until manually flipped.**

Foundation:
- `6a9ba4a` (F) — `tools/discovered_pool.yaml` empty file with growth+dividend tiers + `_load_discovered_pool()` loader
- `6029b4f` (G) — `tools/evicted_names.yaml` empty + `_load_evicted_names()` loader
- `5ed25f0` (H) — `_get_growth_universe()` / `_get_dividend_universe()` helpers; routed 5 universe iteration sites through merged pools (CANDIDATE_POOLS + discovered − evicted). Verified equivalent to original behavior with empty yamls.
- `ed76fad` (I) — `AugmentationAudit` SQLAlchemy model added to `src/portfolio/models.py`. Eleven columns: id, run_date, tier, proposed_symbol, proposed_score, cutoff_score, displaced_symbol, displaced_score, accepted, reason, notes. Table auto-creates on next restart via `Base.metadata.create_all` (verified — table exists on this server).

Logic:
- `081ad9c` (J+K) — `_get_growth_swaps()` + `_get_dividend_swaps()` + shared `_call_claude_for_swaps()` helper + `_format_score_table_for_prompt()` + `_AUGMENTATION_RUBRIC_SUMMARY` constant + `_build_growth_augmentation_prompt()` / `_build_dividend_augmentation_prompt()`. Direct text + JSON parse, max_tokens=4000, includes top-60 + ranks 61–120 + rubric + exclusion list.

Orchestration:
- `c79d431` (L) — `_process_augmentation_proposal()` helper + `AUGMENTATION_ENABLED` flag (False) + PHASE 2.5 block in `screen_all`. PHASE 2.5 runs between PHASE 2 (breakthrough scan) and PHASE 3 (portfolio universe build). Splits non-breakthrough scores into growth/dividend pools using the same yield-routing rule as PHASE 3, identifies top_60/top_15 + cutoff, calls Claude, processes each proposal (score round-trip via `_score_stock`, accept if score > cutoff with margin=0, audit-log every proposal), opens SQLAlchemy session via `get_session_factory()`. Best-effort: any exception inside PHASE 2.5 is caught, logged, augmentation skipped, screener continues normally.

Persistence + hygiene:
- `2df64cd` (L+) — `_persist_augmentation_acceptances()` writes accepted symbols to `discovered_pool.yaml` atomically (.tmp + rename). Schema per entry: `symbol, exchange, currency, region, score, added_date, thesis`. Buffer (`pending_yaml_additions`) populated during proposal processing, written once after `audit_session.commit()`. Without this, accepted names would only exist in this run's `all_scores` and disappear next month.
- `49b6785` (M) — `_evict_overflow_from_discovered_pool()`. When pool > cap (180 growth / 45 dividend), sort by score desc, slice to `[:cap]`, log evicted symbols. Atomic write. Eviction is list-size hygiene only — not a quality verdict.

**Architecture decisions made today:**
- Two pools (discovered_growth, discovered_dividend), separate, no yield-routing for discovered names.
- Every symbol evictable — hand-coded names included, no editorial floor.
- Eviction triggers only when pool > cap; no K-parameter, no consecutive-runs logic.
- General augmentation (not slot-specific) — Claude proposes 5–10 high-conviction names, not "replace these specific laggards."
- Margin = +0 (any improvement over rank-60 cutoff accepts). Easy to tune to +3 later if churn is excessive.
- Eviction file at `tools/evicted_names.yaml` (NOT auto-edit source code).
- discovered_pool.yaml lives in source tree (committed).
- Strict failure handling on FMP miss (no retry). Audit row written with reason="scoring_failed".
- Audit trail = SQLite table `augmentation_audit`.

### Other commits today
- `f55d8b2` — Anthropic API timeout 30s → 120s in `tools/screen_universe.py:439` (breakthrough prompt v3 takes ~80s).
- `a23f611` — Breakthrough prompt v3: dynamic CANDIDATE_POOLS exclusion, geographic fix, top-20 hard exclusion, ETF/Fund pattern, existence check.
- `f065259` — Added BAP (Credicorp Peru) and CHT (Chunghwa Telecom Taiwan) to `ADR_DIV` section of DIVIDEND_CANDIDATES.

### Son's clone — solved

Built standalone Script B for importing pre-merge history into son's clone DB. Single-file Python (~150 KB with embedded JSON, no separate data file needed). Implements: position match by (symbol, strike, expiry, opened_at); hard-skip when both his DB and export show OPEN (his side wins); update close+P&L when his is OPEN and export is CLOSED/EXPIRED/ASSIGNED; insert when no match; trade dedup by ibkr_exec_id then natural key; position_id remapping via dict; default dry-run (must pass `--apply` to write); atomic transaction; clean summary report.

File at `/tmp/import_options_history_standalone.py` on this server. Sent to son via email attachment. **Resolved.**

### Pending for next session

**Augmentation:**
1. **Live test** — flip `AUGMENTATION_ENABLED = True`, manually trigger one screener run. Cost: ~$0.40 Anthropic API + ~50 FMP calls. First time the prompt will be exercised against real data — expect potential prompt issues, JSON parse edge cases, scoring-time exceptions for proposed symbols.
2. **Optional Commit N** — `--dry-run-augmentation` flag for sanity-checking prompts without persistence. Nice-to-have.

**Other carry-over:**
- **Commit E** — flip `portfolio_score` formula to actually USE `forward_growth_score`. Currently the field is computed and stored but `portfolio_score` still uses old `40g + 25v + 35q`. Deliberate observation period — accumulate runs to compare old vs new ranking before flipping. Earliest sensible: after 2-3 more screener runs.
- **Breakthrough prompt v4** — geographic spread fix. Today's screener returned only 1 non-USD listing (Sony 6758) vs target 5+.

### Critical working facts (unchanged from May 7, restated)
- All code changes via copy-paste terminal commands; Ryan is non-programmer.
- Strict fix→verify→commit per RULES.md.
- View files before editing; write patches via Python script in /tmp; ast.parse before write; idempotency guards.
- `cd ~/automatic_option_trader` before `.venv/bin/python3`.
- `_score_stock` is the screener's per-symbol scoring entrypoint; called both by regular screening and by augmentation acceptance.
- Augmentation audit columns confirmed: id, run_date, tier, proposed_symbol, proposed_score, cutoff_score, displaced_symbol, displaced_score, accepted, reason, notes (all 11).
