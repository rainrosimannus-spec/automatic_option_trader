# Source-of-Truth Refactor — Phase 1 Map

**Status:** Phase 1 complete (Maggy-side write paths read end-to-end).
**Next:** Phase 2 reads (dashboard, jobs, risk, Winston-side), then target architecture write-up, then phased execution.

**Date:** 2026-05-17.
**Files read in Phase 1:** `src/broker/trade_sync.py`, `src/strategy/wheel.py`, `src/strategy/put_seller.py`, `src/strategy/profit_taker.py`, `src/strategy/hedge.py`.

---

## The Principle

> IBKR is the source of truth. The database is a derived view that reflects IBKR.
> Each concept (Position, Trade, balance) should have exactly one writer; everything else reads.

This refactor exists because today's audit revealed the trading system has multiple files writing to the `Position` table independently, creating race conditions and duplicate-row symptoms (e.g., "3 NVDA assignments where there should be 2"). The IBM duplicate-row bug shipped this morning (`fb3ef28`) was the same pattern on the `portfolio_transactions` table.

The defensive idempotency patch shipped today (`d9550e6` — wheel.py guard against duplicate stock Position creation) closes the immediate symptom but does NOT fix the underlying architectural debt. This document captures the proper fix.

---

## Phase 1 Map — Maggy-side Position Writers

### Writers to the `Position` table

| File | Function | What it writes | When | Pattern |
|---|---|---|---|---|
| `put_seller.py` | `_record_trade` | `short_put` OPEN | Live-mode put sell, after order submission | **DB-first** |
| `wheel.py` | `_handle_assignment` | `stock` OPEN | Put assignment detected | IBKR-confirmed (stock already in account) |
| `wheel.py` | `_handle_called_away` | `stock` CLOSED | Call assignment detected | IBKR-confirmed (stock already removed) |
| `hedge.py` | `_buy_hedge` | `hedge_put` OPEN | Hedge buy, after order submission | **DB-first** |
| `hedge.py` | `_roll_hedge` | `hedge_put` CLOSED | Hedge roll | Local-only close |
| `broker/trade_sync.py` | `sync_ibkr_positions` | Any type | Periodic sync from IBKR portfolio | **IBKR-first** ✓ |
| `controls.py` (force-close) | Force-close handler | Status change | Manual user action | User-driven |

**Notable absences:**
- `profit_taker.py` does NOT write to `Position` table — never. All state changes deferred to trade_sync after IBKR fill confirmation. **This is the clean reference pattern.**
- `wheel.py` does not create a Position for covered calls — only for stock-from-assignment. Covered call positions are created by trade_sync from IBKR fills.

### Writers to the `Trade` table

Every file writes Trade rows in its own way:

- `put_seller._record_trade` → SELL_PUT with SUBMITTED status
- `wheel._handle_assignment` → ASSIGNMENT (FILLED) trade
- `wheel._handle_called_away` → CALLED_AWAY (FILLED) trade
- `wheel._write_call` → SELL_CALL with SUBMITTED status (covered call sold)
- `profit_taker._close_position` → BUY_PUT (close) with SUBMITTED status
- `profit_taker._close_covered_call` → BUY_CALL (close) with SUBMITTED status
- `profit_taker._roll_call_up` → SELL_CALL with SUBMITTED status (new CC after roll)
- `hedge._buy_hedge` → BUY_PUT (hedge) with SUBMITTED status
- `broker/trade_sync.sync_ibkr_trades` → Updates SUBMITTED → FILLED on fill, or creates FILLED rows for manual trades
- `broker/trade_sync.sync_ibkr_positions` → Creates "Synced from IBKR position" trades for positions found in IBKR without matching Trade rows

This is less of a problem than Position duplication because Trade rows are append-only (`ibkr_exec_id` provides idempotency). Trade insert/update logic is already centralized in trade_sync.

---

## The Two Patterns Mixed in the Codebase

### Pattern A — DB-first (problematic)

```python
# put_seller, wheel (assignment-stock), hedge
trade = ibkr.place_order(...)  # Returns immediately
db.add(Position(status=OPEN, ...))  # Written before IBKR confirms fill
db.add(Trade(order_status=SUBMITTED, ...))
```

**Consequences:**
- Dashboard claims a position exists before it actually does
- If IBKR rejects the order, phantom rows persist until next reconciliation
- Race conditions: if `_handle_assignment` runs twice (multiple scheduler invocations, restart during commit), duplicate Position rows are created
- Pre-rebrand the wheel rescue-mode revert dance was a symptom of this — patches addressing position state from multiple angles, none authoritative

