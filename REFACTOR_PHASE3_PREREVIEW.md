# Pre-Review of the Source-of-Truth Refactor Plan — trading-safety gate

Status: **PHASE 3 spec writing NOT recommended as drafted.** Scope reduced to a
safe reporting-only fix (see §5). Written 2026-05-22 at Rain's request: "double
check it will not break anything substantial in trading (that works)."

---

## 1. Verdict

The refactor's core premise — *"remove the strategy-side `Position` writes;
`trade_sync.py` recreates them from IBKR (reader-only, no edits)"* — is **only
true for short options.** It is false for long options, assigned stock, and
closed IPO-flip records. As drafted, commits **1, 4, and 7 (and 5 by
dependency) would silently lose Position rows or corrupt classification on the
live system.** Commit 6 — the one with the risk gate — is the safe one.

## 2. The load-bearing flaw

`src/broker/trade_sync.py:659`:

```python
for key, data in ibkr_positions.items():
    if key in tracked_keys: continue
    if data["position_size"] >= 0: continue   # <-- only SHORT positions
```

`trade_sync` only ever **creates** `short_put` / `short_call` / `covered_call`
(lines 664-673, 718). It never creates long, stock, or closed rows. It also
*reads* an existing open stock `Position` (lines 668-672) to decide
`covered_call` vs `short_call`.

Map against the removal commits (verified against live DB position_type usage):

| Commit | Site | Creates | trade_sync recreates? |
|---|---|---|---|
| 6 `put_seller._record_trade` | put_seller.py:442 | `short_put` (size<0) | ✅ yes (664-665) |
| 1 `hedge._buy_hedge` | hedge.py:226 | `hedge_put`, long BUY put | ❌ skipped by L659 |
| 4 `wheel._handle_assignment` | wheel.py:178 | `stock`, long assigned shares | ❌ never creates stock rows |
| 7 `ipo/trader._record_flip_exit` | ipo/trader.py:406 | `ipo_flip`, **CLOSED** w/ realized_pnl | ❌ never recreates closed history |
| 5 `wheel._handle_called_away` | wheel.py | closes the stock Position | ⚠️ nothing to close if 4 lands |

Two breakages beyond "missing rows":
- **Commit 4 corrupts covered-call classification.** trade_sync reads the stock
  Position (668-672) to tag `covered_call`; remove it and calls against assigned
  stock get tagged **naked `short_call`** — what risk limits exist to block. It
  also breaks wheel's own reads (`check_pre_market_exit` wheel.py:205+, the
  idempotency guard wheel.py:157-162).
- **Commit 7 deletes P&L history.** `_record_flip_exit` writes a CLOSED Position
  with `realized_pnl`; nothing reproduces it.

For 1/4/7 to be safe, `trade_sync` would need edits (create long/stock/closed
rows) — which the plan explicitly forbids — or an explicit `Position →
PortfolioHolding` migration with trade_sync's covered-call query and wheel's
internal reads repointed.

## 3. Secondary issues with the original plan

- **Unaccounted writers:** `src/main.py:460` (a near-duplicate of `sync.py`'s
  holdings sync) and `src/portfolio/scheduler.py:1715` both create
  `PortfolioHolding`; neither is in the sole-writer accounting.
- **`sync.py` mislabels buys as `put_assigned`** (104-125) unless a prior
  `buy`/`put_assigned` tx exists. Commits 9/10 must keep `buyer` writing its
  `buy` tx or ordinary buys get stamped `put_assigned`.
- **`sync.py` reconciles to IBKR every run** (stale-zeroing 128-139). Any
  "intent" holding written before a fill (commit 10) gets zeroed next pass.
- **Q7 `/close-all` rewrite** turns a DB-only footgun into an endpoint that
  submits live market orders for the whole book — needs a confirm-token + dry
  run; arguably more dangerous than the current behavior.
- **Q11 `ipo_flip → generic stock`** — enumerate every risk/sizing filter
  keyed on `position_type=="ipo_flip"` before flipping; silent exposure change.

## 4. What is actually broken (live DB: `data/trades.db`, 2026-05-22)

Diagnosis re-frames the whole effort. Positions/holdings are clean; only the
*transaction log* has duplicates, and it is informational:

- `positions`: **no** duplicate OPEN rows (wheel `d9550e6` guard working).
- `portfolio_holdings`: **no** duplicate symbols.
- `portfolio_transactions`: **cleaner than first feared.** By the reliable
  `ibkr_exec_id` key there are **zero** duplicates (190/193 rows carry a unique
  exec_id). A loose group-by-symbol/day flagged apparent dups (two CRWV
  `buy_call`s same day, etc.) but those are **legitimately distinct fills** — a
  blanket cleanup would have deleted real trades. The **one** genuine logical
  duplicate is a `put_assigned` pair: `RGTI 23.0 20260320` (assignments carry
  no exec_id, so the idempotency guard — not exec_id dedup — is the right fix).
- **Headline financials do NOT come from transactions.** `portfolio.py:239-244`
  derives `total_pnl` from IBKR unrealized P&L / holdings market value, and
  `total_invested` from capital injections. Duplicate transactions are **history-
  list clutter, not a wrong number.**
- Holding share-doubling from a re-run **self-heals**: `sync.py:78` overwrites
  `shares` from IBKR each sync. The only non-self-healing artifact of the
  assignment race is the duplicate transaction (append-only).

Root cause of the tx dups: `buyer._handle_put_assignment` (802-869) has **no
idempotency guard** (unlike wheel's `d9550e6`). Concurrent `_check_put_entries`
runs / retries each create a holding + a `put_assigned` transaction.

## 5. Adopted scope — reporting-only, trading untouched

Per Rain: position display is informational (decide if the system is working);
the trade itself is financial and ranks far higher. So:

**Rule for any change here: additive guards only. Never remove a writer trading
reads. Never touch any `placeOrder` / order-decision logic.**

Done this session:
- **Idempotency + keyed dedup guard on `buyer._handle_put_assignment`**
  (mirrors `d9550e6`). Skips re-processing an already-assigned put-entry and
  skips a duplicate `put_assigned` transaction keyed on symbol+strike+expiry.
  Pure recording path — no order logic touched. Deploy via `~/restart-all.sh`;
  verify next `_check_put_entries` cycle logs the guard once on a re-run and no
  new duplicate `portfolio_transactions` row appears.

Proposed, NOT yet done (needs Rain's go-ahead — it mutates the live DB):
- **Delete the single stray `RGTI 23.0 20260320` `put_assigned` duplicate**
  (keep MIN(id), delete the other). This is the only genuine dup; everything
  else is a legitimate distinct fill, so NO blanket cleanup. Back up
  `data/trades.db` first; show the exact row before deleting.

Explicitly NOT adopted: commits 1, 4, 5, 7 (writer removal) and the
`Position → PortfolioHolding` migration — they touch trading-coupled state.
If the source-of-truth refactor is still wanted later, gate it behind a
`trade_sync` edit that creates long/stock/closed Positions first.
