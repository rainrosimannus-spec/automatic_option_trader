# Maggy & Winston — RULES

This file is read by Claude at the start of every session.
It contains hard rules, non-negotiables, and critical architectural facts.
Nothing here changes unless the architecture changes.

---

## Working Rules — Non-Negotiable

- **No heredoc for Python** — `<< 'EOF'` breaks on quotes. Always write patch files using `python3 -c "open('/tmp/patchN.py','w').write(...)"` or via the bash_tool container
- **No manual file editing** — every code change is a copy-paste ready command in a code block
- **Read before writing** — fetch or grep the exact current code before writing any replacement. Never assume what code looks like
- **Fix -> verify -> commit** — one change at a time. Verify with grep or sed -n before committing. Never bundle unverified changes
- **Admit mistakes immediately** — no excuses, no explanations. Say what went wrong, fix it properly
- **Copy, don't invent** — if something works on the options side, copy it to the portfolio side exactly. Never invent a new solution when a working one exists
- **Syntax check before writing** — for any Python patch: run `ast.parse()` before writing the file. If syntax error, do not write, show the failing lines
- **Never assume a session's context** — always read actual code or DB data before drawing conclusions
- **CREDENTIAL SAFETY (strict, no exceptions)** — NEVER ask Ryan to run commands that expose credentials: `git remote -v`, `env`, `printenv`, `cat .env`, `cat .git/config`, `history`, `ps auxe`. PATs are often embedded in HTTPS git URLs. Use redacted variants like `git remote -v | sed 's|//[^@]*@|//[REDACTED]@|g'`. Always warn before commands touching credential-bearing files. Ryan is not a programmer and will not catch leaks.

---

## File Handoff to the Server (Ryan's preferred workflow)

When Claude generates a file (STATE.md, RULES.md, scripts, etc.), the path is:

1. Claude creates the file via `create_file` + `present_files` for download.
2. Ryan downloads it locally. If the browser auto-numbers (e.g. `state_7.md`), Ryan renames to the exact target filename — **case-sensitive** (`STATE.md`, not `state.md`).
3. Ryan opens `https://github.com/rainrosimannus-spec/automatic_option_trader/upload/main` and drags the file onto the page.
4. Ryan commits via the GitHub web UI ("Commit directly to the main branch").
5. On the server: `cd ~/automatic_option_trader && git pull`

**Never** ask Ryan to: paste long file content into a terminal, use heredocs, use base64 chunks, or run `scp`. The GitHub web upload is the only acceptable path.

---

## Critical Commands

```bash
# Always use the venv
.venv/bin/python3

# get_db() is a CONTEXT MANAGER — always:
with get_db() as db:
    ...
# NEVER: next(get_db())  — this fails

# Check syntax before any file write
.venv/bin/python3 -c "import ast; ast.parse(open('src/file.py').read()); print('OK')"

# Read raw GitHub file (user must paste the URL — Claude cannot initiate web_fetch)
# https://raw.githubusercontent.com/rainrosimannus-spec/automatic_option_trader/refs/heads/main/STATE.md

# Restart
~/restart-all.sh
# Watch phone for 2FA approval(s). In MERGED MODE (current): one 2FA for the portfolio gateway only.
# In pre-merge / re-split mode: TWO 2FA approvals — options gateway first, portfolio ~35s later.

# If restart leaves stale processes
pkill -9 -f ibcalpha; pkill -9 -f IbcGateway; pkill -9 -f GWClient; tmux kill-server; sleep 5; ~/restart-all.sh

# Trader app not starting — see crash reason
cd ~/automatic_option_trader && source .venv/bin/activate && python -m src.main 2>&1 | head -50

# Inspect logs
tmux capture-pane -t trader -p -S -2000
```

---

## Architecture — Critical Facts

**Current operating mode: MERGED (since 2026-04-28).**
Both Maggy and Winston code run against a single account on this server:
- Account: U17562704
- Port: 7496 (portfolio gateway)
- Client IDs: Maggy=12, Winston=97 (same Python process, same gateway)
- Maggy: suggestion mode ON, auto-approve OFF
- Winston: read-only at IBKR protocol level (placeOrder rejected even if app-level flag flipped)

The original Maggy options account U23886415 lives on son's clone on the same machine (user `nexbit`). When a new dedicated options account arrives, follow the re-split path documented in STATE.md L1. Backups for re-split: `~/restart-all.sh.pre-merge-2026-04-28`, `~/watchdog-trader.sh.pre-merge-2026-04-28`.

**Canonical ports when split** (for reference and re-split work):
- Maggy options gateway: 4001
- Winston portfolio gateway: 7496

**DB is SQLite** at path in settings.yaml. Two separate NLV fields in `account_snapshots`:
- `net_liquidation` = options account → options performance graph
- `portfolio_nlv` = portfolio account → portfolio performance graph
- **Never mix these up** — graphs break silently

**Separate tables per strategy** (still true in merged mode):
- Maggy writes to: `positions`
- Winston writes to: `portfolio_holdings`, `portfolio_put_entries`

**`TradeSuggestion` is in `src/core/suggestions.py`** — not models.py

**`get_db()` is a `@contextmanager`** — use `with get_db() as db:` always

**Portfolio lock supervisor** is in `src/broker/connection.py`. When merged, `get_portfolio_lock()` acquires Maggy's `ib_lock` FIRST, then Winston's `_portfolio_lock`. Auto-detected via `_detect_merged_with_options()`. When ports diverge post-re-split, supervisor becomes a no-op automatically — no code change needed.

**Portfolio loans** must use `TotalCashBalance` where `currency == "BASE"` only.
Never sum per-currency balances — causes doubling.

