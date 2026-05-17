# Source-of-Truth Refactor — Phase 2 Map

**Status:** Phase 2 + 2b complete. Full architecture audit done. All Position, PortfolioHolding, and PortfolioTransaction write paths mapped end-to-end.
**Next:** Phase 3 — write the target architecture spec, with concrete per-file changes.

**Date:** 2026-05-17. Phase 2b added later same day.
**Files read in Phase 2:** `src/portfolio/buyer.py`, `src/web/routes/controls.py`, `src/portfolio/sync.py`, `src/portfolio/scheduler.py`, `src/scheduler/jobs.py`, `src/web/routes/api.py`, `src/consigliere/advisor.py`.
**Phase 2b additional file:** `src/ipo/trader.py`.

This document extends `REFACTOR_PHASE1.md`. Read that first for the Maggy-side map and the architectural principle. This phase confirms the same patterns exist on the Winston side, maps the dashboard reader paths, and identifies the IPO Rider as a fifth Position-writing subsystem.

---

## Phase 2 — Winston-side Position-like Writers

The Winston system has its own set of tables that play the same role as Maggy's `Position`/`Trade`:

- `PortfolioHolding` — Winston's "what stocks do we actually own?" (equivalent to Maggy's stock-type `Position` rows)
- `PortfolioPutEntry` — Winston's "what put-entries are open?" (Winston-only concept; no Maggy equivalent)
- `PortfolioTransaction` — Winston's trade history (equivalent to Maggy's `Trade`)
- `PortfolioWatchlist` — screened universe + per-symbol metrics (Winston-specific)

### Writers to `PortfolioHolding`

| File | Function | What it writes | When | Pattern |
|---|---|---|---|---|
| `buyer.py` | `_handle_put_assignment` | New holding or quantity update | Buyer detects expired put assigned | **DB-first** |
| `buyer.py` | `_update_holding` (called from `_execute_buy`) | New holding or quantity update | Direct stock buy placed at IBKR | **DB-first** |
| `sync.py` | `sync_ibkr_holdings` | All holdings reconciled from IBKR | Periodic sync from IBKR portfolio | **IBKR-first** ✓ |
| `scheduler.py` | `job_portfolio_sync_trades` (inline at ~line 1700, `if action == "put_assigned"`) | New holding or quantity update | IBKR fill seen with price ~0 + stock below strike | **DB-first** |
| `ipo/trader.py` | `_check_lockup_fill` | New holding or quantity update | Lockup-period trailing-buy fills at IBKR | **DB-first** |
| `buyer.py` | `update_holdings_prices` | Price/market_value/PnL updates only | Hourly price refresh | Read/update (price only, not shares) |

**Five writers create or change `shares`. Only one is correct (`sync.py`).** The other four are Pattern A — they write before or alongside IBKR confirming the final state, with no inter-writer coordination.

### Writers to `PortfolioPutEntry`

| File | Function | What it writes | When | Pattern |
|---|---|---|---|---|
| `buyer.py` | `_execute_put_entry` | New put-entry row, status=open | Put order submitted | **DB-first** |
| `buyer.py` | `_handle_put_assignment` | Status update assigned + closed_at | Buyer detects put assigned | Idempotent |
| `buyer.py` | `_handle_put_expiry` | Status update expired + closed_at | Buyer detects put expired worthless | Idempotent |

`PortfolioPutEntry` is Winston-only. The lifecycle is OK on the state-transition side (assigned/expired are idempotent updates). The creation is Pattern A — `_execute_put_entry` writes the row immediately after `placeOrder`, before fill is confirmed by IBKR. If the limit order doesn't fill, the row sits there as "open" indefinitely (or until the expiry passes and `_check_put_entries` marks it expired).

### Writers to `PortfolioTransaction`

| File | Function | Action(s) written | Pattern |
|---|---|---|---|
| `buyer.py` | `_execute_put_entry` | `sell_put` | DB-first |
| `buyer.py` | `_execute_buy` | `buy` | DB-first |
| `buyer.py` | `_handle_put_assignment` | `put_assigned` | DB-first |
| `buyer.py` | `_handle_put_expiry` | `put_expired` | DB-first |
| `sync.py` | `sync_ibkr_holdings` | `put_assigned` (with `source="sync"`) | IBKR-first + 3-day dedup window |
| `scheduler.py` | `job_portfolio_sync_trades` | `buy`/`sell`/`sell_put`/`buy_put`/`put_assigned`/`expired`/`sell_call`/`buy_call`/`call_assigned`/`call_expired` (with `source="ibkr_sync"`) | IBKR-first + `ibkr_exec_id` dedup |
| `ipo/trader.py` | `_check_lockup_fill` | `buy` (with `source="ipo_rider"`, signal `"ipo_lockup_reentry"`) | DB-first after IBKR confirms trailing-buy fill |

