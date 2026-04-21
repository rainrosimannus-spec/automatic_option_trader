# STATE.md — Maggy & Winston

**Last updated:** 2026-04-18 (Saturday night, Luxembourg)
**System status:** restarted, Phases 11-20 live
**Next session focus:** Monday open watch + Phase 19 (portfolio-side unknown-event classification)

---

## Session identity

- User: Ryan (Luxembourg, non-programmer, requires copy-paste-ready commands)
- Server: `rain@octoserver-genoax2:~/automatic_option_trader`
- Python: `.venv/bin/python3`
- GitHub: `github.com/rainrosimannus-spec/automatic_option_trader` (public, token-embedded remote for passwordless push)
- Dashboard: http://37.0.30.34:8080
- Restart: `~/restart-all.sh` (requires 2x phone 2FA, wait 15-20s after)
- Log source: `tmux capture-pane -t trader -p -S -2000` (NOT a log file — structlog doesn't write to disk)
- **Working rule:** no heredoc for Python patches. Always write patch script to `/tmp/patchN.py`, ast.parse check, dry-run, commit.

---

## Weekend arc — 19 commits across Fri/Sat/Sun/Sat

### Context

Friday Apr 17 morning: $200 SHOP loss from duplicate buy-to-close fills. Root causes: Black-Scholes-derived "prices" used in real orders, no naked-buy guard, no roll-up re-entry guard, silent bid-as-ask fallbacks. The weekend was recovery + extension.

### Phases shipped

| # | Commit | Day | Summary |
|---|---|---|---|
| 2 | `706bd43` | Fri | CC buyback: removed bid-as-ask + intrinsic fallbacks |
| 3a | `8bbb3b9` | Fri | Put profit-taker: removed BS fallback |
| 3f | `685851b` | Fri | CC roll-up: use real `get_stock_live_price()` |
| 3b+3c | `f15d82c` | Fri | Screener: top-2 candidate iteration, no BS |
| 3d | `43d1084` | Fri | Hedge: removed BS fallback for SPY put ask |
| 3e | `54f04c0` | Fri | Portfolio put-entry: live bid via reqMktData |
| 5 | `307e970` | Fri | `buy_to_close_{call,put}`: `_verify_short_position()` guard via `ib.positions()` |
| 6 | `0eef4a6` | Fri | `_roll_call_up`: skip if pending SELL_CALL at IBKR |
| 7 | `62204a0` | Sat | VIX rate-of-change: spike bumps effective tier |
| 8 | `bb6bdae` | Sat | MA50 trend clamp: tier can't de-escalate while SPY below MA50 |
| 9 | `909723f` | Sat | NLV drawdown sizing: 5-day lookback scales daily cap (1.0/0.75/0.50/0.25) |
| 10 | `f6a55e2` | Sat | `wheel_exit_mode` column on Position; delta 0.35-0.55 + interest surcharge |
| 11 | `3308f2b` | Sat | Strategy Guardrails dashboard panel (4 cards above regime) |
| hotfix | (inline) | Sat | Removed redundant `from src.core.models import Position` on dashboard.py line 252 |
| 12 | `12e1e10` | Sat | Removed vestigial `current_vix` param from `_resolve_dte` |
| 13 | `7f65a3f` | Sat | Honest net_cost_basis: `_realized_cc_premium_per_share` uses closed-CC `realized_pnl` |
| 14 spec | `b27b9ed` | Sat | PHASE_14_SPEC.md committed to repo root |
| 15 | `842ffc6` | Sun | Rule badges panel updated: 3 reworded + 3 added + new red Safety Guards category |
| 16 | `34099b6` | Sun | Iron-logic wheel CC: single 80% profit rule, roll-up call site removed (_roll_call_up body untouched) |
| 14 | `96de94a` | Sun | `reconcile_submitted_trades()` in broker/trade_sync.py, called from `_job_trade_sync` |
| 17 | `1aef19f` | Sun | Portfolio classification fail-closed on stock_price<=0 (fixes CRWV-class misclassification) |
| 18 | *merged into 17* | Sun | portfolio.html: sort Open Options Positions by expiry asc |
| 20 | `e38efeb` | Sat | Remove exit-mode count card from Strategy Guardrails (Phase 16 made exit mode universal → card redundant) |

### Design invariants (still true)

- BS pricing allowed for selection/ranking/delta-filtering ONLY, NEVER for order limits or profit-take comparisons
- `_scan_lock` at scheduler-job level, `get_ib_lock` (RLock, reentrant) at IBKR call level
- One change → verify → commit → push. No stacked patches.
- Wheel CCs are ALL exit-oriented now — single 80% profit rule, no roll-ups, no DTE gaps
- `_roll_call_up` function body UNTOUCHED in profit_taker.py — just no longer called. Deactivated, not removed, for reversibility.
- `Position.wheel_exit_mode` column left intact in DB for potential future use. Only the dashboard count card was removed in Phase 20.

---

## Current positions (options account "Maggy")

| Symbol | Position | Strike/Expiry | Entry |
|---|---|---|---|
| TTD | 100 shares + short CC | $26 May 8 | $0.86 |
| SHOP | 100 shares + short CC | $135 May 15 | $9.20 |
| PANW | short CC | $162.50 Apr 24 | $5.47 |
| PG | 100 shares + short CC | $146 Apr 24 | $2.74 |

**Phase 16 trigger prices** (CC ask drops to ≤20% of entry = 80% profit → close):
- PG: ~$0.55
- TTD: ~$0.17
- SHOP: ~$1.84 (deep ITM, unlikely without big stock drop)
- PANW: ~$1.09

None fire at current market. Safe Monday deployment.

---

## Monday-open checklist

1. **App already restarted.** Phases 11-20 are live.
2. **Watch dashboard** at http://37.0.30.34:8080 — Strategy Guardrails panel now has 3 cards (SPY vs MA50, 5-day drawdown, Effective Tier). Should populate after first scheduler cycle.
3. **Auto-exec decision** — currently ON per STATE. Ryan to decide by Monday open.
4. **First 30 min after open** — watch tmux buffer for: `cc_profit_target_hit`, `phantom_trade_reconciled`, `ma50_clamp_applied`, `drawdown_scaling_applied`. Any should log exactly as expected.

---

## Known issues — next session work

### Phase 19: Portfolio-side unknown-event classification

**The BLK label bug.** Naked short BLK $1000 call was assigned → IBKR auto-generated -100 short stock position → `src/portfolio/sync.py:120` labels it `put_assigned` ("Detected via IBKR sync — put assigned or manual buy").

**Data is correct** (shares=-100, amount=$101,686, price=$1016.86). Only the label is wrong. Cosmetic, not financial.

**Root cause:** `sync.py` treats all non-strategy share movements as `put_assigned` catch-all. Doesn't model off-strategy events (naked calls, short stocks).

**Ryan's strategy reality:** portfolio is long-term; only buys, sells puts, occasionally sells calls/trims. Never shorts. RIO short + BLK naked straddle are off-strategy manual experiments.

**Proposed fix:** add `action="external"` / `action="unknown"` classification rather than guessing. Requires careful read of which `put_assigned` writers correspond to legitimate strategy events vs catch-all. Do NOT rush.

### Other deferred

- Earnings clustering guard — explicitly deferred (per-stock `check_earnings` sufficient for now)
- TimesFM GPU device bug — workaround via Chronos (Chronos preferred anyway)
- NLV staleness 16:00-20:00 ET — `accountValues` push stops idle, known limitation

---

## Tool knowledge & gotchas

- Raw GitHub URLs can be fetched via browser tool but not via bash/web_fetch
- Chrome extension (Claude in Chrome) is unreliable — SSH paste-and-grep is the reliable fallback
- Tmux buffer is primary log source — structlog does NOT write to `logs/trader.log`
- Always wait 15-20 seconds after restart before navigating the dashboard
- DB cleanup from Friday incident: all SUBMITTED Trade rows were manually CANCELLED; SHOP $135 CC Position manually CLOSED; SHOP reconciled to 100 shares + short $135 May 15 @ $9.20

---

## Ryan's working style reminders

- Not a programmer. All code changes as copy-paste-ready terminal commands.
- Strictly sequential: fix → verify → commit, one change at a time
- Direct, brief, no hedging. Push back if Claude over-engineers.
- Values honesty about uncertainty over confident-but-wrong
- Market instincts consistently sharper than Claude's on specific calls
- Sunday off-Claude is sacred (this session was Saturday, good)