**Accrued interest** comes from file cache (`data/portfolio_account_cache.json`), NOT from live Flex call during IBKR refresh. The disconnected path must NOT write 0.0 to the file.

**Accrued interest is YTD** — IBKR portal shows MTD. ~$3,583 YTD vs ~$1,582 MTD is expected and correct.

**`NetLiquidation` already includes accrued interest** as a separate line. Graph is correct as-is; no double-adjustment needed.

**`REVIEW_ONLY_ACTIONS`** in suggestions.py: `sell_stock_review`, `reduce_position_review`, `sell_covered_call_review` — these are NEVER auto-executed even in auto mode. Always manual.

**Chronos venv** is `~/timesfm_env` (Python 3.11) — separate from trading venv `.venv` (Python 3.12). Both need `chronos-forecasting` installed.

**Monthly review has three indentation levels** for `create_suggestion()` calls:
- 12 spaces: regular sell blocks
- 16 spaces: reduce blocks (nested inside `if reduce_shares > 0:`)
- 24 spaces: growth thesis weak block (triply nested)
- 28+ spaces: deeply nested blocks

**`_get_chronos_trend(symbol)`** helper exists in `src/portfolio/scheduler.py` — use it, don't reinvent.

**Earnings gate is fail-CLOSED** — missing data → block. Opposite to most gates (VIX, MA, etc., which are fail-OPEN). Rationale: earnings is the single most predictable cause of overnight gap risk on a CSP.

**LSE prices come in pence.** Normalized at source in `src/portfolio/analyzer.py` (commit `95f7d67`, May 7). Eight other GBP-handling sites still divide by 100 separately; centralization deferred.

---

## What Winston Does (Full Picture)

Entry methods:
1. Score >= 75 → direct buy (threshold raised from 70 in commit `f65b9d4`, May 3)
2. Score 40–75 → sell cash-secured put for entry
3. Score < 40 → watchlist member, no action
4. Stock held, above SMA, in profit → sell covered call suggestion (manual only)
5. Fundamentals deteriorate → sell stock suggestion (manual only)

Composite formula (post-May 3 rebalance, commit `a25ccb1`):
- `composite = (raw − penalty) × 0.30 + compound_quality_pct × 0.70`
- Includes a 0–24 point fair-price base scaled across `discount_pct` from -5% to +5%
- Floor for `buy_signal=True` is `MIN_COMPOSITE_FOR_ACTION = 40.0`

Entry guards (buyer.py, in order):
1. Earnings guard: skip if earnings within 3 days (`has_upcoming_earnings()` — real CalendarReport, fail-CLOSED)
2. Sentiment guard: skip if Finnhub score < -0.3 last 7 days
3. Chronos guard: skip if forecast trend is "down"

Exit intelligence:
- Trailing stop monitor: every 15 min, 5% below peak, creates new suggestion (manual)
- Chronos exit suppression: suppress sell/reduce suggestions if `_get_chronos_trend()` == "up"
- `(not _cu) and create_suggestion(...)` pattern — lazy evaluation, skips call if _cu is True

---

## What Maggy Does (Full Picture)

- Sells short puts 0-3 DTE (USD), 0-7 DTE (non-USD)
- VIX > 30: full HALT
- SPY MA10 < MA20: TREND_BEARISH, delta forced to 0.10-0.20
- Profit taking at 50/65/75% — skips DTE <= 3 (let expire)
- 52-week high filter: blocks puts if stock >40% below year high
- Earnings skip: 3-day window
- Wheel on assignment: covered calls at delta 0.30-0.45 (goal is to get called away)
- No stop-loss on puts by design — wheel strategy, assignment is intended outcome
- DTE enforcement: both `_evaluate_symbol` and `_process_symbol` resolve DTE via `_resolve_dte(currency)` and pass `dte_min`/`dte_max` into `screen_puts` (commit `63f68c6`, Apr 27). Hardcoded 5–14 fallback in `screen_puts` is dead code — DO NOT rely on it.
- Covered call expiry handling: `wheel.py` uses `expiry <= today` but only marks `called_away` when IBKR confirms via shares dropping. Otherwise defers to `trade_sync` (commit `b41e39a`, Apr 24).

---

## Scheduled Jobs

| Time ET | Job |
|---------|-----|
| 09:35 | Account snapshot (both NLV fields) |
| Market hours | Put scans, profit checks, health checks |
| Every 15 min | Trailing stop monitor |
| 08:00 | Accrued interest Flex refresh |
| 17:30 | Chronos nightly forecast (all watchlist stocks) |
| Monthly Mon | Portfolio screener |
| 06:30 | BRK-B history update |
| Market close | Cancel stale orders |

---

## Known Bugs / Not Fixed Yet

1. **NLV staleness 16:00-20:00 ET** — `accountValues()` push stops after idle. Not investigated.
2. **Structlog → file routing was broken pre–Apr 23.** Fixed by commit `65a9dec` (Apr 23): switched to `stdlib.LoggerFactory`. Application events now land in `trader.log` correctly.
3. **TimesFM GPU device bug** — `model.device` returns `cuda:0` even after `.to('cpu')`. Workaround: `type(tfm.model).device = property(lambda self: torch.device('cpu'))` before compile. Not integrated — Chronos preferred.
4. **Maggy `unrealized_pnl` AttributeError on U17562704** — every `put_seller` scan errors at `src/strategy/risk.py:1224` reading `pos.unrealized_pnl`. Account-type/permission-specific bug; expected to disappear when a dedicated options account arrives. One-line fallback fix available if not: `getattr(pos, 'unrealizedPNL', 0.0)`. Cosmetic-only on this server in merged mode.
5. **KSPI-style fill claiming** — post-merge, whichever sync code (Maggy or Winston) runs first claims fills on the shared connection. Display split is annoying, accounting is correct. Moot post-re-split.