`PortfolioTransaction` is mostly OK because every fill-derived write uses `ibkr_exec_id` for idempotency — but only sync.py and scheduler.py set `ibkr_exec_id`. Both buyer.py and ipo/trader.py write rows **without** the exec_id, leaving the IBM-style dedup as the only protection. The IBM bug fixed this morning (`fb3ef28`) widened sync.py's dedup window to catch `put_assigned`/`buy` collisions; whether it correctly handles the IPO Rider's `buy` action with `signal="ipo_lockup_reentry"` is an open verification question.

### Writers to `PortfolioWatchlist`

Watchlist is updated in many places (scoring, metrics refresh, monthly screen, etc.) but it's a metric-storage table, not a position table. Writes are upsert-by-symbol and largely independent. Not a source-of-truth concern.

---

## Phase 2b — IPO Rider Subsystem

`src/ipo/trader.py` operates in two distinct phases:

**Phase 1 (day-one flip):** Detect IPO ticker becoming tradeable, buy on opening auction, sell via trailing stop within a few days. Speculative short-term trade.

**Phase 2 (lockup re-entry):** Track pre-lockup price, monitor for dip after lockup expires, place trailing buy to catch the bounce. Becomes a long-term portfolio holding.

### Maggy-side writes (Phase 1 — IPO flip)

| Function | What it writes | When | Pattern |
|---|---|---|---|
| `_record_flip_exit` | Position with status=CLOSED, position_type="ipo_flip", realized_pnl computed | After trailing-stop, hard-stop-loss, OR force-sell on max hold days fills | DB-after-exit |
| `_record_flip_exit` (same function) | Two FILLED Trade rows: BUY_STOCK + SELL_STOCK | Same moment | Bookended record |

**Important nuance:** The IPO flip position is **never represented in the Position table during the holding period** (could be days). The Position row is created at the moment of exit, not entry. Both Trade rows (buy and sell) are written together at exit time.

**This creates a known duplicate problem with trade_sync:**

During the holding period, the IPO stock exists in IBKR but has no matching Position row in the DB. When `broker/trade_sync.sync_ibkr_positions` runs (every 15 min), it sees a stock at IBKR with no matching Position and creates a generic `stock` Position row — Pattern B doing its job correctly. Then later when the flip exits, `_record_flip_exit` creates an `ipo_flip` Position row for the same logical trade. **Two Position rows for one IPO position.**

This is the IPO-specific manifestation of the same "different code paths write the same logical event" architectural problem that plagues the rest of the system.

### Winston-side writes (Phase 2 — Lockup re-entry)

| Function | What it writes | When | Pattern |
|---|---|---|---|
| `_check_lockup_fill` | PortfolioHolding (new or quantity update) | Trailing buy fills at IBKR | **DB-first** (5th Winston-side writer) |
| `_check_lockup_fill` (same function) | PortfolioTransaction with action="buy", source="ipo_rider", signal="ipo_lockup_reentry" | Same moment | DB-first, **no ibkr_exec_id** |

The Winston-side lockup re-entry adds:
- A fifth writer to `PortfolioHolding` (alongside buyer.py × 2, sync.py, scheduler.py)
- A `PortfolioTransaction` row that lacks `ibkr_exec_id`, leaving it vulnerable to the same IBM-style duplicate-with-sync-writer collision that fb3ef28 was a band-aid for

Both writes happen after `_check_lockup_fill` verifies `fill_price is not None` from IBKR fills, so timing-wise they're after IBKR has confirmed the fill. But the writes are independent of trade_sync's writes — which will see the same fill via `ib.fills()` and create competing rows in `PortfolioTransaction` with `ibkr_exec_id="..."` (and possibly compete on PortfolioHolding creation via `job_portfolio_sync_trades`).

### IpoWatchlist lifecycle (Winston-only concept, no refactor needed)

