"""The dashboard's open-options box answers from disk when the in-memory cache is empty
(fresh restart while a screen holds the portfolio lock, 2026-11-04)."""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import src.portfolio.connection as pc


def test_empty_memory_falls_back_to_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pc, "_cached_portfolio_orders", [])
    assert pc.get_cached_portfolio_open_orders() == []            # no file yet: still empty
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "portfolio_open_options_cache.json").write_text(json.dumps(
        {"as_of": 1.0, "positions": [{"symbol": "CIEN", "sec_type": "OPT", "status": "Open"}]}))
    assert pc.get_cached_portfolio_open_orders() == [{"symbol": "CIEN", "sec_type": "OPT", "status": "Open"}]


def test_memory_wins_over_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "portfolio_open_options_cache.json").write_text(json.dumps(
        {"as_of": 1.0, "positions": [{"symbol": "STALE"}]}))
    monkeypatch.setattr(pc, "_cached_portfolio_orders", [{"symbol": "FRESH"}])
    assert pc.get_cached_portfolio_open_orders() == [{"symbol": "FRESH"}]


def test_refresh_writes_disk_copy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    opt = SimpleNamespace(contract=SimpleNamespace(secType="OPT", symbol="CIEN", lastTradeDateOrContractMonth="20261219",
                                                   strike=120.0, right="C"), position=-5, avgCost=310.0)
    stk = SimpleNamespace(contract=SimpleNamespace(secType="STK", symbol="CIEN"), position=500, avgCost=100.0)
    ib = SimpleNamespace(positions=lambda: [opt, stk])

    @contextmanager
    def lock():
        yield

    monkeypatch.setattr(pc, "is_portfolio_connected", lambda: True)
    monkeypatch.setattr(pc, "get_portfolio_ib", lambda: ib)
    monkeypatch.setattr(pc, "get_portfolio_lock", lock)
    monkeypatch.setattr(pc, "_cached_portfolio_orders", [])
    pc.refresh_portfolio_open_orders_cache()
    on_disk = json.loads((tmp_path / "data" / "portfolio_open_options_cache.json").read_text())
    assert [p["symbol"] for p in on_disk["positions"]] == ["CIEN"]
    assert on_disk["positions"][0]["action"] == "SELL" and on_disk["positions"][0]["quantity"] == 5
    monkeypatch.setattr(pc, "_cached_portfolio_orders", [])      # "restart": memory gone, disk stays
    assert pc.get_cached_portfolio_open_orders()[0]["symbol"] == "CIEN"