### Pattern B — IBKR-first (clean)

```python
# profit_taker
trade = ibkr.place_order(...)
db.add(Trade(order_status=SUBMITTED, ...))  # Only describes intent
# Position table untouched
# trade_sync sees the fill later, updates Position
```

**Why this is right:**
- Position rows exist if-and-only-if IBKR confirms the position
- Phantom rows impossible by construction
- Single Position writer means single source of duplication risk (which is already idempotent via key matching in trade_sync)
- Rejected orders leave a SUBMITTED Trade row that gets reconciled to CANCELLED after the grace period — no Position to clean up

---

## Target Architecture

### Position writes

**Only `broker/trade_sync.py` creates Position rows.**

After the refactor:
- `put_seller._record_trade` writes only the SUBMITTED Trade row. No Position.
- `wheel._handle_assignment` marks the put ASSIGNED, creates the ASSIGNMENT Trade row. No stock Position. trade_sync sees IBKR's new stock and creates the row on its next cycle.
- `wheel._handle_called_away` marks the call ASSIGNED, creates the CALLED_AWAY Trade row. No stock-close. trade_sync sees IBKR's stock disappearing and closes the row.
- `hedge._buy_hedge` writes only the SUBMITTED Trade row. No Position.
- `hedge._roll_hedge` marks the old hedge Position CLOSED (or defers that too to trade_sync — open question, see below).
- `broker/trade_sync.sync_ibkr_positions` continues to be the sole Position writer, now also for stock-from-assignment.

### Trade writes

Trades remain distributed but with a clear contract:

- **Strategy files write SUBMITTED Trade rows** describing intent (sell put, buy back put, sell call, etc.)
- **trade_sync.py is the sole writer of state transitions on Trade rows** (SUBMITTED → FILLED, SUBMITTED → CANCELLED)
- **Trade rows are append-only by `ibkr_exec_id`** — no Trade row is ever deleted or rewritten

### Dashboard reads

After Phase 2 (next session) we will confirm: dashboard reads should only consult Position rows that have status=OPEN, with no assumption that a SUBMITTED Trade implies an OPEN Position. Right now there might be places that conflate the two; that's what Phase 2 needs to map.

---

## Open Questions for Next Session

1. **Force-close path (`controls.py`):** does the manual force-close button write to Position directly, or does it submit an IBKR order and let trade_sync update Position? Need to read.

2. **Hedge close on roll:** should `_roll_hedge` continue to mark Position CLOSED locally, or defer to trade_sync? Hedge positions don't have natural assignment-detection like wheel, so local close may be required. Decision deferred.

3. **`broker/trade_sync.sync_ibkr_positions` Trade-creation logic** (line 631+): when it sees an option in IBKR that has no matching Position, it creates BOTH a Position AND a Trade. After the refactor this remains, but we need to verify the existing logic correctly handles the case where put_seller's SUBMITTED Trade row exists from an earlier submission — does trade_sync update the SUBMITTED Trade to FILLED, or create a duplicate? The `existing_trade` SUBMITTED→FILLED logic at line 164 in `sync_ibkr_trades` handles fills explicitly, but `sync_ibkr_positions` at line 656 only checks for any existing Trade without checking status.

4. **`PortfolioPutEntry` (Winston-side puts):** the Winston system has its own put-sell flow via `src/portfolio/buyer.py:_handle_put_assignment`. This was not read in Phase 1. Whether the same pattern problems exist there is a Phase 2 question.

5. **`profit_taker._roll_call_up`:** writes a SELL_CALL Trade with SUBMITTED status but does NOT create a covered_call Position. Already follows Pattern B. Confirmed clean.

6. **Manual sync button from dashboard:** the "Sync from IBKR" button on the trades page calls `sync_ibkr_trades`. After the refactor, this becomes more important — it's the user-facing way to reconcile state.

---

## Phased Execution Plan (subsequent sessions)

### Phase 2 — Complete the read

Read Winston-side and reader paths. Confirm or refute the open questions above. Files to read:

- `src/portfolio/buyer.py`
- `src/portfolio/scheduler.py`
- `src/portfolio/sync.py`
- `src/scheduler/jobs.py` (focused on Position/Trade interactions)
- `src/web/routes/controls.py` (force-close path)
- `src/web/routes/api.py` (Position/Trade reads)
- `src/consigliere/advisor.py`

### Phase 3 — Write target architecture spec

