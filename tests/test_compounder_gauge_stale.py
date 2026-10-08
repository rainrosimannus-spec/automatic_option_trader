"""
Crash-reserve market gauge (SPY) during a history-data outage (2026-10-08, HMDS farm broken
16h): the scan must carry the LAST KNOWN drawdown, not 0.0, and the dashboard must flag the
gauge stale. Also pins the snapshot-price picker the live fallback relies on.
"""
import math
from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from src.portfolio.compounder import gauge_drawdown_fallback, gauge_is_stale
from src.portfolio.connection import _snapshot_pick


def test_fallback_uses_last_known_percent_as_fraction():
    assert gauge_drawdown_fallback("12.5") == 0.125
    assert gauge_drawdown_fallback(None) == 0.0
    assert gauge_drawdown_fallback("garbage") == 0.0
    assert gauge_drawdown_fallback("-3") == 0.0


def test_stale_after_three_hours_or_unknown():
    now = datetime(2026, 10, 8, 20, 0, 0)
    fresh = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    old = (now - timedelta(hours=4)).isoformat(timespec="seconds")
    assert gauge_is_stale(fresh, now) is False
    assert gauge_is_stale(old, now) is True
    assert gauge_is_stale("", now) is True
    assert gauge_is_stale("not-a-date", now) is True


def test_snapshot_pick_prefers_last_then_close_then_mid_and_skips_nan():
    assert _snapshot_pick(NS(last=772.96, close=777.22, bid=1, ask=2)) == 772.96
    assert _snapshot_pick(NS(last=math.nan, close=777.22, bid=1, ask=2)) == 777.22
    assert _snapshot_pick(NS(last=math.nan, close=math.nan, bid=772.95, ask=772.97)) == 772.96
    assert _snapshot_pick(NS(last=math.nan, close=None, bid=math.nan, ask=5)) is None
    assert _snapshot_pick(NS(last=-1, close=0, bid=0, ask=0)) is None
