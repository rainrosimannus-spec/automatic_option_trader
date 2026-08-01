"""Screener drop-out freeze+buffer selection (2026-08-01).

When the monthly screener drops a HELD name it stays in the watchlist (flagged
pending_removal, or never-screened category 'existing_holding'), but it should stop
drawing new capital — its target is frozen at invested and redistributed to members.
The buffer/hysteresis keeps buying a drop-out that still ranks within top members×mult,
so a one-month wobble at the cut line doesn't strand a good name. _frozen_dropouts picks
exactly the names to freeze; target pinning + redistribution happen in the caller.
"""
from types import SimpleNamespace

from src.portfolio.buyer import _frozen_dropouts


def _w(symbol, pending=False, category="growth"):
    return SimpleNamespace(symbol=symbol, pending_removal=pending, category=category)


def test_dropout_outside_buffer_is_frozen():
    # 4 members + 1 pending-removal drop-out ranked well below the band (rank 20).
    watch = [_w("A"), _w("B"), _w("C"), _w("D"), _w("OLD", pending=True)]
    rank_idx = {"A": 1, "B": 2, "C": 3, "D": 4, "OLD": 20}
    assert _frozen_dropouts(watch, rank_idx, 1.2) == {"OLD"}


def test_dropout_inside_buffer_not_frozen():
    # member_count=4, cutoff=4*1.2=4.8; OLD ranked 4 (<=4.8) still accumulates (hysteresis).
    watch = [_w("A"), _w("B"), _w("C"), _w("D"), _w("OLD", pending=True)]
    rank_idx = {"A": 1, "B": 2, "C": 3, "OLD": 4, "D": 5}
    assert _frozen_dropouts(watch, rank_idx, 1.2) == set()


def test_existing_holding_never_screened_is_legacy():
    # A never-screened auto-added holding (category 'existing_holding') is a drop-out too.
    watch = [_w("A"), _w("B"), _w("C"), _w("D"), _w("HELD", category="existing_holding")]
    rank_idx = {"A": 1, "B": 2, "C": 3, "D": 4, "HELD": 15}
    assert _frozen_dropouts(watch, rank_idx, 1.2) == {"HELD"}


def test_unranked_dropout_is_frozen():
    # A legacy name with no live price this scan isn't in rank_idx → frozen (can't be bought anyway).
    watch = [_w("A"), _w("B"), _w("OLD", pending=True)]
    rank_idx = {"A": 1, "B": 2}
    assert _frozen_dropouts(watch, rank_idx, 1.2) == {"OLD"}


def test_pure_member_book_freezes_nothing():
    watch = [_w("A"), _w("B"), _w("C")]
    rank_idx = {"A": 1, "B": 2, "C": 3}
    assert _frozen_dropouts(watch, rank_idx, 1.2) == set()


def test_all_legacy_no_members_freezes_nothing():
    # Degenerate: no current members → member_count 0 → don't freeze (avoids a div-by-nothing/empty book).
    watch = [_w("X", pending=True), _w("Y", category="existing_holding")]
    rank_idx = {"X": 1, "Y": 2}
    assert _frozen_dropouts(watch, rank_idx, 1.2) == set()


def test_buffer_mult_widens_the_band():
    # Same book, bigger buffer keeps a mid-ranked drop-out buyable.
    watch = [_w("A"), _w("B"), _w("C"), _w("D"), _w("OLD", pending=True)]
    rank_idx = {"A": 1, "B": 2, "C": 3, "D": 4, "OLD": 6}
    assert _frozen_dropouts(watch, rank_idx, 1.2) == {"OLD"}   # cutoff 4.8 < 6 → frozen
    assert _frozen_dropouts(watch, rank_idx, 2.0) == set()     # cutoff 8.0 >= 6 → kept
