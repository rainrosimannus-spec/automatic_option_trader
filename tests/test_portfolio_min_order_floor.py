"""The min-order floor must never make a name PERMANENTLY unbuyable (2026-07-30).

min_buy is 0.4% of NLV and SCALES WITH IT; a per-name target is a slice of the tier budget. On a
large book the floor overtakes the smallest targets, so those names are skipped on every scan
forever — the gap can never grow enough to clear a floor that grows with the account.

Measured on the live book (NLV EUR10.87M -> floor EUR43,474): 18 of 106 names had their ENTIRE
target under the floor, including ranks 10/12/14/15 (MFC, RY, BAP, SU). Rain: "make it fit the
smallest target, otherwise it could never be bought ever."

Rule: when the whole remaining GAP is under the floor, allow ONE order that CLOSES it completely.
Never a partial dust order — if the brick cannot cover the whole gap it still fails and waits.
cc.min_single_buy_floor stays an absolute hard floor so genuine dust is skipped.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

NLV = 10_868_613.0
MIN_BUY = max(NLV * 0.004, 2_000.0)        # ~EUR43,474
MAX_BUY = max(NLV * 0.02, MIN_BUY)
HARD_FLOOR = 2_000.0
BUDGET = 76_144.0


def _eff_floor(tgt: float, cur: float) -> float:
    gap = max(0.0, tgt - cur)
    return MIN_BUY if gap >= MIN_BUY else max(gap, HARD_FLOOR)


def _buys(tgt: float, cur: float, budget: float = BUDGET) -> bool:
    brick = min(MAX_BUY, tgt - cur, budget)
    return brick >= _eff_floor(tgt, cur)


def _buys_old(tgt: float, cur: float, budget: float = BUDGET) -> bool:
    return min(MAX_BUY, tgt - cur, budget) >= MIN_BUY


# ── The names that were permanently starved ─────────────────────────────────

def test_targets_below_the_floor_were_unbuyable_and_now_are_not():
    for sym, tgt in (("MFC", 42_811), ("RY", 41_167), ("BAP", 40_578),
                     ("BMO", 36_122), ("TD", 35_781), ("CFR", 33_497),
                     ("WKL", 13_436)):
        assert not _buys_old(tgt, 0), f"{sym} should have been blocked before"
        assert _buys(tgt, 0), f"{sym} must be buyable now"


def test_a_partially_held_name_can_close_its_remaining_gap():
    """SU: target 39,523, held 633 -> gap 38,890, under the floor."""
    assert not _buys_old(39_523, 633)
    assert _buys(39_523, 633)


def test_large_target_partially_filled_can_be_topped_up_to_completion():
    """ABNB: target 121,866, held 88,125 -> gap 33,741 < floor, but it COMPLETES the position."""
    assert not _buys_old(121_866, 88_125)
    assert _buys(121_866, 88_125)


# ── Guardrails: this must not become a dust generator ───────────────────────

def test_true_dust_is_still_skipped():
    """Below the absolute hard floor, nothing trades."""
    assert not _buys(1_500, 0)
    assert not _buys(500, 0)
    assert not _buys(1_900, 0)            # gap 1,900 -> below the hard floor
    assert _buys(2_100, 100)              # gap 2,000 -> AT the hard floor, allowed by design


def test_partial_order_that_cannot_close_the_gap_is_refused():
    """A short budget must WAIT, not place half a sub-floor order."""
    assert not _buys(42_811, 0, budget=20_000)     # brick 20k < gap 42,811
    assert _buys(42_811, 0, budget=42_811)         # exactly closes it


def test_normal_sized_names_are_completely_unaffected():
    """Anything with a gap at or above the floor keeps the original behaviour."""
    for tgt in (MIN_BUY, 60_000, 121_866, 217_372):
        assert _eff_floor(tgt, 0) == MIN_BUY
        assert _buys(tgt, 0) == _buys_old(tgt, 0)


def test_floor_is_never_raised_by_the_change():
    """The effective floor can only ever be <= the original floor."""
    for tgt in (500, 2_000, 13_436, 43_474, 100_000):
        for cur in (0, 1_000, 40_000):
            assert _eff_floor(tgt, cur) <= MIN_BUY


def test_the_starvation_worsens_with_nlv_which_is_why_this_matters():
    """The floor grows with NLV while targets do not track it 1:1 — more names starve over time."""
    small = max(1_000_000 * 0.004, 2_000.0)
    large = max(50_000_000 * 0.004, 2_000.0)
    assert small < MIN_BUY < large
    assert large > 40_578                  # BAP's whole target, unbuyable at that scale
