"""Crash reserve tranches and funding, Rain 2026-11-20.

What went wrong that evening: the first (-10%) tranche fired after Xetra had closed. The budget gate
counted the EUR 1.95M park as cash, the crash branch deployed the WHOLE remaining gap (undeployed base
+ all three legs of reserve), the scan wrote 59 US cards, and 53 died at the FX leg because the euros
sat in XEON until Monday — 53 alerts, nothing bought. Rain's design: -10% spends one third of the
reserve, -20% the rest, -30% opens the margin basket; the base keeps its normal pace; unreleased
reserve is ring-fenced; the park counts as cash only while its venue is open."""
from datetime import datetime, timedelta

import pytest

from src.portfolio import compounder as cmp


# ── release schedule ──────────────────────────────────────────────────────────────────────────────
SCHED = [1.0 / 3.0, 1.0, 1.0]


def test_schedule_one_third_then_the_rest_then_margin():
    assert cmp.reserve_released_fraction(0, SCHED) == 0.0
    assert abs(cmp.reserve_released_fraction(1, SCHED) - 1 / 3) < 1e-9
    assert cmp.reserve_released_fraction(2, SCHED) == 1.0
    assert cmp.reserve_released_fraction(3, SCHED) == 1.0          # reserve spent; margin basket's turn
    assert cmp.reserve_released_fraction(7, SCHED) == 1.0          # past the end stays at the last step


def test_no_schedule_falls_back_to_linear():
    assert abs(cmp.reserve_released_fraction(2, None, 3) - 2 / 3) < 1e-9


def test_config_ships_rains_schedule():
    from src.portfolio.config import CompounderConfig
    cc = CompounderConfig()
    assert [round(x, 4) for x in cc.reserve_release_schedule] == [0.3333, 1.0, 1.0]
    assert cc.drawdown_tranches == [0.10, 0.20, 0.30]


# ── reserve arithmetic on the 11-20 numbers ───────────────────────────────────────────────────────
INVESTABLE = 9_635_652.0          # NLV 9.93M x 97%
BASE = 0.9


def test_reserve_room_is_one_third_of_the_reserve_at_the_first_leg():
    assert round(cmp.reserve_total(INVESTABLE, BASE)) == 963_565
    assert round(cmp.reserve_room(INVESTABLE, BASE, 1 / 3, spent=0.0)) == 321_188
    assert round(cmp.unreleased_reserve(INVESTABLE, BASE, 1 / 3)) == 642_377


def test_room_shrinks_with_spend_and_never_goes_negative():
    assert round(cmp.reserve_room(INVESTABLE, BASE, 1 / 3, spent=200_000)) == 121_188
    assert cmp.reserve_room(INVESTABLE, BASE, 1 / 3, spent=900_000) == 0.0


def test_second_leg_opens_the_rest():
    assert round(cmp.reserve_room(INVESTABLE, BASE, 1.0, spent=321_188)) == 642_377
    assert cmp.unreleased_reserve(INVESTABLE, BASE, 1.0) == 0.0


def test_todays_reserve_spend_is_deployment_above_the_base_pace():
    assert cmp.reserve_spend_today(400_000, 150_000) == 250_000
    assert cmp.reserve_spend_today(100_000, 150_000) == 0.0


# ── the budget on a crash day ─────────────────────────────────────────────────────────────────────
def _crash_budget(deployed_today, reserve_room, free_cash=2_030_000.0, gap=2_940_000.0):
    return cmp.daily_deploy_budget(INVESTABLE, BASE, 21, 1 / 3, deployed=0.0,
                                   target_total=gap, crash_active=True, free_cash=free_cash,
                                   deployed_today=deployed_today, lump_horizon_days=126,
                                   pace_throttle=1.0, reserve_room=reserve_room)


def test_crash_budget_is_base_pace_plus_released_reserve_not_the_whole_park():
    pace = cmp.base_daily_pace(INVESTABLE, BASE, 21, 2_940_000.0, 0.0, 126, 1.0)
    b = _crash_budget(deployed_today=0.0, reserve_room=321_188)
    assert abs(b - (pace + 321_188)) < 1.0
    assert b < 600_000                      # the 11-20 scan sized this at 1,963,758


def test_crash_budget_hands_back_room_as_the_day_fills():
    pace = cmp.base_daily_pace(INVESTABLE, BASE, 21, 2_940_000.0, 400_000.0, 126, 1.0)
    spent_today = cmp.reserve_spend_today(400_000.0, pace)
    room = cmp.reserve_room(INVESTABLE, BASE, 1 / 3, spent_today)
    b = _crash_budget(deployed_today=400_000.0, reserve_room=room)
    assert abs((400_000.0 + b) - (pace + 321_188)) < 1.0      # day total = pace + released reserve


