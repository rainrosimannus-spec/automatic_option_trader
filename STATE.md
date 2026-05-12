# Maggy & Winston — State Document

Last updated: 2026-05-12 — Restructured into L1/L2/L3 by access pattern. Content preserved from prior chronological version.

**How to read this document:**
- **L1 Fundamentals** — read first on every new session. Rarely changes.
- **L2 Top of Mind** — read second. Current state, active flags, next-session queue.
- **L3 History** — read only when investigating "when did we change X and why."

---

# L1 — FUNDAMENTALS

## Current operating mode (merged, since 2026-04-28)

This server runs **both Maggy and Winston code against a single account, U17562704, on port 7496** (the portfolio gateway). The original Maggy options account (U23886415) was migrated off this server to a clone codebase on the same machine under user `nexbit` (Ryan's son).

- IBKR account in use: **U17562704**
- IBKR port in use: **7496**
- IBKR client IDs: Maggy=12, Winston=97 (same Python process, same gateway)
- Maggy mode: **suggestion mode ON, auto-approve OFF** (Ryan flipped auto-approve off Apr 27 after 4 unintended DTE-bypass fills)
- Winston mode: **read-only at IBKR protocol level** (deliberate two-layer safety; `placeOrder` rejected even if app-level flag flipped)

**Re-split path** (when a dedicated options account arrives):
1. `config/settings.yaml` ibkr block: revert `port: 7496 → 4001`, `account: "U17562704" → new account`
2. `~/restart-all.sh`: restore the options tmux block (backup at `~/restart-all.sh.pre-merge-2026-04-28`)
3. `~/watchdog-trader.sh`: uncomment the options-gateway respawn block (backup at `~/watchdog-trader.sh.pre-merge-2026-04-28`)
4. Run `~/restart-all.sh`

**Merge-period safety infrastructure** (built into the code, auto-detects state):
- `_detect_merged_with_options()` in `connection.py` compares settings.ibkr vs settings.portfolio. When merged, `get_portfolio_lock()` returns a supervisor that acquires Maggy's `ib_lock` FIRST, then Winston's `_portfolio_lock`. Cross-strategy serialization without touching Maggy code.
- When ports diverge post-re-split, the supervisor becomes a no-op automatically. No code change needed.
- Inline removal instructions for permanent re-split live in `connection.py` under the MERGE-ONLY header.

## Accounts, ports, infrastructure

- **Server:** `rain@octoserver-genoax2:~/automatic_option_trader`
- **Python:** `.venv/bin/python3` (always `cd ~/automatic_option_trader` first)
- **GitHub:** https://github.com/rainrosimannus-spec/automatic_option_trader
- **Restart command:** `~/restart-all.sh` (2x phone 2FA, wait 15–20s)
- **Dashboard:** https://37.0.30.34/ (Caddy + basic auth + self-signed HTTPS, user `maggycian`)
- **Dashboard auth:** bcrypt cost 14, hash in `/etc/caddy/Caddyfile`. App binds `127.0.0.1:8080` — localhost only.
- **IBKR ports (canonical, when split):** Maggy=4001, Winston=7496. Currently both on 7496 (merge).
- **DB path:** `data/trades.db` (per `config/settings.yaml`)
- **FMP quota:** 250/day. Screener uses ~150–300; +60 with breakthrough filter checks.
- **First metrics run after restart:** startup + 120 seconds
- **Metrics cycle:** every 4 hours (`check_interval_hours: 4` in `config/settings.yaml`)

## Architecture facts

1. **FMP is dead for LSE/AEB/HKEX/BM.** Use IBKR `ReportsFinSummary` via `src/portfolio/ibkr_fundamentals.py`.
2. **LSE prices come in pence.** Normalized at source in `src/portfolio/analyzer.py` (May 7, commit `95f7d67`). Eight other GBP-handling sites exist in the codebase; centralization deferred.
3. **Maggy and Winston are separate strategies with separate tables.** Maggy: `positions`. Winston: `portfolio_holdings`, `portfolio_put_entries`. They share an IBKR connection in merged mode but write to different tables.
4. **Options universe ⊂ portfolio universe.** Stocks dropping out mid-cycle do NOT abandon covered calls — Maggy reads `positions` table directly.
5. **Running process must be restarted for code changes to take effect.** Pushed ≠ live.
6. **Logging:** structlog routes through stdlib → `trader.log` captures application events (since Apr 23, commit `65a9dec`).
7. **3-consecutive-failure exchange skip** in `update_watchlist_metrics` is still in code. Dormant. Leave alone unless it bites.
8. **Breakthrough tier enforces $500M cap + no ETFs + no recent reverse splits** at code level (Apr 23, commit `3c744c6`).
9. **Watchlist staleness visible** — yellow ⏱ icon on dashboard for `metrics_stale=True`.
10. **Symbols in both CANDIDATE_POOLS and breakthrough scan deduplicate to breakthrough** (Apr 23, commit `23f6f72`).
11. **`NetLiquidation` already includes accrued interest** as a separate line (researched May 5). Graph is correct as-is. Margin interest is reflected in NLV.
12. **Earnings gate is fail-CLOSED** (opposite to most gates). Missing data → block. Rationale: earnings is the single most predictable cause of overnight gap risk on a CSP.

## How scoring works

> ⚠ **Pending: Commit E** (in L2 next-session queue) will flip `portfolio_score` formula to actually use `forward_growth_score`. Currently the field is computed and stored but `portfolio_score` still uses the old `40g + 25v + 35q`. Deliberate observation period.

Columns on `portfolio_watchlist` (38 total):

- `growth_score`, `valuation_score`, `quality_score` — FMP/IBKR fundamentals
- `dividend_total_return_score` — dividend-specific. NULL for non-dividend tier.
- `raw_score` — analyzer's technical signal (SMA discount + RSI). Written by both `_update_watchlist_metrics` and `recalc_scores_from_db`.
- `compound_quality_pct` — within-tier 1–100 percentile from `_compute_compound_quality`
- `composite_score` — dashboard value. Written ONLY by `recalc_scores_from_db`. Formula: `(raw − penalty) × 0.30 + compound_quality_pct × 0.70` (rebalanced May 3).
- `discount_pct`, `rsi_14`, `sma_200`, `current_price` — written by `_update_watchlist_metrics`
- `metrics_stale`, `last_metrics_success` — staleness flag + last-success timestamp
- `forward_growth_score` — new 5-component score (May 8). Stored on `StockScore`. Not yet driving `portfolio_score` (see Commit E).

**Compound quality formulas (Option B, per tier):**
- Growth / breakthrough: `raw = 0.40 × growth + 0.25 × valuation + 0.35 × quality`
- Dividend: `raw = dividend_total_return_score`

**Forward-growth scoring (5 components, weights):**
- Revenue durability — 25%
- Compounding quality — 25%
- Operating leverage — 20%
- Innovation investment — 15%
- Capital efficiency — 15%
- **Hard cap at 30** if 3+ years negative net income AND 3+ years negative FCF over a 5-year window.

**Action mapping (post-May 3 rebalance):**
- Composite < 40: watchlist member, no action
- Composite 40–75: sell CSP at target strike (get paid to wait — most fair-priced quality stocks land here)
- Composite > 75: direct buy (rare; requires both real technical signal AND high quality)
- Override gates (deep_discount > 15%, RSI < 20, volume_surge + trend_healthy) still promote to direct_buy regardless of composite.

**Composite-write chain (after Apr 22 metrics-order fix, commit `2a95c7b`):**
1. `job_portfolio_update_metrics` runs every 4 hours
2. `update_watchlist_metrics` loops every watchlist stock, IBKR calls, writes price/sma/rsi/raw + `last_metrics_success`
3. End of loop: scan for stocks with `last_metrics_success` older than 24h, set `metrics_stale=True`
4. `recalc_scores_from_db` loops again with populated discount/rsi, calls `_compute_compound_quality`, writes `composite_score`
5. Logs `portfolio_metrics_updated` (with `newly_stale` count) and `portfolio_scores_recalced`

## Augmentation pipeline (May 8, live test ran May 11)

Monthly screener invokes Claude to propose 5–10 high-conviction names beyond the hand-coded universe, scores them, accepts those that beat the rank-60/rank-15 cutoff. **First live test ran May 11**, accepted DECK, CPRT, ROL to `discovered_pool.yaml`.

**Architecture decisions:**
- Two discovered pools (`discovered_growth`, `discovered_dividend`), separate, no yield-routing for discovered names.
- Every symbol evictable — hand-coded names included, no editorial floor.
- Eviction triggers only when pool > cap (180 growth / 45 dividend); no K-parameter, no consecutive-runs logic.
- General augmentation (not slot-specific) — Claude proposes high-conviction names, not "replace these specific laggards."
- Margin = +0 (any improvement over rank-60 cutoff accepts). Easy to tune to +3 later if churn is excessive.
- Eviction file at `tools/evicted_names.yaml` (NOT auto-edit source code).
- `discovered_pool.yaml` lives in source tree (committed).
- Strict failure handling on FMP miss (no retry). Audit row written with `reason="scoring_failed"`.
- Audit trail = SQLite table `augmentation_audit` (11 columns: id, run_date, tier, proposed_symbol, proposed_score, cutoff_score, displaced_symbol, displaced_score, accepted, reason, notes).

**Files involved:**
- `tools/screen_universe.py` — orchestration in PHASE 2.5 of `screen_all`
- `tools/discovered_pool.yaml` — accepted names persist here
- `tools/evicted_names.yaml` — evicted names listed here
- `src/portfolio/models.py` — `AugmentationAudit` model

## Working rules

1. **Copy-paste terminal commands only.** Ryan is non-programmer.
2. **One change at a time.** Backup → verify → patch → syntax check → diff → test → commit → push.
3. **View files before editing.** Memory is stale.
4. **Fixes are structural, not manual.**
5. **Don't touch Maggy-side code** when working on Winston (`src/strategy/`, `src/broker/`, options-side of `src/scheduler/jobs.py`).
6. **Don't conflate `PortfolioPutEntry` (Winston's CSPs) with `Position` (Maggy's wheel).**
7. **When Ryan pushes back, re-examine, don't defend.**
8. **Never claim a fix works without proof:** restart, test, verify DB values, then claim.
9. **`composite=0` for valid stocks is NEVER correct.**
10. **Every action needs a turn.** Stop when Ryan stops.
11. **If Ryan asks a question before pasting output, answer the question first.** Don't assume output that wasn't there.
12. **Cross-check STATE.md after writing.** Empty pattern matches mean nothing changed; verify content actually updated. **After GitHub web upload, fetch the raw URL and verify head + tail match what was generated** (May 11 lesson — wrong file landed without detection).
13. **No heredocs for Python patches** — write to `/tmp/patchN.py`, `ast.parse` check, dry-run, commit with `--commit`.
14. **One match assertion per patch with ABORT on mismatch.**
15. **`tmux capture-pane -t trader -p -S -2000`** is the log source.
16. **Raw GitHub URLs** for file reading.
17. **CREDENTIAL SAFETY (third incident, May 11 2026):** NEVER ask Ryan to run commands that expose credentials: `git remote -v`, `env`, `printenv`, `cat .env`, `cat .git/config`, `history`, `ps auxe`. PATs are often in HTTPS git URLs. Use redacted variants: `git remote -v | sed 's|//[^@]*@|//[REDACTED]@|g'`. ALWAYS warn before commands touching credential-bearing files. Ryan is not a programmer and will not catch leaks. Strict rule, no exceptions.

## File handoff workflow (Ryan's path)

When Claude generates a file:
1. Claude creates the file via `create_file` + `present_files`.
2. Ryan downloads locally. If browser auto-numbers (e.g. `state_7.md`), rename to exact target filename — **case-sensitive** (`STATE.md`, not `state.md`).
3. Ryan opens `https://github.com/rainrosimannus-spec/automatic_option_trader/upload/main` and drags the file onto the page.
4. Ryan commits via the GitHub web UI ("Commit directly to the main branch").
5. On the server: `cd ~/automatic_option_trader && git pull`

**Never** ask Ryan to: paste long file content into a terminal, use heredocs, use base64 chunks, or run `scp`. The GitHub web upload is the only acceptable path.

**Post-upload verification (mandatory):** after Ryan reports the upload is done, fetch the raw GitHub URL and compare head + tail against what was generated.

## Don'ts

- Don't touch Maggy-side code.
- Don't run monthly screener without warning (20–40 min, FMP quota, lock held).
- Don't rewrite screener logic in backfill scripts.
- Don't try to work while Ryan sleeps.
- Don't assume pushed == live.
- Don't conflate "dashboard looks wrong" with "is broken."
- Don't assume a fix worked — restart and verify.
- Don't use complex heredoc for patches.
- Don't assume STATE.md write succeeded — verify with `head`/`tail` of the file AND verify against GitHub raw URL.

---

# L2 — TOP OF MIND

## ⏭ Next session — do these first

**1. IBM duplicate-row fix in `src/portfolio/sync.py`.** Diagnosis complete May 12. Root cause: holdings sync's safety-net dedup at lines ~110–114 only checks for `action='put_assigned'`, missing the `action='buy'` row written by `ibkr_sync`. When session timing causes holdings sync to start before `ibkr_sync` commits, dedup misses the buy row and writes a phantom `put_assigned` at `strike − premium` price ($251.84 for IBM 260P with $8.16 premium). Fix shape agreed: extend the `filter()` to `action.in_(["put_assigned", "buy"])`. Single line, no new imports.

**2. Diagnostic blocker (still queued from May 11).** Find where the dashboard web/screener process logs stdout (breakthrough phase, Claude API responses, REJECTED lines). `tmux trader` pane is empty; `watchdog.log` doesn't have it. Check `tmux list-panes -a`, `systemctl`, `~/restart-all.sh` redirects, nohup logs.

**3. Augmentation acceptances housekeeping.** DECK, CPRT, ROL added to `discovered_pool.yaml` May 11. Verify `augmentation_audit` table has matching rows. Currently `discovered_pool.yaml` shows uncommitted on server — commit it.

## Active flags / current state

- `AUGMENTATION_ENABLED = True` in `tools/screen_universe.py` — was flipped on May 11, live test ran successfully
- Maggy: suggestion mode ON, auto-approve OFF
- Winston: read-only at IBKR protocol level (placeOrder rejected)
- Portfolio lock supervisor: `merged=True` (logged at startup)
- Earnings cache table: auto-creates via `Base.metadata.create_all` (verified exists on server)
- AugmentationAudit table: exists on server
- Watchlist scoring May 12 06:21 UTC check: 132 rows, all fresh (last update ~04:21 UTC), `metrics_stale=0` across the board. Healthy.

## In-flight work

**Forward-growth scoring — observation period.** All 5 sub-scores stored on `StockScore`, `forward_growth_score` populated for 133 watchlist rows (range 11.8–89.2, avg 52.5). `portfolio_score` formula NOT yet using it. Commit E pending.

**Augmentation pipeline — first run complete May 11.** DECK, CPRT, ROL added to discovered_pool.yaml. Live run cost ~$0.40 Anthropic + ~50 FMP calls.

## Queued work (after next-session priorities)

**Commit E — flip `portfolio_score` to use `forward_growth_score`.** Earliest sensible: after 2–3 more screener runs accumulate.

**Optional Commit N — `--dry-run-augmentation` flag** in `tools/screen_universe.py` to invoke Claude proposals + scoring without persistence/audit writes. Useful for prompt iteration without polluting audit/yaml.

**Breakthrough prompt v4 — geographic spread fix.** May 8 screener returned only 1 non-USD listing (Sony 6758) vs target 5+. Need stricter geographic distribution constraint in the prompt.

**Covered call roll-up trigger (queued feature).** When stock price > strike + ~7% AND DTE > 5, surface a manual `sell_covered_call_review` suggestion.

## Known unfixed issues (deferred)

**Maggy `unrealized_pnl` AttributeError on U17562704.** Every put scan errors at `src/strategy/risk.py:1224` reading `pos.unrealized_pnl` from IBKR Position objects. Account-type/permission-specific bug — same code wrote 4 puts successfully against old U23886415 on Apr 27. Expected to disappear when new dedicated options account arrives. One-line fallback fix available if not: `getattr(pos, 'unrealizedPNL', 0.0)`. Cosmetic-only on this server (Winston is the active strategy here; son's clone runs real Maggy on U23886415).

**KSPI-style fill claiming.** Post-merge, all IBKR fills arrive on the same shared connection with no strategy tag. Whichever sync code runs first claims the fill. Maggy's `trade_sync` runs every ~5 min; Winston's runs every 4 hours. Maggy almost always wins. Display split is annoying, accounting is correct. Real fix: tag fills by strategy at sync time. Will be moot post-re-split.

**`portfolio_account_updates_failed` (TimeoutError on `reqAccountUpdates`).** Fired once at startup; may be one-time. Watch for recurrence.

**DB-write timing in `buyer.py`.** Holdings/transactions row written at submission time, before fill confirmation. If IBKR rejects, DB has phantom row until trade_sync's next reconcile cycle (15 min). Real fix: write only on fill. Not blocking under current read-only/suggestion-mode operation. Worth fixing before any auto-approve live mode.

**`trade_sync` reopen logic still incomplete.** At `trade_sync.py:625-630` the reopen flips status and clears `closed_at` but does NOT clear `realized_pnl`. With Apr 24 wheel.py fix in place, reopen shouldn't trigger for valid OPEN positions — but defense-in-depth one-liner deferred.

**Watchlist dividend NULL backfill.** 9 dividend holdings still NULL on `dividend_total_return_score` (HDB, IBN, BMY, CEG, 0ZQ, ALV, NLY, PBR, SFL). Phase 2b populates on next screener run.

**Six historical positions with `realized_pnl=0`, `total_premium_collected>0`.** PANW/UBER/SHOP/TTD ASSIGNED puts on Mar 29 (predate Apr 25 commit `2e9708c`), PANW stock CLOSED Apr 25, COIN covered_call EXPIRED Mar 9. ~$1,339 in unrecognized P&L on the dashboard. Decision: do not fix on this server. Merged-mode data is mixed pre-merge Maggy + post-merge Winston; manual UPDATEs now would risk corrupting numbers that should be on the other side of the future re-split.

**CRWV id=152 stuck OPEN despite May 6 buy-back.** Identified; same merged-mode-data caveat applies.

**`check_position_size` cosmetic note.** Variable named `estimated_margin` is misleading — it's notional concentration (price × 100), not margin. Real margin enforcement happens via `get_whatif_margin` in `put_seller`. Rename deferred.

**GBP centralization.** 8+ separate sites in codebase divide by 100 for GBP. Surgical fix landed at `analyzer.py` on May 7; centralization deferred.

## Lower priority

- Scorer fail-closed behavior — screener side largely self-corrects, metrics side now via staleness. Defer.
- 3-consecutive-failure exchange skip — dormant, leave alone unless it bites again.

## Open items for son's clone

**1. Asia/EU put scan validation.** Needs son's diagnostic on U23886415 to confirm whether scans produce zero suggestions due to legitimate market mechanics (live-quote gate, position limits, etc.) or a real bug (universe filter mismatch, market label issue, gate firing only against US data). Diagnostic drafted and ready to forward.

**2. NVDA P&L recovery.** Resolved — Script B delivered May 8 (`/tmp/import_options_history_standalone.py`, ~150 KB with embedded JSON, sent to son via email).

---

# L3 — HISTORY

> Chronological session entries. Read only when investigating "when did we change X and why."

## Monday (2026-04-22) — Option B + Commit B + metrics-order fix

- Dividend tier ranks on dedicated `dividend_total_return_score` column (Option B). Tier-aware compound_quality formulas: growth/breakthrough use `0.40*growth + 0.25*valuation + 0.35*quality`; dividend uses `dividend_total_return_score` directly.
- Held holdings not in top-100 get score refresh in Phase 2b (Commit B). 9 dividend holdings still NULL on `dividend_total_return_score` until next screener run.
- Late evening: discovered ~58/126 watchlist rows had composite=0 because scheduler ran `recalc_scores_from_db` BEFORE `update_watchlist_metrics`. Fix (commit `2a95c7b`): swap order.
- XXII penny stock (1-for-15 reverse split pending) made breakthrough tier at composite 55. Confirmed Claude's breakthrough scan returned it; no post-LLM filter rejected it.

## Thursday (2026-04-23) morning — four targeted fixes

1. **Breakthrough quality filters** (commit `3c744c6`) — `_check_breakthrough_eligibility` rejects ETFs, market_cap < $500M, reverse splits in last 18 months. ~60 FMP calls/run, quota-safe.
2. **Logger routing** (commit `65a9dec`) — structlog → `stdlib.LoggerFactory`. Application events now land in `trader.log`.
3. **Stale-metrics flagging** (commit `06cb459`) — `metrics_stale` (bool) + `last_metrics_success` (datetime) columns. Yellow ⏱ icon for stale > 24h.
4. **Cross-tier dedup** (commit `23f6f72`) — symbols in both CANDIDATE_POOLS and breakthrough scan deduplicate to breakthrough tier.

## Friday (2026-04-24) pre-market — wheel.py covered-call expiry bug

Found `wheel.py:439` used `expiry <= today` to mark covered calls EXPIRED — fired pre-market on expiry day while shares were still held. `trade_sync` (every 15 min) saw IBKR still had the contracts, reopened them. Repeating cycle. Window between flip-out and reopen exposed `wheel.write_covered_calls()` to thinking the lot was uncovered → phantom new calls.

First commit (`dd16ec6`) changed `<=` to `<` — would have skipped early-exercise detection same-day. Reverted via better fix (`b41e39a`): keep `<=` to catch same-day events, but only mark `called_away` when IBKR confirms via shares dropping below covered amount. Otherwise defer to `trade_sync`.

**Cleanup applied live:** buggy expiry handler had set `realized_pnl` on three OPEN positions (PG=274, UBER=132, PANW=543). Cleared to 0.0 via direct UPDATE.

**Trade_sync reopen logic still incomplete:** `trade_sync.py:625-630` reopen flips status and clears `closed_at` but does NOT clear `realized_pnl`. Defense-in-depth one-liner deferred.

## Saturday (2026-04-25) early morning — realized_pnl on covered-call assignments

Dashboard showed Realized P&L = -$36,779.24 after PG/PANW/UBER assignments. Three accounting bugs found in `trade_sync.py:580-595` and `wheel.py _handle_called_away`:

1. Stock-close formula included `ASSIGNMENT` and `CALLED_AWAY` trade types — double-counted on cost side.
2. Timing race: position marked CLOSED before matching `SELL_STOCK` trade arrived.
3. `_handle_called_away` wrote its own `realized_pnl` with formula triple-counting premium.

**Fix (commit `2e9708c`):**
- `trade_sync.py`: sum only `BUY_STOCK` and `SELL_STOCK` (commission inclusive). Defer marking position CLOSED until matching `SELL_STOCK` present.
- `wheel.py`: stop writing `realized_pnl` in `_handle_called_away`. `trade_sync` owns the calculation.

**Accounting model confirmed:** total wheel-cycle realized = `collected_premium + (call_strike − put_strike) × 100 − fees`.

## Monday (2026-04-27) — put_seller stuck + IPO scanner silently broken

**1. position_limit double-counting** (commit `3cb2930`) — covered_call rows consumed extra slots. Fix: count only `short_put` and `stock`.
**2. risk check order** (same commit) — cheap DB-read checks now run FIRST. Slow IBKR/FMP only if cheap ones pass.
**3. IPO scanner timedelta UnboundLocalError** (commit `5607adc`) — redundant nested `from datetime import timedelta` shadowed module-level binding. Removed.

## Monday (2026-04-27) afternoon — DTE bypass fix in _evaluate_symbol

Four orders filled at wrong DTE: NVDA May 6 (9 DTE), PLTR/RKLB/DXCM May 8 (11 DTE). Config says 0-3 DTE for USD at low/mid VIX.

Root cause (commit `63f68c6`): `_evaluate_symbol` called `screen_puts()` without `dte_min`/`dte_max`. Fall-back to hardcoded 5-14 defaults. `_process_symbol` had been correct all along. Fix mirrors `_process_symbol`.

The 4 mistaken puts kept open. Auto-approve flipped OFF after this incident.

## Tuesday (2026-04-28) — account merge

Test account U23886415 decommissioned on this server, migrated to son's clone under user `nexbit`. This server now runs Maggy and Winston code against U17562704 only, port 7496.

Config-only changes:
- `config/settings.yaml`: port 4001 → 7496, account U23886415 → U17562704
- `~/restart-all.sh`: dropped options tmux block. Backup `~/restart-all.sh.pre-merge-2026-04-28`
- `~/watchdog-trader.sh`: commented out options-gateway respawn. Backup `~/watchdog-trader.sh.pre-merge-2026-04-28`

Hiccup: watchdog cron respawned the killed options tmux session within 3 min, holding the U23886415 session and locking son out. Patched watchdog.

## Tuesday (2026-04-28) evening — dashboard auth + lockdown

Dashboard was publicly accessible. Closed via Caddy reverse proxy:
- Caddy 2.11.2 from apt, public on 80/443
- Self-signed TLS cert (caddy `tls internal`)
- Basic auth user `maggycian`, bcrypt cost 14, hash in `/etc/caddy/Caddyfile`
- App binding `0.0.0.0:8080` → `127.0.0.1:8080`
- ufw firewall: ports 80, 443 added
- New URL: https://37.0.30.34/

## Wednesday (2026-04-29) — KSPI fill-claiming + watchlist metrics asyncio fix

**KSPI fill-claiming (no code change):** Manual Winston buy-back showed only on Maggy's trade history. Whichever sync runs first claims fills. Listed in deferred bugs.

**Bug B fixed — get_ib_lock missing import** (commit `877426d`): `reconcile_submitted_trades()` called `with get_ib_lock():` but import only had `get_ib` and `is_connected`. NameError swallowed by `except`. Fix: added import.

**Watchlist metrics catastrophic failure:** Last 3 of 4 cycles had `failed=124 updated=0`. Asyncio races between Maggy clientId 12 and Winston clientId 97 sharing the gateway.

Two-layer fix:
- **Stages 1–3** (commits `ff631e2`, `baa533d`, `59b3197`): Wrap every Winston IBKR call site with `get_portfolio_lock()`. Stage 2 covered 21 sites as 16 lock blocks. Stage 3 covered analyzer/forecaster/sync/fundamentals/bridge.
- **Stages 4–5** (commit `392369b`): Cross-strategy supervisor. When merged-mode detected, `get_portfolio_lock()` acquires Maggy's `ib_lock` FIRST, then `_portfolio_lock`. When ports diverge post-re-split, becomes no-op automatically.

Verification: 23:46 metrics run after restart: `failed=0 updated=124`. Asyncio race dead.

## Saturday (2026-05-03) — reconnect-race + scoring rebalance + pending orders

**1. Reconnect-race fix (commit `b734cf7`):** IBC gateway restart at midnight, trader reconnect attempts looped 47 min with "event loop is already running". Root cause: `_connect()` calls `ib.connect()` which invokes `asyncio.get_event_loop().run_until_complete(...)`. If Winston thread is mid-call holding `_ib_lock`, new asyncio task can't run. Fix: wrap `ib.connect()` in `with _ib_lock:`. RLock so safe even if same thread.

**2. Scoring rebalance — Buffett-style:** Dashboard top sat at 45-65 score range, never reached 70+ except in panic. System effectively "panic-buy or never." Three changes:
- `a25ccb1`: Fair-price base 0-24 points scaled across `discount_pct` from -5% to +5%. Composite blend 80/20 → 30/70.
- `45e6d72`: Composite floor `MIN_COMPOSITE_FOR_ACTION = 40.0` for `buy_signal=True`.
- `f65b9d4`: Direct-buy threshold 70 → 75.

**3. Pending orders dashboard fix (commit `2c161a8`):** Template `{{o.quantity}}` → `{{o.qty}}`. Trigger `refresh_portfolio_pending_orders_cache()` immediately after each `placeOrder`. Visible in ~2 sec.

## Monday (2026-05-05) — Capital injections deposit-proof graphs

**Options graph formula fix (commit `3b407ff`):** €15K injection had caused graph to jump +100%. Five-file fix: `account_id` column on `portfolio_capital_injections`, formula `(nlv / total_invested - 1) × 100` anchored to first-point-zero.

Initial backfill (`65178bd`) failed with `"name 'text' is not defined"`. Removed entirely (`10ef0a4`).

**Margin interest investigation (no code change):** `NetLiquidation = TotalCashValue + AccruedInterest`. Already correct, no graph adjustment.

## Thursday (2026-05-07) — earnings gate, LSE pence, JSON history export

Four commits pushed:
1. `974b538` — `EarningsCache` model (24h cache)
2. `4cf7630` — `get_next_earnings_date(ib, contract)` using `CalendarReport`
3. `58c6a55` — Real `has_upcoming_earnings()` implementation. **Fail-CLOSED on missing data.**
4. `95f7d67` — LSE pence normalization at source in `analyzer.py`. AZN fixed (was 13552, now 135.52).

JSON history export built for son's clone: 37 positions, 92 trades, 127 events, $2,964.76 running realized P&L.

## Friday (2026-05-08) — Forward-growth scoring landed + augmentation pipeline complete (22 commits)

**Forward-growth scoring** (`f55d8b2` → `940507a`, `ed76fad`, plus `699900b`): replaces old `40g + 25v + 35q` with 5-component Buffett-style composite. Revenue durability 25%, compounding quality 25%, operating leverage 20%, innovation investment 15%, capital efficiency 15%. Hard cap at 30 if 3+ years negative NI AND 3+ years negative FCF over a 5-year window.

Screener run May 8: 133 rows, range 11.8–89.2, avg 52.5. Top names MA 89.2, ASML 88.8, LLY 86.6, META 86.5, KLAC 85.7.

**Augmentation pipeline** (`6a9ba4a` → `49b6785`): full pipeline built, gated behind `AUGMENTATION_ENABLED = False`. Files: `tools/discovered_pool.yaml`, `tools/evicted_names.yaml`, `AugmentationAudit` model, prompt builders, PHASE 2.5 orchestration, atomic yaml persistence, eviction-on-overflow.

**Son's clone:** Standalone Script B for importing pre-merge history delivered. Resolved.

## Sunday (2026-05-10) — augmentation pipeline validated, Commit E groundwork

(Commit `c2cced4` — details preserved in prior chronological versions; abbreviated.)

## Monday (2026-05-11) — STATE.md restructure + RULES.md refresh + augmentation live test

- `3d654df` (19:35): L3 history entry added for May 11
- `e25252b` (19:43): L1+L2 surgical edits
- `485fc90` (22:47 "Add files via upload"): Full STATE.md restructure attempted via GitHub web upload. **Result inadvertently overwrote some earlier L1/L2/L3 structure with a hybrid version. Discovered May 12.** Lesson learned: post-upload verification via GitHub raw URL is mandatory.
- RULES.md refresh: added credential safety rule, file handoff workflow, merged-mode operating section.
- **Augmentation live test:** flipped `AUGMENTATION_ENABLED = True`, ran screener. DECK, CPRT, ROL accepted to `discovered_pool.yaml`.

## Tuesday (2026-05-12) — STATE.md re-restructure + IBM duplicate-row diagnosis

**Morning triage:** "Portfolio watchlist scores look stale" report. Diagnosed: not stale. 132 rows, all `metrics_stale=0`, last update ~2h ago. Working as designed — May 3 30/70 blend dominated by slow-moving quality.

**IBM duplicate transaction investigation:** Three rows in `portfolio_transactions` for IBM assignment — `put_assigned 260`, `buy 260`, `put_assigned 251.84`. First two are correct (option leg + stock leg of assignment). Third is phantom.

Diagnosis: `src/portfolio/sync.py` holdings safety-net (lines ~107–124) creates a `put_assigned` row when it detects a new holding without a matching transaction. Dedup at lines ~110–114 only checks for `action='put_assigned'` in last 3 days, missing the `action='buy'` row written by `ibkr_sync` at 01:43. When holdings sync ran at 08:09 in a session that opened before `ibkr_sync` committed, dedup missed row 160, wrote phantom row 159. Price $251.84 = strike − premium ($260 − $8.16, premium from original SELL_PUT row id 225).

Fix shape agreed (not yet written): extend filter to `action.in_(["put_assigned", "buy"])`. Single line. No new imports.

**STATE.md restructure discovered hybrid:** GitHub history showed yesterday's "Add files via upload" commit (`485fc90`) partly overwrote prior L1/L2/L3 structure. Regenerated full STATE.md May 12. Workflow lesson added to working rules: mandatory post-upload verification against raw GitHub URL.
