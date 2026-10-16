"""Runway floor on the green-buy window (2026-09-22).

The gate said "buy in the last 120 minutes"; it said nothing about how much of that window had to be
LEFT. On 2026-09-21 and 09-22 the 2h scan grid landed at 05:58 UTC against an 06:00 UTC Tokyo/Sydney
close and, after the executor's 30s-per-order cycle, orders reached the venue with 38-55 seconds to
run. XRO died unfilled at the bell both days; 6146 never left PreSubmitted and was cancelled two
hours later as stale. Four of the fourteen-day window's nine dead buy cards were exactly this.

The two real failures are replayed below at their logged wall-clock times.

2026-10-16: the venue table became minute-exact (IBKR liquidHours, closing auction included), and that
re-dates every instant here. Sydney's last fill is 16:11 AEST (06:11 UTC in September), Tokyo's 15:30
JST (06:30 UTC), Hong Kong's 16:10 HKT (08:10 UTC), New York's 16:00 ET (20:00 UTC). Read against the
true closes the September XRO orders had 11-12 minutes (the closing auction), not 38 seconds, and the
6146 ones over 30 — those died of price and routing, not runway. The floor keeps its meaning: minutes
to the LAST moment IBKR says a limit can fill.
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
    with at("2026-09-22T06:07:00"):                       # 4 min before the 16:11 AEST close
        assert _late_session("AUD", 120, 5) is False


def test_window_still_open_with_runway_to_spare(at):
    with at("2026-09-22T05:50:00"):                       # 10 min left — over the 5-min floor
        assert _late_session("AUD", 120, 5) is True


def test_boundary_is_exclusive_at_the_floor(at):
    with at("2026-09-22T06:06:00"):                       # exactly 5 min left
        assert _late_session("AUD", 120, 5) is False
    with at("2026-09-22T06:05:59"):                       # a second more than the floor
        assert _late_session("AUD", 120, 5) is True


def test_floor_does_not_open_the_early_edge(at):
    """The floor must only trim the LATE edge — the window still opens 120 min before the close."""
    with at("2026-09-22T04:10:00"):                       # 121 min out: before the window
        assert _late_session("AUD", 120, 5) is False
    with at("2026-09-22T04:12:00"):                       # 119 min out: inside it
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

@pytest.mark.parametrize("sym,ccy,placed,runway_min", [
    ("XRO",  "AUD", "2026-09-22T05:59:22", 11.6),   # id=3480 — continuous trading had ended, the 16:10 auction had not
    ("6146", "JPY", "2026-09-22T05:59:05", 30.9),   # id=3479 — never left PreSubmitted: a routing death, not runway
    ("XRO",  "AUD", "2026-09-21T05:59:30", 11.5),   # id=3477
    ("6146", "JPY", "2026-09-21T05:59:01", 31.0),   # id=3476
])
def test_september_replays_re_read_against_the_true_closes(at, sym, ccy, placed, runway_min):
    """Against the hour-rounded table these looked like 30-60 s orders. Against IBKR's closes they
    had the runway above, so the 10-min floor rightly ADMITS them — the limit price (1.4% under) and
    the TSEJ routing are what killed them, and those have their own fixes."""
    cc = CompounderConfig()
    with at(placed):
        assert _late_session(ccy, cc.late_session_minutes) is True
        assert _late_session(ccy, cc.late_session_minutes, 10) is True, f"{sym}: {runway_min} min is over the floor"


@pytest.mark.parametrize("ccy,placed,left_min", [
    ("AUD", "2026-09-22T06:05:00", 6),    # 16:05 AEST — inside the closing-auction run-up
    ("JPY", "2026-09-22T06:25:00", 5),    # 15:25 JST — Tokyo's closing itayose
    ("HKD", "2026-09-22T08:03:00", 7),    # 16:03 HKT — closing auction
])
def test_the_true_tail_is_refused(at, ccy, placed, left_min):
    with at(placed):
        assert _late_session(ccy, 120) is True, "inside the window…"
        assert _late_session(ccy, 120, 10) is False, f"…but only {left_min} min from the last fill"


def test_only_the_us_has_an_afterhours_rescue():
    """Why refusing beats placing: for every other venue there is no post-close fallback to catch the
    deferred name. 2026-10-16: IBKR contract hours show an extended session for the US alone —
    Copenhagen/Amsterdam/Xetra/Zurich/London/Oslo/Toronto have tradingHours == liquidHours — so
    Europe and Canada get the same floor as Asia, and the after-market pass is US only."""
    for ccy in ("JPY", "AUD", "HKD", "INR", "ZAR", "ILS", "SGD", "CAD", "EUR", "GBP", "CHF", "NOK", "SEK", "DKK"):
        assert _outside_rth_ok(ccy) is False
        assert _aftermarket(ccy, 60) is False
    assert _outside_rth_ok("USD") is True


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
    """No venue may be structurally excluded: for each, some 15-min slot of a plain Monday must
    satisfy both the window and the floor (lunch breaks and early closes included)."""
    cc = CompounderConfig()
    for ccy in _MARKET_HOURS:
        hits = []
        for m in range(0, 36 * 60, 15):                    # Mon 2026-10-12 UTC into Tuesday (Sydney rolls)
            day, mm = ("2026-10-12", m) if m < 24 * 60 else ("2026-10-13", m - 24 * 60)
            with at(f"{day}T{mm // 60:02d}:{mm % 60:02d}:00"):
                if _late_session(ccy, cc.late_session_minutes, 10):
                    hits.append(m)
        assert hits, f"{ccy}: no 15-min slot satisfies window + floor"
        assert max(hits) - min(hits) >= cc.late_session_minutes - 10 - 30, f"{ccy}: window too short {hits}"   # 15-min grid trims up to 14 min at each end


# ── the once-per-window latch ─────────────────────────────────────────────────

def _fire(monkeypatch, utc_iso, enabled=True):
    """Run job_portfolio_late_session_fill at a frozen instant; return whether it scanned.

    The pass ships ON since 2026-10-16; enabled=False exercises the switch."""
    import src.portfolio.scheduler as sch
    from src.portfolio.config import PortfolioConfig, CompounderConfig
    cfg = PortfolioConfig(); cfg.enabled = True
    # Pydantic fields aren't plain class attributes, so override via a subclass and swap the
    # symbol the job resolves at call time (it imports CompounderConfig inside the function).
    class _Cfg(CompounderConfig):
        late_session_fill_pass: bool = enabled
        late_session_min_runway_minutes: int = 5
    monkeypatch.setattr("src.portfolio.config.CompounderConfig", _Cfg)
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
    assert _fire(monkeypatch, "2026-09-22T04:16:00") is True      # Sydney's window opened 04:11
    assert _fire(monkeypatch, "2026-09-22T04:31:00") is True      # Tokyo/Seoul open theirs at 04:30 — a NEW venue, not a re-poll
    assert _fire(monkeypatch, "2026-09-22T04:46:00") is False     # all three latched
    assert _fire(monkeypatch, "2026-09-22T05:46:00") is False     # still the same windows


def test_next_trading_day_re_arms(monkeypatch):
    assert _fire(monkeypatch, "2026-09-22T04:16:00") is True
    assert _fire(monkeypatch, "2026-09-23T04:16:00") is True      # new venue-local date


def test_a_different_venue_window_still_fires(monkeypatch):
    """Latching Tokyo/Sydney must not suppress the later US window on the same UTC day."""
    assert _fire(monkeypatch, "2026-09-22T04:16:00") is True      # AUD
    assert _fire(monkeypatch, "2026-09-22T18:30:00") is True      # USD/CAD


def test_never_fires_in_the_refused_tail(monkeypatch):
    """Inside a window but under the floor, nothing fires (Hong Kong masked off — its window is open then)."""
    import src.portfolio.scheduler as sch
    monkeypatch.setattr(sch, "_watchlist_currencies", lambda: {"AUD", "JPY"})
    assert _fire(monkeypatch, "2026-09-22T06:27:00") is False     # Sydney shut 06:11, Tokyo 3 min from its 06:30 close


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
    runway = 6 * 60 + 11 - fired_at                                # 06:11 UTC — Sydney's 16:11 AEST close
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
    assert _fire(monkeypatch, "2026-09-22T04:30:00") is True      # JPY/AUD — held


def test_one_pass_per_venue_window(monkeypatch):
    """Each venue gets exactly one pass, at the first 15-min tick after ITS window opens: Sydney
    (04:11), Tokyo (04:30), Hong Kong (06:10), Europe (13:40), London (13:50), US/Canada (18:00).
    Six passes a day for seven held currencies — before the minute-exact table the hour-rounded
    closes made Tokyo/Sydney and Europe/London coincide and it looked like four."""
    import src.portfolio.scheduler as sch
    monkeypatch.setattr(sch, "_watchlist_currencies", lambda: {"USD", "EUR", "JPY", "AUD", "HKD", "GBP", "CAD"})
    fired = [m for m in range(0, 24 * 60, 15)
             if _fire(monkeypatch, f"2026-09-22T{m // 60:02d}:{m % 60:02d}:00")]
    assert [f"{m // 60:02d}:{m % 60:02d}" for m in fired] == ["04:15", "04:30", "06:15", "13:45", "14:00", "18:00"]


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
    """No venue inherits another's hours: at 04:30 UTC only Tokyo/Sydney/Seoul are in window.
    (Seoul added 2026-10-04 with the KRW hours: 04:30 UTC is 13:30 KST, inside its last two hours.)"""
    cc = CompounderConfig()
    with at("2026-09-22T04:30:00"):
        inside = {c for c in _MARKET_HOURS
                  if _late_session(c, cc.late_session_minutes, 5)}
    assert inside == {"JPY", "AUD", "KRW"}, inside
    with at("2026-09-22T19:00:00"):                    # 15:00 ET
        inside = {c for c in _MARKET_HOURS
                  if _late_session(c, cc.late_session_minutes, 5)}
    assert inside == {"USD", "CAD", "MXN"}, inside     # Mexico City (added 2026-10-04) is 13:00, its last two hours


# ── the shipped state (2026-10-16): floor 10 min on no-after-hours venues, pass ON ─────────

def test_floor_and_pass_ship_on_for_venues_without_after_hours():
    """Measured 2026-09-22 and kept OFF then: in the sub-5-minute zone the US record was 5 FILLS vs 4
    deaths (closing auction). The 2026-10-16 evidence is ASIAN: Tencent/Pop Mart reached Hong Kong
    with 3-4 min left, 1.4% under the market, after the scan slipped behind a 16-min price update;
    every Asian fill in 30 days came from a scan with ≥20 min to run. So the floor applies where
    there is no after-market to catch a late order, and stays 0 where there is."""
    from src.portfolio.buyer import _runway_floor
    cc = CompounderConfig()
    assert cc.late_session_min_runway_minutes == 10
    assert cc.late_session_fill_pass is True
    for ccy in ("JPY", "AUD", "HKD", "SGD", "ILS", "KRW", "ZAR", "INR",
                "CAD", "EUR", "GBP", "CHF", "NOK", "SEK", "DKK"):     # no extended session on any of these
        assert _runway_floor(ccy, cc) == 10, ccy
    assert _runway_floor("USD", cc) == 0


def test_default_config_keeps_the_us_window_and_refuses_the_asian_tail(at):
    """The four 'dead' placements of September: the US ones are still ADMITTED (MA/BKNG filled at
    the same runway), the Tokyo/Sydney ones are now refused — there, a 40-second order is a lost day."""
    from src.portfolio.buyer import _runway_floor
    cc = CompounderConfig()
    for ccy, t, admitted in [("AUD", "2026-09-22T06:05:00", False), ("JPY", "2026-09-22T06:25:00", False),
                             ("USD", "2026-09-22T19:59:22", True), ("USD", "2026-09-21T19:59:01", True)]:
        with at(t):
            assert _late_session(ccy, cc.late_session_minutes, _runway_floor(ccy, cc)) is admitted, (ccy, t)


def test_asian_order_with_a_quarter_hour_left_is_still_placed(at):
    from src.portfolio.buyer import _runway_floor
    cc = CompounderConfig()
    with at("2026-10-16T07:44:00"):                        # 15:44 HKT, 26 min to the 16:10 auction end
        assert _late_session("HKD", cc.late_session_minutes, _runway_floor("HKD", cc)) is True
    with at("2026-10-16T07:56:00"):                        # 15:56 HKT — today's Tencent/Pop Mart placement: 14 min, ADMITTED
        assert _late_session("HKD", cc.late_session_minutes, _runway_floor("HKD", cc)) is True
    with at("2026-10-16T08:01:00"):                        # 16:01 HKT — 9 min, inside the closing auction
        assert _late_session("HKD", cc.late_session_minutes, _runway_floor("HKD", cc)) is False


def test_pass_can_be_switched_off(monkeypatch):
    assert _fire(monkeypatch, "2026-09-22T04:00:00", enabled=False) is False
