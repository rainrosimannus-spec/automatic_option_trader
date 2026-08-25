"""Froth-gated crash-reserve backstop (2026-08-25, from the 2021-22 backtest).

The old calendar-only backstop (backstop_unlocked_fraction) bled the reserve in on a fixed schedule
regardless of valuation, so a melt-up that ran longer than the start delay force-deployed the whole
reserve into the top — leaving $0 dry powder for the crash that followed. The froth-gated pair
(backstop_bleed_step accumulates eligible bleed-days, backstop_accrued_fraction maps them to unlock)
pauses the bleed whenever SPY is extended above its 200-day trend, holding the reserve for the drawdown.
"""
from src.portfolio.compounder import (backstop_bleed_step, backstop_accrued_fraction,
                                       backstop_unlocked_fraction)


# ---- backstop_bleed_step: the froth-gated accumulator ----
def test_accrues_on_calm_days_past_start():
    assert backstop_bleed_step(0.0, 1, past_start=True, market_extended=False) == 1.0
    assert backstop_bleed_step(10.0, 3, past_start=True, market_extended=False) == 13.0

def test_froth_pauses_accrual():
    # Extended market → the bleed pauses; the reserve is NOT fed into the top.
    assert backstop_bleed_step(10.0, 5, past_start=True, market_extended=True) == 10.0

def test_start_delay_gates_accrual():
    # Before the start delay, nothing accrues even on a calm day.
    assert backstop_bleed_step(0.0, 5, past_start=False, market_extended=False) == 0.0

def test_monotonic_never_relocks():
    # A frothy stretch after some accrual never DECREASES the accrued reserve (we never sell).
    acc = backstop_bleed_step(20.0, 30, past_start=True, market_extended=True)
    assert acc == 20.0

def test_zero_or_negative_step_is_noop():
    assert backstop_bleed_step(7.0, 0, past_start=True, market_extended=False) == 7.0
    assert backstop_bleed_step(7.0, -3, past_start=True, market_extended=False) == 7.0

def test_negative_prior_floored():
    assert backstop_bleed_step(-5.0, 2, past_start=True, market_extended=False) == 2.0


# ---- backstop_accrued_fraction: eligible-days -> 0..1 unlock ----
def test_fraction_zero_at_zero():
    assert backstop_accrued_fraction(0.0, 180) == 0.0

def test_fraction_linear_then_caps():
    assert backstop_accrued_fraction(90.0, 180) == 0.5
    assert backstop_accrued_fraction(180.0, 180) == 1.0
    assert backstop_accrued_fraction(400.0, 180) == 1.0   # clamps

def test_fraction_bad_bleed_days():
    assert backstop_accrued_fraction(50.0, 0) == 0.0


# ---- integration: melt-up-then-calm story vs the old calendar backstop ----
def test_meltup_holds_reserve_then_calm_deploys():
    bleed = 180
    # 300 straight frothy days past the start delay: old backstop would be FULLY unlocked (force-deployed
    # into the top); the froth-gated one accrues ZERO — dry powder preserved.
    eff = 0.0
    for _ in range(300):
        eff = backstop_bleed_step(eff, 1, past_start=True, market_extended=True)
    assert backstop_accrued_fraction(eff, bleed) == 0.0
    assert backstop_unlocked_fraction(300, 90, bleed) == 1.0   # old behaviour: fully deployed
    # then the froth clears — 180 calm days bleed it fully in, as intended (never permanently idle).
    for _ in range(bleed):
        eff = backstop_bleed_step(eff, 1, past_start=True, market_extended=False)
    assert backstop_accrued_fraction(eff, bleed) == 1.0
