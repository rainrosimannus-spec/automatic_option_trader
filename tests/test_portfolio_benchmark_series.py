"""
/portfolio chart benchmarks: BRK-B and SPY are both anchored to 0% at the chart's
first label, forward-filled over non-trading days, and served from one endpoint.
"""
import asyncio
import json

from src.web.routes.portfolio import _benchmark_return_series, brkb_data_endpoint
import src.web.routes.portfolio as portfolio_routes
from src.portfolio.connection import BENCHMARKS, BENCHMARK_CACHE_KEYS


def test_anchor_and_forward_fill():
    history = {"2026-09-18": 100.0, "2026-09-21": 110.0}  # Fri, Mon
    labels = ["2026-09-18", "2026-09-19", "2026-09-20", "2026-09-21"]
    out = _benchmark_return_series(history, labels)
    assert out == [0.0, 0.0, 0.0, 10.0]


def test_empty_history_or_labels_gives_empty_series():
    assert _benchmark_return_series({}, ["2026-09-18"]) == []
    assert _benchmark_return_series({"2026-09-18": 1.0}, []) == []
    # a label before any close → no anchor → empty, never a division by None
    assert _benchmark_return_series({"2026-09-21": 1.0}, ["2026-09-18"]) == []


def test_benchmarks_registry_has_both_lines():
    assert set(BENCHMARK_CACHE_KEYS) == {"brkb_history", "spy_history"}
    assert BENCHMARKS["spy_history"][0] == "SPY"
    assert BENCHMARKS["brkb_history"][0] == "BRK B"


def test_endpoint_returns_both_series(tmp_path, monkeypatch):
    cache = {
        "brkb_history": {"2026-09-18": 100.0, "2026-09-21": 102.0},
        "spy_history": {"2026-09-18": 50.0, "2026-09-21": 55.0},
    }
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "portfolio_account_cache.json").write_text(json.dumps(cache))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        portfolio_routes, "_build_portfolio_performance",
        lambda: {"labels": ["2026-09-18", "2026-09-21"]},
    )
    resp = asyncio.run(brkb_data_endpoint(request=None))
    body = json.loads(resp.body)
    assert body["brkb"] == [0.0, 2.0]
    assert body["spy"] == [0.0, 10.0]


def test_endpoint_missing_spy_degrades_to_empty(tmp_path, monkeypatch):
    """Old cache file (pre-SPY) → brkb still drawn, spy simply empty."""
    cache = {"brkb_history": {"2026-09-18": 100.0, "2026-09-21": 101.0}}
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "portfolio_account_cache.json").write_text(json.dumps(cache))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        portfolio_routes, "_build_portfolio_performance",
        lambda: {"labels": ["2026-09-18", "2026-09-21"]},
    )
    body = json.loads(asyncio.run(brkb_data_endpoint(request=None)).body)
    assert body["brkb"] == [0.0, 1.0]
    assert body["spy"] == []
