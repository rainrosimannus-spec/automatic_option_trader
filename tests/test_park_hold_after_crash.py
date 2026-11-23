"""Post-crash park hold (Rain, 2026-11-23): "Don't re-park it, unless it has been below 10% at least
10 days."

The crash switch is evaluated per scan against the live SPY gauge with no hysteresis. On 2026-11-23
the 08:10 scan saw -10.7% and sold EUR 690k of XEON to fund the released reserve; the 10:10 scan saw
-9.4% (SPY pre-market) and the parker bought EUR 183k straight back. Now, once a tranche has fired,
the park leg stays suspended until the drawdown has been under the first tranche line for
`park_hold_days_after_crash` consecutive calendar days. The un-park leg is never held.
"""
from src.portfolio import compounder as cmp
from src.portfolio.config import PortfolioConfig

HOLD = PortfolioConfig().compounder.park_hold_days_after_crash
LINE = PortfolioConfig().compounder.drawdown_tranches[0]


def test_default_is_ten_days_and_the_line_is_ten_percent():
    assert HOLD == 10 and abs(LINE - 0.10) < 1e-9


def test_no_tranche_fired_means_no_hold():
    assert cmp.park_hold_after_crash(0, 0.094, LINE, None, "2026-11-23", HOLD) == (False, None)
    assert cmp.park_hold_after_crash(0, 0.30, LINE, "2026-11-01", "2026-11-23", HOLD) == (False, None)


def test_the_observed_round_trip_is_blocked():
    # 08:10: -10.7% -> crash on, clock reset
    hold, since = cmp.park_hold_after_crash(1, 0.107, LINE, None, "2026-11-23", HOLD)
    assert hold and since is None
    # 10:10: -9.4% -> under the line, clock starts today, still held
    hold, since = cmp.park_hold_after_crash(1, 0.094, LINE, since, "2026-11-23", HOLD)
    assert hold and since == "2026-11-23"


def test_hold_lifts_after_ten_consecutive_days_under_the_line():
    since = "2026-11-23"
    for day in ("2026-11-24", "2026-11-30", "2026-12-02"):
        hold, since2 = cmp.park_hold_after_crash(1, 0.05, LINE, since, day, HOLD)
        assert hold and since2 == since
    hold, since2 = cmp.park_hold_after_crash(1, 0.05, LINE, since, "2026-12-03", HOLD)   # day 10
    assert not hold and since2 == since


def test_a_dip_back_over_the_line_restarts_the_clock():
    since = "2026-11-23"
    hold, since = cmp.park_hold_after_crash(1, 0.112, LINE, since, "2026-11-27", HOLD)     # crash again
    assert hold and since is None
    hold, since = cmp.park_hold_after_crash(1, 0.09, LINE, since, "2026-11-28", HOLD)      # under again
    assert hold and since == "2026-11-28"
    hold, _ = cmp.park_hold_after_crash(1, 0.09, LINE, since, "2026-12-07", HOLD)          # 9 days
    assert hold
    hold, _ = cmp.park_hold_after_crash(1, 0.09, LINE, since, "2026-12-08", HOLD)          # 10 days
    assert not hold


def test_zero_hold_days_turns_the_rule_off():
    hold, since = cmp.park_hold_after_crash(1, 0.094, LINE, None, "2026-11-23", 0)
    assert not hold and since == "2026-11-23"


def test_garbage_clock_value_restarts_today_rather_than_crashing():
    hold, since = cmp.park_hold_after_crash(1, 0.05, LINE, "not-a-date", "2026-11-23", HOLD)
    assert hold and since == "2026-11-23"


def test_parker_park_leg_honours_the_hold(monkeypatch):
    """The park leg is skipped (and logged) under hold; nothing is bought."""
    from types import SimpleNamespace
    import src.portfolio.buyer as B
    b = B.PortfolioBuyer.__new__(B.PortfolioBuyer)
    b.cfg = SimpleNamespace(cash_yield_enabled=True, readonly=False, base_currency="EUR",
                            cash_yield_symbol="XEON",
                            compounder=SimpleNamespace(cash_buffer_pct=0.03, cash_park_min=5000.0))
    b.ib = object()
    b._open_buy_map = lambda: {}
    b._pending_buy_suggestion_map = lambda: {}
    parked = []
    b._park_cash = lambda amount: parked.append(amount)
    monkeypatch.setattr(B, "_ccy_cash", lambda ib, ccy: 1_500_000.0)   # far above the runway
    events = []
    monkeypatch.setattr(B.log, "info", lambda ev, **kw: events.append(ev))
    b._park_compounder_excess(10_000_000.0, 0.0, day_budget=150_000.0, crash_active=False, park_hold=True)
    assert parked == [] and "compounder_park_skipped_hold_after_crash" in events
    b._park_compounder_excess(10_000_000.0, 0.0, day_budget=150_000.0, crash_active=False, park_hold=False)
    assert len(parked) == 1 and parked[0] > 1_000_000
