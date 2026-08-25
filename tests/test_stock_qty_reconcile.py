"""IBKR is source of truth for a still-held stock lot's share count (2026-08-25 CTAS).

position_sync only ever closed a stock lot on a FULL exit (shares gone from IBKR). A
PARTIAL reduction — a partial call-away of a blended multi-CC lot, where IBKR dropped
from 400 to 200 while the DB row stayed 400 — fell through the presence check and lingered
inflated for days, feeding the exit path a phantom uncovered lot. _reconcile_stock_qty
makes the DB follow IBKR DOWN while the name is still held. It deliberately never revises
UP: extra shares at IBKR are an unbooked assignment for the delivery/self-heal path (which
sets the added shares' real cost basis), not a blind quantity bump.
"""
from src.broker.trade_sync import _reconcile_stock_qty


def test_partial_reduction_reconciles_down():
    # CTAS: DB thinks 400, IBKR holds 200 after a partial call-away → DB must become 200.
    assert _reconcile_stock_qty(400, 200) == 200


def test_equal_qty_unchanged():
    assert _reconcile_stock_qty(200, 200) == 200


def test_ibkr_zero_left_to_close_path():
    # 0 on IBKR means the lot is GONE — the caller's close/realized-P&L path handles that,
    # not a quantity edit, so leave the DB value for that branch to act on.
    assert _reconcile_stock_qty(400, 0) == 400


def test_ibkr_holds_more_does_NOT_reconcile_up():
    # Down-only: extra IBKR shares are an unbooked assignment (delivery/self-heal books them
    # with a real cost basis). A blind bump here would leave that basis wrong — never do it.
    assert _reconcile_stock_qty(200, 400) == 200


def test_negative_ibkr_ignored():
    # Defensive: a short/garbage read must never flip a long lot negative.
    assert _reconcile_stock_qty(200, -100) == 200
