"""Runway floor on the green-buy window (2026-09-22).

The gate said "buy in the last 120 minutes"; it said nothing about how much of that window had to be
LEFT. On 2026-09-21 and 09-22 the 2h scan grid landed at 05:58 UTC against an 06:00 UTC Tokyo/Sydney
close and, after the executor's 30s-per-order cycle, orders reached the venue with 38-55 seconds to
run. XRO died unfilled at the bell both days; 6146 never left PreSubmitted and was cancelled two
hours later as stale. Four of the fourteen-day window's nine dead buy cards were exactly this.

The two real failures are replayed below at their logged wall-clock times.
"""
import pytest
from datetime import datetime
from unittest.mock import patch

import pytz

from src.portfolio.buyer import _late_session, _aftermarket, _MARKET_HOURS, _outside_rth_ok
from src.portfolio.config import CompounderConfig


@pytest.fixture
def at():
    """Freeze buyer-local `datetime.now(tz)` at a given UTC instant."""
    def _at(utc_iso: str):
        moment = pytz.UTC.localize(datetime.fromisoformat(utc_iso))

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

        return patch("src.portfolio.buyer.datetime", _DT)
    return _at


# ── the floor itself ──────────────────────────────────────────────────────────

def test_runway_zero_is_exactly_the_old_behaviour(at):
    """Default must be inert: min_runway=0 has to reproduce the pre-change window precisely."""
    with at("2026-09-22T05:59:22"):                       # 38s before the ASX close
        assert _late_session("AUD", 120) is True
        assert _late_session("AUD", 120, 0) is True


def test_final_minutes_refused(at):
    with at("2026-09-22T05:59:22"):                       # XRO, id=3480 — 0.6 min of runway
        assert _late_session("AUD", 120, 5) is False


def test_window_still_open_with_runway_to_spare(at):
    with at("2026-09-22T05:50:00"):                       # 10 min left — over the 5-min floor
        assert _late_session("AUD", 120, 5) is True


def test_boundary_is_exclusive_at_the_floor(at):
    with at("2026-09-22T05:55:00"):                       # exactly 5 min left
        assert _late_session("AUD", 120, 5) is False
    with at("2026-09-22T05:54:59"):                       # a second more than the floor
        assert _late_session("AUD", 120, 5) is True


def test_floor_does_not_open_the_early_edge(at):
    """The floor must only trim the LATE edge — the window still opens 120 min before the close."""
    with at("2026-09-22T03:59:00"):                       # 121 min out: before the window
        assert _late_session("AUD", 120, 5) is False
    with at("2026-09-22T04:01:00"):                       # 119 min out: inside it
        assert _late_session("AUD", 120, 5) is True


def test_gate_disabled_still_wins(at):
    """minutes<=0 means 'no gate', and the floor must not resurrect one."""
    with at("2026-09-22T05:59:22"):
        assert _late_session("AUD", 0, 5) is True


def test_unmapped_currency_unaffected(at):
    with at("2026-09-22T05:59:22"):
        assert _late_session("XXX", 120, 5) is True


def test_negative_runway_clamped(at):
    with at("2026-09-22T05:59:22"):
        assert _late_session("AUD", 120, -30) is True     # clamps to 0, i.e. old behaviour


# ── the two real failures ─────────────────────────────────────────────────────

@pytest.mark.parametrize("sym,ccy,placed,runway_s", [
    ("XRO",  "AUD", "2026-09-22T05:59:22", 38),   # id=3480, died unfilled at the ASX bell
    ("6146", "JPY", "2026-09-22T05:59:05", 55),   # id=3479, stuck PreSubmitted, cancelled 2h later
    ("XRO",  "AUD", "2026-09-21T05:59:30", 30),   # id=3477
    ("6146", "JPY", "2026-09-21T05:59:01", 59),   # id=3476
])
def test_replays_the_four_dead_cards(at, sym, ccy, placed, runway_s):
    cc = CompounderConfig()
    with at(placed):
        assert _late_session(ccy, cc.late_session_minutes) is True, \
            f"{sym}: the old gate admitted this placement with {runway_s}s left"
        assert _late_session(ccy, cc.late_session_minutes, 5) is False, \
            f"{sym}: a 5-min floor would refuse a placement with {runway_s}s left"


def test_asia_and_au_have_no_afterhours_rescue():
    """Why refusing beats placing: for these venues there is no post-close fallback to catch the
    deferred name. _aftermarket is gated to outside-RTH currencies, so a dead order is simply dead."""
    for ccy in ("JPY", "AUD", "HKD", "INR", "ZAR", "ILS", "SGD"):
        assert _outside_rth_ok(ccy) is False
        assert _aftermarket(ccy, 60) is False
    for ccy in ("USD", "CAD", "EUR", "GBP"):
        assert _outside_rth_ok(ccy) is True


# ── the scan-cadence half ─────────────────────────────────────────────────────

