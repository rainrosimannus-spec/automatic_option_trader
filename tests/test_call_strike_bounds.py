"""Covered-call strike window bounds (ported from son's tree, 2026-07-30).

The 15%-above-spot cap bounded the normal CC band, but when a rescue/token/
below-breakeven branch pins an explicit floor ABOVE that cap (deep-underwater lot:
min_strike ≈ breakeven or basis×0.90 while spot is 20-50% below), the window
[floor, spot*1.15) was the EMPTY SET — get_call_contracts returned zero contracts
before ever seeing a bid, so a >15%-underwater lot could never be covered by ANY
branch. _call_strike_bounds extends the ceiling to floor*1.10 in that case.
"""
import pytest

from src.broker.market_data import _call_strike_bounds


def test_normal_band_unchanged():
    # No explicit floor → classic (spot, spot*1.15) window.
    lower, upper = _call_strike_bounds(100.0, None)
    assert lower == 100.0
    assert upper == pytest.approx(115.0)


def test_mild_floor_keeps_normal_cap():
    # Floor below the 15% cap → cap stays at spot*1.15 (floor just raises the lower bound).
    lower, upper = _call_strike_bounds(341.24, 345.96)
    assert lower == 345.96
    assert upper == pytest.approx(341.24 * 1.15)


def test_deep_floor_extends_cap_to_floor_band():
    # Floor (59.65) far above spot*1.15 (41.5) → ceiling extends to floor*1.10 so the
    # window is searchable instead of empty.
    lower, upper = _call_strike_bounds(36.12, 59.65)
    assert lower == 59.65
    assert upper == pytest.approx(59.65 * 1.10)
    assert upper > lower  # never empty by construction


def test_bbe_floor_reachable_for_deep_drawdown():
    # below-breakeven lot: basis 121, spot 61.21 (−49%), bbe_floor = basis*0.90 = 108.9.
    lower, upper = _call_strike_bounds(61.21, 121.0 * 0.90)
    assert upper > lower
