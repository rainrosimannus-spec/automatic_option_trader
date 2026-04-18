# Phase 14 — Options Trade Sync Reconciliation

**Scope:** options-side only (`src/broker/trade_sync.py` + `src/scheduler/jobs.py`)
**Out of scope:** portfolio-side trade_sync (separate bug, separate session)
**Observed bug:** Friday Apr 17. PG fired 15 buy orders that got rejected at IBKR; SHOP had phantom $149 roll SELL rows. All left `Trade.order_status = SUBMITTED` in DB forever because the existing sync only processes FILLS, never the death of orders.

---

## What we're adding

A new function `reconcile_submitted_trades()` in `src/broker/trade_sync.py`. Runs inside the existing 15-min `_job_trade_sync`. Finds Trade rows stuck in SUBMITTED where the corresponding IBKR order no longer exists, marks them CANCELLED with a reason note.

No new scheduled job. No new config values (thresholds hardcoded with clear comments — tune later if needed).

---

## Design decisions locked

1. **Freshness grace period: 10 minutes.** Only reconcile SUBMITTED rows older than 10 min. Newer rows might not yet appear in IBKR's open orders list due to timing. Avoids false-cancelling legitimate live orders.

2. **"No longer at IBKR" = not in `ib.openTrades()`.** If the order's `orderId` isn't in the set of current live orders, it's either been filled (in which case sync_ibkr_trades would have already recorded a fill) or it's dead. Either way, the SUBMITTED row is stale.

3. **Match by `Trade.order_id` ↔ `openTrades()[i].order.orderId`.** Direct integer comparison.

4. **Write back to DB: set `order_status = CANCELLED`, append to `notes`.** Don't delete the row — preserves history. The append preserves whatever notes were already there.

5. **Lock: reuse `get_ib_lock()`.** IB API call needs it; the RLock is reentrant so no deadlock with nested calls.

6. **Runs alongside existing sync, not as separate job.** Piggyback inside `_job_trade_sync` after the existing two sync calls. One log line, one try/except path.

---

## Read-before-writing anchors (confirmed 2026-04-18)

| File | Line | What |
|---|---|---|
| `src/broker/trade_sync.py` | 48 | `def sync_ibkr_trades() -> int:` |
| `src/broker/trade_sync.py` | 401 | `def sync_ibkr_positions() -> int:` |
| `src/scheduler/jobs.py` | 1617 | `def _job_trade_sync():` |
| `src/scheduler/jobs.py` | 1160 | scheduler.add_job for `_job_trade_sync`, 15 min interval |

---

## Exact patch — to run tomorrow

### Piece 1 — `src/broker/trade_sync.py`: add reconcile function

**Anchor** — the line containing `def sync_ibkr_positions() -> int:` is unique. We insert the new function immediately before it.

**New function body:**

```python
def reconcile_submitted_trades(grace_minutes: int = 10) -> int:
    """
    Find Trade rows stuck in SUBMITTED status whose IBKR order no longer exists.
    Mark them CANCELLED with a reason note. Skip rows newer than grace_minutes
    to avoid false-cancelling legitimately-live new orders.

    Returns number of rows reconciled.
    """
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(minutes=grace_minutes)

    # Snapshot live IBKR order IDs (inside lock)
    try:
        with get_ib_lock():
            ib = get_ib()
            live_order_ids = {t.order.orderId for t in ib.openTrades()}
    except Exception as e:
        log.warning("reconcile_submitted_trades_skipped_ib_error", error=str(e))
        return 0

    reconciled = 0
    with get_db() as db:
        stuck = db.query(Trade).filter(
            Trade.order_status == OrderStatus.SUBMITTED,
            Trade.order_id.isnot(None),
            Trade.created_at < cutoff,
        ).all()

        for t in stuck:
            if t.order_id in live_order_ids:
                continue  # Still live at IBKR, leave alone
            # Phantom row — IBKR has no record of this order anymore
            t.order_status = OrderStatus.CANCELLED
            note_addition = "Reconciled: order no longer at IBKR"
            t.notes = (t.notes + " | " + note_addition) if t.notes else note_addition
            reconciled += 1
            log.info("phantom_trade_reconciled",
                     trade_id=t.id,
                     symbol=t.symbol,
                     order_id=t.order_id,
                     trade_type=str(t.trade_type))

        if reconciled:
            db.commit()

    return reconciled
```

