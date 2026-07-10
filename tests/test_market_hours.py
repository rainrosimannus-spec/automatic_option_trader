"""Venue trading-session gate (`_market_open`).

A currency missing from the table falls through to "always open". That fallback is how NICE (ILS)
was ordered into a shut Tel Aviv exchange at 00:09 UTC on a Friday: the order cannot reach the venue,
rests in PendingSubmit, and the stuck-order detector cancels it and venue-blocks the symbol for 6h.
Every currency the watchlist can hold must be mapped, with its real trading days — TASE runs Sun–Thu.
"""
import datetime as dt

import pytest

from src.portfolio import buyer as _buyer
from src.portfolio.buyer import _MARKET_HOURS, _market_open


@pytest.fixture
def at(monkeypatch):
    """Freeze wall-clock to a given UTC instant, as `_market_open` sees it."""
    class _FrozenDT(dt.datetime):
        _now = None

        @classmethod
        def now(cls, tz=None):
            return cls._now.astimezone(tz) if tz else cls._now

    monkeypatch.setattr(_buyer, "datetime", _FrozenDT)

    def _set(utc_iso):
        _FrozenDT._now = dt.datetime.fromisoformat(utc_iso).replace(tzinfo=dt.timezone.utc)

    return _set


# Every currency the portfolio watchlist can hold. An unmapped one silently trades 24/7.
WATCHLIST_CURRENCIES = ["USD", "CAD", "EUR", "GBP", "JPY", "AUD", "HKD", "SGD", "ZAR", "ILS"]


@pytest.mark.parametrize("ccy", WATCHLIST_CURRENCIES)
def test_every_watchlist_currency_is_mapped(ccy):
    assert ccy in _MARKET_HOURS, f"{ccy} unmapped → _market_open() returns True around the clock"


def test_unknown_currency_stays_permissive(at):
    at("2026-07-10T00:17:00")
    assert _market_open("XXX") is True


# ── TASE trades Sunday–Thursday, not Monday–Friday ───────────────────────────────────────

def test_tase_closed_on_friday(at):
    at("2026-07-10T06:17:00")            # Fri 09:17 Israel — the second doomed NICE order
    assert _market_open("ILS") is False


def test_tase_open_on_sunday(at):
    at("2026-07-12T08:00:00")            # Sun 11:00 Israel
    assert _market_open("ILS") is True


def test_tase_open_midweek(at):
    at("2026-07-09T12:23:00")            # Thu 15:23 Israel — in-session (the real venue hang)
    assert _market_open("ILS") is True


def test_tase_closed_on_saturday(at):
    at("2026-07-11T08:00:00")
    assert _market_open("ILS") is False


# ── the Asia/Europe funding overlap that makes JIT un-parking impossible ──────────────────

def test_xetra_shut_during_tokyo_session(at):
    """Tokyo closes (06:00 UTC) before Xetra opens (07:00 UTC): a Japanese buy can never fund
    itself by selling the EUR park just-in-time. The runway must be restored during EU hours."""
    at("2026-07-10T00:17:00")
    assert _market_open("JPY") is True
    assert _market_open("EUR") is False


def test_xetra_open_during_european_session(at):
    at("2026-07-10T08:28:00")            # 10:28 Berlin
    assert _market_open("EUR") is True


def test_all_venues_shut_at_weekend(at):
    at("2026-07-11T10:00:00")            # Saturday
    for ccy in WATCHLIST_CURRENCIES:
        assert _market_open(ccy) is False, ccy


@pytest.mark.parametrize("ccy,utc,expected", [
    ("HKD", "2026-07-10T02:00:00", True),    # 10:00 Hong Kong
    ("HKD", "2026-07-10T09:00:00", False),   # 17:00 Hong Kong — shut
    ("AUD", "2026-07-10T01:00:00", True),    # 11:00 Sydney
    ("SGD", "2026-07-10T02:00:00", True),    # 10:00 Singapore
    ("SGD", "2026-07-10T00:17:00", False),   # 08:17 Singapore — pre-open
    ("ZAR", "2026-07-10T10:00:00", True),    # 12:00 Johannesburg
    ("ZAR", "2026-07-10T00:17:00", False),
    ("USD", "2026-07-10T14:30:00", True),    # 10:30 New York
    ("USD", "2026-07-10T00:17:00", False),
])
def test_session_boundaries(at, ccy, utc, expected):
    at(utc)
    assert _market_open(ccy) is expected
