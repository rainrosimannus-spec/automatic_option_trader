"""Venue sessions (src.portfolio.venue_hours): the minute-exact static table and IBKR's calendar overlay.

The static closes below were copied from ContractDetails.liquidHours on 2026-10-16, one contract per
watchlist venue. The overlay is fed the same field: it is how the gates learn that Hong Kong is CLOSED
on Monday 2026-10-19 (Chung Yeung Day) or that Tel Aviv shuts at 13:50 on a Friday — things no static
table can know and that used to put orders into shut venues (stuck PendingSubmit, symbol paused 6 h).
"""
import datetime as dt
from types import SimpleNamespace

import pytest
import pytz

from src.portfolio import buyer as _buyer
from src.portfolio import venue_hours as vh
from src.portfolio.buyer import _market_open, _late_session, _aftermarket


@pytest.fixture(autouse=True)
def _clean_calendar():
    vh.reset_calendar()
    yield
    vh.reset_calendar()


@pytest.fixture
def at(monkeypatch):
    class _DT(dt.datetime):
        _now = None

        @classmethod
        def now(cls, tz=None):
            return cls._now.astimezone(tz) if tz else cls._now

    monkeypatch.setattr(_buyer, "datetime", _DT)

    def _set(utc_iso):
        _DT._now = dt.datetime.fromisoformat(utc_iso).replace(tzinfo=dt.timezone.utc)
    return _set


def _hm(sessions):
    return [(a.strftime("%H:%M"), b.strftime("%H:%M")) for a, b in sessions]


# ── the static table against IBKR's liquidHours of 2026-10-16 ─────────────────────────────

IBKR_CLOSES = {   # currency: (venue, last liquid minute, lunch break?)
    "USD": ("SMART", "16:00", False), "CAD": ("SMART", "16:00", False), "MXN": ("MEXI", "15:00", False),
    "EUR": ("AEB", "17:40", False), "CHF": ("EBS", "17:40", False), "GBP": ("LSE", "16:50", False),
    "NOK": ("OSE", "16:30", False), "DKK": ("CPH", "17:00", False), "ZAR": ("JSE", "17:15", False),
    "ILS": ("TASE", "17:25", False), "INR": ("NSE", "15:30", False), "KRW": ("KRX", "15:30", False),
    "JPY": ("TSEJ", "15:30", True), "HKD": ("SEHK", "16:10", True), "SGD": ("SGX", "17:16", True),
    "AUD": ("ASX", "16:11", False),
}


@pytest.mark.parametrize("ccy", sorted(IBKR_CLOSES))
def test_static_close_matches_ibkr(ccy):
    venue, close, lunch = IBKR_CLOSES[ccy]
    sessions = vh.static_sessions(ccy, dt.date(2026, 10, 15))          # a Thursday
    assert sessions and sessions[-1][1].strftime("%H:%M") == close, (ccy, venue, _hm(sessions))
    assert (len(sessions) == 2) is lunch, (ccy, _hm(sessions))


def test_every_table_currency_has_a_known_zone_and_weekday_week():
    for ccy, (tz_name, segments, days) in vh.MARKET_HOURS.items():
        pytz.timezone(tz_name)
        assert days == vh.MON_FRI, ccy
        for o, c in segments:
            assert vh.hhmm(o) < vh.hhmm(c), (ccy, o, c)


def test_weekend_is_shut_everywhere():
    for ccy in vh.MARKET_HOURS:
        assert vh.static_sessions(ccy, dt.date(2026, 10, 17)) == ()
        assert vh.static_sessions(ccy, dt.date(2026, 10, 18)) == ()


def test_unmapped_currency_is_none():
    assert vh.static_sessions("XXX", dt.date(2026, 10, 15)) is None


def test_tase_friday_early_close():
    assert _hm(vh.static_sessions("ILS", dt.date(2026, 10, 16))) == [("09:59", "13:50")]
    assert _hm(vh.static_sessions("ILS", dt.date(2026, 10, 15))) == [("09:59", "17:25")]


# ── the parser ────────────────────────────────────────────────────────────────────────────

ESLT_LIQUID = "20261016:0959-20261016:1350;20261017:CLOSED;20261018:CLOSED;20261019:0959-20261019:1725;20261020:0959-20261020:1725"
HK_LIQUID = ("20261016:0930-20261016:1200;20261016:1300-20261016:1610;20261017:CLOSED;20261018:CLOSED;"
             "20261019:CLOSED;20261020:0930-20261020:1200;20261020:1300-20261020:1610")
TSE_LIQUID = "20261016:0900-20261016:1130;20261016:1230-20261016:1530;20261017:CLOSED"


def test_parse_real_tase_string():
    cal = vh.parse_liquid_hours(ESLT_LIQUID, "Israel")
    assert _hm(cal["20261016"]) == [("09:59", "13:50")]
    assert cal["20261017"] == () and cal["20261018"] == ()
    assert _hm(cal["20261019"]) == [("09:59", "17:25")]
    assert cal["20261016"][0][0].tzinfo is not None


def test_parse_collects_lunch_segments_in_order():
    cal = vh.parse_liquid_hours(TSE_LIQUID, "Japan")
    assert _hm(cal["20261016"]) == [("09:00", "11:30"), ("12:30", "15:30")]


