"""Screener drop-out freeze selection — TOP-K conviction cutoff (2026-08-01).

When the monthly screener drops a HELD name it stays in the watchlist (flagged
pending_removal, or never-screened category 'existing_holding') but it dropped for a
reason, so it stops drawing new capital: target frozen at invested, budget redistributed
to members. A drop-out still ranking in the top-K on the LIVE rank is spared (still elite /
likely re-admit); topk=0 = total freeze. _frozen_dropouts picks exactly the names to freeze.
(The earlier member_count×mult buffer never bound at the live ~90%-member shape — hence the
absolute top-K.)
"""
from types import SimpleNamespace

from src.portfolio.buyer import _frozen_dropouts


def _w(symbol, pending=False, category="growth"):
    return SimpleNamespace(symbol=symbol, pending_removal=pending, category=category)


def test_total_freeze_k0_freezes_every_dropout():
    # topk=0 → no buffer: every held drop-out is frozen regardless of live rank.
    watch = [_w("A"), _w("B"), _w("OLD", pending=True), _w("HELD", category="existing_holding")]
    rank_idx = {"A": 1, "B": 2, "OLD": 3, "HELD": 4}
    assert _frozen_dropouts(watch, rank_idx, 0) == {"OLD", "HELD"}


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


def test_existing_holding_never_screened_is_a_dropout():
    watch = [_w("A"), _w("B"), _w("HELD", category="existing_holding")]
    rank_idx = {"A": 1, "B": 2, "HELD": 30}
    assert _frozen_dropouts(watch, rank_idx, 10) == {"HELD"}


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
        _w("NVDA", pending=True), _w("AVGO", pending=True), _w("GOOG", category="existing_holding")]
    rank_idx = {"M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "NVDA": 6, "AVGO": 20, "GOOG": 22}
    # K=0 (default, total freeze) → all three dropouts frozen, even high-ranked NVDA.
    assert _frozen_dropouts(watch, rank_idx, 0) == {"NVDA", "AVGO", "GOOG"}
    # K=10 would spare NVDA (rank 6) but still freeze AVGO/GOOG.
    assert _frozen_dropouts(watch, rank_idx, 10) == {"AVGO", "GOOG"}