`IpoWatchlist` is the IPO-specific state-tracking table — equivalent role to Winston's `PortfolioPutEntry` for the IPO subsystem. It tracks states: `watching → ipo_trading → flip_done → lockup_waiting → lockup_trading → lockup_done`. All state transitions are idempotent updates on a single row keyed by IPO ID. This table is fine as-is.

---

## Phase 2 — Maggy-side Scheduler and Dashboard

### `src/scheduler/jobs.py`

No new direct writers to `Position` discovered. This file is pure orchestration — every job that needs to write a Position calls into `wheel`/`put_seller`/`hedge`/`trade_sync`/`controls`/`ipo`, all of which were mapped in Phase 1, Phase 2, or Phase 2b.

**Notable observation:** several jobs contain explicit "expire orphaned SUBMITTED suggestions" logic (e.g., `job_scan_market` lines 109-130, `job_check_assignments` line ~209, `job_health_check` line ~480). This cleanup code exists *only because* Pattern A writers (put_seller, wheel-for-stock, hedge) write to the DB before IBKR confirms fills. When those orders don't actually fill, the corresponding SUBMITTED rows are left dangling. After the refactor, this orphan-cleanup logic becomes vestigial; it's not harmful but can be simplified.

### `src/web/routes/controls.py`

| Route | Behavior | Pattern |
|---|---|---|
| `POST /close-all` | Loops every OPEN Position, sets status=CLOSED, **does NOT submit any IBKR orders** | **Pure DB write, no IBKR action** |
| `POST /force-close/{id}` | Submits IBKR market order, then sets status=CLOSED on success | DB-first (on success) |
| `POST /halt`, `/resume` | Updates `SystemState` only | Clean |
| `POST /bridge` | Updates `SystemState` only | Clean |
| `POST /cancel-order/{id}` | Cancels IBKR order, then updates Trade row + matching TradeSuggestion | IBKR-first ✓ |

`/close-all` is the most dangerous endpoint in the codebase. It marks positions CLOSED in the DB while they remain OPEN at IBKR — the next `sync_ibkr_positions` run sees a stock at IBKR with no matching DB row and may recreate it as a new position. **This route should either:**
- Submit market orders to IBKR for each position and defer status update to trade_sync, **or**
- Be removed entirely as a footgun.

`/force-close` is Pattern A on success — the IBKR order may fill in seconds, but a rejected or hung order leaves the DB claiming CLOSED for something that isn't. Better than `/close-all` (at least it submits an order) but should defer the status update to trade_sync.

`/cancel-order` is the cleanest pattern in the file and the right reference for how IBKR-action routes should behave.

### `src/web/routes/api.py`

100% read-only. Four GET endpoints (`/status`, `/positions`, `/pnl`, `/trades/recent`). All queries, no mutations. The dashboard reads `position.realized_pnl` and `position.status == OPEN` directly — after the refactor, trade_sync must continue populating `realized_pnl` correctly at fill time. Already the case (broker/trade_sync.py line 615+ computes P&L from fill prices), but worth confirming in the Phase 3 spec.

### `src/consigliere/advisor.py`

Read-only against Position/Trade/TradeSuggestion/PortfolioHolding/account-summary. Writes only to its own `ConsigliereMemo` table. Six review modules — all observation, no action. Confirmed clean.

Notable for the refactor: Consigliere computes win rate, assignment rate, and delta calibration from Position and Trade rows. After the refactor those reads still work because trade_sync continues to set `realized_pnl` and `delta_at_entry` correctly. No change required.

---

## Updated Open Questions

**Resolved from Phase 1:**

- **Q1 (force-close path):** Confirmed Pattern A. `/force-close` writes status=CLOSED before fill confirmation. `/close-all` doesn't even submit IBKR orders. Both need fixing.
- **Q3 (sync_ibkr_positions Trade-creation logic):** The duplicate-Trade-on-sync issue still applies. The existing logic at line 656 only checks for any matching Trade without filtering by status, so a SUBMITTED Trade from put_seller could still result in a duplicate FILLED Trade being created by sync_ibkr_positions. After the refactor (put_seller writes only SUBMITTED Trades, sync handles state transition to FILLED), this collision goes away naturally.
- **Q4 (PortfolioPutEntry / Winston-side):** Confirmed — the same Pattern A duplicate-write problem exists on the Winston side. `buyer._handle_put_assignment` writes PortfolioHolding without idempotency; `scheduler.job_portfolio_sync_trades` ALSO writes PortfolioHolding from the same event; `sync.sync_ibkr_holdings` is the only Pattern B writer. Three writers, one truth — same architecture as Maggy.
- **Q6 (manual sync button):** "Sync from IBKR" button calls `sync_ibkr_trades` (broker side) for Maggy, and `job_portfolio_sync_trades` for Winston. After refactor these become more important — they're the user-facing reconciliation tools.

