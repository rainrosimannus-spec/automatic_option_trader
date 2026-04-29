# Maggy & Winston — State Document

Last updated: 2026-04-29 (Wed) — Bug B fix + Stages 1-5 portfolio lock supervisor for merged-account asyncio race.

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

---

## All Recent Commits (yesterday + today, all pushed)

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
