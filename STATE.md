# Maggy & Winston — State Document

Last updated: 2026-05-11 (Mon, very late) — **14 commits shipped Monday**: Big-Tickets #1 (eviction `2620c65`) + #2 (anchored 30→25 breakthrough selection, six commits, verified across 3 live runs) done; B#3 (international augmentation) has three fixes in — client-side dedup (`8d6debb`), exchange/currency mapping guide (`46a24c4`), raw_proposal_json diagnostic column (`d0e39f0`). Watchlist field-name bug fixed (`59d9391`, 29 entries with `options_exchange:` renamed to `opt_exchange:`, activating silent European/Asian routing). Augmentation 'US-bias' originally suspected as a bug, conclusively diagnosed as system working correctly per scoring rubric design — see L1 Augmentation Pipeline section. SSH-only origin enforced after PAT-in-URL incident (third credential incident — new L1 rule 17).

This document has three layers. Read top-to-bottom in a normal session; jump to L3 only when investigating *why* something is the way it is.

- **L1 Fundamentals** — rarely change. Architecture, accounts, ports, scoring philosophy, module purposes, design rationale.
- **L2 Top of Mind** — changes often. Current operating mode, active flags, current work, next-session queue, known unfixed issues.
- **L3 History** — chronological dated entries (oldest first, newest last). For deep-inquiry only.

---

# L1 — FUNDAMENTALS

## Operating Mode (merged, since 2026-04-28)

