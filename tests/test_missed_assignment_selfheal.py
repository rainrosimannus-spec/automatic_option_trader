"""Self-heal for MISSED wheel assignments (2026-07-27).

An ITM short put at expiry whose stock-delivery fill never syncs gets mis-booked as
worthless EXPIRED/CLOSED, so its assigned shares live on IBKR but vanish from the
dashboard (CCJ 90-put, ANET 175-put). position_sync recovers them by matching a
recently-closed put whose contract count EXACTLY equals the held shares. The match
must be exact so a symbol with several closed lots (some genuinely worthless) is never
over-booked — the whole reason the guard is safe.
"""
from types import SimpleNamespace

from src.broker.trade_sync import _missed_assignment_put


def _put(qty):
    return SimpleNamespace(quantity=qty)


def test_exact_match_returns_put():
    # ANET: IBKR holds 200, the 175-put was 2 contracts → exact match.
    p = _put(2)
    assert _missed_assignment_put([p], 200) is p


def test_no_match_when_qty_differs():
    # 170-put (3 contracts = 300) must NOT be matched to a 200-share holding.
    assert _missed_assignment_put([_put(3)], 200) is None


def test_picks_the_exact_lot_among_several():
    # Both the worthless 170-put (300) and the assigned 175-put (200) are recently closed;
    # for a 200-share holding only the 2-contract lot may be booked.
    p170, p175 = _put(3), _put(2)
    assert _missed_assignment_put([p170, p175], 200) is p175


def test_no_match_returns_none_for_ambiguous_blend():
    # 300 held but only lots of 200 and 500 exist → no exact match → surface for review.
    assert _missed_assignment_put([_put(2), _put(5)], 300) is None


def test_empty_puts_returns_none():
    assert _missed_assignment_put([], 200) is None


def test_zero_or_missing_qty_never_matches_zero_shares():
    # A None/0-contract put must not spuriously match a 0 holding (loop is only entered for qty>0 anyway).
    assert _missed_assignment_put([_put(None)], 200) is None
