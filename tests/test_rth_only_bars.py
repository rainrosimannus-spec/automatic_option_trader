"""Daily bars feeding the analyzer (fair-price inputs) and the crash gauge are REGULAR-HOURS only.

2026-11-28, ahead of IBKR's 23x5 US stock hours (2026-12-06): with extended hours in, a thin
overnight print sets a phantom 52-week high/low on a small name (reads cheaper than it is → bought
above fair), and the gauge's "today" bar exists from the first overnight print (21:00 ET the evening
before) so the crash switch flipped on and off between the 08:10 and 10:10 UTC scans. Both now pass
useRTH=True; a flip back to False must fail here."""
import time
from types import SimpleNamespace

import src.portfolio.analyzer as an
import src.portfolio.connection as conn


class _NoLock:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _RecordingIB:
    def __init__(self):
        self.kwargs: list[dict] = []

    def qualifyContracts(self, c):
        return [c]

    def reqHistoricalData(self, *a, **k):
        self.kwargs.append(k)
        return [SimpleNamespace(close=100.0 + i, high=101.0 + i, low=99.0 + i, open=100.0 + i,
                                date="2026-01-01", volume=1) for i in range(60)]

    def sleep(self, *a):
        pass


def test_analyzer_daily_bars_are_regular_hours_only(monkeypatch):
    monkeypatch.setattr(an, "get_portfolio_lock", lambda: _NoLock())
    monkeypatch.setattr(an.time, "sleep", lambda s: None)
    a = an.PortfolioAnalyzer.__new__(an.PortfolioAnalyzer)
    a.ib = _RecordingIB()
    a._note_farm_silence = lambda *x, **k: None
    bars = a._fetch_bars(SimpleNamespace(symbol="X", exchange="SMART"), 260)
    assert bars and len(bars) == 60
    assert a.ib.kwargs and all(k["useRTH"] is True for k in a.ib.kwargs)
    assert all(k["barSizeSetting"] == "1 day" for k in a.ib.kwargs)


def test_crash_gauge_price_is_regular_hours_only(monkeypatch):
    ib = _RecordingIB()
    monkeypatch.setattr(conn, "get_portfolio_ib", lambda: ib)
    monkeypatch.setattr(conn, "_ensure_event_loop", lambda: None)
    monkeypatch.setattr(conn, "get_portfolio_lock", lambda: _NoLock())
    px = conn.get_portfolio_stock_price("SPY", exchange="SMART", currency="USD")
    assert px == 159.0                                     # last bar's close
    assert ib.kwargs and all(k["useRTH"] is True for k in ib.kwargs)
