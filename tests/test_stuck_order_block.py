"""Loop-safe prompt blocking of orders that never reach the exchange (RACE/BVME PendingSubmit).

detect_stuck_orders_from_cache reads ONLY the in-memory pending-orders cache (zero IBKR calls — a
frequent job must not add load to the shared asyncio loop). A STK BUY stuck in 'PendingSubmit' longer
than _STUCK_PENDING_SECONDS is venue-blocked (deploy queue skips it → budget to next) and its orphaned
suggestion cancelled; a freshly-placed (briefly PendingSubmit) or healthy 'Submitted' order is not.
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.core.database as db_mod
from src.core.models import Base
from src.core.suggestions import TradeSuggestion  # noqa: F401 (register tables)
import src.portfolio.buyer as buyer
import src.portfolio.connection as conn


@pytest.fixture(autouse=True)
def _clean():
    buyer._permission_blocked.clear()
    buyer._pending_submit_since.clear()
    yield
    buyer._permission_blocked.clear()
    buyer._pending_submit_since.clear()


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    monkeypatch.setattr(db_mod, "_engine", eng)
    monkeypatch.setattr(db_mod, "_SessionLocal", sessionmaker(bind=eng))
    return eng


def _cache(*orders):
    return list(orders)


def _o(symbol, status, sec="STK", action="BUY", order_id=None):
    return {"symbol": symbol, "sec_type": sec, "action": action, "status": status, "order_id": order_id}


class _FakeOrder:
    def __init__(self, oid):
        self.orderId = oid


class _FakeTrade:
    def __init__(self, oid):
        self.order = _FakeOrder(oid)


class _FakeIB:
    def __init__(self, order_ids):
        self._trades = [_FakeTrade(i) for i in order_ids]
        self.cancelled = []
    def trades(self):
        return list(self._trades)
    def cancelOrder(self, order):
        self.cancelled.append(order.orderId)


def test_fresh_pendingsubmit_not_blocked_yet(temp_db, monkeypatch):
    monkeypatch.setattr(conn, "get_cached_portfolio_pending_orders",
                        lambda: _cache(_o("RACE", "PendingSubmit")))
    assert buyer.detect_stuck_orders_from_cache() == 0      # just seen → recorded, not stuck yet
    assert not buyer._is_permission_blocked("RACE")
    assert "RACE" in buyer._pending_submit_since


def test_stuck_pendingsubmit_blocks_and_cancels_card(temp_db, monkeypatch):
    with db_mod.get_db() as db:
        db.add(TradeSuggestion(symbol="RACE", action="buy_stock", status="submitted",
                               source="portfolio", quantity=8, limit_price=326.0))
        db.commit()
    monkeypatch.setattr(conn, "get_cached_portfolio_pending_orders",
                        lambda: _cache(_o("RACE", "PendingSubmit"), _o("HEALTHY", "Submitted")))
    # simulate RACE having been PendingSubmit for > threshold
    buyer._pending_submit_since["RACE"] = datetime.utcnow() - timedelta(seconds=buyer._STUCK_PENDING_SECONDS + 30)

    assert buyer.detect_stuck_orders_from_cache() == 1
    assert buyer._is_permission_blocked("RACE")
    assert not buyer._is_permission_blocked("HEALTHY")      # Submitted = healthy, left alone
    with db_mod.get_db() as db:
        assert db.query(TradeSuggestion).filter(TradeSuggestion.symbol == "RACE").first().status == "cancelled"


def test_disappeared_order_clears_tracker(temp_db, monkeypatch):
    buyer._pending_submit_since["OLD"] = datetime.utcnow() - timedelta(minutes=10)
    monkeypatch.setattr(conn, "get_cached_portfolio_pending_orders", lambda: _cache())  # empty now
    buyer.detect_stuck_orders_from_cache()
    assert "OLD" not in buyer._pending_submit_since         # forgotten → clean retry later


def test_detection_path_makes_no_ibkr_calls(temp_db, monkeypatch):
    # DETECTION (ib=None) must never touch the IB connection — get_portfolio_ib raising proves it.
    monkeypatch.setattr(conn, "get_portfolio_ib",
                        lambda: (_ for _ in ()).throw(AssertionError("detection must not call IBKR")))
    monkeypatch.setattr(conn, "get_cached_portfolio_pending_orders",
                        lambda: _cache(_o("RACE", "PendingSubmit")))
    buyer.detect_stuck_orders_from_cache()                  # ib=None → no IBKR; no AssertionError


def test_targeted_cancel_only_on_block(temp_db, monkeypatch):
    import contextlib
    monkeypatch.setattr(conn, "get_portfolio_lock", lambda: contextlib.nullcontext())
    monkeypatch.setattr(conn, "get_cached_portfolio_pending_orders",
                        lambda: _cache(_o("RACE", "PendingSubmit", order_id=555)))
    ib = _FakeIB(order_ids=[555])
    # not stuck long enough yet → recorded, NO block, NO cancel
    assert buyer.detect_stuck_orders_from_cache(ib=ib) == 0
    assert ib.cancelled == []
    # now simulate it stuck past the threshold → block + targeted cancel of order 555
    buyer._pending_submit_since["RACE"] = datetime.utcnow() - timedelta(seconds=buyer._STUCK_PENDING_SECONDS + 30)
    assert buyer.detect_stuck_orders_from_cache(ib=ib) == 1
    assert buyer._is_permission_blocked("RACE")
    assert ib.cancelled == [555]                            # exactly the stuck order, cancelled once
