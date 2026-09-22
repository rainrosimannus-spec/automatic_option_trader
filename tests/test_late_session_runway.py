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
        assert _late_session(ccy, cc.late_session_minutes,
                             cc.late_session_min_runway_minutes) is False, \
            f"{sym}: the runway floor must refuse a placement with {runway_s}s left"


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
              if window_start <= m < close_utc - cc.late_session_min_runway_minutes]
    assert usable, "a 15-min sub-grid must sample the window somewhere"
    assert (close_utc - max(usable)) > cc.late_session_min_runway_minutes
    # and the earliest pass gets nearly the whole window, not the tail the 2h grid happened to hit
    assert (close_utc - min(usable)) >= cc.late_session_minutes - 15


def test_every_mapped_currency_can_reach_its_window(at):
    """No venue may be structurally excluded: for each, some 15-min slot must satisfy the floor."""
    cc = CompounderConfig()
    for ccy in _MARKET_HOURS:
        tz_name, _open_h, close_h, _days = _MARKET_HOURS[ccy]
        assert close_h * 60 - cc.late_session_minutes < close_h * 60 - cc.late_session_min_runway_minutes, \
            f"{ccy}: runway floor swallows the whole window"


# ── the once-per-window latch ─────────────────────────────────────────────────

def _fire(monkeypatch, utc_iso):
    """Run job_portfolio_late_session_fill at a frozen instant; return whether it scanned."""
    import src.portfolio.scheduler as sch
    from src.portfolio.config import PortfolioConfig
    cfg = PortfolioConfig(); cfg.enabled = True
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
