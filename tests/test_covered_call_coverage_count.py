"""Gap-safe covered-call coverage counting (2026-07-30 LRCX naked-call incident).

LRCX held 100 shares yet the wheel sold 2× 400C: every scan between the first fill
(14:00) and the trade_sync that wrote the covered_call position (14:13) read
covered=0, because the old detector only counted FILLED trades (the wheel records a
SELL_CALL as SUBMITTED at placement) and counted trade ROWS not contracts. The helper
counts positions opened before today + today's SUBMITTED-or-FILLED SELL_CALL CONTRACTS,
excluding today's positions so the two never double-count once the sync catches up.
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import (Base, Position, Trade, TradeType,
                             PositionStatus, OrderStatus)
from src.strategy.wheel import _covered_call_contracts


def _session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _sell_call(db, status, qty=1, when=None):
    db.add(Trade(symbol="LRCX", trade_type=TradeType.SELL_CALL, strike=400.0,
                 expiry="20260918", quantity=qty, premium=10.4, fill_price=10.4,
                 order_status=status, created_at=when or datetime.utcnow()))
    db.flush()


def _cc_position(db, qty, opened_at):
    db.add(Position(symbol="LRCX", status=PositionStatus.OPEN, position_type="covered_call",
                    strike=400.0, expiry="20260918", quantity=qty, entry_premium=10.4,
                    opened_at=opened_at))
    db.flush()


def test_submitted_call_counts_before_sync():
    # The exact gap: a just-placed SELL_CALL sits SUBMITTED, no position yet. It MUST count
    # as coverage so the next scan doesn't write a second (naked) call.
    db = _session()
    _sell_call(db, OrderStatus.SUBMITTED, qty=1)
    assert _covered_call_contracts(db, "LRCX") == 1


def test_filled_call_counts_too():
    db = _session()
    _sell_call(db, OrderStatus.FILLED, qty=1)
    assert _covered_call_contracts(db, "LRCX") == 1


def test_cancelled_call_does_not_count():
    # A price-cap cancel must NOT hold phantom coverage (that would leave shares uncovered).
    db = _session()
    _sell_call(db, OrderStatus.CANCELLED, qty=1)
    assert _covered_call_contracts(db, "LRCX") == 0


def test_multi_contract_order_counts_by_quantity():
    # A single qty=2 order covers two lots — counting rows would under-count and double-write.
    db = _session()
    _sell_call(db, OrderStatus.SUBMITTED, qty=2)
    assert _covered_call_contracts(db, "LRCX") == 2


def test_todays_position_and_its_trade_do_not_double_count():
    # Once trade_sync writes the covered_call position (opened today), the same coverage must
    # not be counted twice (position + its originating trade).
    db = _session()
    _sell_call(db, OrderStatus.FILLED, qty=1)
    _cc_position(db, qty=1, opened_at=datetime.utcnow())   # synced today
    assert _covered_call_contracts(db, "LRCX") == 1


def test_prior_day_position_counts_from_position_table():
    # A position opened before today has no today-trade; it's counted from the position table.
    db = _session()
    _cc_position(db, qty=3, opened_at=datetime.utcnow() - timedelta(days=8))
    assert _covered_call_contracts(db, "LRCX") == 3


def test_lrcx_incident_would_be_blocked():
    # Reproduce 14:10: one call already SUBMITTED from 14:00, no position yet. 100 shares =>
    # lots_needed 1. covered==1 => lots_to_cover 0 => the second (naked) write is blocked.
    db = _session()
    _sell_call(db, OrderStatus.SUBMITTED, qty=1)
    lots_needed = 100 // 100
    assert lots_needed - _covered_call_contracts(db, "LRCX") <= 0
