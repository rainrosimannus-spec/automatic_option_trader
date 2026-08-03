"""Screener drop-out freeze selection — TOP-K conviction cutoff (2026-08-01).

A drop-out = a HELD watchlist name the monthly screen flagged `pending_removal` (set on drop,
CLEARED on re-admit). It dropped for a reason, so it stops drawing new capital: target frozen at
invested, budget redistributed to members. A drop-out still ranking in the top-K on the LIVE rank
is spared (still elite / likely re-admit); topk=0 = total freeze. The freeze keys ONLY on
pending_removal — NOT category=='existing_holding', which the screen never clears on re-admit
(that would strand a returning name frozen forever) and which isn't a "drop-out" anyway.
"""
from types import SimpleNamespace

from src.portfolio.buyer import _frozen_dropouts


def _w(symbol, pending=False, category="growth"):
    return SimpleNamespace(symbol=symbol, pending_removal=pending, category=category)


def test_total_freeze_k0_freezes_every_dropout():
    # topk=0 → no buffer: every pending_removal drop-out is frozen regardless of live rank.
    watch = [_w("A"), _w("B"), _w("OLD", pending=True), _w("OLD2", pending=True)]
    rank_idx = {"A": 1, "B": 2, "OLD": 3, "OLD2": 4}
    assert _frozen_dropouts(watch, rank_idx, 0) == {"OLD", "OLD2"}


def test_topk_spares_a_top_ranked_dropout():
    # topk=5: OLD ranked 3 (<=5) is still elite → spared; FAR ranked 40 → frozen.
    watch = [_w("A"), _w("B"), _w("OLD", pending=True), _w("FAR", pending=True)]
    rank_idx = {"A": 1, "B": 2, "OLD": 3, "FAR": 40}
    assert _frozen_dropouts(watch, rank_idx, 5) == {"FAR"}


def test_topk_boundary_is_inclusive():
    # rank == topk is inside the top-K (spared); rank == topk+1 is frozen.
    watch = [_w("ON", pending=True), _w("OFF", pending=True), _w("A")]
    rank_idx = {"A": 1, "ON": 5, "OFF": 6}
    assert _frozen_dropouts(watch, rank_idx, 5) == {"OFF"}


def test_existing_holding_is_NOT_frozen_when_not_pending_removal():
    # A never-screened auto-added holding (pending_removal=0) is NOT a drop-out → never frozen,
    # even under total freeze. Only the screen's pending_removal flag drives the freeze.
    watch = [_w("A"), _w("B"), _w("HELD", category="existing_holding")]
    rank_idx = {"A": 1, "B": 2, "HELD": 30}
    assert _frozen_dropouts(watch, rank_idx, 0) == set()


def test_readmitted_name_unfreezes():
    # Re-admission clears pending_removal → the name is no longer a drop-out → not frozen, even if
    # its category is still the stale 'existing_holding' the screen didn't reset (the GOOG case).
    watch = [_w("A"), _w("GOOG", pending=False, category="existing_holding")]
    rank_idx = {"A": 1, "GOOG": 22}
    assert _frozen_dropouts(watch, rank_idx, 0) == set()


def test_unranked_dropout_always_frozen_even_with_buffer():
    # No live price this scan → not in rank_idx → frozen (can't be bought anyway), buffer notwithstanding.
    watch = [_w("A"), _w("OLD", pending=True)]
    rank_idx = {"A": 1}
    assert _frozen_dropouts(watch, rank_idx, 50) == {"OLD"}


def test_pure_member_book_freezes_nothing():
    watch = [_w("A"), _w("B"), _w("C")]
    rank_idx = {"A": 1, "B": 2, "C": 3}
    assert _frozen_dropouts(watch, rank_idx, 0) == set()
    assert _frozen_dropouts(watch, rank_idx, 40) == set()


def test_live_shape_k0_freezes_the_current_pending_removal_names():
    # Mirrors today's book: members ranked high, the pending-removal dropouts scattered below.
    watch = [_w(f"M{i}") for i in range(1, 6)] + [
        _w("NVDA", pending=True), _w("AVGO", pending=True), _w("GOOG", pending=True)]
    rank_idx = {"M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "NVDA": 6, "AVGO": 20, "GOOG": 22}
    # K=0 (default, total freeze) → all three dropouts frozen, even high-ranked NVDA.
    assert _frozen_dropouts(watch, rank_idx, 0) == {"NVDA", "AVGO", "GOOG"}
    # K=10 would spare NVDA (rank 6) but still freeze AVGO/GOOG.
    assert _frozen_dropouts(watch, rank_idx, 10) == {"AVGO", "GOOG"}
