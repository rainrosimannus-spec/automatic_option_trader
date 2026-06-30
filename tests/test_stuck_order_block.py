"""Prompt venue-blocking of orders that never reach the exchange (RACE/BVME PendingSubmit).

Two mechanisms: (1) _on_error blocks a symbol the instant IBKR rejects its order with a permission/
definition/route code; (2) sweep_stuck_compounder_orders (run every 5 min by the health check) cancels
+ blocks a STK BUY stuck PendingSubmit/Inactive and cancels its orphaned suggestion. Both feed the
_permission_blocked registry the deploy queue already skips, so the budget routes to the next buy.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.core.database as db_mod
from src.core.models import Base
from src.portfolio.models import PortfolioWatchlist  # noqa: F401 (register tables)
from src.core.suggestions import TradeSuggestion       # noqa: F401
import src.portfolio.buyer as buyer
import src.portfolio.connection as conn


@pytest.fixture(autouse=True)
def _clear_block():
    buyer._permission_blocked.clear()
    yield
    buyer._permission_blocked.clear()


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    monkeypatch.setattr(db_mod, "_engine", eng)
    monkeypatch.setattr(db_mod, "_SessionLocal", sessionmaker(bind=eng))
    return eng


# ── 1. instant block on a hard rejection (_on_error) ──────────────────────────
def test_on_error_blocks_on_reject_codes():
    for code in (200, 201, 460, 10311):
        buyer._permission_blocked.clear()
        conn._on_error(7, code, "No trading permissions", SimpleNamespace(symbol="AZN"))
        assert buyer._is_permission_blocked("AZN"), f"code {code} should block"


def test_on_error_ignores_benign_codes_and_no_contract():
    conn._on_error(7, 162, "Historical data query cancelled", SimpleNamespace(symbol="RACE"))
    conn._on_error(7, 2104, "Market data farm connection is OK", SimpleNamespace(symbol="RACE"))
    conn._on_error(7, 460, "No trading permissions", None)        # no contract → nothing to block
    assert not buyer._is_permission_blocked("RACE")


# ── 2. the 5-min stuck-order sweep ────────────────────────────────────────────
def _trade(symbol, status, sec="STK", action="BUY"):
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol, secType=sec),
        order=SimpleNamespace(action=action),
        orderStatus=SimpleNamespace(status=status))


class _FakeIB:
    def __init__(self, trades):
        self._trades = trades
        self.cancelled = []
    def trades(self):
        return self._trades
    def cancelOrder(self, o):
        self.cancelled.append(o)


def test_sweep_blocks_only_stuck_watchlist_buys(temp_db, monkeypatch):
    import contextlib
    monkeypatch.setattr(conn, "get_portfolio_lock", lambda: contextlib.nullcontext())
    with db_mod.get_db() as db:
        for s in ("RACE", "AAA", "HEALTHY"):
            db.add(PortfolioWatchlist(symbol=s, name=s, exchange="BVME", currency="EUR"))
        db.add(TradeSuggestion(symbol="RACE", action="buy_stock", status="submitted",
                               source="portfolio", quantity=8, limit_price=326.0))
        db.commit()

    ib = _FakeIB([
        _trade("RACE", "PendingSubmit"),       # stuck + watchlist → block + cancel + expire card
        _trade("HEALTHY", "Submitted"),        # working fine → leave alone
        _trade("AAA", "Inactive"),             # stuck + watchlist → block + cancel
        _trade("NOTWL", "PendingSubmit"),      # not on watchlist → ignore
        _trade("RACE", "Submitted", sec="OPT"),# option, not a STK BUY → ignore
    ])
    n = buyer.sweep_stuck_compounder_orders(ib)

    assert n == 2
    assert buyer._is_permission_blocked("RACE")
    assert buyer._is_permission_blocked("AAA")
    assert not buyer._is_permission_blocked("HEALTHY")
    assert not buyer._is_permission_blocked("NOTWL")
    assert len(ib.cancelled) == 2                      # RACE + AAA cancelled, HEALTHY untouched
    with db_mod.get_db() as db:
        race = db.query(TradeSuggestion).filter(TradeSuggestion.symbol == "RACE").first()
        assert race.status == "cancelled"              # orphaned card cleared