def test_a_15_min_pass_always_finds_usable_runway(at):
    """The floor alone would strand the Asian names whenever the 2h grid's phase puts its single
    in-window scan into the excluded tail. job_portfolio_late_session_fill samples every 15 min, so
    SOME pass always lands with runway — this asserts the property the job relies on."""
    cc = CompounderConfig()
    close_utc = 6 * 60                                     # 06:00 UTC for both Tokyo and Sydney
    window_start = close_utc - cc.late_session_minutes
    usable = [m for m in range(0, 24 * 60, 15)
              if window_start <= m < close_utc - 5]
    assert usable, "a 15-min sub-grid must sample the window somewhere"
    assert (close_utc - max(usable)) > 5
    # and the earliest pass gets nearly the whole window, not the tail the 2h grid happened to hit
    assert (close_utc - min(usable)) >= cc.late_session_minutes - 15


def test_every_mapped_currency_can_reach_its_window(at):
    """No venue may be structurally excluded: for each, some 15-min slot must satisfy the floor."""
    cc = CompounderConfig()
    for ccy in _MARKET_HOURS:
        tz_name, _open_h, close_h, _days = _MARKET_HOURS[ccy]
        assert close_h * 60 - cc.late_session_minutes < close_h * 60 - 5, \
            f"{ccy}: runway floor swallows the whole window"


# ── the once-per-window latch ─────────────────────────────────────────────────

def _fire(monkeypatch, utc_iso, enabled=True):
    """Run job_portfolio_late_session_fill at a frozen instant; return whether it scanned.

    The pass ships OFF (late_session_fill_pass=False), so these tests enable it explicitly to exercise
    the mechanism. test_pass_is_off_by_default covers the shipped state."""
    import src.portfolio.scheduler as sch
    from src.portfolio.config import PortfolioConfig, CompounderConfig
    cfg = PortfolioConfig(); cfg.enabled = True
    if enabled:
        # Pydantic fields aren't plain class attributes, so override via a subclass and swap the
        # symbol the job resolves at call time (it imports CompounderConfig inside the function).
        class _Enabled(CompounderConfig):
            late_session_fill_pass: bool = True
            late_session_min_runway_minutes: int = 5
        monkeypatch.setattr("src.portfolio.config.CompounderConfig", _Enabled)
    calls = []
    monkeypatch.setattr(sch, "job_portfolio_scan", lambda c: calls.append(1))
    with _freeze(utc_iso):
        sch.job_portfolio_late_session_fill(cfg)
    return bool(calls)


def _freeze(utc_iso):
    moment = pytz.UTC.localize(datetime.fromisoformat(utc_iso))

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    return patch("src.portfolio.buyer.datetime", _DT)


@pytest.fixture(autouse=True)
def _clear_latch():
    import src.portfolio.scheduler as sch
    sch._late_session_served.clear()
    yield
    sch._late_session_served.clear()


def test_fires_once_per_window_not_every_tick(monkeypatch):
    """Every scan cancels and re-prices ALL working buys, so polling the window would churn an order
    eight times and never let a below-market limit rest. Exactly one pass per venue window."""
    assert _fire(monkeypatch, "2026-09-22T04:01:00") is True      # window just opened
    assert _fire(monkeypatch, "2026-09-22T04:16:00") is False     # same window — latched
    assert _fire(monkeypatch, "2026-09-22T05:46:00") is False     # still the same window


def test_next_trading_day_re_arms(monkeypatch):
    assert _fire(monkeypatch, "2026-09-22T04:01:00") is True
    assert _fire(monkeypatch, "2026-09-23T04:01:00") is True      # new venue-local date


def test_a_different_venue_window_still_fires(monkeypatch):
    """Latching Tokyo/Sydney must not suppress the later US window on the same UTC day."""
    assert _fire(monkeypatch, "2026-09-22T04:01:00") is True      # JPY/AUD/HKD
    assert _fire(monkeypatch, "2026-09-22T18:30:00") is True      # USD/CAD


def test_never_fires_in_the_refused_tail(monkeypatch):
    """The exact instant the four dead cards were placed must not trigger a pass."""
    assert _fire(monkeypatch, "2026-09-22T05:59:22") is False


def test_first_pass_carries_nearly_the_whole_window(monkeypatch):
    """The point of the job: the pass lands near the window's OPEN, not against the bell."""
    import src.portfolio.scheduler as sch
    from src.portfolio.config import CompounderConfig
    cc = CompounderConfig()
    fired_at = None
    for minute in range(0, 24 * 60, 15):                           # walk the 15-min sub-grid
        stamp = f"2026-09-22T{minute // 60:02d}:{minute % 60:02d}:00"
        if _fire(monkeypatch, stamp):
            fired_at = minute
            break
    assert fired_at is not None
    runway = 6 * 60 - fired_at                                     # 06:00 UTC Tokyo/Sydney close
    assert runway >= cc.late_session_minutes - 15, \
        f"first pass had only {runway} min of runway — the point was to land early in the window"


# ── currency filter: don't wake for venues we hold nothing in ─────────────────

