"""Multi-assignment cost-basis blend (2026-06-28).

A second put assignment of an already-held wheel name must BLEND into the single
open stock lot (share-weighted basis, summed shares + premiums) — not be silently
dropped (the old "a stock row exists" guard) and not double-count when trade_sync
reopens the same put and the detector re-fires.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import (Base, Position, Trade, TradeType,
                             PositionStatus)
from src.strategy.wheel import WheelManager


def _session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _put(db, strike, premium, qty=1):
    p = Position(symbol="NVDA", status=PositionStatus.OPEN, position_type="short_put",
                 strike=strike, entry_premium=premium, quantity=qty,
                 total_premium_collected=premium, is_wheel=True)
    db.add(p)
    db.flush()  # assign id (dedup keys on put_pos.id)
    return p


def _stock_rows(db):
    return db.query(Position).filter(
        Position.symbol == "NVDA",
        Position.position_type == "stock",
        Position.status == PositionStatus.OPEN,
    ).all()


def test_blend_two_assignments():
    db = _session()

    put215 = _put(db, 215.0, 5.0)
    WheelManager._handle_assignment(None, db, put215, "NVDA")
    rows = _stock_rows(db)
    assert len(rows) == 1
    assert rows[0].quantity == 100
    assert rows[0].cost_basis == pytest.approx(210.0)        # 215 - 5

    put105 = _put(db, 105.0, 3.0)
    WheelManager._handle_assignment(None, db, put105, "NVDA")
    rows = _stock_rows(db)
    assert len(rows) == 1                                     # blended, NOT a new row
    assert rows[0].quantity == 200
    # share-weighted: (210*100 + 102*100) / 200 = 156
    assert rows[0].cost_basis == pytest.approx(156.0)
    assert rows[0].total_premium_collected == pytest.approx(8.0)


def test_refire_does_not_double_count():
    db = _session()

    put215 = _put(db, 215.0, 5.0)
    WheelManager._handle_assignment(None, db, put215, "NVDA")

    # trade_sync reopens the still-live put → detector re-fires the SAME put
    put215.status = PositionStatus.OPEN
    WheelManager._handle_assignment(None, db, put215, "NVDA")

    rows = _stock_rows(db)
    assert len(rows) == 1
    assert rows[0].quantity == 100                            # NOT 200
    assert rows[0].cost_basis == pytest.approx(210.0)
    n_assign = db.query(Trade).filter(
        Trade.trade_type == TradeType.ASSIGNMENT).count()
    assert n_assign == 1                                      # one ASSIGNMENT trade per put
