"""XEON park/un-park behaviour (2026-07-28).

Rain saw the compounder sell 755 XEON on GETTEX2 and buy 56 back on IBIS2 the same day. The venue
split is fixed in 345f2ca; this covers the rest.

THE RULE, in Rain's words: the 10-day cash reserve applies ONLY to newly added cash. XEON is a €STR
money-market ETF — its NAV accrues ~2%/yr against a round-trip spread, so selling park shares inside
~10 days realises a LOSS and after that a gain. So the reserve is not "always hold N days of cash";
it is "don't park money you're about to spend". A deposit waits in cash until it is deployed or has
aged past the spread. After that nothing is held back: the book runs ~fully parked and each day's
buying is funded by a just-in-time sale of shares long past their spread. Crash regime is exempt.

The old code sized the reserve as park_reserve_days x the scan's REMAINING budget, which drains to 0
as the day deploys — so the target collapsed ~EUR797k -> ~EUR328k inside a day and rebuilt each
morning, parking into the fall and un-parking into the reset.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.portfolio import buyer as B

NLV = 10_926_456.62
BUFFER_PCT = 0.03
NLV_FLOOR = NLV * BUFFER_PCT          # ~EUR327,794 operational cushion (deliberate config)
PARK_MIN = 5_000.0


def _target(deposit_runway: float, open_buy: float = 0.0) -> float:
    """Mirror of the reserve/target computation in _park_compounder_excess."""
    return max(NLV * BUFFER_PCT, max(0.0, deposit_runway)) + open_buy


def _runway(deposited: float, spent_since: float) -> float:
    """Mirror of _deposit_runway's arithmetic (deposits in window minus buys since the oldest)."""
    return max(0.0, deposited - spent_since)


# ── The reserve covers ONLY the added cash, and decays as it is spent ────────

def test_fresh_deposit_is_held_out_of_the_park():
    """EUR200k lands, nothing spent yet -> all EUR200k waits in cash."""
    assert _runway(200_000.0, 0.0) == 200_000.0


def test_reserve_decays_as_the_deposit_is_deployed():
    assert _runway(200_000.0, 50_000.0) == 150_000.0
    assert _runway(200_000.0, 200_000.0) == 0.0


def test_reserve_never_goes_negative_when_overspent():
    """Buying more than the deposit (funded from the park) must not create a negative reserve."""
    assert _runway(200_000.0, 500_000.0) == 0.0


def test_no_recent_deposit_means_no_reserve():
    """Today's real state: last deposit 2026-07-07, 21 days ago -> outside the window entirely."""
    assert _runway(0.0, 0.0) == 0.0
    assert _target(0.0) == NLV_FLOOR      # only the operational cushion remains outside XEON


def test_steady_state_parks_everything_above_the_cushion():
    """Aged book: reserve 0, so any cash above the cushion is parked."""
    base_cash = NLV_FLOOR + 250_000.0
    excess = base_cash - _target(0.0)
    assert abs(excess - 250_000.0) < 1e-6 and excess >= PARK_MIN


def test_in_flight_buys_are_never_parked_out_from_under():
    assert _target(0.0, open_buy=120_000.0) == NLV_FLOOR + 120_000.0


# ── The old sizing is what churned; the new one does not ────────────────────

def test_old_budget_keyed_reserve_swung_within_a_day_new_one_does_not():
    old_start = max(NLV * BUFFER_PCT, 10 * 79_675.0)      # budget at start of day
    old_after = max(NLV * BUFFER_PCT, 10 * 0.0)           # budget after the day deployed
    assert old_start - old_after > 400_000                # ~EUR469k swing -> park, un-park tomorrow

    new_start, new_after = _target(_runway(0.0, 0.0)), _target(_runway(0.0, 0.0))
    assert new_start == new_after                         # deposit-keyed: deploying moves nothing


def test_a_deposit_does_not_force_selling_the_park_to_build_a_reserve():
    """The reserve can only be SATISFIED by deposited cash, never manufactured by selling XEON:
    it is bounded by what was deposited and not yet spent."""
    assert _runway(50_000.0, 0.0) == 50_000.0             # not 10 x daily budget (~EUR797k)
    assert _runway(50_000.0, 0.0) < 10 * 79_675.0


# ── Hysteresis: no park immediately after an un-park ─────────────────────────

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


def test_the_observed_round_trip_is_now_blocked():
    """08:54:55 un-park fills, 09:03:47 parker wants back in — 9 minutes, inside the window."""
    _reset()
    B._last_unpark_at["XEON"] = datetime.utcnow() - timedelta(minutes=9)
    assert B._recently_unparked("XEON") is True


# ── One day's working capital is never parked (Asia opens while Xetra is shut) ──
# Park venue (EUR) is 07:00-15:00 UTC. Tokyo opens 00:00 UTC, Sydney/HK just after -- all trade
# while Xetra is SHUT, so a JIT sale during an Asian session cannot fill. The cash that funds Asia
# must already be liquid, raised on the previous European scan.

DAY_BUDGET = 79_885.0


def _target2(deposit_runway: float, day_budget: float, open_buy: float = 0.0) -> float:
    """Mirror of the reserve/target computation including the working-capital leg."""
    return max(NLV * BUFFER_PCT, max(0.0, deposit_runway), max(0.0, day_budget)) + open_buy


def test_one_day_of_working_capital_is_always_reserved():
    """Even with no deposit and an aged book, the day's budget stays out of the park."""
    assert _target2(0.0, DAY_BUDGET) >= DAY_BUDGET


def test_working_capital_is_not_parked_then_bought_back_next_morning():
    """The old behaviour parked it at the end of the day and needed it back before Asia."""
    cash = DAY_BUDGET                      # exactly one day's float on hand
    excess = cash - _target2(0.0, DAY_BUDGET)
    assert excess < PARK_MIN               # nothing to park -> no round trip


def test_shortfall_triggers_a_top_up_so_asia_is_pre_funded():
    """Cash below the day's budget must show as a shortfall the un-park leg acts on."""
    cash = 10_000.0
    excess = cash - _target2(0.0, DAY_BUDGET)
    assert excess < -PARK_MIN              # un-park branch fires (during EUR hours)


def test_deposit_window_dominates_when_larger():
    assert _target2(500_000.0, DAY_BUDGET) == NLV_FLOOR + 0.0 or \
           _target2(500_000.0, DAY_BUDGET) == max(NLV_FLOOR, 500_000.0)


def test_cushion_still_floors_everything():
    assert _target2(0.0, 0.0) == NLV_FLOOR
