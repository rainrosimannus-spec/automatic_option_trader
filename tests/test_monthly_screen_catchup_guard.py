"""The startup catch-up for the monthly screen re-checks at fire time (2026-11-03: it queued
behind a Run-now screen and re-ran the whole screen the moment that one finished)."""
import json
from types import SimpleNamespace

from src.scheduler import jobs


def test_catchup_skips_when_a_screen_is_running(monkeypatch):
    called = []
    import src.portfolio.scheduler as ps
    monkeypatch.setattr(ps, "job_portfolio_monthly_screen", lambda cfg: called.append(cfg))
    import src.portfolio.screener_flag as sf
    monkeypatch.setattr(sf, "is_screener_running", lambda: True)
    jobs._catchup_monthly_screen_if_still_needed("cfg")
    assert called == []


def test_catchup_skips_after_a_success_this_month(monkeypatch, tmp_path):
    called = []
    import src.portfolio.scheduler as ps
    monkeypatch.setattr(ps, "job_portfolio_monthly_screen", lambda cfg: called.append(cfg))
    import src.portfolio.screener_flag as sf
    monkeypatch.setattr(sf, "is_screener_running", lambda: False)
    from datetime import datetime
    import pytz
    now = datetime.now(pytz.UTC)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "screener_last_run.json").write_text(json.dumps(
        {"status": "success", "run_date": now.strftime("%Y-%m-%d %H:%M UTC")}))
    monkeypatch.chdir(tmp_path)
    jobs._catchup_monthly_screen_if_still_needed("cfg")
    assert called == []


def test_catchup_runs_when_nothing_succeeded(monkeypatch, tmp_path):
    called = []
    import src.portfolio.scheduler as ps
    monkeypatch.setattr(ps, "job_portfolio_monthly_screen", lambda cfg: called.append(cfg))
    import src.portfolio.screener_flag as sf
    monkeypatch.setattr(sf, "is_screener_running", lambda: False)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "screener_last_run.json").write_text(json.dumps(
        {"status": "error", "run_date": "2026-11-03 03:07 UTC"}))
    monkeypatch.chdir(tmp_path)
    jobs._catchup_monthly_screen_if_still_needed("cfg")
    assert called == ["cfg"]