**Resolved from Phase 2b:**

- **Q9 (`src/ipo/trader.py`):** Read end-to-end. IPO Rider has two write paths to map:
  - **Maggy:** `_record_flip_exit` creates `Position(position_type="ipo_flip", status=CLOSED)` at exit. Likely duplicates with whatever trade_sync wrote during the hold (trade_sync creates a generic `stock` Position when it sees IBKR holds the IPO stock).
  - **Winston:** `_check_lockup_fill` creates `PortfolioHolding` and a `PortfolioTransaction(action="buy")` when the lockup trailing-buy fills. Fifth `PortfolioHolding` writer; PortfolioTransaction row has no `ibkr_exec_id`.
  - Adds 2 commits to the Phase 4 plan (Maggy ipo_flip Position no-write; Winston lockup Holding no-write). Refactor target: `_record_flip_exit` keeps the IpoWatchlist state transition + the two Trade rows but stops creating the duplicate Position; `_check_lockup_fill` keeps the IpoWatchlist state transition but lets `sync.sync_ibkr_holdings` materialize the holding from IBKR truth.

**Remaining open from Phase 1:**

- **Q2 (hedge close on roll):** Still open. `hedge._roll_hedge` marks the old hedge Position CLOSED locally. Hedge positions don't have natural assignment-detection like wheel, so local close may be required. Decision deferred to Phase 3.

**New open questions from Phase 2:**

- **Q7 (`/close-all` future):** Should this endpoint be removed entirely, or rewritten to submit IBKR market orders and defer DB update to trade_sync? It exists in the UI and the user has presumably pressed it; removing it would be a UI change. Recommend: rewrite to submit orders + remove the immediate DB write. Phase 4 commit.

- **Q8 (`/force-close` status update):** Defer to trade_sync after IBKR confirms fill, or keep current "set CLOSED on order submit"? Recommend: defer to trade_sync. The endpoint returns immediately ("close order sent"); the dashboard sees status change a few seconds later when trade_sync runs. UX is unchanged. Phase 4 commit.

- **Q10 (orphan-cleanup vestiges):** Three places in `jobs.py` have logic to expire SUBMITTED suggestions/trades that never filled. After Pattern A is eliminated, this cleanup logic still has a purpose (orders can still die at IBKR for legitimate reasons — limit price too far from market, account flagged, etc.) but the cleanup window can probably be tightened. Phase 5 cleanup item.

**New from Phase 2b:**

- **Q11 (IPO flip Position visibility):** During the IPO flip holding period (1-5 days), the IPO stock has no `Position` row in the DB; trade_sync creates one as a generic `stock` Position. After the refactor we need to decide: does `position_type="ipo_flip"` survive the refactor (and how does trade_sync know to use that type), or do IPO positions become generic `stock` positions with a separate `IpoWatchlist` row carrying the IPO-specific metadata? Recommend the latter — `IpoWatchlist` already tracks every IPO-specific field; the `Position` table doesn't need a special type.

- **Q12 (IPO lockup PortfolioTransaction dedup):** Will `sync.sync_ibkr_holdings`'s 3-day-dedup window correctly skip creating a duplicate `buy` transaction when ipo/trader.py wrote one with `signal="ipo_lockup_reentry"`? The dedup at line 110-117 (post-fb3ef28) filters by `symbol` and `action.in_(["put_assigned", "buy"])` — it does NOT filter by signal. So in theory yes, the dedup catches it. But this means the IPO-lockup transaction's `signal` field information is "preserved by accident" — the buyer.py-written row is the one that survives, with the IPO-lockup signal; trade_sync would have written its own row with no IPO context. Verify on next Winston lockup re-entry.

---

## Architectural Pattern Summary

**Same architectural failure on both sides, plus a third subsystem.**

