"""OPTIONS-side history-farm breaker (2026-11-14; the portfolio side has its own in test_history_farm_breaker.py): on 11-12 and 11-13 the options gateway came back from
its nightly auto-restart with the HMDS farm dead. Every get_stock_price() timed out (2 × 20 s
per name), the 24-name put scan held the scan lock for the whole cycle and the CC / stop /
XSP jobs skipped on lock timeout all day. The connection was up, so the existing
"connection dead" abort never fired."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.broker import market_data as md
from src.strategy import put_seller as ps_mod


@pytest.fixture(autouse=True)
def _fresh():
    md._reset_history_farm_for_tests()
    yield
    md._reset_history_farm_for_tests()


def test_slow_failures_trip_after_threshold():
    for sym in ("A", "B", "C"):
        md._farm_note_failure(sym, 40.0)
    assert not md.history_farm_dead()
    md._farm_note_failure("D", 40.0)
    assert md.history_farm_dead()
    st = md.history_farm_state()
    assert st["dead"] and st["consecutive"] == 4 and st["last_symbol"] == "D" and st["dead_since"]


def test_fast_refusals_do_not_count():
    # permission refusal / unknown contract comes back in ~1 s — not a farm symptom
    for sym in ("EVO", "DIKU", "LMDA", "X", "Y"):
        md._farm_note_failure(sym, 1.2)
    assert not md.history_farm_dead()
    assert md.history_farm_state()["consecutive"] == 0


def test_success_clears_the_breaker():
    for sym in ("A", "B", "C", "D"):
        md._farm_note_failure(sym, 40.0)
    assert md.history_farm_dead()
    md._farm_note_success("SPY")
    assert not md.history_farm_dead()
    st = md.history_farm_state()
    assert st["consecutive"] == 0 and st["dead_since"] is None and st["last_ok"]


def test_a_success_in_between_resets_the_count():
    md._farm_note_failure("A", 40.0)
    md._farm_note_failure("B", 40.0)
    md._farm_note_success("C")
    md._farm_note_failure("D", 40.0)
    md._farm_note_failure("E", 40.0)
    assert not md.history_farm_dead()


def _seller(symbols):
    universe = SimpleNamespace(all_symbols=list(symbols),
                               symbols_for_market=lambda m: list(symbols))
    risk = SimpleNamespace(
        get_regime=lambda force_refresh=False: SimpleNamespace(vix=18.0),
        check_spy_ma_gate=lambda: SimpleNamespace(reduce_pct=1.0),
        increment_daily_count=lambda: None,
    )
    s = ps_mod.PutSeller.__new__(ps_mod.PutSeller)
    s.universe = universe
    s.risk = risk
    s.cfg = SimpleNamespace()
    return s


def test_put_scan_aborts_once_farm_is_dead(monkeypatch):
    """Suggestion-mode loop: after the 4th slow failure the scan stops instead of
    burning 40 s per remaining name under the scan lock."""
    import random
    from contextlib import contextmanager

    seller = _seller([f"S{i}" for i in range(24)])
    seen = []

    def fake_eval(symbol, vix, market=None):
        seen.append(symbol)
        md._farm_note_failure(symbol, 40.0)   # what get_stock_price does on a dead farm
        return None

    class _Q:
        def filter(self, *a, **k): return self
        def all(self): return []

    @contextmanager
    def _db():
        yield SimpleNamespace(query=lambda *a, **k: _Q())

    monkeypatch.setattr(seller, "_evaluate_symbol", fake_eval)
    monkeypatch.setattr(seller, "_create_ranked_suggestion", lambda *a, **k: None)
    monkeypatch.setattr(random, "shuffle", lambda x: None)
    cfg = SimpleNamespace(app=SimpleNamespace(suggestion_mode=True))
    with patch.object(ps_mod, "get_settings", return_value=cfg), \
         patch("src.core.database.get_db", _db), \
         patch("src.core.suggestions.reset_margin_circuit_breaker", lambda: None), \
         patch("src.broker.connection.is_connected", lambda: True):
        traded = seller.run_scan(market=None)
    assert traded == []
    assert len(seen) == 4, seen
    assert md.history_farm_dead()


def test_put_scan_runs_whole_universe_when_farm_healthy(monkeypatch):
    import random
    from contextlib import contextmanager

    seller = _seller([f"S{i}" for i in range(10)])
    seen = []

    def fake_eval(symbol, vix, market=None):
        seen.append(symbol)
        md._farm_note_success(symbol)
        return None

    class _Q:
        def filter(self, *a, **k): return self
        def all(self): return []

    @contextmanager
    def _db():
        yield SimpleNamespace(query=lambda *a, **k: _Q())

    monkeypatch.setattr(seller, "_evaluate_symbol", fake_eval)
    monkeypatch.setattr(random, "shuffle", lambda x: None)
    cfg = SimpleNamespace(app=SimpleNamespace(suggestion_mode=True))
    with patch.object(ps_mod, "get_settings", return_value=cfg), \
         patch("src.core.database.get_db", _db), \
         patch("src.core.suggestions.reset_margin_circuit_breaker", lambda: None), \
         patch("src.broker.connection.is_connected", lambda: True):
        seller.run_scan(market=None)
    assert len(seen) == 10


# ── FMP price fallback (2026-11-14) ─────────────────────────
from contextlib import nullcontext


class _DeadFarmIB:
    """qualifyContracts fine, every history request comes back empty (dead HMDS farm)."""
    def qualifyContracts(self, c): return [c]
    def reqHistoricalData(self, *a, **k): return []
    def reqMarketDataType(self, t): pass
    def sleep(self, s): pass


def _dead_farm(monkeypatch):
    md._price_cache.clear()
    md._price_fail_counts.clear(); md._price_fail_until.clear()
    monkeypatch.setattr(md, "get_ib_lock", lambda: nullcontext())
    monkeypatch.setattr(md, "get_ib", lambda: _DeadFarmIB())


def test_fmp_quote_covers_usd_name_when_history_is_dead(monkeypatch):
    _dead_farm(monkeypatch)
    import src.portfolio.fmp as fmp
    monkeypatch.setattr(fmp, "_get", lambda ep, sym, params={}: [{"symbol": sym, "price": 243.08, "previousClose": 238.54}])
    assert md.get_stock_price("ADBE", "SMART", "USD") == 243.08
    # cached like an IBKR price, and the symbol is not put on the failure block list
    assert md._price_cache[("ADBE", "SMART", "USD")][0] == 243.08
    assert "ADBE" not in md._price_fail_counts


def test_fmp_fallback_uses_previous_close_before_the_open(monkeypatch):
    _dead_farm(monkeypatch)
    import src.portfolio.fmp as fmp
    monkeypatch.setattr(fmp, "_get", lambda ep, sym, params={}: [{"symbol": sym, "price": 0, "previousClose": 86.82}])
    assert md.get_stock_price("DECK", "SMART", "USD") == 86.82


def test_no_fmp_fallback_for_non_usd_names(monkeypatch):
    _dead_farm(monkeypatch)
    import src.portfolio.fmp as fmp
    calls = []
    monkeypatch.setattr(fmp, "_get", lambda ep, sym, params={}: calls.append(sym) or [{"price": 1.0}])
    assert md.get_stock_price("ASML", "AEB", "EUR") is None
    assert calls == []


def test_fmp_miss_still_returns_none_and_blocks_symbol(monkeypatch):
    _dead_farm(monkeypatch)
    import src.portfolio.fmp as fmp
    monkeypatch.setattr(fmp, "_get", lambda ep, sym, params={}: None)
    assert md.get_stock_price("VRT", "SMART", "USD") is None
    assert md._price_fail_counts.get("VRT") == 1