After Phase 2, write a definitive spec: who writes what, who reads what, what changes per file. Reviewable as one document before any code change.

### Phase 4 — Execute, one writer at a time

Convert each Pattern A site to Pattern B in its own commit:

1. **First commit: `put_seller._record_trade` no longer creates Position.** Riskiest because it's the main entry path. Ship in dry-run / suggestion mode first, observe for a week before any live mode change.

2. **Second commit: `wheel._handle_assignment` no longer creates stock Position.** Move the responsibility to trade_sync. This is the duplicate-position bug fix at the architectural level — replaces today's idempotency patch.

3. **Third commit: `wheel._handle_called_away` similar treatment** if the audit confirms it's a Pattern A site.

4. **Fourth commit: `hedge._buy_hedge` no longer creates Position.** Lowest risk (hedge is a small piece of the system, easy to verify in isolation).

5. **Fifth commit: cleanup** — remove orphaned safety-net code in trade_sync that exists only to compensate for Pattern A writers (e.g., the IBM duplicate-buy-row check from this morning's `fb3ef28` may no longer be needed after the refactor).

Each commit should:
- Be independently testable
- Have a clear revert path
- Be deployed via `~/restart-all.sh` and observed for at least 48 hours before the next commit
- Have unit tests where feasible (currently the codebase has no test suite — adding one is a separate piece of work, not blocking this refactor)

### Phase 5 — Document and consolidate

Update STATE.md with the new invariants:

- "Only `broker/trade_sync.py` writes Position rows"
- "Strategy files write SUBMITTED Trade rows; trade_sync transitions status"
- "Dashboard reads from Position rows with status=OPEN; SUBMITTED Trade rows alone do not imply an OPEN Position"

Lock these in via the working rules so future patches can't accidentally re-introduce Pattern A.

---

## Risk Assessment

The refactor itself is **lower-risk than it appears**, because:

1. The current system is in suggestion mode with auto-approve OFF. Pattern A's eager DB writes are mostly dormant — `_record_trade` only fires in live mode. The bug surface is real but currently quiescent.

2. Each phase is a single-file change with a clear before/after. No "big bang" rewrite.

3. The defensive idempotency patch shipped today (`d9550e6`) protects against the duplicate-Position symptom even during the refactor window.

4. trade_sync.py is already well-instrumented and tested in production — adding more Position-creation responsibility to it is incremental, not novel.

The refactor is **higher-cost than it appears** because:

1. Reading and understanding each file's full contract takes 30-45 minutes per file. The 14 files identified in the audit list represent 7-10 hours of careful reading before any code change.

2. Each commit needs observation time on a running system before the next. Calendar time will be 2-4 weeks, even if active work is just a few sessions.

3. Some "improvements" we'll want to make along the way (a real test suite, a proper migration system for legacy DB rows) are tangentially related and easy to scope-creep into.

The right discipline is: complete Phase 2 first (the rest of the read), then write the actual spec, then resist the temptation to do anything before that spec is reviewed and approved.

---

## What Was NOT Decided Today

- Whether the Winston-side flow (`PortfolioPutEntry`, `portfolio_capital_injections`, etc.) needs the same refactor. This depends on Phase 2 findings.
- Whether to add a test suite as part of this work or as a separate effort.
- Whether to migrate existing data (e.g., reconcile the 6 historical positions with `realized_pnl=0` from the merge period) as part of this work or leave that for post-re-split audit per current plan.
- Timeline. Today was scoping only. Actual execution depends on Phase 2 outcomes and available session time.

---

## Today's Shipped Defensive Patches (related context)

These ship before the refactor lands; they close immediate symptoms and stay in place during the multi-session refactor work:

- `fb3ef28` — sync.py IBM duplicate-row dedup (`put_assigned` + `buy`)
- `3c0bf55` — wheel.py rescue mode hoisted out of exit_mode branch
- `cac0d2d` — Claude model migration (sonnet-4-20250514 → sonnet-4-6) before June 15 deadline
- `f70381a` `4364047` `efcc298` `70c5656` — Bridge v2 (4 files, stays inert until enabled=False is flipped post-re-split)
- `ccd3677` — Orphaned `src/scheduler/trade_sync.py` deleted (verified zero imports across repo, scripts, crontab, systemd)
- `d9550e6` — wheel.py `_handle_assignment` idempotency guard (defensive against the duplicate-stock-Position symptom while the architectural refactor is in flight)

All require `~/restart-all.sh` to activate in the running process.

---

*End of Phase 1 map. Ready for Phase 2 in next session.*