Maggy side has writers to `Position`:
- `wheel._handle_assignment` (Pattern A)
- `put_seller._record_trade` (Pattern A)
- `hedge._buy_hedge` (Pattern A)
- `ipo/trader.py:_record_flip_exit` (Pattern A — DB-after-exit, duplicates with trade_sync)
- `controls.py` routes (mixed — `/close-all` is the worst)
- `broker/trade_sync.sync_ibkr_positions` (Pattern B ✓)

Winston side has writers to `PortfolioHolding`:
- `buyer._handle_put_assignment` (Pattern A)
- `buyer._update_holding` from `_execute_buy` (Pattern A)
- `scheduler.job_portfolio_sync_trades` `put_assigned` block (Pattern A)
- `ipo/trader.py:_check_lockup_fill` (Pattern A — lockup re-entry)
- `sync.sync_ibkr_holdings` (Pattern B ✓)

The fix is the same across all of it:

- **Sole Position writer:** `broker/trade_sync.py:sync_ibkr_positions` on Maggy side; `portfolio/sync.py:sync_ibkr_holdings` on Winston side. Both already exist and already do the right thing. The refactor's job is to stop the OTHER writers from writing.
- **Strategy/buyer/IPO files** only write intent: SUBMITTED Trade rows (Maggy) or SUBMITTED PortfolioPutEntry/PortfolioTransaction rows (Winston). IpoWatchlist state transitions stay in `_record_flip_exit` and `_check_lockup_fill`; only the Position/Holding creation moves out.
- **Force-close routes** submit IBKR orders and defer state update to trade_sync.
- **Idempotency-via-IBKR**: when trade_sync sees the IBKR fill, it transitions SUBMITTED → FILLED and creates/updates the Position or Holding row. Single writer, single source of truth.

---

## Phased Execution Plan — Updated for 11 Commits

This supersedes Phase 1's plan with Winston-side awareness and Phase 2b IPO Rider awareness.

### Phase 3 — Write target architecture spec

Read complete. Phase 3 is now the next significant work. Spec needs to cover:
- All 11 refactor commit targets with before/after code sketches
- The five "Q" decisions remaining (Q2, Q7, Q8, Q11, Q12)
- The interaction between `IpoWatchlist` lifecycle states and the new Pattern B reality

### Phase 4 — Execute, one writer at a time

11 commits in suggested order, lowest-risk first:

1. **`hedge._buy_hedge` Position no-write.** Hedge is the smallest piece of the system and easy to verify in isolation. Hedge fires once daily; failure mode is "hedge doesn't get placed today" which is observable and recoverable.
2. **`/force-close` deferral.** Single dashboard endpoint; defer status update to trade_sync. Low blast radius.
3. **`/close-all` rewrite (or removal).** Either remove or rewrite to submit orders. Decision required first.
4. **`wheel._handle_assignment` no stock-Position write.** Replaces today's idempotency band-aid (`d9550e6`) with the architectural fix. Riskiest because assignment is a real production event during market hours.
5. **`wheel._handle_called_away` similar treatment** (covered call assignment).
6. **`put_seller._record_trade` no Position write.** Riskiest of the strategy refactors because put-selling is the main entry path. Test in suggestion mode for at least a week before any live mode change.
7. **`ipo/trader.py:_record_flip_exit` no Position write.** Keep the IpoWatchlist state transition + the two Trade rows, drop the Position creation. trade_sync handles position creation on the buy fill; on exit trade_sync handles the close.
8. **Winston: `scheduler.job_portfolio_sync_trades` remove the `put_assigned` block.** Defer to `sync.sync_ibkr_holdings`. Removes one of the four Winston-side writers.
9. **Winston: `buyer._handle_put_assignment` no PortfolioHolding write.** Mark put-entry assigned, write transaction with status=submitted, let `sync.sync_ibkr_holdings` materialize the holding from IBKR truth.
10. **Winston: `buyer._execute_buy` and `buyer._execute_put_entry` write intent only.** Equivalent to put_seller refactor on the Maggy side.
11. **Winston: `ipo/trader.py:_check_lockup_fill` no PortfolioHolding write.** Keep the IpoWatchlist state transition + the SUBMITTED PortfolioTransaction, drop the PortfolioHolding creation.

Each commit:
- Independently testable, clear revert path.
- `~/restart-all.sh` deployment with at least 48 hours of observation before next commit.
- Log entries verified for the expected absence of Pattern A writes ("position_created_by_wheel" should disappear after step 4, etc.).

### Phase 5 — Document and consolidate

Update STATE.md with the new invariants:

