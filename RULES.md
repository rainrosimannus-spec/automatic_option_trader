# Maggy & Winston — RULES

This file is read by Claude at the start of every session.
It contains hard rules, non-negotiables, and critical architectural facts.
Nothing here changes unless the architecture changes.

---

## Working Rules — Non-Negotiable

- **Read code via git link** — always ask the user to paste a raw GitHub URL rather than grepping blindly. Format: `https://raw.githubusercontent.com/rainrosimannus-spec/automatic_option_trader/main/PATH`. User pastes the URL into chat, Claude fetches it with web_fetch. This is the preferred way to read any source file.

- **No heredoc for Python** — `<< 'EOF'` breaks on quotes. Always write patch files using `python3 -c "open('/tmp/patchN.py','w').write(...)"` or via the bash_tool container
- **No manual file editing** — every code change is a copy-paste ready command in a code block
- **Read before writing** — fetch or grep the exact current code before writing any replacement. Never assume what code looks like
- **Fix -> verify -> commit** — one change at a time. Verify with grep or sed -n before committing. Never bundle unverified changes
- **Admit mistakes immediately** — no excuses, no explanations. Say what went wrong, fix it properly
- **Copy, don't invent** — if something works on the options side, copy it to the portfolio side exactly. Never invent a new solution when a working one exists
- **Syntax check before writing** — for any Python patch: run `ast.parse()` before writing the file. If syntax error, do not write, show the failing lines
- **Never assume a session's context** — always read actual code or DB data before drawing conclusions

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

# Read raw GitHub file (user must paste URL first — Claude cannot initiate fetch)
# https://raw.githubusercontent.com/rainrosimannus-spec/automatic_option_trader/main/PATH

# Restart
~/restart-all.sh
# Watch phone for TWO 2FA approvals: options gateway first, portfolio ~35s later

# If restart leaves stale processes
pkill -9 -f ibcalpha; pkill -9 -f IbcGateway; pkill -9 -f GWClient; tmux kill-server; sleep 5; ~/restart-all.sh

# Trader app not starting — see crash reason
cd ~/automatic_option_trader && source .venv/bin/activate && python -m src.main 2>&1 | head -50
```

---

## Architecture — Critical Facts

**Two accounts, two connections:**
- Maggy (options): U23886415, port 4001, tmux `options` + `trader`
- Winston (portfolio): U17562704, port 7496, tmux `portfolio`
- App tmux `trader` runs everything: web server on port 8080, scheduler, both connections

**DB is SQLite** at path in settings.yaml. Two separate NLV fields in `account_snapshots`:
- `net_liquidation` = options account → options performance graph
- `portfolio_nlv` = portfolio account → portfolio performance graph
- **Never mix these up** — graphs break silently

**`TradeSuggestion` is in `src/core/suggestions.py`** — not models.py

**`get_db()` is a `@contextmanager`** — use `with get_db() as db:` always

**Portfolio loans** must use `TotalCashBalance` where `currency == "BASE"` only.
Never sum per-currency balances — causes doubling.

**Accrued interest** comes from file cache (`data/portfolio_account_cache.json`), NOT from live Flex call during IBKR refresh. The disconnected path must NOT write 0.0 to the file.

**Accrued interest is YTD** — IBKR portal shows MTD. ~$3,583 YTD vs ~$1,582 MTD is expected and correct.

**`REVIEW_ONLY_ACTIONS`** in suggestions.py: `sell_stock_review`, `reduce_position_review`, `sell_covered_call_review` — these are NEVER auto-executed even in auto mode. Always manual.

**Chronos venv** is `~/timesfm_env` (Python 3.11) — separate from trading venv `.venv` (Python 3.12). Both need `chronos-forecasting` installed.

**Monthly review has three indentation levels** for `create_suggestion()` calls:
- 12 spaces: regular sell blocks
- 16 spaces: reduce blocks (nested inside `if reduce_shares > 0:`)
- 24 spaces: growth thesis weak block (triply nested)
- 28+ spaces: deeply nested blocks

**`_get_chronos_trend(symbol)`** helper exists in `src/portfolio/scheduler.py` — use it, don't reinvent.

---

## What Winston Does (Full Picture)

Entry methods:
1. Score >= 70 → direct buy
2. Score below 70 but high enough → sell cash-secured put for entry
3. Stock held, above SMA, in profit → sell covered call suggestion (manual only)
4. Fundamentals deteriorate → sell stock suggestion (manual only)

Entry guards (buyer.py, in order):
1. Earnings guard: skip if earnings within 3 days (`has_upcoming_earnings()`)
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
2. **Structlog not writing to `logs/trader.log`** — only stdlib logging writes to file. Low priority.
3. **TimesFM GPU device bug** — `model.device` returns `cuda:0` even after `.to('cpu')`. Workaround: `type(tfm.model).device = property(lambda self: torch.device('cpu'))` before compile. Not integrated — Chronos preferred.
