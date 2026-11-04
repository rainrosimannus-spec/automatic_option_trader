"""Per-run screener archive: before-snapshot, results, status, retention (2026-11-04)."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from src.portfolio import screener_runs as sr


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "config" / "screened_universe.yaml").write_text("growth: [A]\n")
    (tmp_path / "config" / "options_universe.yaml").write_text("- A\n")
    (tmp_path / "tools" / "discovered_pool.yaml").write_text("breakthrough: []\n")
    (tmp_path / "data" / "screener_last_run.json").write_text('{"status": "success"}')
    monkeypatch.setattr(sr, "_dump_watchlist", lambda: [
        {"symbol": "A", "tier": "growth", "pending_removal": False, "screened_at": datetime(2026, 11, 3, 1, 51)},
        {"symbol": "B", "tier": "dividend", "pending_removal": True, "screened_at": None},
    ])
    return tmp_path


def test_start_run_snapshots_files_and_watchlist(workdir):
    run_dir = sr.start_run(datetime(2026, 11, 4, 5, 20, 37))
    assert run_dir == Path("data/screener_runs/20261104-052037")
    before = run_dir / "before"
    assert (before / "screened_universe.yaml").read_text() == "growth: [A]\n"
    assert (before / "options_universe.yaml").exists()
    assert (before / "discovered_pool.yaml").exists()
    assert (before / "screener_last_run.json").exists()
    assert not (before / "durability_index.yaml").exists()      # absent file: skipped, no crash
    rows = json.loads((before / "watchlist.json").read_text())
    assert [r["symbol"] for r in rows] == ["A", "B"]
    assert rows[0]["screened_at"] == "2026-11-03T01:51:00"
    st = sr.read_status(run_dir)
    assert st["status"] == "running" and st["watchlist_rows"] == 2
    assert "config/durability_index.yaml" not in st["snapshot_files"]
    assert (Path("data/screener_runs") / ".gitignore").read_text() == "*\n"


def test_save_results_handles_dataclasses_and_nan(workdir):
    from tools.screen_universe import StockScore
    run_dir = sr.start_run(datetime(2026, 11, 4, 5, 20, 37))
    a = StockScore(symbol="A", tier="growth", portfolio_score=70.0, price=float("nan"))
    b = StockScore(symbol="B", tier="breakthrough", portfolio_score=55.0)

    class Screener:
        _rejects = [{"symbol": "C", "category": "growth_gate", "detail": "x", "score": 40.0}]
        _reject_counts = {"growth_gate": 1}
        _breakthrough_proposed = ["B"]
        _breakthrough_graduated = []
        _durability = {"picks": ["A"]}

    sr.save_results(run_dir, Screener(), [a, b], [b], [a, b])
    res = json.loads((run_dir / "results.json").read_text())
    assert [s["symbol"] for s in res["selected"]] == ["A", "B"]
    assert res["options_universe"] == ["B"]
    assert res["scores"][0]["price"] is None                     # NaN → null, valid JSON
    assert res["scores"][0]["portfolio_score"] == 70.0
    assert res["rejects"][0]["symbol"] == "C" and res["reject_counts"] == {"growth_gate": 1}
    assert res["durability"] == {"picks": ["A"]}


def _mk(name: str, status: str) -> Path:
    p = Path("data/screener_runs") / name
    (p / "before").mkdir(parents=True)
    (p / "status.json").write_text(json.dumps({"status": status}))
    return p


def test_success_prunes_failures_and_keeps_newest_earlier_success(workdir):
    _mk("20261001-000000", "success")
    _mk("20261102-220500", "success")      # the broken "success" of 11-02: still the newest earlier one
    _mk("20261103-015300", "error")
    _mk("20261103-210800", "error")
    run_dir = sr.start_run(datetime(2026, 11, 4, 5, 20, 37))
    sr.finish_run(run_dir, "success")
    names = [p.name for p in sr.list_runs()]
    assert names == ["20261102-220500", "20261104-052037"]
    assert sr.read_status(run_dir)["status"] == "success"


def test_error_keeps_everything(workdir):
    _mk("20261102-220500", "success")
    _mk("20261103-015300", "error")
    run_dir = sr.start_run(datetime(2026, 11, 4, 5, 20, 37))
    sr.finish_run(run_dir, "error", error="collapsed")
    assert [p.name for p in sr.list_runs()] == ["20261102-220500", "20261103-015300", "20261104-052037"]
    assert sr.read_status(run_dir)["error"] == "collapsed"
