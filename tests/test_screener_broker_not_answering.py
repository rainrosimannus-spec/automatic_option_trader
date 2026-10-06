"""The monthly screen must stop within a minute when the broker connection answers nothing.

2026-10-05: the scheduled screen ran 2h45m and scored 0 of 579 names. Every name raised a bare
TimeoutError after 15 s — the job's thread was bound to the event loop of a connection that
Sunday's gateway restart had replaced, so replies went to a loop nobody ran. The screen held the
portfolio lock the whole time (no reconnect possible) and then reported "likely FMP API failure".
Pinned here: the loop is re-bound before the first broker call, a dead connection is detected by a
probe and by a run of timeouts, and the job reconnects and retries once.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
su = pytest.importorskip("screen_universe")
import src.portfolio.scheduler as sched


class _DeadIB:
    """Accepts every request, answers none — what a stale event loop looks like from the caller."""
    def qualifyContracts(self, *c):
        raise TimeoutError()


def test_probe_stops_the_screen_before_a_single_name_is_scored(monkeypatch):
    scored = []
    monkeypatch.setattr(su.UniverseScreener, "_score_stock",
                        lambda self, symbol, exchange, currency: scored.append(symbol))
    with pytest.raises(su.BrokerNotAnswering, match="not answering"):
        su.UniverseScreener(_DeadIB()).screen_all()
    assert scored == []


def test_eight_timeouts_in_a_row_stop_the_screen_but_other_errors_do_not():
    scr = su.UniverseScreener(ib=None)
    scr._rejects = []
    for i in range(7):
        scr._note_score_error(f"S{i}", TimeoutError(), "growth")
    scr._note_score_error("ODD", ValueError("bad json"), "growth")        # a real error resets the run
    for i in range(7):
        scr._note_score_error(f"T{i}", TimeoutError(), "growth")
    with pytest.raises(su.BrokerNotAnswering, match="8 names in a row"):
        scr._note_score_error("T7", TimeoutError(), "growth")
    assert scr._reject_counts["error"] == 16


def test_per_name_error_names_the_exception_type(capsys):
    scr = su.UniverseScreener(ib=None)
    scr._rejects = []
    scr._note_score_error("AAPL", TimeoutError(), "growth")
    assert "Error: TimeoutError:" in capsys.readouterr().out      # was "Error: " — nothing to go on


@pytest.fixture
def quiet_job(monkeypatch, tmp_path):
    """The screen job with its edges replaced: no broker, no alerts, run-log written under tmp."""
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    calls = SimpleNamespace(loop_bound=0, reconnects=0)
    monkeypatch.setattr(sched, "_ensure_event_loop", lambda: setattr(calls, "loop_bound", calls.loop_bound + 1))
    monkeypatch.setattr(sched, "get_portfolio_ib", lambda: object())
    monkeypatch.setattr(sched, "reconnect_portfolio", lambda: setattr(calls, "reconnects", calls.reconnects + 1))
    import src.portfolio.connection as conn
    monkeypatch.setattr(conn, "get_cached_portfolio_account", lambda: {})
    import src.core.alerts as alerts
    monkeypatch.setattr(alerts, "get_alert_manager",
                        lambda: SimpleNamespace(critical=lambda *a, **k: None, rescreen_alert=lambda *a, **k: None))
    import src.portfolio.screener_flag as flag
    monkeypatch.setattr(flag, "set_running_flag", lambda: None)
    monkeypatch.setattr(flag, "clear_running_flag", lambda: None)
    return calls


def _dead_screen(self, **kw):
    raise su.BrokerNotAnswering("portfolio IBKR connection not answering — nothing scored")


def test_screen_job_binds_the_loop_first_and_asks_for_a_retry_when_the_broker_is_dead(quiet_job, monkeypatch):
    monkeypatch.setattr(su.UniverseScreener, "screen_all", _dead_screen)
    cfg = SimpleNamespace(enabled=True, rescreen_regions="", rescreen_min_market_cap=1e9,
                          tier_count_growth=60, tier_count_dividend=15, tier_count_breakthrough=25)
    with pytest.raises(sched._RetryAfterReconnect):
        sched._job_portfolio_monthly_screen(cfg)
    assert quiet_job.loop_bound == 1
    assert not Path("data/screener_last_run.json").exists()          # first failure is not final


def test_second_attempt_failing_is_recorded_as_the_run_result(quiet_job, monkeypatch):
    monkeypatch.setattr(su.UniverseScreener, "screen_all", _dead_screen)
    cfg = SimpleNamespace(enabled=True, rescreen_regions="", rescreen_min_market_cap=1e9,
                          tier_count_growth=60, tier_count_dividend=15, tier_count_breakthrough=25)
    sched._job_portfolio_monthly_screen(cfg, attempt=2)               # no raise: the normal error path
    import json
    rec = json.loads(Path("data/screener_last_run.json").read_text())
    assert rec["status"] == "error" and "not answering" in rec["error"]


def test_wrapper_reconnects_and_runs_the_screen_once_more(quiet_job, monkeypatch):
    attempts = []

    def fake_inner(cfg, attempt=1):
        attempts.append(attempt)
        if attempt == 1:
            raise sched._RetryAfterReconnect("dead")

    monkeypatch.setattr(sched, "_job_portfolio_monthly_screen", fake_inner)
    sched.job_portfolio_monthly_screen(SimpleNamespace(enabled=True))
    assert attempts == [1, 2]
    assert quiet_job.reconnects == 1


def test_review_job_binds_the_loop_before_touching_the_broker(quiet_job, monkeypatch):
    seen = []
    monkeypatch.setattr(sched, "_review_existing_holdings_monthly", lambda *a, **k: seen.append(a) or [])
    monkeypatch.setattr(sched, "_get_current_screened_symbols", lambda: set())
    monkeypatch.setattr(sched, "_get_current_screened_tiers", lambda: {})
    try:
        sched.job_portfolio_monthly_review(SimpleNamespace(enabled=True))
    except Exception:
        pass                                   # whatever follows the review is not under test
    assert quiet_job.loop_bound >= 1 and seen
