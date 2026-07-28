"""XEON park/un-park churn guards (2026-07-28).

Rain saw the compounder sell 755 XEON on GETTEX2 and buy 56 back on IBIS2 the same day. Three
independent causes; the venue split is fixed in 345f2ca, these are the other two.

  A. The cash RUNWAY was sized off the scan's REMAINING budget, which drains to 0 as the day deploys
     (instantly, if a manual buy lands). runway = park_reserve_days x that, so the park target
     collapsed ~EUR797k -> ~EUR328k inside one day and rebuilt each morning. The parker holds cash AT
     the target, so it parked into the fall and un-parked into the reset -- a daily round trip caused
     by a counter, not by cash. Now sized off base_daily_pace, which does not move as the day deploys.
  B. A park could immediately follow an un-park (late partial fill, target moved between scans), so a
     BUY landed right behind a SELL on the same ETF. Now gated by a cool-down.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.portfolio import compounder as cmp
from src.portfolio import buyer as B

INVESTABLE = 10_599_799.0
BASE_PCT = 0.90
DCA, LUMP, THROTTLE = 21, 126, 0.95
GAP = 9_497_003.0 - 1_097_603.0
RESERVE_DAYS = 10
NLV = 10_926_456.62
NLV_FLOOR = NLV * 0.03


def _pace(deployed_today: float) -> float:
    return cmp.base_daily_pace(INVESTABLE, BASE_PCT, DCA, GAP - deployed_today,
                               deployed_today, LUMP, THROTTLE)


def _budget(deployed_today: float) -> float:
    return cmp.daily_deploy_budget(
        INVESTABLE, BASE_PCT, DCA, 0.0, 1_097_603.0 + deployed_today, 9_497_003.0,
        False, 10_000_000.0, deployed_today=deployed_today,
        lump_horizon_days=LUMP, pace_throttle=THROTTLE)


def _target(runway_source: float) -> float:
    return max(NLV_FLOOR, RESERVE_DAYS * max(0.0, runway_source))


# ── A. runway must not move as the day deploys ───────────────────────────────

def test_remaining_budget_collapses_within_a_day():
    """The OLD source. This is the churn engine, shown explicitly."""
    assert _budget(0.0) > 70_000
    assert _budget(80_050.80) == 0.0          # today: one manual ASML buy zeroed it


def test_base_pace_is_stable_across_the_same_day():
    """The NEW source. Deploying the day's budget must not move the runway."""
    start, after = _pace(0.0), _pace(80_050.80)
    assert abs(after - start) / start < 0.01


def test_park_target_no_longer_swings_by_hundreds_of_thousands():
    old_start, old_after = _target(_budget(0.0)), _target(_budget(80_050.80))
    new_start, new_after = _target(_pace(0.0)), _target(_pace(80_050.80))
    assert old_start - old_after > 400_000          # ~EUR469k swing -> park, then un-park tomorrow
    assert abs(new_start - new_after) < 5_000       # inside the cash_park_min deadband: no trade


def test_target_still_floors_at_the_nlv_buffer():
    """A zero pace must not drive the runway to zero and dump the whole cash line into the ETF."""
    assert _target(0.0) == NLV_FLOOR


# ── B. no park immediately after an un-park ──────────────────────────────────

def _reset(sym="XEON"):
    B._last_unpark_at.pop(sym, None)


def test_no_cooldown_when_nothing_was_unparked():
    _reset()
    assert B._recently_unparked("XEON") is False


def test_cooldown_blocks_a_park_right_after_an_unpark():
    _reset()
    B._last_unpark_at["XEON"] = datetime.utcnow()
    assert B._recently_unparked("XEON") is True


def test_cooldown_expires():
    _reset()
    B._last_unpark_at["XEON"] = datetime.utcnow() - timedelta(
        minutes=B._PARK_COOLDOWN_MINUTES + 1)
    assert B._recently_unparked("XEON") is False


def test_cooldown_is_per_symbol():
    _reset("XEON")
    _reset("XFFE")
    B._last_unpark_at["XEON"] = datetime.utcnow()
    assert B._recently_unparked("XFFE") is False


def test_the_observed_round_trip_is_now_blocked():
    """08:54:55 un-park fills, 09:03:47 parker wants back in — 9 minutes later, inside the window."""
    _reset()
    B._last_unpark_at["XEON"] = datetime.utcnow() - timedelta(minutes=9)
    assert B._recently_unparked("XEON") is True
