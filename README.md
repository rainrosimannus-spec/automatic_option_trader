# Maggy & Winston

**Automated options trading and long-term portfolio management system running 24/7 on a Linux server, connected to two live IBKR accounts.**

---

## Working Style

The owner is not a programmer. All instructions must be:
- **Complete and copy-paste ready** — every command goes in a code block, no steps skipped
- **Sequential** — one change at a time, verify output before proceeding to the next
- **Atomic** — fix one thing, verify it works, then commit before moving on
- **Explicit** — never assume the user knows what a command does or why
- **Never assume — always check** — before drawing conclusions, read the actual code or data. Do not guess based on how things usually work
- **No shortcuts** — always do it properly, as if the outcome depended on it. The easy path that skips steps is always wrong
- **Admit mistakes immediately** — if a fix didn't work, say so directly. Do not make excuses, do not linger on explanations, do not blame timing or external factors. Acknowledge the mistake, find the real cause, fix it properly
- **No manual file editing** — the owner is not a programmer. All code changes must be delivered as copy-paste ready terminal commands that modify files directly (e.g. `sed`, Python heredoc replacements). Never instruct the owner to open a file and edit it manually. Every change goes in a code block that can be run as-is.
- **Read before writing** — always read the exact current code before writing a replacement. Never assume what the code looks like based on context or previous sessions.
- **Verify after every change** — after every file modification, run a targeted `grep` or `sed -n` to confirm the new code is exactly in place before moving to the next change.

Standard sequence for every change: **fix → verify → commit**. Never bundle unverified changes into a single commit.

**Bug handling:**
- Before fixing anything, check if the same functionality already exists and works somewhere else in the codebase — copy it, don't reinvent it
- If the options side does something correctly, the portfolio side should do it exactly the same way
- Never invent a new solution when a working one already exists in the code

---

## Table of Contents