def test_crash_budget_with_reserve_exhausted_is_just_the_base_pace():
    pace = cmp.base_daily_pace(INVESTABLE, BASE, 21, 2_940_000.0, 0.0, 126, 1.0)
    assert abs(_crash_budget(0.0, reserve_room=0.0) - pace) < 1.0


def test_crash_budget_still_capped_by_gap_and_cash():
    assert _crash_budget(0.0, 321_188, free_cash=50_000.0) == 50_000.0
    assert _crash_budget(0.0, 321_188, gap=10_000.0) == 10_000.0


def test_non_crash_branch_unchanged():
    a = cmp.daily_deploy_budget(INVESTABLE, BASE, 21, 0.0, 0.0, 2_940_000.0, False, 1e9)
    b = cmp.daily_deploy_budget(INVESTABLE, BASE, 21, 0.0, 0.0, 2_940_000.0, False, 1e9,
                                reserve_room=5e6)
    assert a == b                           # reserve_room is a crash-only lever


# ── fundable cash: the park counts only while Xetra is open ───────────────────────────────────────
def test_park_counts_only_while_its_venue_is_open():
    liquid = 40_000.0; parked = 1_950_000.0; unreleased = 642_377.0
    shut = cmp.fundable_cash(liquid, parked, park_open=False, unreleased=unreleased)
    open_ = cmp.fundable_cash(liquid, parked, park_open=True, unreleased=unreleased)
    assert shut == 40_000.0                                   # 11-20 evening: only the liquid runway
    assert round(open_) == round(40_000.0 + 1_950_000.0 - 642_377.0)


def test_unreleased_reserve_is_ring_fenced_from_the_park_not_from_liquid_cash():
    # ring-fence never pushes liquid cash negative, and a park smaller than the fence contributes 0
    assert cmp.fundable_cash(100_000.0, 500_000.0, True, unreleased=642_377.0) == 100_000.0
    assert cmp.fundable_cash(100_000.0, 0.0, False, unreleased=642_377.0) == 100_000.0


def test_crash_margin_and_derate_apply_last():
    assert cmp.fundable_cash(100_000.0, 0.0, False, 0.0, crash_margin=50_000.0, soft_scale=0.5) == 75_000.0


# ── executor: a cash-timing wait is not an FX failure ─────────────────────────────────────────────
def test_unfunded_alert_is_throttled_to_one_per_half_hour(monkeypatch):
    import src.portfolio.buyer as b
    monkeypatch.setattr(b, "_unfunded_alert_at", None)
    t0 = datetime(2026, 11, 20, 20, 10)
    assert b._unfunded_alert_due(t0) is True
    assert b._unfunded_alert_due(t0 + timedelta(minutes=10)) is False
    assert b._unfunded_alert_due(t0 + timedelta(minutes=29)) is False
    assert b._unfunded_alert_due(t0 + timedelta(minutes=30)) is True


def test_fx_funding_records_a_liquidity_deferral(monkeypatch):
    """_ensure_currency_funding returns False and tags the reason when the EUR to sell is not liquid."""
    import src.portfolio.buyer as b
    from types import SimpleNamespace
    from contextlib import contextmanager

    @contextmanager
    def lock():
        yield
    monkeypatch.setattr(b, "get_portfolio_lock", lock)
    pair = SimpleNamespace(symbol="EUR", currency="USD", conId=12087792)
    ib = SimpleNamespace(
        accountValues=lambda: [SimpleNamespace(tag="CashBalance", currency="USD", value="-19850")],
        qualifyContracts=lambda c: None,
    )
    monkeypatch.setattr(b, "Forex", lambda s: pair)
    monkeypatch.setattr(b, "_fx_rate_ccy_per_base", lambda *a, **k: 1.16)
    monkeypatch.setattr(b, "_ccy_cash", lambda ib_, ccy: 17_163.0)       # the 11-20 EUR line
    b._FX_DEFER_REASON.clear()
    ok = b._ensure_currency_funding(ib, "USD", "EUR", 55_000.0, cfg=SimpleNamespace(
        fx_idealpro_min_base=22_000.0, fx_fill_wait_secs=1.0))
    assert ok is False
    assert b._FX_DEFER_REASON.get("USD") == "liquidity"
