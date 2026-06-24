"""
SKXHoldco IBKR → Standard Books (by Excellent) bookkeeping bridge.

End-of-day, cost-basis double-entry posting. The pipeline is:

    flex_extract  →  journal  →  standard_books
    (IBKR Flex)      (translate)   (POST TRBlock, or dry-run)

Driven once per day by `daily_sync.run_daily_sync()`. Defaults to DRY-RUN:
it prints the exact journals it WOULD post (balanced, with idempotency keys)
without touching Standard Books, so the accounting can be reviewed before any
live REST credentials are wired in.

Scope (per the agreed design): trades + commissions, dividends + interest,
cash transfers + FX. No mark-to-market (cost basis only).
"""
from __future__ import annotations

__all__ = ["run_daily_sync", "run_all_entities"]


def __getattr__(name: str):
    # Lazy re-export so `python -m src.bookkeeping.daily_sync` doesn't double-import.
    if name in ("run_daily_sync", "run_all_entities"):
        from src.bookkeeping import daily_sync
        return getattr(daily_sync, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