**Check imports at top of trade_sync.py.** Needs `Trade`, `OrderStatus`, `get_db`, `get_ib`, `get_ib_lock`, `log`. All already imported by the existing functions in this file — verify first, but no new imports expected.

### Piece 2 — `src/scheduler/jobs.py`: call from `_job_trade_sync`

**Anchor:**

```python
        from src.broker.trade_sync import sync_ibkr_trades, sync_ibkr_positions
        imported = sync_ibkr_trades()
        if imported:
            log.info("trade_sync_job_done", imported=imported)
        # Also sync positions
        pos_changes = sync_ibkr_positions()
        if pos_changes:
            log.info("position_sync_job_done", changes=pos_changes)
    except Exception as e:
        log.error("trade_sync_job_error", error=str(e))
```

**Replace with:**

```python
        from src.broker.trade_sync import (
            sync_ibkr_trades, sync_ibkr_positions, reconcile_submitted_trades
        )
        imported = sync_ibkr_trades()
        if imported:
            log.info("trade_sync_job_done", imported=imported)
        # Also sync positions
        pos_changes = sync_ibkr_positions()
        if pos_changes:
            log.info("position_sync_job_done", changes=pos_changes)
        # Reconcile phantom SUBMITTED rows (orders that died at IBKR without a fill)
        reconciled = reconcile_submitted_trades()
        if reconciled:
            log.info("trade_sync_reconciled_phantoms", count=reconciled)
    except Exception as e:
        log.error("trade_sync_job_error", error=str(e))
```

---

## Verification plan after commit

1. Syntax check both files.
2. Dry-run: before restart, check current DB state.
   ```bash
   .venv/bin/python3 -c "
   import sqlite3
   conn = sqlite3.connect('data/trades.db')
   stuck = conn.execute(\"SELECT id, symbol, trade_type, order_id, created_at FROM trades WHERE order_status='SUBMITTED' AND order_id IS NOT NULL\").fetchall()
   print(f'Currently {len(stuck)} SUBMITTED rows with order_id')
   for r in stuck[:10]:
       print(r)
   "
   ```
3. Restart. Next `_job_trade_sync` cycle (up to 15 min) should log `trade_sync_reconciled_phantoms` if any phantoms existed, then quietly do nothing on subsequent runs.
4. Watch tmux buffer for 30-45 min to confirm no false-cancels on live new orders. If `phantom_trade_reconciled` ever logs for a trade that's actually still live at IBKR, the grace period was too short and needs tuning.

---

## Edge cases handled

- **IBKR connection down:** the `try/except` around `ib.openTrades()` returns 0 without touching DB. Silent failure, logged as warning. Safe.
- **Very old orders still live at IBKR:** if an order has been SUBMITTED for 2 days but IBKR still shows it in `openTrades()`, we leave it alone. Not our problem to decide.
- **Order that JUST died seconds ago:** the `created_at < cutoff` filter catches this; we wait 10 min before touching.
- **Order filled between our `openTrades` snapshot and our DB update:** the existing `sync_ibkr_trades()` ran first in the same job and would have already marked it FILLED. If there's still a race, we mark SUBMITTED → CANCELLED incorrectly on a filled order. Very narrow race window (seconds). Cost: one trade shows status=CANCELLED despite filling, but the underlying position and realized_pnl are correct from other code paths. Low-impact.

---

## Not included on purpose

- **No new config values.** 10-minute grace is hardcoded with comment. Tunable via argument if ever needed; adding config is overkill for a single-use threshold.
- **No reconciliation for non-options tables.** Portfolio side uses separate `job_portfolio_sync_trades`. Out of scope per user directive.
- **No dashboard UI for this.** Runs silently. User only sees the effect (no more phantom rows).
- **No backfill of existing stuck rows.** Natural self-cleanup on first run. No migration needed.

---

## Expected commit message

```
trade_sync: reconcile phantom SUBMITTED rows — kill DB rows whose IBKR order died without a fill
```