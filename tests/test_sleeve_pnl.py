"""Per-sleeve P&L (2026-11-04): puts / covered calls / stock / index puts from the account's own ledger."""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import Base, Trade, Position, TradeType, OrderStatus, PositionStatus
from src.strategy.sleeve_pnl import compute_sleeve_pnl


def _db():
    eng = create_engine("sqlite:///:memory:"); Base.metadata.create_all(eng); return sessionmaker(bind=eng)()


def _t(db, sym, tt, prem=0.0, qty=1, fill=0.0, comm=1.0, days_ago=1):
    db.add(Trade(symbol=sym, trade_type=tt, strike=100.0, expiry="20261120", premium=prem, quantity=qty,
                 fill_price=fill or prem, commission=comm, order_status=OrderStatus.FILLED,
                 created_at=datetime.utcnow() - timedelta(days=days_ago)))


def test_sleeves_are_separated_and_windowed():
    db = _db()
    now = datetime.utcnow()
    _t(db, "AAA", TradeType.SELL_PUT, prem=2.0, qty=1, days_ago=5)        # +200 −1
    _t(db, "AAA", TradeType.BUY_PUT, prem=0.5, qty=1, days_ago=4)         # −50 −1
    _t(db, "BBB", TradeType.SELL_PUT, prem=1.0, qty=2, days_ago=45)       # +200 −1 (outside 30d)
    _t(db, "AAA", TradeType.SELL_CALL, prem=3.0, qty=1, days_ago=3)       # +300 −1
    _t(db, "XSP", TradeType.SELL_PUT, prem=1.2, qty=5, days_ago=2)        # index sleeve +600 −1
    _t(db, "XSP", TradeType.BUY_PUT, prem=0.3, qty=5, days_ago=1)         # −150 −1
    _t(db, "AAA", TradeType.ASSIGNMENT, fill=100.0, qty=1, comm=0, days_ago=3)
    _t(db, "AAA", TradeType.SELL_STOCK, fill=95.0, qty=100, comm=1.0, days_ago=1)
    db.add(Position(symbol="AAA", status=PositionStatus.CLOSED, position_type="stock", quantity=100,
                    entry_premium=2.0, cost_basis=98.0, wheel_exit_mode=True, total_premium_collected=0,
                    realized_pnl=-500.0, is_wheel=True, opened_at=now - timedelta(days=3), closed_at=now - timedelta(days=1)))
    db.add(Position(symbol="CCC", status=PositionStatus.OPEN, position_type="stock", quantity=200,
                    entry_premium=1.0, cost_basis=50.0, wheel_exit_mode=True, total_premium_collected=0,
                    realized_pnl=0.0, unrealized_pnl=-1200.0, is_wheel=True, opened_at=now - timedelta(days=10)))
    db.flush()
    r = compute_sleeve_pnl(db, now=now)
    rows = r["rows"]
    assert round(rows["single_puts"]["all"]) == 200 - 1 - 50 - 1 + 200 - 1
    assert round(rows["single_puts"]["d30"]) == 200 - 1 - 50 - 1
    assert rows["single_puts"]["n_all"] == 2 and rows["single_puts"]["n_30"] == 1
    assert round(rows["covered_calls"]["all"]) == 299
    assert round(rows["index_puts"]["all"]) == 600 - 1 - 150 - 1 and rows["index_puts"]["n_all"] == 1
    assert rows["stock"]["all"] == -500.0 and rows["stock"]["d30"] == -500.0
    assert rows["stock"]["open_unrealised"] == -1200.0 and rows["stock"]["open_lots"] == 1
    assert round(r["total_all"]) == round(rows["single_puts"]["all"] + 299 + 448 - 500 - 1200)
    # the parking ETF never counts as a sleeve
    _t(db, "XEON", TradeType.SELL_STOCK, fill=150.0, qty=10, comm=5.0)
    db.flush()
    r2 = compute_sleeve_pnl(db, now=now)
    assert round(r2["total_all"]) == round(r["total_all"])
