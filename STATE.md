# Maggy & Winston — State Document

Last updated: 2026-05-17 — L1/L2/L3 structure. Content current through end of May 17 session (Bruno initial build complete).

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
- **Restart command:** `~/restart-all.sh` (2x phone 2FA in split mode, 1x in merged mode, wait 15–20s)
- **Dashboard:** https://37.0.30.34/ (Caddy + basic auth + self-signed HTTPS, user `maggycian`)
- **Dashboard auth:** bcrypt cost 14, hash in `/etc/caddy/Caddyfile`. App binds `127.0.0.1:8080` — localhost only.
- **IBKR ports (canonical, when split):** Maggy=4001, Winston=7496. Currently both on 7496 (merge).
- **DB path:** `data/trades.db` (per `config/settings.yaml`)
- **FMP quota:** 250/day. Screener uses ~150–300; +60 with breakthrough filter checks.
- **First metrics run after restart:** startup + 120 seconds
- **Metrics cycle:** every 4 hours (`check_interval_hours: 4` in `config/settings.yaml`)
- **Claude model:** `claude-sonnet-4-6` (migrated from `claude-sonnet-4-20250514` on May 14 due to June 15 deprecation)

## Architecture facts

1. **FMP is dead for LSE/AEB/HKEX/BM.** Use IBKR `ReportsFinSummary` via `src/portfolio/ibkr_fundamentals.py`.
2. **LSE prices come in pence.** Normalized at source in `src/portfolio/analyzer.py` (May 7, commit `95f7d67`). Eight other GBP-handling sites exist in the codebase; centralization deferred.
3. **Maggy and Winston are separate strategies with separate tables.** Maggy: `positions`. Winston: `portfolio_holdings`, `portfolio_put_entries`. They share an IBKR connection in merged mode but write to different tables.
4. **Options universe ⊂ portfolio universe.** Stocks dropping out mid-cycle do NOT abandon covered calls — Maggy reads `positions` table directly.
5. **Running process must be restarted for code changes to take effect.** Pushed ≠ live.
6. **Logging:** structlog routes through stdlib → `trader.log` (intended) but file may not exist on disk; logs land in `tmux capture-pane -t trader`. **Logging diagnostic blocker still queued in L2.**
7. **3-consecutive-failure exchange skip** in `update_watchlist_metrics` is still in code. Dormant. Leave alone unless it bites.
8. **Breakthrough tier enforces $500M cap + no ETFs + no recent reverse splits** at code level (Apr 23, commit `3c744c6`).
9. **Watchlist staleness visible** — yellow ⏱ icon on dashboard for `metrics_stale=True`.
10. **Symbols in both CANDIDATE_POOLS and breakthrough scan deduplicate to breakthrough** (Apr 23, commit `23f6f72`).
11. **`NetLiquidation` already includes accrued interest** as a separate line (researched May 5). Graph is correct as-is. Margin interest is reflected in NLV.
12. **Earnings gate is fail-CLOSED** (opposite to most gates). Missing data → block. Rationale: earnings is the single most predictable cause of overnight gap risk on a CSP.
13. **Capital injections are account-tagged.** Both Maggy and Winston injections flow through `portfolio_capital_injections` table with `account_id` column. `get_total_invested_usd(account_id=...)` filters per-account.

## How scoring works

> Commit E (was in queued work; per memory now done or not needed) — `portfolio_score` formula and `forward_growth_score` integration considered settled. See L3 for history.

Columns on `portfolio_watchlist` (38 total):

- `growth_score`, `valuation_score`, `quality_score` — FMP/IBKR fundamentals
- `dividend_total_return_score` — dividend-specific. NULL for non-dividend tier.
- `raw_score` — analyzer's technical signal (SMA discount + RSI). Written by both `_update_watchlist_metrics` and `recalc_scores_from_db`.
- `compound_quality_pct` — within-tier 1–100 percentile from `_compute_compound_quality`
- `composite_score` — dashboard value. Written ONLY by `recalc_scores_from_db`. Formula: `(raw − penalty) × 0.30 + compound_quality_pct × 0.70` (rebalanced May 3).
- `discount_pct`, `rsi_14`, `sma_200`, `current_price` — written by `_update_watchlist_metrics`
- `metrics_stale`, `last_metrics_success` — staleness flag + last-success timestamp
- `forward_growth_score` — 5-component score (May 8). Stored on `StockScore`.

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
- Composite 40–75: sell CSP at target strike
- Composite > 75: direct buy (rare; requires both real technical signal AND high quality)
- Override gates (deep_discount > 15%, RSI < 20, volume_surge + trend_healthy) still promote to direct_buy regardless of composite.

