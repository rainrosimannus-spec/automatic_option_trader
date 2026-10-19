"""Live price at order time: the last one-minute bar, only if it is fresh (2026-10-16, Ibiden ¥9,607
in the daily bar vs ¥9,780 live → a 0.7% discount became 2.5% and the order never came close)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.portfolio.buyer import _fresh_bar_price, _runway_floor


class _IB:
    def __init__(self, by_rth):
        self.by_rth = by_rth
        self.calls = []

    def reqHistoricalData(self, contract, **kw):
        self.calls.append(kw["useRTH"])
        v = self.by_rth.get(kw["useRTH"])
        if isinstance(v, Exception):
            raise v
        return v or []


def _bar(age_s, close):
    return SimpleNamespace(date=datetime.now(timezone.utc) - timedelta(seconds=age_s), close=close)


def test_fresh_rth_bar_is_used():
    ib = _IB({True: [_bar(120, 9780.0)]})
    price, age = _fresh_bar_price(ib, SimpleNamespace(symbol="4062"), "JPY")
    assert price == 9780.0 and 100 <= age <= 140
    assert ib.calls == [True]


def test_stale_bar_is_ignored():
    ib = _IB({True: [_bar(3600, 9607.0)], False: [_bar(3500, 9600.0)]})
    assert _fresh_bar_price(ib, SimpleNamespace(symbol="4062"), "JPY") is None
    assert ib.calls == [True, False]


def test_delayed_feed_bar_is_still_live():
    """2026-10-19: no real-time subscription outside the US, so an open venue's last bar is 10-16
    minutes old (Amsterdam 932 s, Singapore 632 s). At the old 600 s cap the refresh never fired and
    6857 went out at the morning-session close, 2.5% under the market. 16 minutes is accepted; 26 is
    not (lunch break / shut venue / farm lag)."""
    ib = _IB({True: [_bar(16 * 60, 34300.0)]})
    price, age = _fresh_bar_price(ib, SimpleNamespace(symbol="6857"), "JPY")
    assert price == 34300.0 and 940 <= age <= 980
    ib = _IB({True: [_bar(26 * 60, 34300.0)], False: [_bar(26 * 60, 34300.0)]})
    assert _fresh_bar_price(ib, SimpleNamespace(symbol="6857"), "JPY") is None


def test_falls_back_to_all_hours_bars_when_rth_has_none():
    ib = _IB({True: [], False: [_bar(60, 417.4)]})
    price, _ = _fresh_bar_price(ib, SimpleNamespace(symbol="700"), "HKD")
    assert price == 417.4


def test_request_failure_never_raises():
    ib = _IB({True: RuntimeError("timeout"), False: RuntimeError("timeout")})
    assert _fresh_bar_price(ib, SimpleNamespace(symbol="S68"), "SGD") is None


def test_london_bar_comes_back_in_pounds():
    ib = _IB({True: [_bar(30, 12450.0)]})
    price, _ = _fresh_bar_price(ib, SimpleNamespace(symbol="AZN"), "GBP")
    assert price == pytest.approx(124.50)


def test_naive_timestamp_is_treated_as_utc():
    bar = SimpleNamespace(date=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30), close=100.0)
    ib = _IB({True: [bar]})
    price, age = _fresh_bar_price(ib, SimpleNamespace(symbol="AAPL"), "USD")
    assert price == 100.0 and age < 60


def test_runway_floor_only_where_no_after_hours():
    cc = SimpleNamespace(late_session_min_runway_minutes=10)
    assert _runway_floor("HKD", cc) == 10
    assert _runway_floor("JPY", cc) == 10
    assert _runway_floor("USD", cc) == 0
    assert _runway_floor("EUR", cc) == 10     # Europe: no extended session (2026-10-16)
    assert _runway_floor("GBP", cc) == 10     # Europe: no extended session (2026-10-16)
    assert _runway_floor("XXX", SimpleNamespace(late_session_min_runway_minutes=-3)) == 0
