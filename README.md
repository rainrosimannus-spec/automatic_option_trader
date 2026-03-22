# Maggy & Winston

**Automated options trading and long-term portfolio management system running 24/7 on a Linux server, connected to two live IBKR accounts.**

---

## Working Style

The owner is not a programmer. All instructions must be:
- **Complete and copy-paste ready** — every command goes in a code block, no steps skipped
- **Sequential** — one change at a time, verify output before proceeding to the next
- **Atomic** — fix one thing, verify it works, then commit before moving on
- **Explicit** — never assume the user knows what a command does or why

Standard sequence for every change: **fix → verify → commit**. Never bundle unverified changes into a single commit.

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
| `src/strategy/wheel.py` | Assignment detection, covered call writing |
| `src/strategy/risk.py` | All risk gates: VIX, margin, sector, position size, earnings |
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
| `src/core/config.py` | Pydantic config models including `DteTiers` |
| `config/watchlist.yaml` | Options universe: `growth:` / `dividend:` sections, contract sizes for UK stocks |
| `config/settings.yaml` | All strategy parameters: `dte_tiers`, VIX thresholds, profit targets |
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
| 20–30 | 7–14 DTE | 7–14 DTE |
| > 30 | **halt** | **halt** |

Fail-open: if VIX data unavailable → mid tier (7–14 DTE).

**52-week high filter** *(options only — does not apply to portfolio buying)*: New puts are blocked if the stock is more than 40% below its 52-week high (`get_52week_high()` via IBKR historical data). Prevents selling puts on structurally broken stocks. Fail-open if data unavailable.

**Profit taking**: Closes at 50/65/75% profit depending on DTE. **Skips positions with DTE ≤ 3** — lets them expire worthless instead. Commissions would eat the remaining premium.

**Active risk gates** (always on):
- VIX > 30 → halt all new puts
- SPY MA10 < MA20 → 50% position size reduction
- Max 10 trades/day (scales with NLV)
- Adaptive position size by NLV
- Max 30% exposure per sector
- Market hours gate (±60 min)
- Skip earnings within 3 days
- Dynamic delta by VIX regime
- 40% below 52-week high → no new puts
- 0–3 DTE positions expire — no early close
- Wheel on assignment

### Portfolio Manager (Winston)

**Allocation target**: 50% growth / 25% dividend / 25% breakthrough

**Universe**: ~50 stocks, planned expansion to 100 in Month 2 alongside $5M scaling. Originally 100 stocks, halved for scanning efficiency when FMP free tier (250 calls/day) became the bottleneck. Expanding back to 100 requires either an FMP paid plan (~$20/month) or batching the monthly screener across two days.

**Philosophy**: Slow, deliberate deployment. Best stocks at the right price, 10-year horizon. No rush to be fully invested — patience is the edge.

**Invested capital**: Always sourced from `portfolio_capital_injections` table — not cost basis, not market value. Sum of all cash deposits = true invested capital ($498,514 as of March 2026).

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
| `dte_tiers.mid_vix.dte_min_usd` | Min DTE in mid VIX | 7 |
| `dte_tiers.mid_vix.dte_max_usd` | Max DTE in mid VIX | 14 |
| `vix_pause_threshold` | Hard halt above this VIX | 30.0 |
| `delta_min` / `delta_max` | Put delta range (config fallback) | 0.15 / 0.30 |
| `min_premium_put` | Minimum acceptable put premium | $0.50 |
| `contracts_per_stock` | Contracts per position | 1 |
| `profit_take_enabled` | Enable profit taker | true |
| `cc_dte_min` / `cc_dte_max` | Covered call DTE range | 5 / 30 |

### `config/watchlist.yaml`

Structured as `growth:` and `dividend:` sections. UK stocks (RIO, SHEL, HSBA, AZN) require `contract_size: 1000`. Both this file **and** the DB `portfolio_watchlist` table must be kept in sync when tiers change — YAML feeds the annual screener, DB drives the dashboard.

---

## Dashboard

`http://37.0.30.34:8080`

- **Options tab** — open positions, P&L, performance graph (options NLV vs ~11,700 EUR seed capital), active risk rules
- **Portfolio tab** — holdings, watchlist, performance graph (portfolio NLV vs $498,514 invested, BRK-B benchmark), accrued interest, loans
- **Controls** — force close, cancel order (both update DB after IBKR action)
- **Sync buttons** — trade history sync, positions sync, capital injections sync

> Portfolio performance graph starts from **2026-03-20** — first day with correct `portfolio_nlv` snapshots. The graph will be flat/empty until the first snapshot job runs at 09:35 ET on the next trading day.

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
Flex Query refreshes at 08:00 ET. If IBKR hadn't settled overnight data by then, yesterday's value was cached. Trigger manually if needed:
```bash
cd ~/automatic_option_trader && .venv/bin/python3 -c "
from src.portfolio.capital_injections import fetch_accrued_interest_usd
print(fetch_accrued_interest_usd())
"
```

**Portfolio performance graph is empty**
Normal after a restart or `graph_start_date` reset. First data point writes at 09:35 ET on the next trading day. If it stays empty longer, check `system_state` table key `graph_start_date`.

**`FMP 402 Payment Required` errors**
The `/stable/quote` endpoint requires a paid FMP plan. The system uses IBKR for 52-week high and VIX instead — these errors are harmless. FMP is only used for the monthly screener (income, ratios, balance sheet — 250 free calls/day sufficient for ~50 stocks, not enough for 100).

---

## Roadmap

### Open items

| # | Item | Priority |
|---|------|----------|
| 1 | NLV staleness 16:00–20:00 — investigate `accountValues()` push on read-only connections | Medium |
| 2 | `restart-all.sh` — stress-test through real crash with active positions | Medium |
| 3 | Cancel / Force close buttons — test on a live open position | Low |
| 4 | iPhone Safari sync button unreliable | Low |

### $5M options account scaling (Month 2 — April/May 2026)

The system is **not safe at $5M** without explicit capital scaling safeguards. Current NLV-based position sizing would produce $300–750K collateral per single put at that scale.

Required before going live at $5M:

| Safeguard | Description |
|-----------|-------------|
| Hard dollar cap per position | Absolute ceiling on collateral regardless of NLV (e.g. $50K max) |
| Hard cap on total exposure | Max total open collateral across all positions |
| Daily deployment limit | Max new collateral per day — prevent $5M deployment in one scan cycle |
| Emergency halt threshold | Auto-halt if unrealized loss exceeds X% of NLV in a single day |
| Suggestion mode ramp | Manual approval of every trade for first 2–4 weeks at new capital level |

**Suggested roadmap:**
- **Month 1 (March–April)** — run test account through full cycle, observe, fix
- **Month 2 (April–May)** — implement scaling safeguards; expand portfolio universe to 100 stocks; upgrade FMP plan
- **Month 3 (May–June)** — simulate $5M parameters in suggestion mode, flip to live when confident

### Portfolio strategy session (separate)

- Review all ~50 watchlist names with strict 10-year quality lens
- Expand universe to 100 stocks (25 growth / 12–13 dividend / 12–13 breakthrough additions)
- Position sizing for $5M scale
- Buy signal conservatism calibration
- **Max return session**: what would change if life depended on it

---

## Git

```
github.com/rainrosimannus-spec/automatic_option_trader
```

Commit after every meaningful change. `data/` is not in git (cache files). `config/settings.yaml` and `config/watchlist.yaml` are in git.

---

*Last updated: March 20, 2026 — v0.4 (VIX-adaptive DTE, 52-week high filter, portfolio graph separation)*