**Composite-write chain (after Apr 22 metrics-order fix, commit `2a95c7b`):**
1. `job_portfolio_update_metrics` runs every 4 hours
2. `update_watchlist_metrics` loops every watchlist stock, IBKR calls, writes price/sma/rsi/raw + `last_metrics_success`
3. End of loop: scan for stocks with `last_metrics_success` older than 24h, set `metrics_stale=True`
4. `recalc_scores_from_db` loops again with populated discount/rsi, calls `_compute_compound_quality`, writes `composite_score`
5. Logs `portfolio_metrics_updated` (with `newly_stale` count) and `portfolio_scores_recalced`

## How wheel.py covered calls work (post May 14 rescue-mode fix)

Three-branch evaluation in `_write_call`:

1. **Rescue mode** — `current_price < cost_basis × 0.95`. Delta range 0.05–0.35. Min strike still net_cost_basis (never sells below breakeven). Captures the case where stock has dropped well below cost basis; the 0.30 floor wouldn't allow strikes above cost basis on a depressed stock.
2. **Exit mode** — `stock_pos.wheel_exit_mode=True` (default for all assignments). Delta range from `risk_cfg.wheel_exit_delta_min/max`. Adds interest_surcharge to min_strike.
3. **Normal** — close to ATM, delta 0.30–0.45.

Pre-May-14 the rescue check was nested inside the else of `wheel_exit_mode`, making it unreachable for any assigned position (since assignments default to exit_mode=True). The hoisted version (`3c0bf55`) places rescue check first and wins regardless of exit_mode flag.

## Augmentation pipeline (live since May 11)

Monthly screener invokes Claude to propose 5–10 high-conviction names beyond the hand-coded universe, scores them, accepts those that beat the rank-60/rank-15 cutoff.

**Status:** `AUGMENTATION_ENABLED = True` since May 11. First run accepted DECK, CPRT, ROL plus ~21 breakthrough names. Committed to `discovered_pool.yaml` (079446a).

**Architecture decisions:**
- Two discovered pools (`discovered_growth`, `discovered_dividend`), separate, no yield-routing for discovered names.
- Every symbol evictable — hand-coded names included, no editorial floor.
- Eviction triggers only when pool > cap (180 growth / 45 dividend); no K-parameter, no consecutive-runs logic.
- General augmentation (not slot-specific) — Claude proposes high-conviction names.
- Margin = +0 (any improvement over rank-60 cutoff accepts). Easy to tune to +3 later if churn is excessive.
- Eviction file at `tools/evicted_names.yaml` (NOT auto-edit source code).
- `discovered_pool.yaml` lives in source tree (committed).
- Strict failure handling on FMP miss (no retry). Audit row written with `reason="scoring_failed"`.
- Audit trail = SQLite table `augmentation_audit` (11 columns).

**Files involved:**
- `tools/screen_universe.py` — orchestration in PHASE 2.5 of `screen_all`
- `tools/discovered_pool.yaml` — accepted names persist here
- `tools/evicted_names.yaml` — evicted names listed here
- `src/portfolio/models.py` — `AugmentationAudit` model

## Cash Bridge v2 (built May 15, stays disabled until re-split)

Replaces the v1 annual-date sweep with a performance-based design. **`enabled=False` by default** and merged-mode defensive check refuses self-transfer.

**Trigger:** NLV >= benchmark × factor (default 2.0)
**Sweep amount:** NLV × transfer_pct (default 10%)
**Cooldown:** optional days between sweeps (default 0)

**Benchmark management — two events update `bridge_benchmark`:**
- Sweep: `benchmark = post_sweep_NLV` (next doubling on remaining capital)
- Injection: `benchmark += injection_amount` (capital deposits never trigger fake sweeps)

**State source of truth:** all settings in `PortfolioState` rows written by Controls page. `BridgeConfig` provides defaults only.

**Files:**
- `src/portfolio/bridge.py` — CashBridge class + `bump_bridge_benchmark` hook
- `src/web/routes/controls.py` — form handler, state load
- `src/web/templates/controls.html` — form fields + display panel
- `src/portfolio/capital_injections.py` — injection hook calls `bump_bridge_benchmark`

**Activation path** (when new options account arrives):
1. Set `cfg.ibkr.account = <new_options_account_id>` in settings
2. Visit Controls page in dashboard
3. Configure factor, transfer_pct, cooldown_days
4. Uncheck Dry Run when comfortable
5. Check Enabled

## Bruno — loan portfolio management (live since May 17)

