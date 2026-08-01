"""Event-driven assignment booking trigger (2026-08-01).

position_sync DEFERS a detected assignment (delivery fill present, put kept OPEN) to the
wheel's check_assignments, which runs on mon–fri crons + one 03:00 UTC daily run. An
assignment whose executions sync AFTER the Saturday 03:00 run therefore sat detected-but-
unbooked all weekend — off the holdings view and uncovered (live DECK/CTAS, 2026-08-01).
pending_assignment_symbols() lets the trade-sync job fire check_assignments the moment a
delivery lands. It must report exactly the OPEN puts that already have a stock-delivery
fill, and nothing else — so the trigger is self-limiting (booked puts leave OPEN and drop).
"""
from contextlib import contextmanager
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.broker.trade_sync as ts
from src.core.models import (
    Base, Trade, TradeType, OrderStatus, Position, PositionStatus,
)

OPENED = datetime(2026, 7, 28, 16, 13, 0)
DELIVERED = datetime(2026, 8, 1, 3, 39, 53)   # IBKR booked the delivery overnight


@pytest.fixture
def db(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    session = sessionmaker(bind=eng)()

    @contextmanager
    def _fake_get_db():
        yield session

    monkeypatch.setattr(ts, "get_db", _fake_get_db)
    return session


def _put(db, symbol, strike, qty=1, status=PositionStatus.OPEN, expiry="20260731"):
    db.add(Position(symbol=symbol, position_type="short_put", strike=strike, expiry=expiry,
                    quantity=qty, status=status, opened_at=OPENED))
    db.flush()


def _delivery(db, symbol, fill_price, qty=100):
    # IBKR books the assignment delivery as a FILLED BUY_STOCK at ~strike, strike=0.0.
    db.add(Trade(symbol=symbol, trade_type=TradeType.BUY_STOCK, strike=0.0, expiry="",
                 premium=0.0, fill_price=fill_price, order_status=OrderStatus.FILLED,
                 quantity=qty, created_at=DELIVERED))
    db.flush()


def test_open_put_with_delivery_is_pending(db):
    _put(db, "DECK", 101.0, qty=5)
    _delivery(db, "DECK", 101.0, qty=500)
    assert ts.pending_assignment_symbols() == ["DECK"]


def test_worthless_expiry_no_delivery_not_pending(db):
    # Put vanished worthless: the $0 BUY_PUT close exists but NO stock delivery.
    _put(db, "AVGO", 375.0)
    db.add(Trade(symbol="AVGO", trade_type=TradeType.BUY_PUT, strike=375.0, expiry="20260731",
                 premium=0.0, fill_price=0.0, order_status=OrderStatus.FILLED,
                 quantity=1, created_at=DELIVERED))
    db.flush()
    assert ts.pending_assignment_symbols() == []


def test_already_booked_put_not_pending(db):
    # Once check_assignments books it the put is ASSIGNED (not OPEN) → trigger clears.
    _put(db, "ISRG", 355.0, status=PositionStatus.ASSIGNED)
    _delivery(db, "ISRG", 355.0)
    assert ts.pending_assignment_symbols() == []


def test_delivery_far_from_strike_not_pending(db):
    # A covered-call lot bought far from this put's strike must not count as a delivery.
    _put(db, "QCOM", 148.0)
    _delivery(db, "QCOM", 130.0)
    assert ts.pending_assignment_symbols() == []


def test_multiple_pending_sorted_deduped(db):
    _put(db, "CTAS", 212.5, qty=2)
    _delivery(db, "CTAS", 212.5, qty=200)
    _put(db, "DECK", 101.0, qty=5)
    _delivery(db, "DECK", 101.0, qty=500)
    assert ts.pending_assignment_symbols() == ["CTAS", "DECK"]


def test_none_when_no_open_puts(db):
    assert ts.pending_assignment_symbols() == []