- "Only `broker/trade_sync.py` writes Position rows on the Maggy side"
- "Only `portfolio/sync.py:sync_ibkr_holdings` writes PortfolioHolding rows on the Winston side"
- "Strategy, buyer, and IPO files write SUBMITTED Trade and SUBMITTED PortfolioTransaction rows; sync functions transition status to FILLED"
- "Dashboard reads from Position/Holding rows; SUBMITTED rows alone do not imply an OPEN position"
- "Force-close routes submit IBKR orders and defer status update"
- "IpoWatchlist tracks IPO-specific lifecycle; Position/PortfolioHolding rows for IPO trades are generic stock entries created by sync"

Add to RULES.md as inviolable rules so future patches can't accidentally regress.

Optionally simplify the orphan-cleanup logic in `jobs.py` once Pattern A writers are gone.

---

## Risk Assessment — Updated

The refactor remains lower-risk than it appears because:

1. The system is in suggestion mode with auto-approve OFF. Pattern A's eager DB writes only fire in live mode. Currently the bug surface is dormant on the Maggy side.

2. Winston is mostly live-mode (put-entries are real orders), but Winston's Pattern A bug is well-defended by the IBM-style transaction dedup (`fb3ef28`) and the limited number of assignment events. The duplicate-PortfolioHolding bug has been theoretical for months without obvious failure (or has produced quiet duplicates the user hasn't noticed — worth checking on next session).

3. Each phase is a single-file change with a clear before/after.

4. The defensive idempotency patch shipped today (`d9550e6`) protects Maggy-side stock-Position duplicates during the refactor window. Winston has no equivalent guard yet — recommended to ship a similar idempotency patch on `buyer._handle_put_assignment` before the next Winston assignment event. **This is the most important short-term action item from Phase 2.**

5. The refactor reuses code that already works: `sync_ibkr_positions` (Maggy) and `sync_ibkr_holdings` (Winston) are battle-tested. The refactor doesn't introduce new code; it just removes the duplicate code paths.

6. IPO Rider is currently inactive (no IPOs in flight). Phase 2b refactor commits (7, 11) can ship anytime without affecting live trading. Lowest blast radius of the eleven.

The refactor remains higher-cost than it appears because:

1. Phase 2b added the IPO Rider dimension (Maggy ipo_flip Position + Winston lockup PortfolioHolding). Two more commits, two more open questions (Q11, Q12).

2. Phase 4 commit ordering matters — getting the order wrong (e.g., refactoring `wheel._handle_assignment` before `put_seller._record_trade`) could create a window where Position state is inconsistent. The spec must call out the right sequence.

3. The IPO flip "position_type" decision (Q11) ripples through the dashboard read paths — if `position_type="ipo_flip"` goes away, any dashboard query filtering by that type also needs updating. Phase 3 spec must trace through.

---

## What Was NOT Decided in Phase 2 / 2b

- Whether the Maggy-side idempotency patch (`d9550e6`) should have a Winston counterpart shipped immediately. Recommendation: yes, before the next put assignment on Winston. ~30 minutes of work.
- Whether `/close-all` should be removed or rewritten. Discussed but not decided.
- Whether `/force-close` defers status to trade_sync. Discussed but not decided.
- Whether `position_type="ipo_flip"` survives the refactor or becomes a generic `stock` position with `IpoWatchlist` carrying the metadata. (Q11.)
- Whether the IBM-style PortfolioTransaction dedup correctly handles the IPO lockup case. (Q12.)
- Phase 3 timing. Phases 2 + 2b took longer than expected; Phase 3 should be its own focused session.

---

## Immediate Short-Term Action Item

**Ship a Winston-side idempotency guard on `buyer._handle_put_assignment` analogous to `d9550e6` for `wheel._handle_assignment`.**

The Maggy side now has a defensive guard preventing duplicate stock Position rows on race conditions. The Winston side has the same vulnerability with no guard. The next time a Winston put gets assigned and `_check_put_entries` fires concurrently with `job_portfolio_sync_trades`, duplicate PortfolioHolding rows are possible.

This is a separate, ~30-minute commit before any larger refactor work. Recommend doing it in the same session as Phase 3 spec writing.

---

*End of Phase 2 + 2b map. Architecture audit complete. Phase 3 (target architecture spec) is the next significant work; 11 refactor commits to plan and execute over Phase 4.*