1. [What It Does](#what-it-does)
2. [System Architecture](#system-architecture)
3. [Server & Access](#server--access)
4. [Key Files](#key-files)
5. [Database](#database)
6. [Strategy Logic](#strategy-logic)
7. [Configuration Reference](#configuration-reference)
8. [Dashboard](#dashboard)
9. [Daily Operations](#daily-operations)
10. [Troubleshooting](#troubleshooting)
11. [Roadmap](#roadmap)

---

## What It Does

Maggy & Winston runs two parallel strategies on two separate IBKR accounts. The **Options Trader** (Maggy) sells short puts on a curated universe of stocks, takes profit at 50–75%, and rolls into covered calls on assignment — the classic wheel strategy. The **Portfolio Manager** (Winston) holds a long-term concentrated portfolio of ~50 stocks across growth, dividend, and breakthrough tiers, deploying capital slowly when prices are right. Both are fully automated, risk-gated, and visible through a single web dashboard. The system has been live since February 20, 2026.

---

## System Architecture

```
Server: rain@37.0.30.34
Project: ~/automatic_option_trader
Restart: ~/restart-all.sh
Dashboard: http://37.0.30.34:8080
```

| Component | Account | Port | IBC tmux | App tmux |
|-----------|---------|------|----------|----------|
| Options Trader | U23886415 | 4001 | `options` | `trader` |
| Portfolio Manager | U17562704 | 7496 | `portfolio` | — |
| IPO Rider | both | both | both | — |

The Python app (`trader` tmux) serves the web dashboard via uvicorn on port 8080. Each IBKR gateway runs in its own IBC tmux session. One Python process per gateway, single IBKR connection per process, lock-based to prevent race conditions.

---

## Server & Access

```bash
ssh rain@37.0.30.34
cd ~/automatic_option_trader

# Restart everything
~/restart-all.sh

# Check tmux sessions
tmux ls

# Attach to options trader logs
tmux attach -t trader

# Attach to portfolio gateway
tmux attach -t portfolio

# Manual DB query
.venv/bin/python3 -c "from src.core.database import get_db; ..."
```

> **Always use `.venv/bin/python3`**, never `python3` directly. The venv is `.venv`, not `venv`.

---

## Key Files

| File | Purpose |
|------|---------|
| `src/main.py` | Entry point, startup sequence, connections, scheduler, web server |
| `src/scheduler/jobs.py` | All scheduled jobs: health check, profit check, scans, expiry, snapshots |
| `src/strategy/put_seller.py` | Put selling logic, `_resolve_dte()`, 52-week high filter, GBP pence conversion |
| `src/strategy/profit_taker.py` | Profit taking, market hours gate, skips DTE ≤ 3 |
| `src/strategy/screener.py` | `screen_puts()`, DTE override params, Black-Scholes contract scoring |
| `src/strategy/wheel.py` | Assignment detection, covered call writing, regime-aware CC delta |
| `src/strategy/risk.py` | All risk gates: VIX, margin, sector, position size, scaling safeguards |
| `src/broker/connection.py` | Options IBKR connection, client ID 12, lock-based |
| `src/broker/market_data.py` | Stock/option prices, IV, SPY MAs, `get_option_live_price()`, `get_52week_high()` |
| `src/broker/trade_sync.py` | Syncs IBKR executions to DB, marks positions expired/filled |
| `src/portfolio/connection.py` | Portfolio IBKR connection, `get_cached_portfolio_account()` |
| `src/portfolio/scheduler.py` | Portfolio jobs, pence conversion for GBP transactions |
| `src/portfolio/fmp.py` | FMP API client, fundamentals, `get_year_high()` (unused — IBKR preferred) |
| `src/portfolio/capital_injections.py` | Flex Query for deposits, `fetch_accrued_interest_usd()`, `get_total_invested_usd()` |
| `src/web/routes/dashboard.py` | Options dashboard, performance graph using `net_liquidation` |
| `src/web/routes/portfolio.py` | Portfolio dashboard, `_build_portfolio_performance()` using `portfolio_nlv` |
| `src/web/routes/controls.py` | Force close, cancel order buttons |
| `src/core/models.py` | All DB models including `AccountSnapshot` (has both `net_liquidation` and `portfolio_nlv`) |
| `src/core/config.py` | Pydantic config models including `DteTiers` and scaling safeguard fields |
| `config/watchlist.yaml` | Options universe: `growth:` / `dividend:` sections, contract sizes for UK stocks |
| `config/settings.yaml` | All strategy parameters: `dte_tiers`, VIX thresholds, profit targets, scaling safeguards |
| `data/portfolio_account_cache.json` | File cache: NLV, loans, accrued interest, FX rates, BRK-B history (not in git) |

---

## Database

SQLite. Path configured in `settings.yaml`.

| Table | Contents |
|-------|----------|
| `positions` | Open and closed short puts / covered calls |
| `trades` | All order executions |
| `account_snapshots` | Daily NLV snapshots for both performance graphs |
| `portfolio_transactions` | Portfolio buy/sell/dividend history |
| `portfolio_watchlist` | Screened universe with scores and buy signals |
| `portfolio_holdings` | Current long-term holdings |
| `portfolio_capital_injections` | Cash deposits — authoritative invested capital source |
| `system_state` | Key-value store: VIX, SPY MA, start dates, regime flags |

> **`account_snapshots` has two separate NLV fields:**
> - `net_liquidation` — options account NLV (feeds options performance graph)
> - `portfolio_nlv` — portfolio account NLV (feeds portfolio performance graph)
>
> These were separated on March 20, 2026. **Never mix them up** — the graphs break silently if the wrong field is written.

---

## Strategy Logic

### Options Trader (Maggy)

**VIX-adaptive DTE selection** (`_resolve_dte()` in `put_seller.py`):

| VIX | USD stocks | Non-USD stocks |
|-----|-----------|----------------|
| < 20 | 0–3 DTE | 0–7 DTE |
| 20–30 | 0–3 DTE | 0–7 DTE |
| > 30 | **halt** | **halt** |

Both tiers below 30 use the same DTE range. 7-14 DTE was removed in March 2026 — see Strategy Decisions section for rationale.

Fail-open: if VIX data unavailable → mid tier (0–3 DTE).

**52-week high filter** *(options only)*: New puts are blocked if the stock is more than 40% below its 52-week high. Prevents selling puts on structurally broken stocks. Fail-open if data unavailable.

**Profit taking**: Closes at 50/65/75% profit depending on DTE. **Skips positions with DTE ≤ 3** — lets them expire worthless instead. Commissions would eat the remaining premium.

**Active risk gates** (always on):
- VIX > 30 → halt all new puts
- SPY MA10 < MA20 → 50% position size reduction (TREND_BEARISH)
- Max 10 trades/day (scales with NLV)
- Adaptive position size by NLV
- Hard dollar cap per position (scaling safeguard)
- Total open exposure cap (scaling safeguard)
- Daily deployment limit (scaling safeguard)
- Intraday loss halt (scaling safeguard)
- Max 30% exposure per sector
- Market hours gate (±60 min)
- Skip earnings within 3 days
- Dynamic delta by VIX regime and trend
- 40% below 52-week high → no new puts
- 0–3 DTE positions expire — no early close
- Wheel on assignment

### Portfolio Manager (Winston)

**Allocation target**: 50% growth / 25% dividend / 25% breakthrough

**Universe**: ~50 stocks, planned expansion to 100 in Month 2 alongside $5M scaling.

**Philosophy**: Slow, deliberate deployment. Best stocks at the right price, 10-year horizon. No rush to be fully invested — patience is the edge.

**Invested capital**: Always sourced from `portfolio_capital_injections` table — not cost basis, not market value. Sum of all cash deposits = true invested capital ($498,514 as of March 2026).

---

## Strategy Decisions and Rationale (March 24, 2026)

### Why 0-3 DTE across all VIX regimes

The original system used 7-14 DTE in elevated VIX to provide a recovery buffer. This was wrong for the wheel strategy. The buffer only helps traders who close positions early at a loss. For a wheel operator who holds to expiry and accepts assignment, a longer DTE just means a longer period of unrealized loss with no ability to reset. The 7-14 DTE window was removed. Both VIX tiers below 30 now use 0-3 DTE for USD stocks and 0-7 DTE for non-USD stocks (liquidity reasons — European and Asian option chains are thinner at very short DTE).

The HALT at VIX 30+ already handles the one scenario where 0-3 DTE is genuinely dangerous (gap-down, no-recovery, instant assignment in a panicking market). Below that threshold, faster expiry and faster reset is strictly better for wheel.

### Delta calibration by regime

| Condition | Delta range | Rationale |
|---|---|---|
| TREND_NEUTRAL, VIX < 20 | 0.20–0.30 | Normal market, standard wheel |
| TREND_NEUTRAL, VIX 20–25 | 0.15–0.25 | Elevated, step back |
| TREND_NEUTRAL, VIX 25–30 | 0.10–0.20 | High, conservative |
| TREND_BEARISH, any VIX | 0.10–0.20 | Force high-VIX range regardless of VIX number |
| TREND_BEARISH + VIX > 25 | 0.08–0.15 | Tightest range — directional risk dominates |

TREND_BEARISH is detected by SPY MA10 < MA20. In a trending bear market, strikes that look safe at neutral-market delta levels are not safe — the market is actively moving against you. The TREND_BEARISH override compensates for directional risk that VIX alone does not capture.

### Covered call delta by regime

In TREND_BEARISH the priority is letting the stock recover, not maximizing call premium. Selling tight calls in a downtrend locks the position permanently below cost basis.

| Condition | CC delta range | Rationale |
|---|---|---|
| TREND_NEUTRAL, VIX < 20 | 0.20–0.30 | Collect premium aggressively |
| TREND_NEUTRAL, VIX >= 20 | 0.15–0.25 | IV inflated, good premium further out |
| TREND_BEARISH | 0.10–0.20 | Let stock recover, don't cap upside |
| TREND_BEARISH + VIX > 25 | 0.08–0.15 | Maximum distance, tiny premium acceptable |

### No stop-loss on puts — by design

Stop-losses on short puts are not implemented and should not be. For the wheel strategy, hitting the strike means assignment — the strategy working as intended, not a failure. The cost of stop-losses (closing at a loss, paying commission, missing recovery) exceeds the benefit over a full wheel cycle. The HALT at VIX 30+ is the circuit breaker for extreme scenarios.

---

## $5M+ Scaling Safeguards

All limits are adaptive — they scale with NLV as a percentage, with hard ceilings that prevent runaway exposure at very large account sizes. All parameters are tunable in `config/settings.yaml` without code changes.

| Safeguard | Formula | At $5M | At $15M |
|---|---|---|---|
| Per-position cap | min(NLV × 1%, $150K) | $50K | $150K |
| Total exposure cap | min(NLV × 20%, $2M) | $1M | $2M |
| Daily deployment limit | min(NLV × 3%, $500K) | $150K | $450K |
| Intraday loss halt | NLV × 2% | $100K | $300K |

Position count tiers:

| NLV | Max positions |
|---|---|
| < $50K | 4 |
| < $200K | 8 |
| < $500K | 15 |
| < $2M | 20 |
| < $5M | 30 |
| $5M+ | 40 |

### Suggested ramp to $5M live

- **Month 1** — run current account through full wheel cycle, observe all new risk gates firing
- **Month 2** — simulate $5M parameters in suggestion mode, verify no single scan cycle would breach limits
- **Month 3** — flip to live; keep suggestion mode on for first 2–4 weeks at new capital level

---

## Configuration Reference

### `config/settings.yaml` — key strategy parameters

| Parameter | Purpose | Default |
|-----------|---------|---------|
| `dte_tiers.low_vix.vix_max` | VIX threshold for short-DTE mode | 20 |
| `dte_tiers.mid_vix.vix_max` | VIX threshold for halt | 30 |
| `dte_tiers.low_vix.dte_min_usd` | Min DTE for USD stocks in low VIX | 0 |
| `dte_tiers.low_vix.dte_max_usd` | Max DTE for USD stocks in low VIX | 3 |
| `dte_tiers.low_vix.dte_min_other` | Min DTE for non-USD in low VIX | 0 |
| `dte_tiers.low_vix.dte_max_other` | Max DTE for non-USD in low VIX | 7 |
| `dte_tiers.mid_vix.dte_min_usd` | Min DTE in mid VIX | 0 |
| `dte_tiers.mid_vix.dte_max_usd` | Max DTE in mid VIX | 3 |
| `vix_pause_threshold` | Hard halt above this VIX | 30.0 |
| `delta_min` / `delta_max` | Put delta range (config fallback) | 0.15 / 0.30 |
| `min_premium_put` | Minimum acceptable put premium | $0.50 |
| `contracts_per_stock` | Contracts per position | 1 |
| `profit_take_enabled` | Enable profit taker | true |
| `cc_dte_min` / `cc_dte_max` | Covered call DTE range | 5 / 30 |
| `position_dollar_pct` | Per-position cap as % of NLV | 0.01 |
| `max_position_dollars` | Hard ceiling per position | $150,000 |
| `min_position_dollars` | Floor — small accounts unaffected | $25,000 |
| `total_exposure_pct` | Total open collateral cap as % of NLV | 0.20 |
| `max_total_exposure` | Hard ceiling total open collateral | $2,000,000 |
| `daily_deployment_pct` | Max new collateral per day as % of NLV | 0.03 |
| `max_daily_deployment` | Hard ceiling new collateral per day | $500,000 |
| `intraday_loss_halt_pct` | Halt if unrealized loss exceeds this % of NLV | 0.02 |

### `config/watchlist.yaml`

Structured as `growth:` and `dividend:` sections. UK stocks (RIO, SHEL, HSBA, AZN) require `contract_size: 1000`. Both this file **and** the DB `portfolio_watchlist` table must be kept in sync when tiers change — YAML feeds the annual screener, DB drives the dashboard.

---

## Dashboard

`http://37.0.30.34:8080`

- **Options tab** — open positions, P&L, performance graph (options NLV vs ~11,700 EUR seed capital), active risk rules
- **Portfolio tab** — holdings, watchlist, performance graph (portfolio NLV vs $498,514 invested, BRK-B benchmark), accrued interest, loans
- **Controls** — force close, cancel order (both update DB after IBKR action)
- **Sync buttons** — trade history sync, positions sync, capital injections sync

> Portfolio performance graph starts from **2026-03-20** — first day with correct `portfolio_nlv` snapshots.

---

## Daily Operations

### Scheduled jobs (US Eastern time)

| Time | Job |
|------|-----|
| 09:35 ET | Account snapshot (options NLV + portfolio NLV) |
| Market hours | Put selling scans, profit checks, health checks |
| 08:00 ET | Accrued interest Flex Query refresh |
| Monthly | Portfolio screener (FMP fundamentals re-score) |
| Daily | BRK-B history update |
| Market close | Cancel stale SUBMITTED orders |

### Watchlist changes

When adding/removing/reclassifying stocks, **always update both**:
1. `config/watchlist.yaml`
2. The `portfolio_watchlist` DB table

### Restarting

```bash
~/restart-all.sh
```

Kills all `automatic_option_trader` Python processes, then restarts IBC gateways and the Python app. Web server starts immediately in a background thread — dashboard available within seconds.

---

## Troubleshooting

**Dashboard shows black screen on load**
Restart was recently run. Web server starts in a background thread — wait 10–15 seconds and refresh.

**`remove Client 99` loop in logs**
Missing `_ensure_event_loop()` in `_get_portfolio_connection()`. Fix was applied March 17. If it reappears, the portfolio connection is leaking — check `src/portfolio/connection.py`.

**NLV shows stale value (16:00–20:00)**
Known issue. `accountValues()` push likely stops on read-only connections after extended idle. NLV refreshes on the next connection cycle. Uninvestigated.

**`restart-all.sh` leaves stale client ID 12**
The script kills all `automatic_option_trader` Python processes before restarting. If the conflict persists, check for zombie Java gateway processes:
```bash
pkill -f "ibgateway"
```
Then re-run `~/restart-all.sh`.

**Accrued interest shows same number as yesterday**
Flex Query refreshes at 08:00 ET. Trigger manually if needed:
```bash
cd ~/automatic_option_trader && .venv/bin/python3 -c "
from src.portfolio.capital_injections import fetch_accrued_interest_usd
print(fetch_accrued_interest_usd())
"
```

**Portfolio performance graph is empty**
Normal after a restart or `graph_start_date` reset. First data point writes at 09:35 ET on the next trading day. If it stays empty longer, check `system_state` table key `graph_start_date`.

**`FMP 402 Payment Required` errors**
The `/stable/quote` endpoint requires a paid FMP plan. The system uses IBKR for 52-week high and VIX instead — these errors are harmless.

---

## Roadmap

### Open items

| # | Item | Priority |
|---|------|----------|
| 1 | NLV staleness 16:00–20:00 — investigate `accountValues()` push on read-only connections | Medium |
| 2 | `restart-all.sh` — stress-test through real crash with active positions | Medium |
| 3 | Cancel / Force close buttons — test on a live open position | Low |
| 4 | Next session: universe watchlist review for $5M scale | Medium |
| 5 | Next session: portfolio strategy session (50/25/25, universe expansion to 100 stocks) | Medium |

### $5M options account scaling (Month 2 — April/May 2026)

All scaling safeguards implemented March 24, 2026. See $5M+ Scaling Safeguards section above for full details and ramp plan.

### Portfolio strategy session (separate)

- Review all ~50 watchlist names with strict 10-year quality lens
- Expand universe to 100 stocks (25 growth / 12–13 dividend / 12–13 breakthrough additions)
- Position sizing for $5M scale
- Buy signal conservatism calibration
- **Max return session**: what would change if life depended on it

---

## Handoff — March 24, 2026 (end of day)

**Strategy changes (this session):**
- 7-14 DTE mid-VIX tier dropped — both tiers now 0-3 DTE (USD) / 0-7 DTE (non-USD)
- Delta tiers recalibrated: VIX<20=0.20-0.30, VIX 20-25=0.15-0.25, VIX 25-30=0.10-0.20
- TREND_BEARISH delta floor added to `risk.py` — forces high-VIX range regardless of actual VIX
- TREND_BEARISH + VIX>25 forces tightest range 0.08-0.15
- Regime-aware covered call delta added to `wheel.py`
- Screener DTE scoring: hardcoded 7-DTE target replaced with dynamic midpoint of passed range

**Scaling safeguards added (this session):**
- Hard dollar cap per position in `risk.py` and `put_seller.py`
- Total open exposure cap — `check_total_exposure()` in `risk.py`
- Daily deployment limit — `check_daily_deployment()` in `risk.py`
- Intraday loss halt — `check_intraday_loss()` in `risk.py`
- Position count tiers extended to $5M+ in `risk.py`
- 8 new scaling fields added to Pydantic config model in `config.py`

**Current positions:**
5 open puts expiring March 27 — all ITM, all likely assignment: PANW, PG, SHOP, TTD, UBER.
Wheel begins on all five Friday. System is in TREND_BEARISH — covered call logic will automatically select wide strikes (delta 0.10-0.20). Do not fight for premium, let the stocks recover.

**System state:**
Both connections stable. All new risk gates active and logging correctly.
Margin at 91% due to open puts — no new trades until Friday expiry clears margin.

---

## Git

```
github.com/rainrosimannus-spec/automatic_option_trader
```

Commit after every meaningful change. `data/` is not in git (cache files). `config/settings.yaml` and `config/watchlist.yaml` are in git — note that `settings.yaml` is in `.gitignore` due to API keys; use `git add -f` if intentional commit is needed.

---

*Last updated: March 24, 2026 — v0.5 (regime-aware DTE/delta, $5M scaling safeguards)*

---

## Claude Access Instructions (for next sessions)

### Dashboard access
Claude can read the dashboard directly using the Claude in Chrome browser tool:
- Navigate to `http://37.0.30.34:8080` for the main options dashboard
- Navigate to `http://37.0.30.34:8080/suggestions/options` for options suggestions
- Navigate to `http://37.0.30.34:8080/suggestions/` for portfolio suggestions

### Git access
Token at `~/.github_claude_token`. Claude cannot fetch GitHub URLs directly — must read files via bash on the server.

---

## Handoff — March 27, 2026 (end of day)

**Fixed today:**
- `controls.py`: cancel-order imported wrong function (`_get_order_connection` → `get_ib` + `get_ib_lock`)
- `controls.py`: cancel-order and force-close use `ensure_main_event_loop()` instead of asyncio executor
- `controls.py`: cancel-order now also cancels matching suggestion by symbol+strike+expiry
- `jobs.py`: every put scan and CC scan cancels all unfilled IBKR orders before placing new ones
- `jobs.py`: expired submitted suggestions only if no matching filled trade exists
- `jobs.py`: health check reconciles submitted suggestions against live IBKR orders every 5 min
- `jobs.py`: NLV snapshot skips writing 0.0 — guard added same as portfolio_nlv
- `screener.py`: screen_calls now fetches real market bid/ask from IBKR (same as screen_puts)
- `wheel.py`: CC delta fixed at 0.30-0.45 regardless of regime — goal is to get called away, not protect
- `logger.py`: rotating file logger added at `logs/trader.log`, 7 days retention

**Current positions (options account):**
- PG: stock at $143, covered call $146 sold at $2.65 (April expiry) — waiting for fill confirmation
- TTD $26, SHOP $123, UBER $75, PANW $162 — all expired today, all ITM, all assigned
- Wheel will fire covered calls on TTD, SHOP, UBER, PANW after assignment detection

**System state:**
- VIX at 31 — HALT active, no new puts until VIX drops below 30
- Both connections stable
- Suggestion lifecycle now clean: pending → submitted → filled/expired/cancelled

**Open items:**
- Verify TTD, SHOP, UBER, PANW assignments detected and covered calls written
- Structlog output does not write to `logs/trader.log` — only stdlib logging does. Fix needed.
- File logging added but structlog still goes to stdout only
