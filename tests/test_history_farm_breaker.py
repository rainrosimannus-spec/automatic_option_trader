"""A dead history farm must abort the scan and free the portfolio lock, not crawl the whole list.

2026-10-29: every bar request on the portfolio session timed out from 15:05; the scan took
34 s per name holding the lock, the buy executor waited 80 minutes, the ESLT order never went
out and the European window was lost."""
import time
from types import SimpleNamespace

import pytest

import src.portfolio.analyzer as an
import src.portfolio.scheduler as sch
from src.portfolio.analyzer import HistoryFarmDead, PortfolioAnalyzer


class SilentIB:
    """reqHistoricalData that runs to its timeout and returns nothing, like a dead farm."""
    def __init__(self, timeout_s):
        self.timeout_s, self.calls = timeout_s, 0

    def reqHistoricalData(self, *a, **k):
        self.calls += 1
        time.sleep(self.timeout_s)
        return []

    def qualifyContracts(self, c):
        return [c]


class RefusingIB(SilentIB):
    """Answers at once with an error — the farm is alive, it just has nothing for this name."""
    def reqHistoricalData(self, *a, **k):
        self.calls += 1
        raise RuntimeError("HMDS query returned no data")


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(an, "BAR_TIMEOUT_S", 0.02)
    _sleep = time.sleep
    monkeypatch.setattr(an.time, "sleep", lambda s: _sleep(min(s, 0.03)))   # shorten the pacing gap
    monkeypatch.setattr(an, "get_portfolio_lock", lambda: _NoLock())


class _NoLock:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _c():
    return SimpleNamespace(symbol="X", exchange="SMART", primaryExchange="NYSE")


def test_four_silent_names_in_a_row_raise(fast):
    a = PortfolioAnalyzer(SilentIB(0.02))
    for _ in range(an.FARM_DEAD_AFTER - 1):
        assert a._fetch_bars(_c(), 300) == []
    with pytest.raises(HistoryFarmDead):
        a._fetch_bars(_c(), 300)


def test_a_quick_refusal_is_not_silence(fast):
    a = PortfolioAnalyzer(RefusingIB(0.02))
    for _ in range(an.FARM_DEAD_AFTER + 2):
        assert a._fetch_bars(_c(), 300) is None          # never raises
    assert a._farm_silent_streak == 0


def test_an_answer_resets_the_streak(fast):
    a = PortfolioAnalyzer(SilentIB(0.02))
    for _ in range(an.FARM_DEAD_AFTER - 1):
        a._fetch_bars(_c(), 300)
    assert a._farm_silent_streak == an.FARM_DEAD_AFTER - 1
    a._note_farm_silence(answered=True, silent=False)
    assert a._farm_silent_streak == 0
    for _ in range(an.FARM_DEAD_AFTER - 1):
        a._fetch_bars(_c(), 300)                            # still under the threshold again


def test_analyze_stock_lets_the_signal_through(fast):
    a = PortfolioAnalyzer(SilentIB(0.02))
    a._farm_silent_streak = an.FARM_DEAD_AFTER - 1
    with pytest.raises(HistoryFarmDead):                   # not swallowed as portfolio_analysis_error
        a.analyze_stock("X", "SMART", "USD", tier="growth")


def test_scan_job_aborts_reconnects_once_and_reports_false(monkeypatch):
    calls = []
    monkeypatch.setattr(sch, "get_portfolio_lock", lambda: _NoLock())
    monkeypatch.setattr(sch, "get_portfolio_ib", lambda: object())
    monkeypatch.setattr(sch, "is_portfolio_connected", lambda: True)
    import src.portfolio.connection as conn
    monkeypatch.setattr(conn, "reconnect_portfolio", lambda: calls.append("reconnect"))

    class DeadBuyer:
        def __init__(self, ib, cfg): pass
        def run_scan(self): raise HistoryFarmDead("4 names silent")
    monkeypatch.setattr(sch, "PortfolioBuyer", DeadBuyer)
    monkeypatch.setattr(sch, "_last_farm_reconnect", 0.0)
    cfg = SimpleNamespace(enabled=True)
    assert sch.job_portfolio_scan(cfg) is False
    assert calls == ["reconnect"]
    assert sch.job_portfolio_scan(cfg) is False            # a second abort inside the window
    assert calls == ["reconnect"]                          # does not reconnect again

    class GoodBuyer(DeadBuyer):
        def run_scan(self): return []
    monkeypatch.setattr(sch, "PortfolioBuyer", GoodBuyer)
    assert sch.job_portfolio_scan(cfg) is True


def test_aborted_late_session_pass_gives_the_window_back():
    sch._late_session_served["GBP"] = "2026-10-29"
    sch._late_session_served["USD"] = "2026-10-29"
    sch._unlatch_late_session(["GBP"])
    assert "GBP" not in sch._late_session_served and sch._late_session_served["USD"] == "2026-10-29"
    sch._late_session_served.clear()