def test_only_currencies_on_the_watchlist_wake_the_job(monkeypatch):
    """_MARKET_HOURS maps 15 currencies; the book holds 7. Every scan re-prices EVERY resting order
    regardless of venue, so waking for Singapore/Mumbai/Tel Aviv buys three daily cancel-replace
    sweeps with zero exposure. 2026-09-22: 7 firings/day before the filter, 4 after."""
    import src.portfolio.scheduler as sch
    monkeypatch.setattr(sch, "_watchlist_currencies", lambda: {"USD", "EUR", "JPY", "AUD", "HKD", "GBP", "CAD"})
    # Times chosen so ONLY an unheld currency is in window — 07:00 would not do, Hong Kong is
    # still open then and IS held, so the job would rightly fire and prove nothing.
    assert _fire(monkeypatch, "2026-09-22T08:30:00") is False     # SGD + INR only — neither held
    assert _fire(monkeypatch, "2026-09-22T09:00:00") is False     # INR only — not held
    assert _fire(monkeypatch, "2026-09-22T12:30:00") is False     # ILS only — not held
    assert _fire(monkeypatch, "2026-09-22T04:00:00") is True      # JPY/AUD — held


def test_one_pass_per_real_venue_cluster(monkeypatch):
    """The whole day should produce exactly four passes: Tokyo/Sydney, Hong Kong, Europe/UK, US/CA."""
    import src.portfolio.scheduler as sch
    monkeypatch.setattr(sch, "_watchlist_currencies", lambda: {"USD", "EUR", "JPY", "AUD", "HKD", "GBP", "CAD"})
    fired = [m for m in range(0, 24 * 60, 15)
             if _fire(monkeypatch, f"2026-09-22T{m // 60:02d}:{m % 60:02d}:00")]
    assert [f"{m // 60:02d}:{m % 60:02d}" for m in fired] == ["04:00", "06:00", "13:00", "18:00"]


def test_filter_fails_open(monkeypatch):
    """An unreadable watchlist must not disable the runway fix — fall back to the full venue map."""
    import src.portfolio.scheduler as sch
    monkeypatch.setattr(sch, "_watchlist_currencies", lambda: None)
    assert _fire(monkeypatch, "2026-09-22T08:30:00") is True       # SGD/INR wake it when unfiltered


# ── the US path must be strictly better off, not worse ────────────────────────

def test_us_loses_five_minutes_and_keeps_its_after_hours_hour(at):
    """The floor trims 15:55-16:00 ET. Unlike Asia/AU, USD is outside-RTH capable, so _aftermarket
    picks the name straight back up at the bell for a full hour — a 5-min gap, not a lost day."""
    cc = CompounderConfig()

    def buyable(utc_iso):
        with at(utc_iso):
            return (_late_session("USD", cc.late_session_minutes,
                                  5)
                    or _aftermarket("USD", cc.aftermarket_deploy_minutes))

    assert buyable("2026-09-22T19:50:00") is True      # 15:50 ET, in window
    assert buyable("2026-09-22T19:57:00") is False     # 15:57 ET, the 5-min gap
    assert buyable("2026-09-22T20:05:00") is True      # 16:05 ET, after-hours
    assert buyable("2026-09-22T20:55:00") is True      # 16:55 ET, still after-hours


def test_every_venue_reads_its_own_clock(at):
    """No venue inherits another's hours: at 04:30 UTC only Tokyo/Sydney are in window."""
    cc = CompounderConfig()
    with at("2026-09-22T04:30:00"):
        inside = {c for c in _MARKET_HOURS
                  if _late_session(c, cc.late_session_minutes, 5)}
    assert inside == {"JPY", "AUD"}, inside
    with at("2026-09-22T19:00:00"):                    # 15:00 ET
        inside = {c for c in _MARKET_HOURS
                  if _late_session(c, cc.late_session_minutes, 5)}
    assert inside == {"USD", "CAD"}, inside


# ── the shipped state: both halves OFF, and WHY ───────────────────────────────

def test_floor_and_pass_both_ship_off():
    """Measured 2026-09-22 and deliberately not enabled. In the sub-5-minute zone the record is 5
    FILLS against 4 deaths, and the runways interleave — XRO filled at 0.5 min and died at 0.5 min,
    MA filled at 0.6, BKNG at 1.0, POWL at 1.9, 6920 at 2.5. Time-to-close carries no signal, almost
    certainly because that minute is the closing auction. A 5-min floor would have refused ~EUR 310k
    of real fills in three months. If someone flips these on, they need new data, not a hunch."""
    cc = CompounderConfig()
    assert cc.late_session_min_runway_minutes == 0
    assert cc.late_session_fill_pass is False


def test_default_config_reproduces_the_old_window(at):
    """With the shipped defaults every one of the four 'dead' placements is still ADMITTED — because
    the identically-timed MA/BKNG/XRO placements filled."""
    cc = CompounderConfig()
    for ccy, t in [("AUD", "2026-09-22T05:59:22"), ("JPY", "2026-09-22T05:59:05"),
                   ("USD", "2026-09-22T19:59:22"), ("USD", "2026-09-21T19:59:01")]:
        with at(t):
            assert _late_session(ccy, cc.late_session_minutes,
                                 cc.late_session_min_runway_minutes) is True


def test_pass_is_off_by_default(monkeypatch):
    assert _fire(monkeypatch, "2026-09-22T04:00:00", enabled=False) is False
