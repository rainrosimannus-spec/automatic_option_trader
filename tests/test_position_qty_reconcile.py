"""IBKR is source of truth for a still-open option's contract count (2026-07-30 LRCX).

position_sync only ever detected FULL closes (option gone from IBKR); a PARTIAL close —
buying back 1 of 2 covered calls, leaving IBKR at 1 while the DB still read 2 — fell
through the presence check and stuck open forever. _reconcile_option_qty makes the DB
follow IBKR's contract count whenever the option is still held.
"""
from src.broker.trade_sync import _reconcile_option_qty


def test_partial_close_reconciles_down():
    # LRCX: DB thinks 2, IBKR holds 1 after a manual buy-back → DB must become 1.
    assert _reconcile_option_qty(2, 1) == 1


def test_equal_qty_unchanged():
    assert _reconcile_option_qty(1, 1) == 1


def test_ibkr_zero_left_to_close_path():
    # 0 on IBKR means the option is GONE — the caller's close/expire path handles that,
    # not a quantity edit, so leave the DB value for that branch to act on.
    assert _reconcile_option_qty(2, 0) == 2


def test_ibkr_holds_more_reconciles_up():
    # IBKR is truth in both directions: if it somehow holds more than the DB recorded, follow it.
    assert _reconcile_option_qty(1, 3) == 3
