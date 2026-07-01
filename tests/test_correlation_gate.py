"""Options-side correlation risk gate (re-enabled 2026-07-01 with a working FMP price source).

Guarantees: (1) it BLOCKS a put on a name too correlated with the open book; (2) it FAILS OPEN — any
missing data / FMP error returns allow, so a data hiccup can never block the live trading path;
(3) it's skipped below the NLV threshold or with < 3 open positions.
"""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import src.strategy.risk as risk_mod
from src.strategy.risk import RiskManager


class _Q:
    def __init__(self, rows): self._rows = rows
    def filter(self, *a, **k): return self
    def all(self): return self._rows


class _DB:
    def __init__(self, rows): self._rows = rows
    def query(self, *a, **k): return _Q(self._rows)


def _mk(monkeypatch, *, nlv, positions, price_hist):
    """Wire a RiskManager with mocked account summary, open positions, and price history."""
    rm = RiskManager(SimpleNamespace())
    monkeypatch.setattr(risk_mod, "get_account_summary",
                        lambda: SimpleNamespace(net_liquidation=nlv))
    @contextmanager
    def _fake_db():
        yield _DB([SimpleNamespace(symbol=s) for s in positions])
    monkeypatch.setattr(risk_mod, "get_db", _fake_db)
    # get_price_history is imported inside the method from src.portfolio.fmp
    import src.portfolio.fmp as fmp_mod
    monkeypatch.setattr(fmp_mod, "get_price_history", price_hist)
    return rm


_RISING = [100 + i for i in range(40)]          # 40 daily closes, strictly rising
_FALLING = [200 - i for i in range(40)]         # inversely-moving series


def test_blocks_when_highly_correlated(monkeypatch):
    # Candidate and all 3 open names share the SAME series → corr ≈ 1.0 > 0.85 → BLOCK.
    rm = _mk(monkeypatch, nlv=1_000_000, positions=["AAA", "BBB", "CCC"],
             price_hist=lambda sym, days=70: list(_RISING))
    res = rm.check_correlation("NEWSYM")
    assert res.allowed is False
    assert "correlation" in (res.reason or "").lower()


def test_fails_open_when_no_data(monkeypatch):
    # get_price_history returns None (FMP error / rate-limit) → must ALLOW (never block trading).
    rm = _mk(monkeypatch, nlv=1_000_000, positions=["AAA", "BBB", "CCC"],
             price_hist=lambda sym, days=70: None)
    assert rm.check_correlation("NEWSYM").allowed is True


def test_fails_open_on_exception(monkeypatch):
    # A raising data source must be caught → ALLOW.
    def _boom(sym, days=70): raise RuntimeError("FMP down")
    rm = _mk(monkeypatch, nlv=1_000_000, positions=["AAA", "BBB", "CCC"], price_hist=_boom)
    assert rm.check_correlation("NEWSYM").allowed is True


def test_skipped_below_nlv_threshold(monkeypatch):
    called = {"n": 0}
    def _hist(sym, days=70):
        called["n"] += 1
        return list(_RISING)
    rm = _mk(monkeypatch, nlv=1_000, positions=["AAA", "BBB", "CCC"], price_hist=_hist)  # NLV < 50k
    assert rm.check_correlation("NEWSYM").allowed is True
    assert called["n"] == 0                    # never even fetched prices


def test_skipped_with_few_positions(monkeypatch):
    rm = _mk(monkeypatch, nlv=1_000_000, positions=["AAA"],   # < 3 open positions
             price_hist=lambda sym, days=70: list(_RISING))
    assert rm.check_correlation("NEWSYM").allowed is True