def test_parse_is_never_fatal():
    assert vh.parse_liquid_hours("", "Japan") == {}
    assert vh.parse_liquid_hours("garbage;20261016:0900-20261016:1130", "Japan") == {
        "20261016": vh.parse_liquid_hours("20261016:0900-20261016:1130", "Japan")["20261016"]}
    assert vh.parse_liquid_hours(TSE_LIQUID, "Not/AZone") == {}


# ── the overlay changes the gates' answers ────────────────────────────────────────────────

def _details(liquid, tz):
    return [SimpleNamespace(liquidHours=liquid, timeZoneId=tz)]


def test_hong_kong_holiday_shuts_the_gate(at):
    at("2026-10-19T03:00:00")                                   # Mon 11:00 HKT — a normal trading hour
    assert _market_open("HKD") is True                          # the static table cannot know better
    assert vh.note_venue_hours("HKD", "SEHK", _details(HK_LIQUID, "Hongkong")) == 5      # five dates listed
    assert _market_open("HKD") is False                         # Chung Yeung Day
    assert _late_session("HKD", 120) is False
    at("2026-10-20T03:00:00")                                   # Tuesday — overlay says open
    assert _market_open("HKD") is True


def test_overlay_only_speaks_for_days_it_knows(at):
    vh.note_venue_hours("HKD", "SEHK", _details(HK_LIQUID, "Hongkong"))
    at("2026-10-27T03:00:00")                                   # a Tuesday beyond the listed week
    assert _market_open("HKD") is True                          # static table


def test_tase_friday_from_the_overlay_drives_the_late_window(at):
    vh.note_venue_hours("ILS", "TASE", _details(ESLT_LIQUID, "Israel"))
    at("2026-10-16T09:30:00")                                   # Fri 12:30 Israel
    assert _late_session("ILS", 120) is True
    at("2026-10-16T10:45:00")                                   # 13:45 — 5 min from the 13:50 close
    assert _late_session("ILS", 120, 10) is False
    at("2026-10-16T11:00:00")                                   # 14:00 — shut
    assert _market_open("ILS") is False


def test_exchange_specific_beats_merged_and_merged_is_conservative(at):
    aeb = "20261026:0900-20261026:1740"
    ibis = "20261026:0900-20261026:1745"
    vse = "20261026:CLOSED"                                     # Austrian National Day
    vh.note_venue_hours("EUR", "AEB", _details(aeb, "MET"))
    vh.note_venue_hours("EUR", "IBIS", _details(ibis, "MET"))
    vh.note_venue_hours("EUR", "VSE", _details(vse, "MET"))
    at("2026-10-26T15:42:00")                                   # 16:42 CET (winter) — AEB shut at 17:40? no: 15:42 UTC = 16:42 CET
    assert _market_open("EUR", "VSE") is False                  # Vienna closed all day
    assert _market_open("EUR", "IBIS") is True
    assert _market_open("EUR") is True                          # merged: not everyone is closed
    at("2026-10-26T16:42:00")                                   # 17:42 CET — past AEB's 17:40, before IBIS's 17:45
    assert _market_open("EUR", "IBIS") is True
    assert _market_open("EUR") is False                         # merged takes the EARLIEST close
    assert _market_open("EUR", "AEB") is False


def test_overlay_does_not_touch_the_us_aftermarket(at):
    vh.note_venue_hours("USD", "SMART", _details("20261016:0930-20261016:1600", "US/Eastern"))
    at("2026-10-16T20:30:00")                                   # 16:30 ET
    assert _market_open("USD") is False
    assert _aftermarket("USD", 60) is True


def test_note_is_never_fatal():
    assert vh.note_venue_hours("HKD", "SEHK", None) == 0
    assert vh.note_venue_hours("HKD", "SEHK", [SimpleNamespace()]) == 0
    assert vh.note_venue_hours("", "SEHK", _details(HK_LIQUID, "Hongkong")) == 0


# ── the refresh ───────────────────────────────────────────────────────────────────────────

class _FakeIB:
    def __init__(self):
        self.calls = []

    def reqContractDetails(self, contract):
        self.calls.append(contract)
        return _details(HK_LIQUID if contract[2] == "HKD" else ESLT_LIQUID,
                        "Hongkong" if contract[2] == "HKD" else "Israel")


def test_refresh_notes_every_venue_once_then_sleeps_six_hours():
    ib = _FakeIB()
    venues = [("HKD", "SEHK", "700"), ("ILS", "TASE", "ESLT")]
    make = lambda sym, exch, ccy: (sym, exch, ccy)
    assert vh.refresh_calendar(ib, venues, contract_factory=make) == 2
    assert len(ib.calls) == 2
    assert vh.refresh_calendar(ib, venues, contract_factory=make) == 0      # too young
    assert len(ib.calls) == 2
    assert vh.calendar_age_s() is not None and vh.calendar_age_s() < 5
    assert vh.overlay_sessions("HKD", pytz.UTC.localize(dt.datetime(2026, 10, 19, 3))) == ()


def test_refresh_survives_a_broken_venue():
    class _IB:
        def reqContractDetails(self, c):
            if c[2] == "HKD":
                raise RuntimeError("farm down")
            return _details(ESLT_LIQUID, "Israel")
    assert vh.refresh_calendar(_IB(), [("HKD", "SEHK", "700"), ("ILS", "TASE", "ESLT")],
                               contract_factory=lambda s, e, c: (s, e, c)) == 1