MesiCap Technologies OÜ's internal loan tracking and origination system. Lives at `/borrower` in the dashboard. Source: `src/borrower/` (models + accrual engine), `src/web/routes/borrower.py` (routes), `src/web/templates/borrower_*.html` (6 templates).

**Database:** `data/bruno.db` (SQLite, separate from Maggy's `data/trades.db`, gitignored). Seven tables: counterparties, loans, loan_movements, loan_amendments, interest_accruals, payments, audit_log.

**Pages:**
- `/borrower` — landing
- `/borrower/loans` — index with totals by currency and purpose
- `/borrower/loans/{id}` — full loan detail (movements, amendments, payments, accrual)
- `/borrower/loans-new` — origination form
- `/borrower/counterparties-new` — new counterparty form
- `/borrower/lender-admin` — placeholder

**Accrual engine** (`src/borrower/accrual.py`): three methods (capitalizing/simple/amortizing) with rate-amendment + principal-change handling. Daily snapshots recorded by `job_record_accruals` at 05:30 UTC. 819 historical snapshots backfilled May 17.

**Dev/Prod separation:** `cfg.app.bruno_run_integrations` (default `False` on this codebase) gates future external integrations (IBKR NLV reads, LHV bank statements, etc.). Rasmus's MesiCap clone enables it to `True`. The accrual job has no external dependencies, runs unconditionally on both. Bruno code lives in Rain's codebase as the dev environment; Rasmus's clone is the production environment because MesiCap's actual financials live there.

**Architectural decisions locked:**
- Path 3 + Option C (Bruno-first with contract attachment): admin creates loan in Bruno, contract generated from template, signed externally, signed PDF uploaded back as the authoritative artifact. Template engine and document storage not yet built.
- LTV framework: Asset Coverage ≥ 2.0x (50% LTV) at Phase 3 start, with explicit understanding it loosens to 1.67x (60%) after 12-18 months of operational track record. Liquidity Reserve ≥ 2.0x of 12-month cash debt service. Operating Cash Coverage ≥ 1.5x. Net Worth tracked as observability, not binding.
- Subordination: shareholder loans (Waddy/Arvutitugi operational + all trading capital) require formal subordination to external lenders before Phase 3 launches. Master agreement amendment needed.

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
13. **No heredocs for Python patches.** Plain-text OLD/NEW files in `/tmp/` + a Python script that reads them is fine; `python3 -c "..."` with escaped quotes inside is the banned pattern. Cat heredoc for plain Python files (full-file writes, not match-and-replace) is also fine.
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

**Restart-required May-12 fixes are now live.** `~/restart-all.sh` was run multiple times during Bruno work on May 16-17. `fb3ef28`, `3c0bf55`, `cac0d2d`, Bridge v2 commits all active in the current running process.

**1. Logging diagnostic blocker (still queued).** Find where dashboard/screener process logs stdout. `tmux trader` pane is empty; `watchdog.log` doesn't have it. `trader.log` exists and captures scheduler/strategy logs but NOT web errors (we hit this during Bruno work — web 500s only surfaced via direct Python invocation). Check `tmux list-panes -a`, `systemctl`, `~/restart-all.sh` redirects, nohup logs.

**2. Bruno next-build choices, in priority order:**
- **Headroom calculator** with four-metric debt-burden gate (the "block loans that cross limits" feature Rain flagged as the main strategic goal). Depends on: accrual engine ✓ + IBKR NLV read (gated) + LHV cash read (gated). Needs both integrations enabled, which means MesiCap clone, not this server.
- **Movement and Payment recording forms** — UI replacements for the Python seed scripts. Each ~150-line form template.
- **Mobile UX pass** on loan detail page — tables overflow, key facts cramped. Rain flagged it Sunday morning.
- **Counterparty detail page** — currently no equivalent to loan detail page for counterparties.
- **Contract template engine + PDF generation** — pending lawyer-drafted Estonian templates (legal track, parallel to Bruno code).
- **Document attachment storage** — table linking signed PDFs to loans, retrofit existing 7 loans.

**3. Bruno DB sync for Rasmus's clone.** When he starts using Bruno operationally, his `data/bruno.db` needs population: either repeat the seed scripts on his side or one-time SQL dump export-import from Rain's. Coordinate when he's ready.

## Active flags / current state

- `AUGMENTATION_ENABLED = True` in `tools/screen_universe.py` (live since May 11)
- Bridge: `enabled=False` (will stay until re-split delivers dedicated options account)
- Maggy: suggestion mode ON, auto-approve OFF
- Winston: read-only at IBKR protocol level (placeOrder rejected)
- Portfolio lock supervisor: `merged=True` (logged at startup)
- Earnings cache table: exists
- AugmentationAudit table: exists
- TTD position (Maggy wheel, 100 shares cost basis $26.01): covered through June 26 by manually-sold $25.50 CC at $0.69 premium
- Bruno: live at `/borrower`. 5 counterparties, 7 loans, 13 movements, 3 restructures, 72 payments, 819 accrual snapshots. Daily accrual job scheduled 05:30 UTC. `cfg.app.bruno_run_integrations = False` (dev-codebase posture).

## In-flight work

**Forward-growth scoring** — `forward_growth_score` populated for 133 watchlist rows; integration considered done.

**Augmentation pipeline** — running. DECK, CPRT, ROL plus ~21 breakthrough names accepted May 11. Audit table active.

**Bridge v2** — built but disabled. Activates when new options account arrives.

## Known unfixed issues (deferred)

**Maggy `unrealized_pnl` AttributeError on U17562704.** Every put scan errors at `src/strategy/risk.py:1224` reading `pos.unrealized_pnl` from IBKR Position objects. Account-type/permission-specific bug — same code wrote 4 puts successfully against old U23886415 on Apr 27. Expected to disappear when new dedicated options account arrives. One-line fallback fix available if not: `getattr(pos, 'unrealizedPNL', 0.0)`. Cosmetic-only on this server (Winston is the active strategy here; son's clone runs real Maggy on U23886415).

**KSPI-style fill claiming.** Post-merge, all IBKR fills arrive on the same shared connection with no strategy tag. Whichever sync code runs first claims the fill. Maggy's `trade_sync` runs every ~5 min; Winston's runs every 4 hours. Maggy almost always wins. Display split is annoying, accounting is correct. Real fix: tag fills by strategy at sync time. Will be moot post-re-split.

**`portfolio_account_updates_failed` (TimeoutError on `reqAccountUpdates`).** Fired once at startup; may be one-time. Watch for recurrence.

**DB-write timing in `buyer.py`.** Holdings/transactions row written at submission time, before fill confirmation. If IBKR rejects, DB has phantom row until trade_sync's next reconcile cycle (15 min). Real fix: write only on fill. Not blocking under current read-only/suggestion-mode operation. Worth fixing before any auto-approve live mode.

**`trade_sync` reopen logic still incomplete.** At `trade_sync.py:625-630` the reopen flips status and clears `closed_at` but does NOT clear `realized_pnl`. With Apr 24 wheel.py fix in place, reopen shouldn't trigger for valid OPEN positions — but defense-in-depth one-liner deferred.

**Watchlist dividend NULL backfill.** 9 dividend holdings still NULL on `dividend_total_return_score` (HDB, IBN, BMY, CEG, 0ZQ, ALV, NLY, PBR, SFL). Phase 2b populates on next screener run.

**Six historical positions with `realized_pnl=0`, `total_premium_collected>0`.** PANW/UBER/SHOP/TTD ASSIGNED puts on Mar 29 (predate Apr 25 commit `2e9708c`), PANW stock CLOSED Apr 25, COIN covered_call EXPIRED Mar 9. ~$1,339 in unrecognized P&L on the dashboard. Decision: do not fix on this server. Merged-mode data is mixed pre-merge Maggy + post-merge Winston; manual UPDATEs now would risk corrupting numbers that should be on the other side of the future re-split. Kept here for post-re-split audit.

**CRWV id=152 stuck OPEN despite May 6 buy-back.** Identified; same merged-mode-data caveat applies.

**`check_position_size` cosmetic note.** Variable named `estimated_margin` is misleading — it's notional concentration (price × 100), not margin. Real margin enforcement happens via `get_whatif_margin` in `put_seller`. Rename deferred.

**GBP centralization.** 8+ separate sites in codebase divide by 100 for GBP. Surgical fix landed at `analyzer.py` on May 7; centralization deferred.

## Open items for son's clone

**Asia/EU put scan validation.** Needs son's diagnostic on U23886415 to confirm whether scans produce zero suggestions due to legitimate market mechanics (live-quote gate, position limits, etc.) or a real bug (universe filter mismatch, market label issue, gate firing only against US data). Diagnostic drafted and ready to forward.

## Lower priority

- Scorer fail-closed behavior — screener side largely self-corrects, metrics side now via staleness. Defer.
- 3-consecutive-failure exchange skip — dormant, leave alone unless it bites again.

---

# L3 — HISTORY

> Chronological session entries. Read only when investigating "when did we change X and why."

## Monday (2026-04-22) — Option B + Commit B + metrics-order fix

- Dividend tier ranks on dedicated `dividend_total_return_score` column (Option B). Tier-aware compound_quality formulas: growth/breakthrough use `0.40*growth + 0.25*valuation + 0.35*quality`; dividend uses `dividend_total_return_score` directly.
- Held holdings not in top-100 get score refresh in Phase 2b (Commit B). 9 dividend holdings still NULL on `dividend_total_return_score` until next screener run.
- Late evening: discovered ~58/126 watchlist rows had composite=0 because scheduler ran `recalc_scores_from_db` BEFORE `update_watchlist_metrics`. Fix (commit `2a95c7b`): swap order.
- XXII penny stock (1-for-15 reverse split pending) made breakthrough tier at composite 55.

## Thursday (2026-04-23) morning — four targeted fixes

1. **Breakthrough quality filters** (`3c744c6`) — `_check_breakthrough_eligibility` rejects ETFs, market_cap < $500M, reverse splits in last 18 months.
2. **Logger routing** (`65a9dec`) — structlog → `stdlib.LoggerFactory`.
3. **Stale-metrics flagging** (`06cb459`) — `metrics_stale` + `last_metrics_success` columns. Yellow ⏱ icon for stale > 24h.
4. **Cross-tier dedup** (`23f6f72`) — symbols in both CANDIDATE_POOLS and breakthrough scan deduplicate to breakthrough tier.

## Friday (2026-04-24) pre-market — wheel.py covered-call expiry bug

`wheel.py:439` used `expiry <= today` to mark covered calls EXPIRED — fired pre-market on expiry day while shares were still held. `trade_sync` reopened them. Repeating cycle exposed `wheel.write_covered_calls()` to thinking the lot was uncovered.

First commit (`dd16ec6`) changed `<=` to `<`. Reverted via better fix (`b41e39a`): keep `<=` but only mark `called_away` when IBKR confirms shares dropped. Otherwise defer to `trade_sync`.

**Cleanup applied live:** buggy expiry handler had set `realized_pnl` on three OPEN positions (PG=274, UBER=132, PANW=543). Cleared to 0.0.

## Saturday (2026-04-25) early morning — realized_pnl on covered-call assignments

Dashboard showed Realized P&L = -$36,779.24 after PG/PANW/UBER assignments. Three accounting bugs in `trade_sync.py` and `wheel.py`. Fix (`2e9708c`): `trade_sync.py` sums only `BUY_STOCK`/`SELL_STOCK`; defers CLOSED until matching `SELL_STOCK` present. `wheel.py` stops writing `realized_pnl` in `_handle_called_away`.

## Monday (2026-04-27) — put_seller stuck + IPO scanner broken

1. position_limit double-counted covered_call rows (`3cb2930`). Fixed.
2. Risk check order: cheap DB checks now run FIRST.
3. IPO scanner timedelta UnboundLocalError (`5607adc`). Fixed.

## Monday (2026-04-27) afternoon — DTE bypass fix

Four orders filled at wrong DTE: NVDA May 6 (9 DTE), PLTR/RKLB/DXCM May 8 (11 DTE). Root cause (`63f68c6`): `_evaluate_symbol` called `screen_puts()` without `dte_min`/`dte_max`. Fall-back to hardcoded 5-14 defaults. Fixed. Auto-approve flipped OFF after this incident.

## Tuesday (2026-04-28) — account merge

Test account U23886415 decommissioned on this server, migrated to son's clone. This server now runs Maggy and Winston code against U17562704 only, port 7496.

Config-only changes:
- `config/settings.yaml`: port 4001 → 7496, account U23886415 → U17562704
- `~/restart-all.sh`: dropped options tmux block. Backup `~/restart-all.sh.pre-merge-2026-04-28`
- `~/watchdog-trader.sh`: commented out options-gateway respawn. Backup `~/watchdog-trader.sh.pre-merge-2026-04-28`

Hiccup: watchdog cron respawned the killed options tmux session within 3 min, locking son out. Patched watchdog.

## Tuesday (2026-04-28) evening — dashboard auth + lockdown

Dashboard was publicly accessible. Closed via Caddy reverse proxy with basic auth + self-signed HTTPS. App binding `0.0.0.0:8080` → `127.0.0.1:8080`. ufw firewall: ports 80, 443 added. New URL: https://37.0.30.34/.

## Wednesday (2026-04-29) — KSPI fill-claiming + watchlist metrics asyncio fix

**KSPI fill-claiming (no code change):** Manual Winston buy-back showed only on Maggy's trade history. Whichever sync runs first claims fills. Listed in deferred bugs.

**Bug B fixed — get_ib_lock missing import** (`877426d`).

**Watchlist metrics catastrophic failure:** Asyncio races between Maggy clientId 12 and Winston clientId 97 sharing the gateway.

Two-layer fix:
- **Stages 1–3** (`ff631e2`, `baa533d`, `59b3197`): Wrap every Winston IBKR call site with `get_portfolio_lock()`.
- **Stages 4–5** (`392369b`): Cross-strategy supervisor — when merged-mode detected, acquires Maggy's `ib_lock` FIRST. Becomes no-op automatically post-re-split.

## Saturday (2026-05-03) — reconnect-race + scoring rebalance + pending orders

**Reconnect-race fix (`b734cf7`):** Wrap `ib.connect()` in `with _ib_lock:`.

**Scoring rebalance — Buffett-style:**
- `a25ccb1`: Fair-price base 0-24 points scaled across `discount_pct` from -5% to +5%. Composite blend 80/20 → 30/70.
- `45e6d72`: Composite floor `MIN_COMPOSITE_FOR_ACTION = 40.0`.
- `f65b9d4`: Direct-buy threshold 70 → 75.

**Pending orders dashboard fix (`2c161a8`).**

## Monday (2026-05-05) — Capital injections deposit-proof graphs

**Options graph formula fix (`3b407ff`):** €15K injection had caused graph to jump +100%. Five-file fix: `account_id` column on `portfolio_capital_injections`, formula `(nlv / total_invested - 1) × 100` anchored to first-point-zero.

**Margin interest investigation (no code change):** `NetLiquidation = TotalCashValue + AccruedInterest`. Already correct.

## Thursday (2026-05-07) — earnings gate, LSE pence, JSON history export

Four commits: `974b538` (EarningsCache model), `4cf7630` (get_next_earnings_date via CalendarReport), `58c6a55` (real has_upcoming_earnings, fail-CLOSED), `95f7d67` (LSE pence normalization).

JSON history export built for son's clone: 37 positions, 92 trades, 127 events.

## Friday (2026-05-08) — Forward-growth scoring + augmentation pipeline (22 commits)

**Forward-growth scoring** (`f55d8b2` → `940507a`, `ed76fad`, `699900b`): replaces old `40g + 25v + 35q` with 5-component Buffett-style composite. NVDA=80.5, MSFT=77.5, AAPL=68.0, JNJ=59.5, XOM=33.0.

**Augmentation pipeline** (`6a9ba4a` → `49b6785`): full pipeline built, gated behind `AUGMENTATION_ENABLED = False`.

**Son's clone:** Standalone Script B for importing pre-merge history delivered. Resolved.

## Sunday (2026-05-10) — augmentation pipeline validated, Commit E groundwork

(Commit `c2cced4` — details preserved in prior chronological versions; abbreviated.)

## Monday (2026-05-11) — STATE.md restructure + RULES.md refresh + augmentation live test

- `3d654df`: L3 history entry added for May 11
- `e25252b`: L1+L2 surgical edits
- `485fc90` ("Add files via upload"): Full STATE.md restructure attempted via GitHub web upload. **Result inadvertently overwrote some earlier L1/L2/L3 structure with a hybrid version.** Lesson learned: post-upload verification via GitHub raw URL is mandatory.
- RULES.md refresh: added credential safety rule, file handoff workflow, merged-mode operating section.
- **Augmentation live test:** flipped `AUGMENTATION_ENABLED = True`, ran screener. DECK, CPRT, ROL accepted to `discovered_pool.yaml`.

## Tuesday (2026-05-12) — STATE.md re-restructure + IBM duplicate-row fix shipped

**Morning triage:** "Portfolio watchlist scores look stale" — diagnosed not stale. Working as designed (slow-quality dominance).

**IBM duplicate transaction investigation:** Three rows in `portfolio_transactions` for IBM assignment — phantom row at $251.84 = strike − premium ($260 − $8.16). Root cause: holdings safety-net dedup at lines ~110–114 only checks for `action='put_assigned'`, missing `action='buy'`. Fix shipped: `fb3ef28` extends filter to `action.in_(["put_assigned", "buy"])`.

**Discovered_pool.yaml committed:** `079446a` — DECK, CPRT, ROL plus ~21 breakthrough additions from May 11 live test.

**STATE.md restructure:** GitHub history showed `485fc90` ("Add files via upload") partly overwrote prior L1/L2/L3 structure. Regenerated full STATE.md. Workflow lesson added: mandatory post-upload verification.

## Thursday (2026-05-14) — wheel.py rescue-mode (revert + refix) + Claude model migration

**TTD investigation:** assigned at $26.01 March 27, stock dropped to $20.49. From May 8 (prior CC expired worthless) onward, wheel.py wrote no new CCs. User manually sold $25.50 / June 26 CC at $0.69 premium (delta ~0.15).

**Diagnosis:** wheel.py's `_write_call` had hardcoded 0.30-0.45 delta range. Strike floor demanded above cost basis, delta range demanded close to spot. With stock far below cost basis, no overlap.

**Three commits:**
1. `c194715` (broken) — added rescue mode but nested inside `else` of `if wheel_exit_mode:`. Assignments default to `wheel_exit_mode=True`, so rescue branch unreachable for the exact scenario it was designed for. Son identified the bug during port to mesicap_trader.
2. `bfe3759` — revert of `c194715`.
3. `3c0bf55` — hoisted rescue check above the exit/normal split. Correct version. Three-branch evaluation: rescue (current_price < cost_basis × 0.95) → exit_mode → normal.

**Claude model migration (`cac0d2d`):** Anthropic deprecated `claude-sonnet-4-20250514` April 14, retires June 15 2026. Four call sites updated to `claude-sonnet-4-6`: `src/portfolio/scheduler.py:239`, `tools/screen_universe.py:626/875/1348`.

## Friday (2026-05-15) — Bridge v2 rewrite (4 commits) + STATE.md regen

**Cash Bridge v2 design and implementation:** replaced v1 annual-date sweep (July 31, fixed % of NLV above fixed EUR threshold) with performance-based design.

**Design decisions:**
- Trigger: NLV >= benchmark × factor (default 2.0)
- Sweep: NLV × transfer_pct (default 10%)
- Benchmark resets to post-sweep NLV after sweep
- Injections bump benchmark by exactly the injection amount (deposits never trigger fake sweeps)
- State is source of truth; BridgeConfig provides defaults only
- Daily check; optional cooldown_days between sweeps
- Merged-mode defensive: refuses self-transfer when source==target
- Stays `enabled=False` until re-split delivers dedicated options account
- CP Gateway transfer + manual-execution fallback preserved from v1

**Four commits, file-by-file with verification at each step:**
1. `f70381a` — `src/portfolio/bridge.py` rewrite. CashBridge class + module-level `bump_bridge_benchmark` hook function.
2. `4364047` — `src/web/routes/controls.py`. Removed min_portfolio_value/transfer_month/transfer_day. Added factor/cooldown_days. Added display state (bridge_benchmark, bridge_last_sweep_date, bridge_last_transfer_amount). State key rename `bridge_last_check_net_liq` → `bridge_last_check_nlv`.
3. `efcc298` — `src/web/templates/controls.html`. Form fields with descriptive+technical labels. 3-column display panel (Benchmark / Last sweep / Last check).
4. `70c5656` — `src/portfolio/capital_injections.py`. Hook: collects pending bridge bumps during injection loop, fires `bump_bridge_benchmark` after db.commit() succeeds. Wrapped in try/except — injection sync returns success even if Bridge hook fails.

**STATE.md regenerated end-of-day** with all May 12-15 changes folded into L2/L3. Surgical-edit attempt failed earlier due to byte-mismatch; full regeneration via the established GitHub web upload + raw URL verification path.

## Saturday (2026-05-16) — Bruno data model + initial pages + restructure modeled

**Built Bruno from skeleton to working portfolio view.** Day's work:

- **Data model:** 7-table SQLAlchemy schema in `src/borrower/models.py`. Generic enough to handle shareholder loans, private external loans, bank facilities, amortizing/capitalizing/revolving structures, multi-currency, back-to-back, restructurings, paid-payment tracking, audit log.
- **Separate database:** `data/bruno.db` (SQLite). Distinct from Maggy's `trades.db`. Gitignored.
- **Real data seeded:** 5 counterparties (MesiCap, Thirona, SK4 HoldCo, Waddy, Arvutitugi), 7 loans across 3 currencies (EUR/USD), 13 bank movements, 3 paper restructures, 72-payment Thirona octoserver schedule. Reconciled to LHV statements at €0.00 / $0.00 diff.
- **Loans index page:** totals by currency and purpose, table with all 7 loans, color-coded purpose badges. After several iteration rounds for the right layout (Outstanding / Facility / Headroom; "— bullet —" for non-revolving; cash + premium sub-line where applicable).
- **02.05.2026 restructure modeled correctly:** Thirona trading €8,500 → €8,682.90 (premium share €182.90). SK4 HoldCo trading €3,200 → €3,592.13 (premium share €392.13, valued from USD+AUD trading-account balances). Multiple incorrect attempts before landing on the right interpretation (SK4 contract clause 1.1 specifies single EUR figure; USD+AUD value priced into it). All three currencies on trading account reconcile to allocated amounts to the cent.
- **Wasted ~hour on phantom paste failures.** `mv /tmp/file path` was silently not taking effect. Solved by writing directly with `cat > path <<'EOF'` rather than `/tmp` middle step. Made it a permanent rule: direct writes for non-credential files.

## Sunday (2026-05-17) — Bruno detail page + origination forms + accrual engine + scheduling + commit

**Major build day. Bruno went from view-only to a system that can originate loans, record financial reality, and project debt burden.** Final state committed as `c7af8d0`.

**Built:**
- **Loan detail page** at `/borrower/loans/{id}`: breadcrumb, status badge, four key-facts cards (outstanding/rate/maturity/structure), outstanding breakdown table, full loan terms, movement history, amendment history, payment schedule, notes. Lender name on the Loans index now hyperlinks to the detail page. Route order initially placed `/loans/new` after `/loans/{loan_id}` — caused 422 (FastAPI tried to coerce "new" to int loan_id). Worked around by renaming to `/loans-new` (cleaner than route-order surgery).
- **New Loan form** at `/borrower/loans-new`: 4 card sections (Lender & Identification, Economic Terms, Schedule, Optional Details), ~20 fields, draft/active status toggle, lender dropdown from existing counterparties, +Add new counterparty link.
- **New Counterparty form** at `/borrower/counterparties-new`: 3 sections (Identification, Contact & Banking, Notes). Returns to New Loan form after save.
- **Accrual engine** (`src/borrower/accrual.py`, 447 lines): three methods (capitalizing/simple/amortizing). First version had bug: applied current rate from origination, ignoring rate amendments. Fixed by adding `_rate_segments` parallel to `_principal_segments`, then `_merge_timelines` to walk both timelines together. Verified against hand calculations: loan 4 (Thirona trading) €103.71 → €18.11; loan 5 (SK4) €39.72 → €7.49.
- **Snapshot recorder:** `record_snapshot(loan, date)` and `record_all_snapshots(date)` in `accrual.py`. Idempotent. Writes `accrued_amount` (delta since last snapshot) and `cumulative_accrued` (running total) to `interest_accruals` table.
- **Daily scheduling:** `job_record_accruals` registered at 05:30 UTC daily (quiet window — only 03:00 and 06:15 jobs nearby). Verified registered in trader.log scheduler startup line.
- **Historical backfill:** 819 daily snapshots written across all 7 loans from each loan's origination to 2026-05-17.
- **Display integration:** loan detail page outstanding breakdown now shows accrued interest + total amount owed today + method used + as-of date.
- **Config gating:** `AppConfig.bruno_run_integrations: bool = False` added. Future IBKR/LHV integrations check this flag and skip on this codebase, run on Rasmus's clone (where he'll set it `True`).

**Architectural decisions made during the day:**
- **Path 3 + Option C** (build contract generation now, with Bruno as source of truth, signed PDFs attached as evidence) — committed direction for contract handling. Template work blocked on lawyer.
- **Multi-metric LTV framework** (4 metrics + Net Worth as observability) — calibrated. Not too conservative for Phase 3 start; loosens with track record.
- **NLV definition recalibrated:** for LTV denominator, use gross unencumbered assets, not net-of-debt. Subordinated debt doesn't reduce collateral (protected by contract ranking). This is the bank-style approach.
- **Borrowing economics validated.** 24% target Maggy return vs 8% average cost of capital = ~16% gross spread. Real but compresses to ~10% in bad-year scenarios. The four-metric framework caps leverage at a level where worst-case drawdown is survivable.
- **Bruno dev/prod separation:** Rain's codebase = development. Rasmus's MesiCap clone = production (because MesiCap financials live on his server). Config flag is the gate.

**Process wins:**
- Switched to direct `cat > path` writes (avoiding `/tmp` middle step that silently failed yesterday). Worked reliably all day even over flaky phone-terminal sessions.
- Chunked multi-line writes to avoid paste failures on narrow terminal — verified line counts between chunks.
- All commit-worthy work staged carefully (Bruno DB excluded; backup files excluded via expanded .gitignore patterns).

**Committed and pushed:** `c7af8d0` "Bruno: loan portfolio management — initial system". 2,015 insertions, 14 files.