This server runs **both Maggy code and Winston code against the portfolio account U17562704**, on the portfolio gateway (port 7496). The previous test account U23886415 was migrated to a clone codebase running on a separate machine under user `nexbit` (Ryan's son).

- **Suggestion mode: ON** for both strategies
- **Auto-approve: OFF** for options (flipped off 2026-04-27 after the 4 unintended NVDA/PLTR/RKLB/DXCM fills)
- **Winston (portfolio side) is read-only at IBKR level** — even if app-level suggestion_mode is flipped, IBKR rejects `placeOrder` at the protocol level. Two-layer safety: IBKR side read-only + app side suggestion mode + auto-approve OFF. For Winston to ever execute, BOTH switches need to be flipped deliberately.

**Backup files preserved on disk for clean revert when a new dedicated options account arrives:**
- `~/restart-all.sh.pre-merge-2026-04-28`
- `~/watchdog-trader.sh.pre-merge-2026-04-28`
- Original `~/restart-all.sh` and `~/watchdog-trader.sh` were patched in place; `config/settings.yaml` is gitignored due to API keys

**To re-split** (when new options account arrives): revert the three patches above (port back to 4001, account back to new ID, restart-all.sh options block restored, watchdog options block uncommented), point at new gateway, run `~/restart-all.sh`.

## Accounts & Connections

| Strategy | Account | Port | Client ID | Tmux | Status |
|---|---|---|---|---|---|
| Maggy code (options) | U17562704 (merged) | 7496 | 12 | `trader` | Runs against portfolio account, scans uselessly (cosmetic), zero suggestions |
| Winston code (portfolio) | U17562704 | 7496 | 97 | `trader` | Active, read-only at IBKR level |
| Son's clone (Maggy real) | U23886415 | (elsewhere) | — | — | Runs on separate machine under user `nexbit` |

App tmux `trader` runs everything: web server on port 8080 (localhost only), scheduler, both connections. Public access via Caddy reverse proxy at `https://37.0.30.34/` with HTTP basic auth.

## Son's Clone — Porting Relationship

Son's mesicap-trader (running U23886415 on a separate machine under user `nexbit`) is forked from this repo. **Options-side code is shared in lineage** — both sides run the same `src/strategy/`, `src/broker/trade_sync.py`, `config/options_universe.yaml`, etc. Over time the two repos drift independently:

- Son's side gets options-trading fixes faster because that's the live execution side (real auto-approve, real fills against U23886415).
- This server's side gets Winston (portfolio) fixes faster because Winston is the active strategy here.
- **Capital injections diverged 2026-05-05 → 2026-05-11**: this server got `3b407ff` (per-account deposits), son's side built its own anchor-removal fix on a `port-capital-injections-refactor` branch. The two implementations are not identical; that's accepted divergence.

**Porting workflow:** manual, case-by-case. No automated sync, no cherry-pick from son's repo. When son ships an options-side fix, the steps are:
1. Capture the diff (paste, screenshot, or commit hash + repo URL if reachable)
2. Read your current code at the same site
3. Decide: already covered, applies cleanly, applies with modification, or not relevant to your merged setup
4. If port: write patch via `/tmp/patchN.py` per RULES.md (no heredoc), one match assertion, fix → verify → commit

Port candidates accumulate in L2 next-session queue. Don't blind-apply.

## Module Purposes (what lives where)

| Path | Owner | Purpose |
|---|---|---|
| `src/strategy/` | Maggy | Options wheel logic (put_seller, wheel.py, risk.py) |
| `src/broker/trade_sync.py` | Maggy | Single source of truth for realized P&L; reconciles fills every ~15 min |
| `src/broker/connection.py` | Shared | IBKR connection management, `_ib_lock` (Maggy) and `get_portfolio_lock` (Winston supervisor) |
| `src/broker/market_data.py` | Shared | `has_upcoming_earnings()` — fail-CLOSED earnings gate |
| `src/portfolio/buyer.py` | Winston | CSP/direct-buy execution, entry guards (earnings → sentiment → Chronos) |
| `src/portfolio/analyzer.py` | Winston | Per-symbol scoring entrypoint: `_compute_composite_score`, raw_score, discount/RSI metrics |
| `src/portfolio/sync.py` | Winston | Portfolio holdings reconciliation |
| `src/portfolio/forecaster.py` | Winston | Chronos forecast integration |
| `src/portfolio/ibkr_fundamentals.py` | Winston | IBKR `ReportsFinSummary` + `CalendarReport` fetch (LSE/AEB/HKEX/BM fallback after FMP failures) |
| `src/portfolio/scheduler.py` | Winston | All scheduled jobs (metrics, snapshot, screener, chronos nightly) |
| `src/portfolio/models.py` | Winston | SQLAlchemy models: `PortfolioPutEntry`, `StockScore`, `EarningsCache`, `AugmentationAudit`, `PortfolioCapitalInjection` |
| `src/core/suggestions.py` | Shared | `TradeSuggestion` + `REVIEW_ONLY_ACTIONS` (sell_stock_review, reduce_position_review, sell_covered_call_review — NEVER auto-executed) |
| `src/web/routes/` | Shared | Dashboard endpoints (portfolio.py, dashboard.py) |
| `tools/screen_universe.py` | Winston | Monthly screener; **off-limits to changes per Ryan guardrail** for Buffett-style core scoring |
| `tools/discovered_pool.yaml` | Winston | Symbols accepted by Claude augmentation pipeline (lives in source tree, committed) |
| `tools/evicted_names.yaml` | Winston | Symbols banned from augmentation re-proposal |

## What Winston Does (Full Picture)

**Entry methods:**
1. Score ≥ 75 → direct buy (raised from 70 on 2026-05-03)
2. Score 40–75 → sell cash-secured put at target strike (get paid to wait)
3. Below 40 → watchlist member, no action
4. Stock held, above SMA, in profit → sell covered call suggestion (manual only)
5. Fundamentals deteriorate → sell stock suggestion (manual only)

**Entry guards** (`buyer.py`, in order):
1. **Earnings guard** — skip if earnings within 3 days. **Fail-CLOSED** (no IB / qualify fail / fetch fail / parse fail → block). 24h DB cache via `EarningsCache` table.
2. **Sentiment guard** — skip if Finnhub score < −0.3 last 7 days
3. **Chronos guard** — skip if forecast trend is "down". Helper `_get_chronos_trend(symbol)` lives in `src/portfolio/scheduler.py`

**Exit intelligence:**
- Trailing stop monitor: every 15 min, 5% below peak, creates manual suggestion
- Chronos exit suppression: suppress sell/reduce suggestions if `_get_chronos_trend()` == "up"
- `(not _cu) and create_suggestion(...)` pattern — lazy evaluation, skips call if `_cu` is True

## What Maggy Does (Full Picture)

- Sells short puts 0–3 DTE (USD), 0–7 DTE (non-USD)
- VIX > 30: full HALT
- SPY MA10 < MA20: TREND_BEARISH, delta forced to 0.10–0.20
- Profit taking at 50/65/75% — skips DTE ≤ 3 (let expire)
- 52-week high filter: blocks puts if stock > 40% below year high
- Earnings skip: 3-day window
- Wheel on assignment: covered calls at delta 0.30–0.45 (goal is to get called away)
- **No stop-loss on puts by design** — wheel strategy, assignment is intended outcome

## Scoring Philosophy

**Three-tier portfolio allocation:** 15% dividend / 60% growth / 25% breakthrough. Tier classification done in screener; dedup across tiers favors breakthrough (more specific thesis).

### Composite score (dashboard value)

Stored on `portfolio_watchlist`, computed by `recalc_scores_from_db`. Formula since 2026-05-03:

```
composite_score = 0.30 × raw_score + 0.70 × compound_quality_pct
```

**Action mapping:**
- Below 40 → watchlist member, no action
- 40–75 → sell CSP at target strike
- Above 75 → direct buy (rare; requires both real price signal AND high quality)
- Override gates (deep_discount > 15%, RSI < 20, volume_surge + trend_healthy) promote to direct_buy regardless

### `raw_score` (technical signal, by `_compute_composite_score` in analyzer)

- Fair-price base: 0–24 points scaled across `discount_pct` from −5% (above SMA, 0 pts) to +5% (below SMA, 24 pts). Saturates exactly where the gated SMA signal takes over.
- Plus SMA-discount, RSI-oversold, and 52w-low gates (panic-detector layer)
- Anti-chase guard at −20% blocks deeply overpriced stocks

### `compound_quality_pct` (within-tier 1–100 percentile)

Tier-aware formulas (Option B from April 22):
- **Growth / breakthrough:** `0.40 × growth + 0.25 × valuation + 0.35 × quality`
- **Dividend:** `dividend_total_return_score` directly

### `forward_growth_score` (5-component Buffett-style, May 8)

Stored on `StockScore`. Five sub-components, weighted:
- Revenue durability — 25%
- Compounding quality — 25%
- Operating leverage — 20%
- Innovation investment — 15%
- Capital efficiency — 15%

**Hard cap at 30** if 3+ years negative net income AND 3+ years negative FCF over a 5-year window.

All five sub-scores preserved on `StockScore` (Commit O) so the augmentation prompt can show them per-name.

**Status:** computed and stored, but `portfolio_score` formula does NOT yet use it — see L2 next-session queue (Commit E flip).

### Composite-write chain (after Wed 2026-04-22 metrics-order fix)

1. `job_portfolio_update_metrics` runs every 4 hours
2. `update_watchlist_metrics` loops every watchlist stock → IBKR calls → writes price/sma/rsi/raw + `last_metrics_success`
3. End of loop: scan for stocks with `last_metrics_success` older than 24h → set `metrics_stale=True`
4. `recalc_scores_from_db` loops again with populated discount/RSI, calls `_compute_compound_quality`, writes `composite_score`
5. Logs `portfolio_metrics_updated` (with `newly_stale` count) and `portfolio_scores_recalced`

## Augmentation Pipeline (Claude-driven universe extension)

Monthly screener invokes Claude to propose 5–10 high-conviction names beyond the hand-coded universe, scores them, accepts those that beat the rank-60/rank-15 cutoff.

**Architecture (all landed 2026-05-08):**
- Two pools: `discovered_growth`, `discovered_dividend` — separate, no yield-routing for discovered names
- Every symbol evictable (including hand-coded names); no editorial floor
- Eviction triggers only when pool > cap (180 growth / 45 dividend); list-size hygiene, not a quality verdict
- General augmentation, not slot-specific — Claude proposes high-conviction names, not "replace these specific laggards"
- Margin = +0 (any improvement over rank-60 cutoff accepts); tunable to +3 later if churn excessive
- Strict failure handling on FMP miss (no retry); audit row written with `reason="scoring_failed"`
- Audit trail = SQLite table `augmentation_audit` (11 columns: id, run_date, tier, proposed_symbol, proposed_score, cutoff_score, displaced_symbol, displaced_score, accepted, reason, notes)
- `discovered_pool.yaml` and `evicted_names.yaml` live in source tree (committed)

**Phase placement:** PHASE 2.5 between PHASE 2 (breakthrough scan) and PHASE 3 (portfolio universe build). Best-effort: any exception inside PHASE 2.5 caught, logged, augmentation skipped, screener continues normally.

**Status:** gated behind `AUGMENTATION_ENABLED = False` in `tools/screen_universe.py`. No API calls until manually flipped. See L2 next-session queue for live test plan.

**Augmentation prompt hardening (2026-05-11):** Three structural improvements landed during Big-Ticket #3 work.
1. Client-side dedup of Claude's proposals against the 518-symbol exclusion set, since the prompt-level exclusion is unreliable at that length (commit `8d6debb`).
2. Exchange/currency mapping guide embedded in both growth and dividend prompts, using actual CANDIDATE_POOLS conventions (21 markets mapped), plus ticker convention reminders like TTE-not-FP (commit `46a24c4`).
3. Per-proposal raw JSON stored in `augmentation_audit.raw_proposal_json` for diagnostic forensics (commit `d0e39f0`). Schema auto-migration runs on startup via `_migrate_columns()`.

**Geographic distribution by design (not bug):** The growth tier ends up 84% US, dividend tier 58% non-US, breakthrough 96% US. This reflects where rubric-specific quality actually lives: growth (revenue durability + compounding quality) concentrates in US tech, dividend in foreign income-payers (Canadian banks, UK/EU telcos/utilities, Norwegian energy, Chinese insurers), breakthrough in speculative US innovation. Universe input is 77% non-US growth + 39% non-US dividend; the scoring rubric correctly filters down to where quality actually exists. Forcing geographic diversity via augmentation would push Claude to propose lower-quality names that fail scoring correctly. **No "Fix B" needed.** Augmentation proposing mostly US names is rational, not biased.

## Asyncio Lock Architecture (post-merge serialization)

Post-merge, two `ib_insync` clients (Maggy clientId 12, Winston clientId 97) hit the same gateway through the same Python process. Pre-merge they had separate gateways; collisions were physically impossible. Post-merge, every overlapping IBKR call is a race.

**Two-layer fix (Stages 1–5, all landed 2026-04-29):**

- **Layer 1 (Stages 1–3):** every Winston IBKR call site wrapped with `get_portfolio_lock()`. Files covered: connection.py, buyer.py, analyzer.py, forecaster.py, sync.py, ibkr_fundamentals.py, bridge.py.
- **Layer 2 (Stages 4–5):** `get_portfolio_lock()` returns a supervisor that acquires Maggy's `_ib_lock` FIRST, then Winston's `_portfolio_lock`. Cross-strategy serialization without touching Maggy code. Detected at module import via `_detect_merged_with_options()` (compares `settings.ibkr.host/port/account` vs `settings.portfolio.ibkr_host/ibkr_port/ibkr_account`). Logged at startup as `portfolio_lock_mode merged=True`.
- **Layer 3 (reconnect race fix, 2026-05-03):** `ib.connect()` plus post-connect setup wrapped in `with _ib_lock:`. `_ib_lock` is RLock so safe if called from a thread already holding it.

**Auto-disengage:** when new options account arrives and ports diverge, `_detect_merged_with_options()` returns False automatically. Supervisor becomes a no-op without code change. Removal instructions for permanent re-split inline in `connection.py` under the MERGE-ONLY header.

## Dashboard Security

- **Caddy 2.11.2** reverse proxy on ports 80 + 443 (public-facing)
- **Self-signed TLS cert** auto-generated for 37.0.30.34 (`caddy tls internal`) — browser warns once per device, click through, remembered after
- **HTTP basic auth** user `maggycian` (bcrypt cost 14, hash stored in `/etc/caddy/Caddyfile`)
- **HTTP (80) redirects to HTTPS (443)**
- **Trader app web binding** `127.0.0.1:8080` (localhost only) — world cannot bypass Caddy by hitting `:8080` directly
- **ufw firewall:** ports 80 and 443 open; 8080 closed externally

**Files:**
- `/etc/caddy/Caddyfile` — root-owned, sudo to edit. Backup at `/etc/caddy/Caddyfile.default-2026-04-28`
- `/var/log/caddy/dashboard-access.log` — caddy:caddy ownership

**URL:** https://37.0.30.34/ — username `maggycian`, password set via `caddy hash-password`.

## Capital Injections (per-account)

Since 2026-05-05 (commit `3b407ff`), capital deposits are tracked per-account via `portfolio_capital_injections.account_id` (VARCHAR(20)). Options graph formula is now anchored to deposits:

```
performance_pct = (nlv / total_invested - 1) × 100
```

Anchored to first-point-zero. Reads `options_account` from `cfg.ibkr.account`, calls `get_total_invested_usd(account_id=options_account)`. When new options account arrives and is configured for Flex sync, the options graph will automatically filter deposits to that account only — no manual intervention needed.

## Database Critical Facts

- **DB type:** SQLite, path in `settings.yaml`
- **`get_db()` is `@contextmanager`** — always `with get_db() as db:`. NEVER `next(get_db())` — fails.
- **`account_snapshots` has two NLV fields:** `net_liquidation` (options → options performance graph), `portfolio_nlv` (portfolio → portfolio performance graph). Never mix.
- **`TradeSuggestion` lives in `src/core/suggestions.py`** — not `models.py`
- **Portfolio loans** must use `TotalCashBalance` where `currency == "BASE"` only. Never sum per-currency (causes doubling).
- **Accrued interest** comes from file cache (`data/portfolio_account_cache.json`), NOT from live Flex call during IBKR refresh. The disconnected path must NOT write 0.0 to the file. **Accrued interest is YTD** — IBKR portal shows MTD. ~$3,583 YTD vs ~$1,582 MTD is expected.

## Wheel Accounting Model (confirmed 2026-04-25)

- **Total wheel-cycle realized** = `collected_premium + (call_strike − put_strike) × 100 − fees`
- **Stored on call positions (EXPIRED):** the collected_premium portion (net of any roll buy-backs)
- **Stored on stock positions (CLOSED):** the strike-difference portion only
- **trade_sync owns realized_pnl calculation** — wheel.py does NOT write realized_pnl (since commit `2e9708c`)
- **trade_sync stock-close formula:** sums only `BUY_STOCK` and `SELL_STOCK` (commission inclusive). Defers marking position CLOSED until matching SELL_STOCK trade present in DB.

## Critical Per-Region Facts

- **FMP is dead** for LSE/AEB/HKEX/BM. Use IBKR `ReportsFinSummary` via `src/portfolio/ibkr_fundamentals.py`.
- **LSE prices come in pence**, not pounds. Normalized at source in `analyzer.py` since 2026-05-07 (commit `95f7d67`). Eight other GBP-handling sites in codebase still divide by 100 surgically; centralization deferred.
- **DTE config** for options: USD 0–3 DTE at low/mid VIX. Hardcoded fallback in `screen_puts()` was 5–14; that fallback caused the April 27 mistaken fills before `_evaluate_symbol` was patched to pass DTE explicitly.
- **Watchlist YAML field name:** `opt_exchange:` is the correct field for derivatives-exchange routing in `config/watchlist.yaml`. Son's clone uses `options_exchange:` (different naming convention). If importing entries from son's repo, rename the field. Code at `src/strategy/universe.py:147` reads `opt_exchange` only; `options_exchange:` is silently ignored. Bug surfaced 2026-05-11 — 29 entries had wrong name, all silently ignored for weeks, fixed in commit `59d9391`.

## Scheduled Jobs

| Time ET | Job |
|---|---|
| 09:35 | Account snapshot (both NLV fields) |
| Market hours | Put scans, profit checks, health checks |
| Every 15 min | Trailing stop monitor, trade_sync |
| Every 4h (00:43, 04:52, 08:54, 12:54) | Watchlist metrics + recalc scores |
| 08:00 | Accrued interest Flex refresh |
| 08:00 ET | IPO date calendar scan |
| Every 5 min | IPO ticker scan |
| 17:30 | Chronos nightly forecast (all watchlist stocks) |
| Monthly Mon | Portfolio screener |
| 06:30 | BRK-B history update |
| Market close | Cancel stale orders |
| Midnight UTC | IBC gateway restart |
| Every 5 min (cron) | Watchdog (portfolio + trader only; options block disabled merge-mode) |

## Operational Facts

- **Server:** `rain@octoserver-genoax2:~/automatic_option_trader`
- **Python venv:** `.venv/bin/python3` (Python 3.12)
- **Chronos venv:** `~/timesfm_env` (Python 3.11) — separate from trading venv. Both need `chronos-forecasting` installed.
- **GitHub:** https://github.com/rainrosimannus-spec/automatic_option_trader
- **Restart:** `~/restart-all.sh` — wait for **2 phone 2FA approvals** (gateway first, ~35s pause, then app). NOTE: in merged mode only one gateway starts (portfolio @ 7496), but watchdog patches mean only one 2FA needed at gateway boot; the second 2FA prompt mentioned in legacy docs was for the now-disabled options gateway.
- **Stale-process recovery:** `pkill -9 -f ibcalpha; pkill -9 -f IbcGateway; pkill -9 -f GWClient; tmux kill-server; sleep 5; ~/restart-all.sh`
- **Crash diagnostic:** `cd ~/automatic_option_trader && source .venv/bin/activate && python -m src.main 2>&1 | head -50`
- **FMP quota:** 250/day (screener uses ~150–300, +60 for breakthrough filter checks)
- **`check_interval_hours`:** 4 (`config/settings.yaml`)
- **First metrics run after restart:** startup + 120 seconds

## Working Rules (also enforced in RULES.md)

1. **No heredoc for Python** — `<< 'EOF'` breaks on quotes. Write patch files via `open('/tmp/patchN.py','w').write(...)`.
2. **No manual file editing** — every code change is a copy-paste-ready terminal command in a code block.
3. **Read before writing** — fetch or grep the exact current code before any replacement.
4. **Fix → verify → commit** — one change at a time. Never bundle unverified changes.
5. **Syntax check before writing** — `ast.parse()` before file write. If syntax error, do not write; show failing lines.
6. **Admit mistakes immediately** — no excuses. Say what went wrong, fix it properly.
7. **Copy, don't invent** — if something works on the options side, copy it exactly. Don't invent new solutions when working ones exist.
8. **One match assertion per patch** with ABORT on mismatch.
9. **Never assume a session's context** — read actual code or DB data before drawing conclusions.
10. **Don't touch Maggy-side code** (`src/strategy/`, `src/broker/`, options-side of `src/scheduler/jobs.py`) when working on Winston issues.
11. **Don't conflate** `PortfolioPutEntry` (Winston's CSPs) with `Position` (Maggy's wheel).
12. **When Ryan pushes back, re-examine, don't defend.**
13. **Never claim a fix works without proof:** restart, test, verify DB values, then claim.
14. **`composite=0` for valid stocks is NEVER correct.**
15. **If Ryan asks a question before pasting output, answer the question first.** Don't assume output that wasn't there.
16. **Cross-check STATE.md after writing.** Empty pattern matches mean nothing changed; verify content actually updated.
17. **Never request commands that expose credentials.** Personal Access Tokens are often embedded in HTTPS git remote URLs. Never ask Ryan to run `git remote -v`, `env`, `printenv`, `cat .env`, `cat .git/config`, `history`, or `ps auxe` directly. Use redacted variants like `git remote -v | sed 's|//[^@]*@|//[REDACTED]@|g'`. Ryan is not a programmer and will not catch leaks. **Third incident on 2026-05-11 — strict rule, no exceptions.**

## Don'ts

- Don't touch Maggy-side code while working on Winston
- Don't run monthly screener without warning (20–40 min, FMP quota, lock held)
- Don't rewrite screener logic in backfill scripts
- Don't try to work while Ryan sleeps
- Don't assume pushed == live (always restart)
- Don't conflate "dashboard looks wrong" with "is broken"
- Don't use complex heredoc for patches
- Don't assume STATE.md write succeeded — verify with `head`/`tail`

---

# L2 — TOP OF MIND

## Current Operating Flags

| Flag | Value | Notes |
|---|---|---|
| `suggestion_mode` | ON (both strategies) | Maggy + Winston |
| `auto_approve` (options) | **OFF** | Flipped 2026-04-27 after 4 unintended fills |
| Winston IBKR-level | **read-only** | Two-layer safety with app-level suggestion mode |
| `AUGMENTATION_ENABLED` | **False** | `tools/screen_universe.py` — no Claude API calls until manually flipped |
| `portfolio_lock_mode` | merged=True | Cross-strategy supervisor active; auto-disengages when ports diverge |
| `portfolio_score` formula | **`forward_growth_score`** | Shipped 2026-05-09 in commit `e958fc1` (Commit E). Old `40g + 25v + 35q` retired. |
| `direct_buy_threshold` | 75 | Raised from 70 on 2026-05-03 |
| `composite_floor` | 40 | Below this, no buy_signal (since 2026-05-03) |
| Composite blend | 30% raw / 70% quality | Since 2026-05-03 |
| Breakthrough prompt | **v4.1 validated** | 22 names, healthy megatrend spread, 1 non-USD (Sony 6758) — same as v3. Geographic deferred to universe expansion in `CANDIDATE_POOLS`, not prompt. |
| Breakthrough selection mode | **Anchored 30→25** | Phase A fresh proposals + Phase B Claude-select-25-from-merged. Shipped 2026-05-11. Verified across 3 live runs. |
| `breakthrough_history` cap | 75 entries | Eviction shipped 2026-05-11 (`2620c65`). `last_seen` window protection (current-week entries protected). |
| `augmentation_audit.raw_proposal_json` | populated | From 2026-05-11 18:28 onward; rows before that have empty string. |
| Git origin URL | **SSH-only** | `git@github.com:rainrosimannus-spec/automatic_option_trader.git`. PAT-in-URL incident 2026-05-11 forced SSH switchover. |

## Active Covered Calls

| Symbol | Status | Notes |
|---|---|---|
| SHOP | Active | — |
| UBER | Active | — |
| PANW | Active | — |
| PG | Active | — |
| TTD | Scanning | Not yet placed |

**Roll-up feature (confirmed future spec):** when stock price > strike + ~7% AND DTE > 5 → surface manual `sell_covered_call_review` suggestion to roll up and out.

## Next-Session Queue (priority order)

### High priority

1. **Pull INFY + ITC from `config/options_universe.yaml`** (free, immediate). NSE is un-tradeable until IBKR subscription added (see L3 May 11 NSE diagnosis). Leaving these in burns scan cycles producing zero candidates every run. Surgical removal — one-line YAML edit per symbol.

2. **NSE SMART-routing fix at `src/broker/market_data.py:100`** — `contract.exchange = "SMART"` override rejects INR stocks because IBKR's SMART aggregator doesn't include NSE for INR-denominated names. Conditionally skip the override when `currency == 'INR'` and use `primaryExchange='NSE'` directly. **Only useful if NSE subscription gets added at IBKR** — otherwise pulling INFY/ITC (#1) is sufficient. Defer until subscription decision.

### Medium priority

4. **Verify watchlist rename effect on next foreign-market scan.** Tomorrow's scheduled scans for European and Asian markets (EUREX/AEB/BVME/SBF/ASX/SEHK etc.) should now produce real candidates instead of silently no-op'ing. Watch dashboard suggestions tab + tmux trader pane for any new put suggestions on EU/Asian symbols. Reverification of `59d9391` impact in production.

5. **Breakthrough fresh-proposal count variance investigation.** Run id=1 had fresh=24, id=2 had fresh=17, id=3 had fresh=20. Claude's conviction count varies meaningfully between runs. Is this noise (resampling) or signal (changing market conditions)? Worth tracking over the next 10 runs to see if a pattern emerges. Don't intervene yet — instrument first.

6. **Son's clone — verify EU scan firing post-asyncio-reentry-fix.** 07:58 UTC wake-up re-pulls his journal to confirm LSE / AEB / BVME scans actually fired after Europe opened at 07:00 UTC. Status: pending son's verification, not your work directly. Just track outcome.

8. **Son's clone — NVDA P&L recovery.** Needs JSON history import (Script B). Standalone single-file Python at `/tmp/import_options_history_standalone.py` on this server, sent to son via email. **Resolved on this end; awaits son's run.**

9. **Watchlist dividend NULL backfill** — 9 dividend holdings still NULL on `dividend_total_return_score` (HDB, IBN, BMY, CEG, 0ZQ, ALV, NLY, PBR, SFL). Phase 2b populates on next screener run; verify after next monthly screener.

10. **Optional Commit N** — `--dry-run-augmentation` flag for sanity-checking prompts without persistence/audit writes. Nice-to-have.

### Lower priority

11. **RULES.md merged-mode update — overdue.** RULES.md still describes pre-merge architecture (Maggy 4001 / Winston 7496 separate). Since 2026-04-28 both run against U17562704 on 7496; U23886415 on son's clone. Needs new "Current operating mode (merged, since 2026-04-28)" section noting suggestion mode + auto-approve OFF, plus the backup files `~/restart-all.sh.pre-merge-2026-04-28` and `~/watchdog-trader.sh.pre-merge-2026-04-28` for re-split. Flagged twice now in handoffs; should land next session.

12. **GBP centralization** — 8+ separate sites in codebase divide by 100 for GBP. Surgical fix landed at `analyzer.py` on May 7; centralization deferred.

13. **Scorer fail-closed behavior** — screener side largely self-corrects; metrics side now via staleness flag. Defer.

14. **3-consecutive-failure exchange skip** in `update_watchlist_metrics` — dormant in code. Leave alone unless it bites again.

## Known Unfixed Issues

### Cosmetic / log-noise only (not blocking)

- **`'Position' object has no attribute 'unrealized_pnl'`** — fires on every Maggy put_seller scan against U17562704 (ISRG, CRWD, CNR, AVGO, SOFI, NFLX, etc.). Maggy code expects an attribute that U17562704 Position objects don't have. Same code wrote 4 puts successfully against U23886415 on April 27 — proves the bug is account-type/permission-specific, not a code defect. When Maggy gets a new independent options account configured like U23886415, the existing code should work without patching. If the new account also fails, fix is a one-line `getattr(pos, 'unrealizedPNL', 0.0)` at `src/strategy/risk.py:1224`. **Until then, Maggy on this server scans uselessly and generates zero suggestions — cosmetic-only because Winston is the active strategy here.**

- **Watchlist staleness "looks stale" feeling** — investigated 2026-05-07. All 129 rows have `metrics_stale=0`, last update 2.9h ago — well within 4h cycle. The "looks stale" is the new 30/70 scoring blend producing stable scores dominated by slow-moving quality (70% weight), correctly per design.

- **Tier-count shortage (59 growth, 14 dividend on some runs vs 60/15 target)** — investigated 2026-05-11. Root cause is not a bug. Augmentation only accepts proposals that beat the rank-60 (growth) or rank-15 (dividend) cutoff. On runs where no Claude proposal beats the cutoff, the tier stays at whatever count the regular ranking produced. Forcing names to fill quota would lower quality. **Honest result. Not fixing.**

### Architectural debt (will resolve when re-split happens)

- **KSPI-style fill claiming** — post-merge, all IBKR fills arrive on shared connection with no strategy tag. Whichever sync code runs first claims the fill. Maggy's trade_sync runs every ~5 min; Winston's every 4 hours. Maggy almost always wins. Real fix is per-strategy fill tagging — non-trivial. Will be moot post-re-split. Decision: not fixing now.

- **Six historical positions with `realized_pnl=0`, `total_premium_collected>0`** — PANW/UBER/SHOP/TTD ASSIGNED puts on March 29 (predate April 25 commit `2e9708c` stock-close fix), PANW stock CLOSED on April 25 (possibly correct at break-even), COIN covered_call EXPIRED on March 9. ~$1,339 in unrecognized P&L on dashboard. **Decision: do not fix on this server.** Merged-mode data is mixed pre-merge Maggy + post-merge Winston; manual UPDATEs now would risk corrupting numbers that should be on the other side of the future re-split.

- **CRWV id=152 stuck OPEN despite May 6 buy-back** — identified but not pursued; same merged-mode-data caveat applies.

- **`trade_sync.py:625-630` reopen logic incomplete** — reopen flips status and clears `closed_at` but does NOT clear `realized_pnl`. With wheel.py fix (`b41e39a`) in place, reopen shouldn't trigger for valid OPEN positions, but defense-in-depth fix would be a one-line addition (`realized_pnl=0` on reopen). Deferred.

- **DB-write timing in `buyer.py`** — holdings/transactions row written at submission time, before fill confirmation. If IBKR rejects, DB has phantom row until trade_sync's next reconcile cycle (15 min). Real fix is to write only on fill. Not blocking under current read-only/suggestion-mode operation. **Worth fixing before any auto-approve live mode.**

- **`portfolio_account_updates_failed`** (TimeoutError on `reqAccountUpdates`) — fired once at startup post-merge. May or may not be lock-related. Watch for recurrence.

### Long-standing low-priority

- **NLV staleness 16:00–20:00 ET** — `accountValues()` push stops after idle. Not investigated.
- **Structlog routing to file** — fixed 2026-04-23 (commit `65a9dec`, stdlib.LoggerFactory). Application events now land in `trader.log`.
- **TimesFM GPU device bug** — `model.device` returns `cuda:0` even after `.to('cpu')`. Workaround: `type(tfm.model).device = property(lambda self: torch.device('cpu'))` before compile. Not integrated — Chronos preferred.

## Dashboard Quick Reference

- **URL:** https://37.0.30.34/
- **Username:** `maggycian`
- **Password:** set via `caddy hash-password` (stored in `/etc/caddy/Caddyfile` as bcrypt hash, cost 14)
- **Old URL dead from outside:** `http://37.0.30.34:8080/` — connection refused for non-localhost. Caddy still uses it internally.
- **Safari quirk:** may hang on first visit to self-signed cert page until cache cleared or page reopened. Other browsers normal.

---

# L3 — HISTORY

Chronological, oldest first → newest last. Read only when investigating *why* something is the way it is. Every change to L1 or L2 should leave a footprint here.

## Wednesday (2026-04-22) — Option B + Commit B + metrics-order fix

- Dividend tier ranks on dedicated `dividend_total_return_score` column (Option B). Tier-aware compound_quality formulas: growth/breakthrough use `0.40×growth + 0.25×valuation + 0.35×quality`; dividend uses `dividend_total_return_score` directly.
- Held holdings not in top-100 get score refresh in Phase 2b (Commit B). 9 dividend holdings still NULL on `dividend_total_return_score` (HDB, IBN, BMY, CEG, 0ZQ, ALV, NLY, PBR, SFL) until next screener run.
- Late evening: discovered ~58/126 watchlist rows had composite=0 because scheduler ran `recalc_scores_from_db` BEFORE `update_watchlist_metrics`. Fix (commit `2a95c7b`): swap order. New stocks now get fresh discount/RSI BEFORE compound_quality computation. Holdings were unaffected because they had populated metrics from prior cycles.
- XXII (22nd Century — $15M tobacco penny stock with 1-for-15 reverse split pending) made breakthrough tier at composite 55. Confirmed Claude's breakthrough scan returned it; no post-LLM filter rejected it.

## Thursday (2026-04-23) morning — four targeted fixes

1. **Breakthrough quality filters** (commit `3c744c6`) — `_check_breakthrough_eligibility` rejects ETFs (FMP `isEtf=true`), market_cap < $500M (uses FMP profile `mktCap` as backup), and any reverse split in last 18 months. Adds 2 FMP calls per breakthrough candidate (~60/run). Quota-safe.

2. **Logger routing** (commit `65a9dec`) — switched structlog from `PrintLoggerFactory` to `stdlib.LoggerFactory`. Application events (`portfolio_score_saved`, `portfolio_metrics_updated`, etc.) now land in `trader.log` instead of stdout-only. Fixes the debugging blind-spot from Wednesday night where tmux buffer rolled and we had to guess at what happened.

3. **Stale-metrics flagging** (commit `06cb459`) — added `metrics_stale` (bool) and `last_metrics_success` (datetime) columns. Inner `_update_watchlist_metrics` writes timestamp on success. Outer loop scans at end and flips `metrics_stale=True` for stocks with `last_metrics_success` older than 24h. Dashboard shows yellow ⏱ icon next to stale symbols. No extra IBKR calls. First run after deployment will flag 100+ stocks (NULL timestamps); self-corrects within 4h.

4. **Cross-tier dedup** (commit `23f6f72`) — symbols can appear in `CANDIDATE_POOLS` (scored as growth/dividend) AND in Claude's breakthrough scan. Without dedup, Phase 2 of screener reclassifies the same symbol twice in one run (growth→breakthrough, then breakthrough→growth). TSLA, NVDA, PLTR all flip-flopped in latest screenshot. Fix: dedup before tier slicing — breakthrough wins because it's the more specific thesis assignment.

## Friday (2026-04-24) pre-market — wheel.py covered-call expiry bug

Investigating dashboard showing PG, PANW, UBER calls as expired pre-close, system trying to write new calls on positions still open in IBKR. Found `wheel.py:439` used `expiry <= today` to mark covered calls EXPIRED — fired pre-market on expiry day while shares were still held. trade_sync (every 15 min) saw IBKR still had the contracts, reopened them. Repeating cycle. Window between flip-out and reopen exposed `wheel.write_covered_calls()` to thinking the lot was uncovered → phantom new calls.

First commit (`dd16ec6`) changed `<=` to `<` — would have skipped early-exercise detection same-day. **Reverted via better fix (`b41e39a`)**: keep `<=` to catch same-day events, but only mark `called_away` when IBKR confirms via shares dropping below covered amount. Otherwise defer to trade_sync (the proper authority for "is contract still alive at IBKR"). Mirrors the put-side defensive pattern at `wheel.py:99` which already does this correctly.

After restart: PG/PANW/UBER stay OPEN through expiry day. trade_sync detects assignment or worthless expiry from IBKR's portfolio state at/after market close.

**Cleanup applied live:** the buggy expiry handler had set `realized_pnl` on the three OPEN positions (PG=274, UBER=132, PANW=543) — phantom values matching premium collected. Cleared to 0.0 via direct UPDATE. Schema enforces NOT NULL so 0.0 not NULL. Dashboard wasn't displaying these because realized P&L queries filter to status IN (CLOSED, EXPIRED, ASSIGNED), but the dirty values would have caused incorrect accounting on actual close.

**Trade_sync reopen logic still incomplete:** at `trade_sync.py:625-630` the reopen flips status and clears `closed_at` but does NOT clear `realized_pnl`. With wheel.py fixed, reopen shouldn't trigger for valid OPEN positions anymore — but if it ever does fire, the same bug returns. Defense-in-depth fix would be a one-line addition (set `realized_pnl=0` on reopen). Deferred.

## Saturday (2026-04-25) early morning — realized_pnl on covered-call assignments

Dashboard showed Realized P&L = −$36,779.24 after PG/PANW/UBER assignments at expiry. Investigation found the loss was approximately the sum of stock sale proceeds at strike — meaning sale proceeds were being recorded as a loss instead of offsetting the assignment cost basis.

**Three accounting bugs found:**

1. **`trade_sync.py:580-595`** stock-close formula included ASSIGNMENT and CALLED_AWAY trade types in the realized P&L sum. These are IBKR accounting markers alongside the underlying BUY_STOCK/SELL_STOCK rows — not separate cash flows. Including them double-counted on the cost side and off-by-strike on the proceeds side.

2. **`trade_sync.py` timing race:** the code marked stock CLOSED in the sync that detected the IBKR position disappear, but the matching SELL_STOCK trade often arrived in a *later* sync. Realized got computed with only BUY_STOCK present, freezing at `-cost_basis`. Never recomputed afterward.

3. **`wheel.py` `_handle_called_away`** wrote its own `realized_pnl` with formula `(sale - cost + total_premium)`. But `cost_basis` was already net of put premium, AND `total_premium_collected` had already been realized when each option closed. Triple-counted on the premium side, undercounted on the put-strike side.

**Fix (commit `2e9708c`):**
- `trade_sync.py`: sum only BUY_STOCK and SELL_STOCK (commission inclusive). Defer marking the position CLOSED until a matching SELL_STOCK trade is present in the DB. Single sweep handles both timing and accounting.
- `wheel.py`: stop writing `realized_pnl` in `_handle_called_away`. Just mark CLOSED. trade_sync owns the calculation. Single source of truth.

**Accounting model confirmed (per Ryan):**
- Total wheel-cycle realized = `collected_premium + (call_strike − put_strike) × 100 − fees`
- Stored on call positions (EXPIRED): the `collected_premium` portion (net of any roll buy-backs)
- Stored on stock positions (CLOSED): the strike-difference portion only

**Manual cleanup applied to today's three positions:**
- PG stock (id 130): −$421.24 (sold 146, bought 150, plus −$21 14-share roundtrip)
- UBER stock (id 134): −$100.00 (sold 74, bought 75)
- PANW stock (id 135): $0.00 (sold 162.5, bought 162.5)
- Call positions (id 131, 138, 139): kept at $539/$242/$543 — those were already correct
- Total historical realized after cleanup: $1,708.26

## Monday (2026-04-27) — put_seller stuck + IPO scanner silently broken

Ryan noticed put_seller making no suggestions despite 24% margin used and Asian/EU markets open. Two issues found:

**1. `position_limit` double-counting** (commit `3cb2930`) — `check_position_limit` counted ALL Position rows including covered_call entries. A wheel cycle (stock + covered call) consumed 2 of 4 slots when it should consume 1. Account at NLV $15.5k has cap=4; with 2 wheels open the cap appeared full. Fix: count only `short_put` and `stock` types. Covered calls are bound to existing stock positions, don't take a slot.

**2. Risk check order** (same commit) — `position_limit` was 5th in the check list. The first 4 checks made IBKR/FMP calls (~7s each), so a position-limit block took ~28s. The put_seller scanner classified any None-result that took >10s as a "connection failure" and aborted after 3 in a row (`scan_aborted_connection_dead`). Reorder: cheap DB-read checks (position_limit, duplicate, daily_limit, vix_gate) run FIRST. Slow IBKR/FMP checks only run if cheap ones pass. Risk-blocks now return in <1s, no more false connection-dead aborts.

**3. IPO scanner `timedelta UnboundLocalError`** (commit `5607adc`) — `scan_ipo_calendar()` had `from datetime import datetime, timedelta` at module level (line 18) AND a redundant `from datetime import timedelta` inside a nested if-branch (line 168). Python's scope rules mark `timedelta` as a local variable for the entire function, masking the module-level binding. Line 92 (which uses `timedelta` unconditionally near top of function) errors before line 168 ever runs.

Result: IPO Date Calendar Scan job (8 AM ET daily) had been failing for at least 2 days with `cannot access local variable 'timedelta'`. Finnhub data never reached `expected_date` column on `ipo_watchlist`. IPO Ticker Scan loop's `if not ipo.expected_date: continue` guard skipped every row. Scan completed in 10ms doing nothing, every 5 minutes. Fix: remove the redundant local import.

**Post-restart verification (10:30 UTC scan):**
- Position-limit reorder works: scan completes in seconds with honest blocking reasons
- Current put_seller correctly blocked by cash-reserve constraint ($1,355 cash vs $2,328 reserve floor) and sector-concentration limit (Communications 100% > 88%)
- These are correct constraints for the current account state, not bugs
- IPO date scan will run at 8 AM ET (12:00 UTC) tomorrow — should populate `expected_date` for Kraken (KRKN), Lambda (LMDA), Dataiku (DIKU), Xanadu (XNDU) all with `expected_date='2026-06-01'` and now ~35 days out (still beyond the 1-day scan trigger window, but data flow restored)

**Discussion outcome on scoring (no code change):**
- Quality formula has been silently broken since FMP rename — `valuation_score=50.0` for all 127 stocks because PE/PEG fields renamed in `/ratios`. Fixed Friday (commit `1905e04`). Effect on next screener run.
- Composite formula `0.80×raw + 0.20×CQ%` means max possible composite = 76. 70+ requires both timing AND quality alignment — rare by design.
- HCLTech surfaced at top despite 50/50/50 fundamentals. Backfilled `fundamentals_complete=False` on 13 NSE Indian stocks + 1 FWB2 dividend. Dashboard ⚠ icon now shows them as unverified.
- Ryan's $11M deployment concern: scoring is calibrated for "buy good companies on dips" (wheel-friendly). Not for steady accumulation of quality at fair value. Separate accumulate-signal would need different scoring (60–70% quality, 20–30% own-history valuation, 10–20% timing). Deferred.

## Monday (2026-04-27) afternoon — DTE bypass fix in `_evaluate_symbol`

Ryan noticed put_seller had filled four orders at wrong DTE: NVDA May 6 (9 DTE), PLTR/RKLB/DXCM May 8 (11 DTE). Config says 0–3 DTE for USD at low/mid VIX — these were way out of bounds. QCOM order also tried, canceled by IBKR.

**Root cause** (commit `63f68c6`): `_evaluate_symbol` called `screen_puts()` without `dte_min`/`dte_max`. `screen_puts()` then fell back to `getattr(cfg, 'dte_min', 5)` and `getattr(cfg, 'dte_max', 14)` — but current `settings.yaml` has no `dte_min`/`dte_max` keys (only `.bak` does), so the hardcoded defaults 5–14 were used. That window perfectly covers May 6 (9 DTE) and May 8 (11 DTE).

`_process_symbol` had been correct all along — it called `_resolve_dte()` and passed both values into `screen_puts`. Only `_evaluate_symbol` was buggy. Fix mirrors `_process_symbol`: resolve DTE via `_resolve_dte(currency)`, halt on VIX-halt return, pass `dte_min=dte_min, dte_max=dte_max` into `screen_puts`. Both scan paths now correctly enforce DTE.

**Margin guard verified during investigation:** initial suspicion was margin guard had failed because account hit 68% margin used. False alarm. Logs showed ranks 1–6 passed with legitimate headroom ($12,171 down to $1,776), ranks 8–20 correctly rejected with `expired_no_margin`. The 4 fills consumed margin proportionally; the 68% used is the correct downstream result of those approved orders, not a guard failure.

**`check_position_size` cosmetic note (no code change):** variable named `estimated_margin` is misleading — it's notional concentration (price × 100), not margin. Real margin enforcement happens via `get_whatif_margin` in put_seller. Docstring already says this. Rename deferred to a future session.

**SHOP fully closed:** Ryan manually bought back $135 May15 call and sold 100 shares earlier today. Frees Maggy capacity once the four mistaken puts close.

**The 4 mistaken puts kept open:** NVDA May 6, PLTR/RKLB/DXCM May 8. Bug was upstream — these are filled, premium collected, accounting fine. Decision: let them ride. With DTE fix in place, no new violations from next scan onward. Monitor as expirations approach; close early if margin pressure forces it.

## Tuesday (2026-04-28) — account merge: this server now runs both Maggy and Winston on portfolio account

Test account U23886415 was decommissioned on this server and migrated to a clone codebase running on the same machine under user `nexbit` (Ryan's son). This server now runs Maggy and Winston code against U17562704 only, on the portfolio gateway (port 7496). Suggestion mode kept on for both, auto-approve toggle OFF for options (Ryan flipped it off Monday after the 4 unintended NVDA/PLTR/RKLB/DXCM fills).

**Config-only changes (no application code touched):**

- `config/settings.yaml` `ibkr` block: `port: 4001` → `7496`, `account: "U23886415"` → `"U17562704"`. Comments updated to flag merged mode.
- `~/restart-all.sh`: dropped the `tmux new-session -d -s options ...` block + 35s sleep. Added a "MERGED MODE" banner. Defensive `tmux kill-session -t options` in the kill block left in place (harmless cleanup).
- `~/watchdog-trader.sh`: commented out the options-gateway respawn block with a DISABLED note. Cron still runs the watchdog every 5 min, just skips options now. Portfolio + trader checks unchanged.

Both `~/restart-all.sh` and `~/watchdog-trader.sh` live outside the repo (in `$HOME`); `config/settings.yaml` is gitignored due to API keys. Backups stored as `~/restart-all.sh.pre-merge-2026-04-28` and `~/watchdog-trader.sh.pre-merge-2026-04-28` for clean revert when the new dedicated options account arrives.

**Migration sequence (executed):**

1. `settings.yaml` patched (matches found: 1, written)
2. `restart-all.sh` patched (matches found: 1, written)
3. `~/restart-all.sh` executed — one 2FA tap on phone, portfolio gateway came up on 7496
4. Trader app started, both Maggy code and Winston code connected to U17562704 (clientId 12 and 97 respectively)
5. Son started clone server, took over U23886415

**Hiccup during cutover — son couldn't log in:**

Watchdog cron (`*/5 * * * *`) respawned the killed options tmux session within 3 minutes, holding the U23886415 IBKR session and locking son out. Patched watchdog to skip the options check, killed the respawned options session, son immediately logged in to U23886415 on his clone. 2FA prompts to son's phone (from this server's repeated respawn attempts) stopped after the watchdog patch.

**Open issues from the merged setup (non-blocking, deferred to next session):**

- `'Position' object has no attribute 'unrealized_pnl'` — fires on every put_seller scan. Maggy code expects an attribute U17562704 Position objects don't have. Likely a 2-line `getattr(pos, 'unrealized_pnl', 0)` fix once we read the code.
- `trade_sync_fetch_error 'This event loop is already running'` + `'There is no current event loop in thread Thread-2'` — asyncio contention. Two ib_insync clients (id 12 + 97) on one Python process amplifies the existing event-loop fragility.
- `reconcile_submitted_trades_skipped_ib_error "name 'get_ib_lock' is not defined"` — missing import surfaced by the merge.
- `portfolio_account_updates_failed` (TimeoutError on `reqAccountUpdates`) — fired once at startup. May be one-time, watch for recurrence.

None of these block the merged setup operationally because Maggy is in suggestion mode with auto-approve OFF — at worst, no options suggestions reach the dashboard. Winston (read-only) is unaffected.

## Tuesday (2026-04-28) evening — dashboard authentication + localhost lockdown

Dashboard at `http://37.0.30.34:8080/` was publicly accessible with no authentication. Anyone with the URL could see positions, NLV, suggestions. Closed via Caddy reverse proxy with HTTP basic auth + self-signed HTTPS cert.

**Architecture:**

- Caddy 2.11.2 installed from official repo (apt) — public-facing reverse proxy on ports 80 and 443
- Self-signed TLS cert auto-generated for 37.0.30.34 (caddy `tls internal`) — browser warns once per device, click through, remembered after
- Basic auth user `maggycian` (bcrypt cost 14, hash stored in `/etc/caddy/Caddyfile`)
- HTTP (80) redirects to HTTPS (443)
- Caddy reverse-proxies authenticated requests to `localhost:8080` (the trader app)
- Trader app web binding moved from `0.0.0.0:8080` to `127.0.0.1:8080` — localhost only — so the world cannot bypass Caddy by hitting `:8080` directly
- ufw firewall: ports 80 and 443 added (were missing — caused initial "can't connect" from Safari until added)

**Files modified:**

- `/etc/caddy/Caddyfile` — caddy config (root-owned, sudo to edit). Backup at `/etc/caddy/Caddyfile.default-2026-04-28`
- `/var/log/caddy/dashboard-access.log` — access log (caddy:caddy ownership)
- `config/settings.yaml` `web` block: `host: "0.0.0.0"` to `"127.0.0.1"` with merge-mode comment

**New URL:** https://37.0.30.34/ — username `maggycian`, password set via `caddy hash-password`.
**Old URL dead from outside:** `http://37.0.30.34:8080/` — connection refused for non-localhost. Caddy still uses it internally.

**Verification confirmed:**

- `ss -tlnp` shows `127.0.0.1:8080` (python), `*:443` (caddy), `*:80` (caddy)
- `curl -k -i https://37.0.30.34/` without credentials returns `HTTP/2 401 Unauthorized` (auth enforced)
- `curl http://37.0.30.34:8080/` from outside times out (lockdown enforced)
- Authenticated access via Safari + curl works end to end

**Known cosmetic issue:** Safari may hang on first visit to the self-signed cert page until cache is cleared or page is reopened. Other browsers behave normally.

## Wednesday (2026-04-29) — KSPI fill-claiming clarification + watchlist metrics asyncio fix (Bug B + Stages 1–5)

KSPI buy-back triggered a discovery: post-merge fill-claiming. Then a deeper investigation revealed the watchlist metrics job had been silently failing every 4h since the merge. Five commits address it.

**KSPI fill-claiming (architectural note, no code change):**

Ryan placed a manual buy-back limit order on a long-standing Winston cash-secured put (KSPI 70 strike, June 18 expiry). Order filled at 0.88. Phone notified, but the buy-back showed only on Maggy's trade history, not Winston's transactions or trade-history pages. Winston's open-position counter correctly went 53 to 52, but the realized P&L of 888 landed in `positions` (Maggy's table) instead of `portfolio_put_entries` (Winston's table).

Root cause: post-merge, all IBKR fills arrive on the same shared connection with no strategy tag. Whichever sync code runs first claims the fill. Maggy's trade_sync runs every ~5 min; Winston's runs every 4 hours. Maggy almost always wins. The KSPI position was originally placed manually in IBKR (not via dashboard), so Winston never had a `portfolio_put_entries` record — only the open-counter knew about it. Maggy's sync ran first after the merge, found the open IBKR position with no DB match, created a fresh `positions` row claiming it as Maggy's. Today's buy-back closed Maggy's record cleanly. Display split is annoying, accounting is correct.

Decision: not fixing now. Real fix would tag fills by strategy at sync time — non-trivial. Listed in deferred bugs. Will be moot once the new options account arrives and the strategies separate again.

**Bug B fixed — `get_ib_lock` missing import in trade_sync (commit `877426d`):**

`reconcile_submitted_trades()` at line 414 of `src/broker/trade_sync.py` called `with get_ib_lock():` but the import at line 14 only imported `get_ib` and `is_connected`. NameError on every reconcile run, swallowed by `except as reconcile_submitted_trades_skipped_ib_error`. Surfaced post-merge because trade_sync now runs more frequently against the shared gateway.

Fix: added `get_ib_lock` to the import. Verified clean by absence of the error in 14:25, 14:40 reconcile cycles after restart.

**Watchlist metrics investigation:**

User asked to verify `update_watchlist_metrics` was running on schedule. Found it WAS — every 4h at 00:43, 04:52, 08:54, 12:54 — but the last 3 of those failed catastrophically: `failed=124 updated=0`. All 124 watchlist symbols failing in lockstep meant a connection-wide issue. Confirmed via "event loop is already running", "no current event loop in thread Thread-2", `spy_ma_fetch_error`, `price_fetch_error` errors clustering around the failed runs.

Root cause: post-merge, two ib_insync clients (Maggy clientId 12, Winston clientId 97) hit the same gateway through the same Python process. Pre-merge they had separate gateways; collisions were physically impossible. Post-merge, every overlapping IBKR call is a race.

Maggy already had `_ib_lock` infrastructure used consistently. Winston had `_portfolio_lock` defined but barely used — only at 2 sites in `connection.py` and 6 sites in `scheduler.py`. Roughly 26 IBKR call sites across `connection.py`, `buyer.py`, `analyzer.py`, `forecaster.py`, `sync.py`, `ibkr_fundamentals.py`, `bridge.py` were unlocked.

**Two-layer fix architecture:**
- **Layer 1 (Stages 1–3):** Wrap every Winston IBKR call site with `get_portfolio_lock()`. Winston serializes its own calls.
- **Layer 2 (Stages 4–5):** For the merge period, `get_portfolio_lock()` returns a supervisor that acquires Maggy's `ib_lock` FIRST, then Winston's `_portfolio_lock`. Cross-strategy serialization without touching Maggy code.

**Stage 1 (commit `ff631e2`) — `connection.py`:** Wrapped `refresh_portfolio_account_cache_from()` `accountValues()` and `refresh_brkb_history()` `reqHistoricalData()`. Connection-setup code at lines 115/144 intentionally not wrapped (no contention possible).

**Stage 2 (commit `baa533d`) — `buyer.py`:** 21 IBKR call sites wrapped as 16 lock blocks (per logical operation). Sites: VIX/SPY regime fetch, option chain discovery, option qualify+live bid sequence, place put order, assignment check, place stock buy, cash park sequence, three account-value queries, holdings update loop.

**Stage 3 (commit `59b3197`) — `analyzer.py`, `forecaster.py`, `sync.py`, `ibkr_fundamentals.py`, `bridge.py`:** 8 lock blocks across 5 files. After Stage 3, every Winston IBKR call site holds the portfolio lock during the call.

**Stage 4+5 (commit `392369b`) — Cross-strategy supervisor:** Replaced `get_portfolio_lock()` with a context-manager-returning function. When merged (detected once at module import by comparing `settings.ibkr.host/port/account` vs `settings.portfolio.ibkr_host/ibkr_port/ibkr_account`), it acquires Maggy's `ib_lock` FIRST, then `_portfolio_lock`. When split, returns plain `_portfolio_lock`. Lock acquisition order is fixed (`ib_lock` then `portfolio_lock`) and Maggy never acquires `portfolio_lock`, so no deadlock. Logged at startup as `portfolio_lock_mode merged=True`.

When the new options account arrives and ports diverge, `_detect_merged_with_options()` returns False automatically. Supervisor becomes a no-op without code change. Removal instructions for permanent re-split are inline in `connection.py` under the MERGE-ONLY header.

**Verification:** Restart at 19:46:10 logged `portfolio_lock_mode merged=True` confirming supervisor is active. Next watchlist metrics run is at ~23:46. If `failed=0 updated=124`, asyncio race is dead.

## Saturday (2026-05-03) — reconnect-race fix + scoring rebalance + pending orders fix

Three lines of work today, all pushed.

**1. Reconnect-race fix (commit `b734cf7`):**

Yesterday's lock work proved itself: 03:49 watchlist metrics run logged `failed=0 updated=124`. Asyncio race against the metrics job is dead.

But discovered a related bug overnight. IBC restarted the gateway at midnight UTC; trader reconnected cleanly at 00:01, ran 03:49 metrics fine, then connection dropped around 05:46. Reconnect attempts started failing with "This event loop is already running" every 5/10/20s, looped for 47 min until manual restart at 06:35. 195 failed-reconnect log entries.

Root cause: `_connect()` in `src/broker/connection.py` calls `ib.connect()` which internally invokes `asyncio.get_event_loop().run_until_complete(...)`. If a Winston thread is mid-call holding `_ib_lock` via the merge-period supervisor, the new asyncio task can't run. Yesterday's lock work protected Winston's CALLS but not Maggy's RECONNECT against Winston's calls.

Fix: wrapped `ib.connect()` plus the post-connect setup (`RequestTimeout`, `reqMarketDataType`, sleep) in `with _ib_lock:`. Lock is RLock so safe even if called from a thread already holding it. Reconnect now waits for any in-flight Winston operation to release the lock before grabbing the event loop.

**2. Scoring rebalance — Buffett-style (commits `a25ccb1`, `45e6d72`, `f65b9d4`):**

User concern: dashboard top sat at 45–65 score range, never reaching 70+ direct-buy threshold except in panic. 75 of 124 stocks at `raw_score=0`, 25 at exactly 40. Top stable for days. System effectively "panic-buy or never."

Architecture investigation revealed: the screener (`tools/screen_universe.py`) already does Buffett-style work properly — `_score_growth` (40%, revenue + gross margin level + trend), `_score_valuation` (25%, PEG-first with PE fallback), `_score_quality` (35%, D/E + FCF consistency + FCF margin trend). Composite `0.40*growth + 0.25*valuation + 0.35*quality`. Already calibrated for "wonderful business at fair price." OFF-LIMITS to changes per explicit user guardrail.

The downstream scoring was the problem. `analyzer.py:_compute_composite_score` was purely a panic detector (SMA-discount + RSI-oversold + 52w-low gates). If no gate fired, returned 0. Most quality stocks at fair valuation hit no gates, scored 0. The composite blend was 80% raw + 20% quality, so quality couldn't lift them. And `analyzer.py` was setting `composite_score = score` directly without ANY blend — discrepancy with `recalc_scores_from_db` which used 80/20.

Three changes restored Buffett-style behavior:

- **`a25ccb1`** — Two simultaneous changes:
  - Added a fair-price base of 0–24 points to `_compute_composite_score`, scaled across `discount_pct` from −5% (above SMA, 0 pts) to +5% (below SMA, 24 pts). Saturates exactly where the existing gated SMA signal takes over. Stocks at fair valuation now have a foot in the door even without panic-level signals. Anti-chase guard at −20% still blocks deeply overpriced stocks.
  - Composite blend: 80% raw + 20% quality to 30% raw + 70% quality. Applied symmetrically in `_evaluate_symbol` (was no blend at all — discrepancy fixed) and `recalc_scores_from_db`.

- **`45e6d72`** — Composite floor: stocks below `MIN_COMPOSITE_FOR_ACTION = 40.0` don't get `buy_signal=True`. Filters out fair-priced stocks with weak quality (whose composite was lifted only by fair-price base). At score=0, the `0.70*quality_pct` term means floor=40 is roughly `quality_pct >= 57`. Below that, watchlist-only, no CSP suggestion.

- **`f65b9d4`** — Direct-buy threshold bumped from 70 to 75. Under the new 30/70 blend, composite=70 was reachable by top-quality stock at exact-SMA price (zero technical signal). Bumping to 75 ensures every direct-buy candidate has `raw_score >= 15` — some real price-side reason to act, not pure quality lift.

**Resulting action mapping:**
- Below 40: watchlist member, no action
- 40–75: sell CSP at target strike (get paid to wait — most fair-priced quality stocks land here)
- Above 75: direct buy (rare, requires both real signal AND high quality)
- Override gates (`deep_discount > 15%`, RSI < 20, volume_surge + trend_healthy) still promote to direct_buy regardless

**3. Pending orders dashboard fix (commit `2c161a8`):**

User wanted Pending Orders view to reflect IBKR state in near-real-time, with order lifecycle (Submitted, PartiallyFilled, Filled disappears to Holdings, Cancelled disappears entirely) handled cleanly.

Audit revealed most of the lifecycle infrastructure already exists:
- `refresh_portfolio_pending_orders_cache()` captures all the right fields (status, filled, remaining, order_id, etc.)
- Uses `reqAllOpenOrders()` so it sees orders from all clients including manually placed TWS orders
- `trade_sync` handles fills (moves to Holdings/Open Options) and ghost detection (rejected → CANCELLED)
- Dashboard renders the table with status column

Two real issues found:
1. Template variable mismatch: `{{o.quantity}}` in `portfolio.html`, but cache stores it as `'qty'`. Qty column silently empty on dashboard.
2. Cache refreshed only every 15 min (inline with `_job_trade_sync`). Freshly placed orders invisible for up to 15 minutes.

Fixes (commit `2c161a8`):
- Template: `{{o.quantity}}` to `{{o.qty}}`
- Trigger `refresh_portfolio_pending_orders_cache()` immediately after each `placeOrder` + sleep block. Three sites: CSP (line 695), direct buy (line 1074), cash park (line 1149). Wrapped in try/except so dashboard issues never break order placement.

Result: newly placed orders appear on the dashboard within ~2 seconds.

**Architectural state confirmation (no change, but worth recording):**

Portfolio IBKR connection (clientId 97) remains in **read-only mode at IBKR level** — even if app-level `suggestion_mode` is flipped off, IBKR rejects `placeOrder` at the protocol level. This is a deliberate two-layer safety:
- IBKR side: read-only (protocol-level lockout)
- App side: `suggestion_mode` + `auto-approve OFF`

For Winston to ever execute, BOTH switches need to be flipped deliberately. Today's scoring/lifecycle work prepares for that future state but doesn't enable it.

**Verification:**
- 21:22 trader restart logged clean: `ibkr_connected`, `portfolio_connection_established`, `portfolio_ibkr_ready`. Lock supervisor still active (`merged=True` from yesterday).
- First metrics + recalc cycle after the scoring change runs at ~01:22 UTC, then 05:22, 09:22, 13:22 etc.

## Monday (2026-05-05) — Capital injections deposit-proof graphs + margin interest investigation

**Options graph formula fix (commit `3b407ff`):**

Ryan reported: adding €15K capital injection on options account caused the options graph to jump +100% that day (bullshit). The formula was `(current_nlv - first_nlv) / first_nlv × 100` — pure NLV diff, treated capital deposits as growth. Portfolio side had the correct pattern; options side never copied it.

Five-file atomic commit fixed it:

1. `src/core/database.py` — add migration for `portfolio_capital_injections.account_id` (VARCHAR(20))
2. `src/portfolio/models.py` — add `account_id` field to `PortfolioCapitalInjection` model
3. `src/portfolio/capital_injections.py` — add `get_total_invested_usd(account_id=None)` parameter filtering; `sync_injections_from_ibkr(account_id=None)` tags new rows with `account_id`
4. `src/web/routes/dashboard.py` — replace buggy `(nlv - first_nlv) / first_nlv` formula with `(nlv / total_invested - 1) × 100`, anchored to first-point-zero; reads `options_account` from `cfg.ibkr.account`; calls `get_total_invested_usd(account_id=options_account)`
5. `src/web/routes/portfolio.py` — update call sites to pass `account_id` (reverted later; see below)

Post-split readiness: when new options account arrives, options graph will automatically filter deposits to that account only. No cross-account interference.

**Backfill issue discovered at restart (commits `65178bd` + `10ef0a4`):**

The backfill UPDATE in `risk_backfills` loop failed with "name 'text' is not defined" error, breaking the `account_snapshot` job. The SQL query `UPDATE portfolio_capital_injections SET account_id = 'U17562704' WHERE account_id IS NULL` was malformed or being evaluated in wrong scope.

Rather than debug the backfill, removed it entirely (commit `10ef0a4`). The migration itself creates the column with no DEFAULT, so existing rows get NULL. That's fine — they're historical. New rows from trade_sync will have `account_id` set. Post-split, the new options account's Flex sync will populate its own rows correctly.

**Margin interest investigation (no code change):**

User asked: does IBKR's `NetLiquidation` already include accrued margin interest, or is it shown separately?

Research from IBKR docs: `NetLiquidation = TotalCashValue + AccruedInterest`. Interest accrues daily and posts monthly. The accrued amount shown is interest that has NOT YET been charged to cash — it's a liability shown separately. Once posted at month-end, it reverses and moves from "Accrued Interest" to "Total Cash Value".

Conclusion: `NetLiquidation` already includes accrued interest (as a separate line), so your graph is correct as-is. The margin interest cost is already reflected. The strike-bumping heuristic in `wheel.py` (line 305, `interest_surcharge`) operationally tries to recover the interest cost through higher premiums. No additional graph adjustment needed.

Portfolio side shows accrued interest via `fetch_accrued_interest_usd()` which reads Flex data. This is informational — the interest is already baked into NLV.

**Web server went blank after restart:**

The five-file commit broke something at runtime. Both dashboards were blank/error. Root cause: the backfill UPDATE was failing silently, triggering exception handling that masked a Python import error downstream.

Restart after removing the backfill (`10ef0a4`) brought both dashboards back up. Options side shows the new capital-aware formula. Portfolio side unaffected.

**Pending issue: portfolio.py still has account_id filtering:**

Patch 5/5 modified `portfolio.py` to call `get_total_invested_usd(account_id=cfg.portfolio.ibkr_account)`. After the revert, this broke because the function signature was reverted too. Quick fix applied: changed both call sites back to `get_total_invested_usd()` with no args. Portfolio dashboard returned. Not pushed yet because we're in cleanup mode post-incident.

## Thursday (2026-05-07) — earnings gate, LSE pence, JSON history export, two open items for son's clone

**Four commits pushed, restart pending:**

1. **`974b538`** — Added `EarningsCache` model (symbol PK, status, next_earnings_date, fetched_at) for 24h cache backing the earnings gate. Auto-creates via `Base.metadata.create_all` at next startup.
2. **`4cf7630`** — Added `get_next_earnings_date(ib, contract)` in `src/portfolio/ibkr_fundamentals.py`. Mirrors existing `ReportsFinSummary` pattern — same lock acquisition (`get_portfolio_lock`), same XML parse, same exception shape. Calls `ib.reqFundamentalData(contract, "CalendarReport")`. Returns `EarningsResult(next_date, status)` with three explicit states: `found`, `none_scheduled`, `fetch_failed`. Tries `<EarningsAnnouncement Date="...">` first, falls back to `<EPSDate>` children if format differs.
3. **`58c6a55`** — Replaced the always-False `has_upcoming_earnings()` stub in `src/broker/market_data.py` with real implementation. **Fail-CLOSED on missing data** (no IB / qualify failure / fetch failure / parse failure all return True = block). 24h DB cache; cached entries auto-invalidated when their date passes. Three states from `get_next_earnings_date` map to: `found` + within 3 days → block, `found` + outside window → allow, `none_scheduled` → allow, `fetch_failed` → block.
4. **`95f7d67`** — LSE pence normalization at source in `src/portfolio/analyzer.py`. IBKR returns LSE prices in pence; analyzer was storing raw pence into `analysis.current_price`, `sma_*`, `52w_high/low`. Now normalized once where `closes`/`highs`/`lows` are extracted from bars, before any computation. All downstream metrics inherit correct units. AZN was the symptom (showed 13552 instead of 135.52); fix is universal for any GBP symbol. **Eight other GBP-handling sites** in the codebase exist (screener.py:35, put_seller.py:440, trade_sync.py:321, etc.) — surgical fix at this site, no centralization. Centralization deferred.

**Architecture note: earnings gate is now fail-CLOSED, opposite to most gates.** Rationale: earnings is the single most predictable cause of overnight gap risk on a CSP. Better to skip a trade than mis-trade through earnings. VIX gate, MA gate, and most others remain fail-OPEN.

**JSON history export built (not committed to repo, lives at `/tmp/options_history_export.json` + `~/options_history_export.json`):**

`tools/export_options_history.py` (drafted, run from `/tmp`): exports pre-merge Maggy-side data from your DB for handoff to son's clone. Window 2026-02-22 to 2026-04-28, FILLED trades only. Trades scoped by (symbol, strike, expiry) match against in-window positions because `Trade.position_id` was unpopulated on most historical rows (only 7 of 92 had FK link). Output: 37 positions, 92 trades, 127 events with running realized P&L, total $2,964.76. JSON file ~126 KB, ready to hand off to son.

Import script (Script B) **not yet written** — waiting for son's schema diagnostic + existing-rows snapshot. He will run two read-only checks on his clone, paste output, then we write the import tailored to his actual schema (since his fork may have diverged).

**Investigations that did not become commits:**

- **Asia/EU put scan question (deferred to son's clone):** This server's diagnostic shows scans run correctly, hit AEB and LSE, evaluate ASM/ASML/AZN — but every symbol gets blocked by `Position limit reached: 23/15`. Not a bug per se on this server, but the cross-strategy position-counting on the merged Maggy+Winston `positions` table makes the diagnostic meaningless for the real options-trader account. Son's clone (clean U23886415, separate `positions` table) is the only place this can be validated. Diagnostic prepared and ready to forward.
- **NVDA realized P&L = 0 on son's dashboard (deferred to son's clone):** His DB has only `BUY_PUT @ 0.0` (the IBKR expiry-recognition row), no SELL_PUT, because the original April 27 sale fell in your server's gateway session and never reached his `ib.fills()` after cutover. trade_sync's expiry handler queried the Trade ledger, summed to 0, wrote 0. Once written, no recovery path. Possible defensive fixes discussed and rejected: would not have prevented son's specific case (no SELL row anywhere) and would risk corrupting your already-correct data. JSON history import is the right path for son. **No code change made on your server for this.**
- **Watchlist staleness alarm:** Investigated. All 129 rows have `metrics_stale=0`, last update 2.9h ago — well within 4h cycle. The "looks stale" feeling is the new May 3 30/70 scoring blend producing stable scores dominated by slow-moving quality (70% weight), correctly per design. The 06:56 and 12:13 partial-failure metrics cycles were restart artifacts (manual restarts that day), not regressions of the asyncio race fix.
- **Six historical positions with `realized_pnl=0`, `total_premium_collected>0`:** Surfaced during CRWV review. PANW/UBER/SHOP/TTD ASSIGNED puts on March 29 (predate April 25 commit `2e9708c` stock-close fix), PANW stock CLOSED on April 25 (possibly correct at break-even), COIN covered_call EXPIRED on March 9 (pre-everything). ~$1,339 in unrecognized P&L on the dashboard. **Decision: do not fix on this server.** Merged-mode data is mixed pre-merge Maggy + post-merge Winston; manual UPDATEs now would risk correcting numbers that should be on the other side of the future re-split. Wait for new options account, separate the data, re-evaluate.
- **CRWV id=152 stuck OPEN despite May 6 buy-back:** Identified but not pursued; same merged-mode-data caveat applies.

## Friday (2026-05-08) — Forward-growth scoring landed + augmentation pipeline complete (22 commits)

**Big day. Two major themes:** built the 5-component forward-growth scoring system (Path A refactor) and the full Claude-driven augmentation pipeline. Plus son's clone JSON import script delivered.

### Forward-growth scoring (commits `f55d8b2` → `940507a`, `ed76fad`)

Replaces the old `40g + 25v + 35q` portfolio score formula with a Buffett-style composite weighted across 5 sub-components: revenue durability (25%), compounding quality (25%), operating leverage (20%), innovation investment (15%), capital efficiency (15%). **Hard cap at 30** if a name has 3+ years negative net income AND 3+ years negative FCF over a 5-year window.

Implementation in 4 commits (Path A):
- **`d5dedf6`** — Commit A: extended `_get_fmp_fundamentals()` to extract 5-year history fields (operating_margin, R&D intensity, share dilution, ROIC sustained, goodwill stability, FCF trend, neg-NI/neg-FCF year counts). NO new API calls — all derived from existing income/balance/key-metrics responses.
- **`f285358`** — Commit B: 5 sub-scorer functions with explicit-value sector lookup for 23 distinct sectors observed in watchlist. Score breakdowns documented in code comments.
- **`449402a`** — Commit C: `_score_forward_growth(fmp, sector)` aggregator. Smoke test: NVDA=80.5, MSFT=77.5, AAPL=68.0, JNJ=59.5, XOM=33.0.
- **`940507a`** — Commit D: wired `forward_growth_score` into screener flow. Stored on `StockScore`. **Does NOT yet replace `portfolio_score` formula** — that's Commit E (deferred to observation period).

Plus **`699900b`** (Commit O) — preserve all 5 sub-scores on `StockScore` so the augmentation prompt can show them per-name. Without this, augmentation would prompt with all-zeros for sub-scores.

**Screener run after these landed (May 8, dashboard "Run now"):** 133 rows populated, range 11.8–89.2, avg 52.5. Top-20 by `forward_growth_score`: MA 89.2, ASML 88.8 (+21 vs old), LLY 86.6, META 86.5, KLAC 85.7, ANET 84.5, TSM 83.5, GOOG 83.0, NFLX 80.7, MSFT 80.5, NVDA 80.5 (−10.5 vs old, dilution + cyclical risk tempers), ISRG 80.5, RACE 79.8, ABNB 79.5, NVO 79.0, BKNG 76.2, V 75.9, CDNS 75.3, FSLR 73.0. Picks-and-shovels representation jumped from zero to ~10 names.

### Augmentation pipeline — full feature complete (commits `6a9ba4a` → `49b6785`)

Goal: monthly screener invokes Claude to propose 5–10 high-conviction names beyond the hand-coded universe, scores them, accepts those that beat the rank-60/rank-15 cutoff. **All gated behind `AUGMENTATION_ENABLED = False` — default OFF, no API calls until manually flipped.**

Foundation:
- **`6a9ba4a`** (F) — `tools/discovered_pool.yaml` empty file with growth+dividend tiers + `_load_discovered_pool()` loader
- **`6029b4f`** (G) — `tools/evicted_names.yaml` empty + `_load_evicted_names()` loader
- **`5ed25f0`** (H) — `_get_growth_universe()` / `_get_dividend_universe()` helpers; routed 5 universe iteration sites through merged pools (`CANDIDATE_POOLS` + discovered − evicted). Verified equivalent to original behavior with empty yamls.
- **`ed76fad`** (I) — `AugmentationAudit` SQLAlchemy model added to `src/portfolio/models.py`. Eleven columns: id, run_date, tier, proposed_symbol, proposed_score, cutoff_score, displaced_symbol, displaced_score, accepted, reason, notes. Table auto-creates on next restart via `Base.metadata.create_all` (verified — table exists on this server).

Logic:
- **`081ad9c`** (J+K) — `_get_growth_swaps()` + `_get_dividend_swaps()` + shared `_call_claude_for_swaps()` helper + `_format_score_table_for_prompt()` + `_AUGMENTATION_RUBRIC_SUMMARY` constant + `_build_growth_augmentation_prompt()` / `_build_dividend_augmentation_prompt()`. Direct text + JSON parse, `max_tokens=4000`, includes top-60 + ranks 61–120 + rubric + exclusion list.

Orchestration:
- **`c79d431`** (L) — `_process_augmentation_proposal()` helper + `AUGMENTATION_ENABLED` flag (False) + PHASE 2.5 block in `screen_all`. PHASE 2.5 runs between PHASE 2 (breakthrough scan) and PHASE 3 (portfolio universe build). Splits non-breakthrough scores into growth/dividend pools using the same yield-routing rule as PHASE 3, identifies top_60/top_15 + cutoff, calls Claude, processes each proposal (score round-trip via `_score_stock`, accept if score > cutoff with margin=0, audit-log every proposal), opens SQLAlchemy session via `get_session_factory()`. Best-effort: any exception inside PHASE 2.5 is caught, logged, augmentation skipped, screener continues normally.

Persistence + hygiene:
- **`2df64cd`** (L+) — `_persist_augmentation_acceptances()` writes accepted symbols to `discovered_pool.yaml` atomically (.tmp + rename). Schema per entry: `symbol, exchange, currency, region, score, added_date, thesis`. Buffer (`pending_yaml_additions`) populated during proposal processing, written once after `audit_session.commit()`. Without this, accepted names would only exist in this run's `all_scores` and disappear next month.
- **`49b6785`** (M) — `_evict_overflow_from_discovered_pool()`. When pool > cap (180 growth / 45 dividend), sort by score desc, slice to `[:cap]`, log evicted symbols. Atomic write. Eviction is list-size hygiene only — not a quality verdict.

**Architecture decisions made today:**
- Two pools (`discovered_growth`, `discovered_dividend`), separate, no yield-routing for discovered names.
- Every symbol evictable — hand-coded names included, no editorial floor.
- Eviction triggers only when pool > cap; no K-parameter, no consecutive-runs logic.
- General augmentation (not slot-specific) — Claude proposes 5–10 high-conviction names, not "replace these specific laggards."
- Margin = +0 (any improvement over rank-60 cutoff accepts). Easy to tune to +3 later if churn is excessive.
- Eviction file at `tools/evicted_names.yaml` (NOT auto-edit source code).
- `discovered_pool.yaml` lives in source tree (committed).
- Strict failure handling on FMP miss (no retry). Audit row written with `reason="scoring_failed"`.
- Audit trail = SQLite table `augmentation_audit`.

### Other commits today
- **`f55d8b2`** — Anthropic API timeout 30s → 120s in `tools/screen_universe.py:439` (breakthrough prompt v3 takes ~80s).
- **`a23f611`** — Breakthrough prompt v3: dynamic `CANDIDATE_POOLS` exclusion, geographic fix, top-20 hard exclusion, ETF/Fund pattern, existence check.
- **`f065259`** — Added BAP (Credicorp Peru) and CHT (Chunghwa Telecom Taiwan) to `ADR_DIV` section of `DIVIDEND_CANDIDATES`.

### Son's clone — JSON import script delivered

Built standalone Script B for importing pre-merge history into son's clone DB. Single-file Python (~150 KB with embedded JSON, no separate data file needed). Implements: position match by (symbol, strike, expiry, opened_at); hard-skip when both his DB and export show OPEN (his side wins); update close+P&L when his is OPEN and export is CLOSED/EXPIRED/ASSIGNED; insert when no match; trade dedup by `ibkr_exec_id` then natural key; `position_id` remapping via dict; default dry-run (must pass `--apply` to write); atomic transaction; clean summary report.

File at `/tmp/import_options_history_standalone.py` on this server. Sent to son via email attachment. **Resolved.**

## Sunday (2026-05-10) — STATE.md restructure (this document)

Reorganized STATE.md from pure chronology into three layers (L1 Fundamentals / L2 Top of Mind / L3 History) per Ryan-requested format. All prior content preserved verbatim in L3 below the existing entries; L1 and L2 are synthesized views of the current state.

No code change. RULES.md merged-mode update deferred to a separate session.

## Monday (2026-05-11) — breakthrough v4 reverted → v4.1 validated + son's three-market diagnosis + earlier May 9-10 work captured

### May 9-10 commits not previously captured in STATE.md

Four commits happened in the chat between Sunday's restructure and tonight that should be recorded:

- **`80f08c3`** — Augmentation NameError fix. Working.
- **`2faa4df`** — Portfolio dashboard merge-mode. Working.
- **`e958fc1`** — **Commit E shipped.** `portfolio_score` now uses `forward_growth_score` instead of the old `40g + 25v + 35q`. The observation-period deferral noted on May 8 was concluded; flip executed. Operating flags table updated accordingly. Composite score architecture verified.
- **`c098919`** — Breakthrough Step B persistence. Working. History pool tracking 36 entries cleanly across 3 runs.

### Breakthrough prompt v4 → v4.1

First v4 attempt added a dedicated COMPUTE BUILDOUT THESIS section + country-specific quotas (Korean AND Japanese) + memory/HBM quota + datacenter electrical quota + tightened non-USD count (6+/4+ vs v3's 5+/3+). Result: produced empty arrays under the stacked constraints — Claude couldn't satisfy all the new requirements simultaneously and returned nothing instead of a degraded list.

**Reverted at `bf830c3`** then re-fixed as **v4.1 at `f5e090f`**.

What v4.1 keeps from v4:
- Megatrend #2 expanded (still one entry in the megatrend list, no quota, no dedicated section, no named-ticker examples — named tickers bias Claude toward specific names rather than letting it surface what looks attractive now)
- **Safety-net clause** at top of validation checklist: "if you cannot satisfy ALL items below, return your best honest attempt with as many names as you can — do not return an empty array. A shorter list of high-conviction names is strictly preferred to no list." Defends against future constraint additions causing the same empty-array failure mode.

What v4.1 deliberately drops from v4:
- COMPUTE BUILDOUT THESIS dedicated section (overweighted compute vs 16 other megatrends)
- Country-specific quotas — country prescription embeds static geopolitical assumptions in a dynamic-conditions prompt
- Memory/HBM quota — if those names belong in the universe, add to `CANDIDATE_POOLS` directly
- Datacenter electrical quota — same reasoning
- Tightened non-USD count (6+/4+) — left at v3's 5+/3+

### v4.1 test run results (validated)

| Metric | v4.1 (tonight) | v3 (yesterday) | v4 original (broken) |
|---|---|---|---|
| Breakthrough count | **22** | 19 | 0 |
| Non-USD listings | 1 (Sony 6758 JPY) | 1 (4565) | n/a |
| Megatrends represented | 10 | ~8 | n/a |
| History pool growth | 20 → 36 (+16 net-new) | 3 → 20 (+17 new) | n/a |

**Composition tonight:** RKLB, BEAM, SITM, FSLR, CRSP, SPWR, ALB, 6758, ZS, EPAM, TMDX, RDWR, CHWY, DKNG, HUBS, CVNA, MNDY, IONQ, SMCI, PATH, STEM, TDOC.

**Megatrend distribution healthy:** Genomics, energy transition, AI applications, compute infrastructure, cybersecurity, space, quantum, critical minerals — broad spread, no over-concentration. 4 names in #1 (AI applications) slightly bends Claude's own "max 3 per megatrend" cap — acceptable variance.

**Geographic spread still v3-level (1 non-USD).** v4.1 didn't fix this — wasn't the goal. Country-specific prescription was wrong-shaped. **Right fix is universe expansion** (add international names to `CANDIDATE_POOLS`), not prompt prescription. Queued as L2 #6.

**Compute representation:** SITM + SMCI (infrastructure) + EPAM, MNDY, HUBS, DKNG (AI applications) = 6/22 = 27%. Solid. Megatrend #2 expansion worked — Claude surfacing names across the compute stack.

### Son's clone — three-market diagnosis (resolves the open "Asia/EU scan validation" item from May 7)

Son ran the diagnostic on his mesicap-trader (U23886415, real options-trading account, unmerged). Results split cleanly across three markets:

**1. ASX (Australia) — working-as-designed.** JHX surfaced as a candidate but blocked by low-IV-rank filter (same gate that blocks TSLA on quiet days). Not a bug.

**2. NSE (India) — confirmed broken, two compounding issues.** Journal at 06:30/07:00 UTC scan for INFY+ITC shows:

```
Error 200: No security definition ... contract: Stock(conId=44652017, symbol='INFY',
  exchange='SMART', primaryExchange='NSE', currency='INR' ...)
no_price_data exchange=NSE symbol=INFY
option_chains_raw count=0 exchange=NSE exchanges=[] symbol=INFY
no_option_chains
```

Two distinct failures stacked:

- **SMART-routing rejects INR stocks.** Even with `conId`, `primaryExchange='NSE'`, `localSymbol`, `tradingClass` all set, IBKR returns Error 200. Root cause: `contract.exchange = "SMART"` override at `src/broker/market_data.py:100`. IBKR's SMART aggregator doesn't include NSE for INR-denominated names.
- **Option chains return empty (`count=0`)** — separate from the price fetch. Even when the chain query goes through, IBKR returns 0 chains for NSE. **Market-data subscription gap** — IBKR account doesn't have NSE options data enabled. Possibly also missing NSE equity-data subscription (which would explain the SMART failure too).

**Net:** even if the SMART-override at L100 gets fixed, NSE still produces 0 candidates because no option chains exist for this account. **NSE is effectively un-tradeable until the IBKR subscription is added.** Son suggests pulling INFY+ITC from `options_universe.yaml` until that's resolved. Queued as L2 #1.

**3. Europe (LSE / AEB / BVME) — jobs never fire.** Real bug. No "scan started" log line, no "scan failed" log line — silent failure. Tonight son shipped an **asyncio-reentry fix** to address it. Verification path: 07:58 UTC wake-up re-pulls his journal to confirm LSE / AEB / BVME scans actually fired after Europe opened at 07:00 UTC.

### TTD covered-call dashboard display fix (son's side, verified yesterday)

All closed puts disappeared from the Positions dashboard at expiry, but TTD's *manually-opened* covered call (which expired at the same date) did not. The dashboard close-out filter caught dashboard-originated positions but not manually-opened ones. Son fixed it. Worth checking whether this server's dashboard has the same display gap.

### RACE → IDEM exchange routing fix (son's investigation, applies to your code)

Son's investigation surfaced: RACE (Ferrari) options chains are found on **IDEM** (Borsa Italiana derivatives), but `placing_sell_put` calls go to **BVME** (the equity exchange). Journal evidence: `option_chain_data exchange=IDEM strikes=266 symbol=RACE` followed by `placing_sell_put exchange=BVME strike=279.0 symbol=RACE`.

Root cause: `config/options_universe.yaml` — RACE has no `opt_exchange` field, so `universe.get_options_exchange()` at `src/strategy/universe.py:144-147` falls through to `stock.opt_exchange` which gives BVME. Fix: add `opt_exchange: IDEM` to the RACE entry. Same pattern is already used in `config/watchlist.yaml` (other stocks have `options_exchange: EUREX` / `OSE.JPN`).

### Porting workflow codified

L1 now has a "Son's Clone — Porting Relationship" section codifying the manual case-by-case workflow. Three port-check items in L2 next-session queue (RACE/IDEM, asyncio-reentry diff against your locks, TTD CC display gap check).

### What's NOT changed on your code tonight

Beyond breakthrough v4 → v4.1, no code change on this server tonight. Son's work happens on his clone with his own AI; this server records status and identifies port candidates.

---

## All Commits Reference (recent, all pushed)

**2026-05-09 to 2026-05-11:**
- `f5e090f` screener: breakthrough prompt v4.1 — expand megatrend #2 + safety-net clause (validated tonight: 22 names, healthy spread)
- `bf830c3` Revert "screener: breakthrough prompt v4 — compute buildout thesis + tightened geographic spread"
- (v4 original commit hash not preserved in current chat — was reverted in `bf830c3`)
- `c098919` breakthrough: history persistence Step B — pool tracking confirmed working (36 entries across 3 runs)
- `e958fc1` **Commit E: portfolio_score = forward_growth_score** (old 40g+25v+35q formula retired)
- `2faa4df` portfolio dashboard merge-mode fix
- `80f08c3` augmentation: NameError fix
- `c2cced4` STATE.md: Sunday — augmentation pipeline validated, Commit E, breakthrough infrastructure (5 commits)

**2026-05-08:**
- `49b6785` augmentation: `_evict_overflow_from_discovered_pool` (180/45 cap, atomic write)
- `2df64cd` augmentation: persist acceptances to `discovered_pool.yaml` atomically
- `c79d431` augmentation Commit L: PHASE 2.5 block in `screen_all` + `_process_augmentation_proposal` + `AUGMENTATION_ENABLED` flag
- `081ad9c` augmentation J+K: growth/dividend swap helpers + Claude prompt builders
- `ed76fad` augmentation I: `AugmentationAudit` model
- `5ed25f0` augmentation H: growth/dividend universe helpers + 5 site routing
- `6029b4f` augmentation G: `evicted_names.yaml` + loader
- `6a9ba4a` augmentation F: `discovered_pool.yaml` + loader
- `699900b` (O) preserve 5 sub-scores on `StockScore`
- `940507a` forward-growth D: wire into screener flow
- `449402a` forward-growth C: `_score_forward_growth` aggregator
- `f285358` forward-growth B: 5 sub-scorer functions
- `d5dedf6` forward-growth A: extend `_get_fmp_fundamentals` for 5-year history
- `a23f611` breakthrough prompt v3
- `f55d8b2` Anthropic API timeout 30s → 120s
- `f065259` add BAP + CHT to `ADR_DIV`

**2026-05-07:**
- `95f7d67` LSE pence normalization at source in `analyzer.py`
- `58c6a55` `has_upcoming_earnings()` real implementation, fail-CLOSED
- `4cf7630` `get_next_earnings_date()` in `ibkr_fundamentals.py`
- `974b538` `EarningsCache` model

**2026-05-05:**
- `10ef0a4` database: remove problematic `account_id` backfill
- `3b407ff` capital_injections: per-account deposit tracking + deposit-aware graphs

**2026-05-03:**
- `2c161a8` pending orders: fix qty template var + trigger refresh after `placeOrder`
- `f65b9d4` scoring: bump direct-buy threshold from 70 to 75
- `45e6d72` scoring: add composite floor of 40 for `buy_signal` trigger
- `a25ccb1` scoring: rebalance composite to 30% raw + 70% quality, add fair-price base
- `b734cf7` fix: hold `_ib_lock` during `ib.connect()` to avoid reconnect-vs-Winston race

**2026-04-29:**
- `392369b` Stage 4+5: cross-strategy lock supervisor for merged accounts
- `59b3197` Stage 3: lock IBKR calls in analyzer, forecaster, sync, fundamentals, bridge
- `baa533d` Stage 2: lock IBKR calls in `portfolio/buyer.py`
- `ff631e2` Stage 1: lock IBKR calls in `portfolio/connection.py`
- `877426d` Fix: import `get_ib_lock` in `trade_sync.py`

**2026-04-27:**
- `63f68c6` Fix: pass DTE range to `screen_puts` in `_evaluate_symbol`
- `5607adc` IPO scanner: remove redundant `timedelta` local import (`UnboundLocalError` fix)
- `3cb2930` `position_limit` double-counting + risk check order

**2026-04-25:**
- `2e9708c` realized_pnl on covered-call assignments: 3 accounting bug fixes

**2026-04-24:**
- `b41e39a` Wheel: defer call-expired status to trade_sync, detect early exercise via shares drop
- `dd16ec6` Wheel: don't mark calls EXPIRED on expiry day (incomplete fix, superseded)

**2026-04-23:**
- `3c744c6` Breakthrough tier: reject ETFs, sub-500M caps, reverse splits
- `65a9dec` Logger: route structlog through stdlib so events land in `trader.log`
- `06cb459` Watchlist: flag stocks with stale metrics
- `23f6f72` Screener: dedup symbols across tiers, breakthrough wins

**2026-04-22:**
- `5738250` Fix 0: `composite_score` clobber in `_update_watchlist_metrics`
- `8ae93c3` Fix P: tier proportions unified
- `d9821da` Fix A narrow: pool-aware dividend routing
- `785d83f` Piece 2 (partially superseded by Option B)
- `e1d9568` Dashboard green-box persistence
- `1a8b883` Fix B: IBKR fundamentals fallback + pence normalization
- `077aab5` Option B: `dividend_total_return_score` column + tier-aware compound quality
- `c6d5e06` Commit B: Phase 2b refresh scores for held holdings not in top-100
- `2a95c7b` Fix metrics job order: fetch metrics before recalc

## Monday (2026-05-11) — Big-Ticket #1 + #2 shipped, #3 fixes partial, watchlist field-name bug surfaced (14 commits)

**Long day. Three structural items shipped, plus a real bug fix, plus repo hygiene. Big-Ticket #3 ended with a deeper-than-expected diagnostic detour that reframed the original problem.**

### Big-Ticket #1: breakthrough_history eviction (commit `2620c65`)

Caps `breakthrough_history` pool at 75 entries. When the pool exceeds cap, evicts oldest entries first by `last_seen` with window protection (entries within the current week are protected from eviction). Earlier shipped + verified dormant — pool was below cap. The cap matters most when the system runs continuously for months and the natural growth rate from new fresh proposals would otherwise unbounded the pool.

### Big-Ticket #2: anchored 30→25 breakthrough selection — fully shipped + verified across 3 runs

**Design**: instead of generating 30 fresh names every run and selecting 25 (high churn), now the breakthrough selection runs in two phases. Phase A (`_get_breakthrough_candidates`) produces fresh names. Phase B (`_run_breakthrough_selection`) merges fresh + anchor (last run's selected 25) and asks Claude to pick the final 25. Anchor preservation creates natural continuity across runs; high-conviction names from prior weeks survive multiple selections unless a stronger candidate displaces them.

Five commits built it:
- **`91af94a`** (B#2-1) — Added `last_run_at` ISO timestamp field to breakthrough_history entries. Each persisted entry now records when it was last selected, enabling anchor identification as "entries with max(last_run_at)."
- **`8eb7e11`** (B#2-2) — Added `BreakthroughSelectionAudit` SQLAlchemy table. 12 columns including JSON blobs for fresh_symbols, anchor_symbols, selected, and Claude's raw response. Schema auto-creates via `Base.metadata.create_all`.
- **`04bc137`** (B#2-3) — `_build_breakthrough_selection_prompt()` builder. Inputs: 30 fresh + 25 anchor (deduplicated by symbol, fresh wins on conflict). Output: instructions to pick 25 final names.
- **`e38d2b6`** (B#2-4a) — `_call_claude_for_selection()` + `_run_breakthrough_selection()` orchestration helpers.
- **`1128891`** (B#2-4b) — Wire-in to `screen_all()`. Calls `_run_breakthrough_selection()` after fresh proposal generation, persists selected.
- **`51d764b`** (B#2-4c) — **Bug fix discovered mid-session.** First post-wire-in run (id=1, 11:24) had `selected_count=25` but YAML only persisted 17 names with new timestamp. Cause: persist loop iterated only `breakthrough_fresh_meta`; anchor-only selected names (8 of 25) were never persisted, would have dropped from anchor next run. Fix: filter breakthrough_scores, then score anchor-only names via `self._score_stock`, append to scores, then unified persist loop over `_selected_syms` pulling metadata fresh-first then anchor.
- **`a7da6ad`** — Manual migration. `/tmp/mark_anchor_22.py` marked today's 22 breakthrough names with `last_run_at='2026-05-11T07:59:00'` to seed anchor.

**Live verification across 3 runs:**
- id=1 (11:24:02): anchor=22 fresh=24 merged=39 selected=25 fallback=False — first activation, exposed the bug
- id=2 (13:32:30): anchor=17 fresh=17 merged=30 selected=25 fallback=False — 4c verified, persisted 25/25 ✓
- id=3 (18:28:40): anchor=25 fresh=20 merged=35 selected=25 fallback=False — anchor cascade fully working ✓

Pool state at session end: 57 entries, 4 distinct timestamps (`07:59:00:11`, `11:24:02:3`, `13:32:30:5`, `18:28:40:25`).

### Watchlist field-name bug (commit `59d9391`)

**Surfaced unexpectedly via son's-clone investigation.** Earlier today, while comparing repos to decide what to port, noticed son's code uses field name `options_exchange:` for derivatives-exchange routing while our code reads `opt_exchange:` at `src/strategy/universe.py:147`. Then grep'd our `config/watchlist.yaml` and found 29 entries silently using `options_exchange:` — son's name — which our code completely ignored. Result: `universe.get_options_exchange()` returned "SMART" fallback for all 29 European/Asian stocks (EUREX, OSE.JPN, OSE, ASX, ICEEU), foreign put-scans returned no chains, scans silently no-op'd.

How they got there: presumably hand-copied from son's clone at some point without renaming. Bug had been live for weeks.

Fix: single-pattern rename `options_exchange:` → `opt_exchange:` across 29 entries in `config/watchlist.yaml`. No code change. Activates 29 European/Asian watchlist entries — Swiss (NESN, NOVN, ROG, SIKA, LONN, GIVN, GEBN, UBSG, ZURN, SREN, ABBN, SLHN), Norwegian (BWLPG, HAUTO, EQNR, MOWI, AKRBP, DNB, ORK, YAR, SUBC, SFL), Australian (WDS, XRO, ALL), German/EUREX entries, and ICEEU (UK derivatives). Next foreign-market scan after restart should now actually evaluate option chains for these stocks instead of silently no-op'ing.

Suggestion-mode + auto-approve OFF means any produced suggestions go through manual review before execution.

### Big-Ticket #3: international augmentation handling — partial (3 commits) + diagnostic detour

The original problem framing: today's run (id=2, 13:32) augmentation had `FP` (TotalEnergies, France) fail scoring with `score=0.0, reason=scoring_failed`. Root cause: Claude proposed FP with exchange=SMART, currency=USD because the OUTPUT FORMAT example in both augmentation prompts uses those as placeholder values. With wrong contract inputs, `ib.qualifyContracts()` returned empty, `_score_stock` returned None.

Three fixes shipped:
- **`8d6debb`** (Fix C) — Client-side dedup of augmentation proposals. id=2 had 8 dividend duplicates (V, WMT, UNP, MSFT, PG, KO, JNJ, CDNS) Claude proposed despite the 518-symbol EXCLUDED list in the prompt. Hypothesis: 518 names is too long for Claude to attend to reliably. Fix: 5-line insert at each call site (growth + dividend) that filters Claude's proposals against `exclusion` set before processing. Audit rows for filtered dupes never get created.
- **`46a24c4`** (Intl mapping guide) — Added EXCHANGE/CURRENCY MAPPING block to both prompts (growth + dividend). 21 markets mapped using actual CANDIDATE_POOLS conventions: US/SMART/USD, France/SBF/EUR, Germany/IBIS/EUR, Netherlands/AEB/EUR, Switzerland/SWX/CHF, Italy/BVME/EUR, UK/LSE/GBP, Japan/TSEJ/JPY, etc. Plus ticker conventions note (TTE not FP, MC not LVMUY). Designed to fix the case where Claude proposes a foreign name but with wrong exchange/currency.
- **`d0e39f0`** (Raw logging) — Added `raw_proposal_json` TEXT column to AugmentationAudit. Uses existing `_migrate_columns()` pattern in `src/core/database.py` — auto-applies ALTER TABLE on startup. `json.dumps(proposal)` once at top of `_process_augmentation_proposal`, passed to all 4 audit row sites (duplicate, scoring_failed, beat_cutoff, below_cutoff). Pure diagnostic infrastructure; immediately paid off in evaluating fix effectiveness.

**Post-restart augmentation run (id=3, 18:28)** verified all three fixes worked technically:
- Dedup: 0 duplicates this run (vs 8 in id=2) ✓
- raw_proposal_json populated for all rows ✓
- Mapping guide formatting available
- No fallback errors

BUT id=3 revealed something new: **Claude proposed 100% US names** (13 proposals total). Zero international. Mapping guide didn't make Claude reach for foreign names — it only told Claude how to format them IF proposed.

### Diagnostic detour — what the data actually shows

Several hypotheses tested and rejected before arriving at the right diagnosis:

1. **"Claude has US bias in augmentation prompts"** — rejected. Breakthrough prompt produces ~20% non-US fresh proposals consistently. Same Claude API, same screener, different prompt — different behavior. So it's prompt structure not Claude.
2. **"Scoring rubric biases toward US"** — rejected by direct check. Top-15 dividend tier is **12/15 non-US** (Canadian banks, UK telco, Dutch banks, Chinese insurer, South African, Norwegian, Spanish utilities). The dividend rubric correctly rewards foreign income-payers.
3. **"Foreign names attrit through the screener"** — partially right. Growth tier: 369 non-US inputs collapse to 13 outputs (96% attrition). Dividend tier: 29 non-US inputs → 15 outputs (48% attrition, actually BETTER survival than US).

Final correct diagnosis (Ryan's framing): **the geographic distribution is the system working correctly, not failing.** Growth ends up 84% US because growth-as-defined-by-rubric (revenue durability, compounding quality, operating leverage) IS concentrated in the US tech sector. European industrials/banks aren't growth investments; they fail the rubric for legitimate reasons. The 96% non-US growth attrition isn't bias — it's correct identification that mature European businesses aren't compounders.

Dividend tier proposing US names is also rational: foreign dividend quality is already captured (12/15 of top-15), so marginal additions are most likely US names not yet covered. Augmentation has little new to add internationally because the curated pools + scoring already capture most foreign quality.

**Outcome: no Fix B needed.** The original Fix B (force international diversity in augmentation prompts) would have pushed Claude to propose mediocre European industrials and Norwegian variable-dividend stocks that fail scoring correctly. The Fix C dedup + intl mapping guide + raw logging are sufficient. Augmentation behaving as it should.

### Repo hygiene (commits `728695f`, `077c219`)

`728695f` — untracked 5 machine-rewritten files via `git rm --cached`: `config/options_universe.yaml`, `config/screened_universe.yaml`, `config/structural_risks.yaml`, `data/portfolio_account_cache.json`, `data/screener_last_run.json`. Files stay on disk; app keeps writing them; git stops watching commit churn.

`077c219` — Added to `.gitignore`: runtime cache patterns (`__pycache__/`, `.pytest_cache/`), `*.bak` files (deleted 14 stale ones from 2026-05-04), and the now-untracked files.

### Son's clone investigation (NOT ported)

Added `mesicap` SSH remote (push disabled). Compared `mesicap/main` vs our `main`. 16 unique commits on son's side. Per-Ryan classification:
- `4ab28ad` (asyncio reentry locks) — our work backported to his fork. Comment-only diff vs ours.
- `75df952` (3 trade_sync bugs) — Bug 1 + Bug 3 not in our code. Patch prepared (`/tmp/patch_tradesync_v1.py`) but **decided to skip — not failure-mode-critical, suggestion-mode is the safety net for going live**.
- `45158a9` (RACE/IDEM options exchange + CLOSED/EXPIRED defer logic) — RACE is son-specific. Parked.
- Other commits: deploy/systemd/healthcheck (his), graphs (cosmetic), per-account risk limits (his).

### CREDENTIAL SAFETY INCIDENT (third occurrence)

Claude asked Ryan to run `git remote -v`. Output contained live GitHub Personal Access Token `github_pat_11B7H5GGI0Y...` embedded in HTTPS remote URL. Token went into Anthropic conversation logs. Ryan rightly angry — Ryan is not a programmer, told Claude this in user preferences, Claude should have anticipated.

**Resolution**: Ryan revoked token at https://github.com/settings/personal-access-tokens. Switched origin to SSH: `git remote set-url origin git@github.com:rainrosimannus-spec/automatic_option_trader.git`. SSH already authenticated.

**Rule reinforced** (third incident — strict rule, no exceptions): NEVER ask Ryan to run commands that expose credentials: `git remote -v`, `env`, `printenv`, `cat .env`, `cat .git/config`, `history`, `ps auxe`. PATs are often in HTTPS git URLs. Always use redacted variants: `git remote -v | sed 's|//[^@]*@|//[REDACTED]@|g'`. ALWAYS warn before commands touching credential-bearing files. Ryan will not catch leaks.

### Session-end state

Commits shipped today (all on origin):
1. `2620c65` — B#1: Eviction
2. `91af94a` — B#2-1: last_run_at timestamp
3. `8eb7e11` — B#2-2: Audit table
4. `04bc137` — B#2-3: Selection prompt
5. `e38d2b6` — B#2-4a: Orchestration helpers
6. `a7da6ad` — B#2: Anchor migration (YAML)
7. `728695f` — Untrack runtime artifacts
8. `077c219` — Gitignore caches + .bak
9. `1128891` — B#2-4b: Wire-in to screen_all
10. `51d764b` — B#2-4c: Anchor-only persist+score fix
11. `59d9391` — Watchlist opt_exchange rename (29 entries)
12. `8d6debb` — B#3 Fix C: Client-side dedup
13. `46a24c4` — B#3 Intl: Exchange/currency mapping guide
14. `d0e39f0` — B#3 Raw logging: raw_proposal_json column

**Big-Ticket #1 done. Big-Ticket #2 done + verified. Big-Ticket #3 has its three fixes in; Fix B deferred/cancelled (system working correctly). Watchlist Issue 2 fixed. Repo hygiene done. SSH-only remote.**

Running PID at session end had Commit 4c but not 8d6debb, 46a24c4, d0e39f0. Restart picks all three up + applies the auto-migration that adds `raw_proposal_json` column.

