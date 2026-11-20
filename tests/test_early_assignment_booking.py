"""2026-11-20 THC: a put exercised EARLY (the evening before its expiry) must be booked as a
stock lot as soon as the delivery fill is synced — not the morning after expiry.

check_assignments used to gate everything on `expiry < today`. THC 280P (exp 11-20) was
assigned 11-19 evening, trade_sync pulled BUY_STOCK 100 @ 280 + BUY_PUT @ 0 at 03:43 UTC,
the delivery-triggered check ran every 15 min all day and returned assigned=[] every time:
dashboard showed an open put, IBKR held 100 shares, no covered call, stop check blind.
The worthless-expiry branch keeps its expiry gate (a $0 close alone is not proof of anything
until the option has actually expired).
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import Base, Position, PositionStatus, Trade, TradeType, OrderStatus
from src.strategy.wheel import WheelManager

TODAY = datetime.now().strftime("%Y%m%d")
TOMORROW = (datetime.now() + timedelta(days=3)).strftime("%Y%m%d")
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
OPENED = datetime.now() - timedelta(days=3)


def _session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _put(db, symbol, strike, expiry, qty=1):
    p = Position(symbol=symbol, status=PositionStatus.OPEN, position_type="short_put",
                 strike=strike, expiry=expiry, entry_premium=1.54, quantity=qty,
                 total_premium_collected=154.0, is_wheel=True, opened_at=OPENED)
    db.add(p); db.flush()
    return p


def _fill(db, symbol, ttype, fill_price, qty=100, when=None):
    db.add(Trade(symbol=symbol, trade_type=ttype, strike=0.0, expiry="", premium=0.0,
                 fill_price=fill_price, order_status=OrderStatus.FILLED, quantity=qty,
                 created_at=when or (datetime.now() - timedelta(hours=10))))
    db.flush()


def _run(db):
    """check_assignments against this session; _handle_assignment recorded, not executed."""
    handled = []

    @contextmanager
    def _db():
        yield db

    wm = WheelManager.__new__(WheelManager)
    with patch("src.strategy.wheel.get_db", _db), \
         patch("src.strategy.wheel.get_stock_positions", return_value=[]), \
         patch.object(WheelManager, "_handle_assignment",
                      lambda self, db_, pos, sym: handled.append((sym, pos.strike, pos.expiry))):
        out = wm.check_assignments()
    return out, handled


def test_early_assignment_on_expiry_day_is_booked():
    """THC 11-20: expiry == today, delivery fill already synced → booked now."""
    db = _session()
    _put(db, "THC", 280.0, TODAY)
    _fill(db, "THC", TradeType.BUY_STOCK, 280.0)    # delivery at strike
    _fill(db, "THC", TradeType.BUY_PUT, 0.0, qty=1)  # the $0 option close IBKR books too
    out, handled = _run(db)
    assert out == ["THC"] and handled == [("THC", 280.0, TODAY)]


def test_early_assignment_days_before_expiry_is_booked():
    db = _session()
    _put(db, "THC", 280.0, TOMORROW)
    _fill(db, "THC", TradeType.BUY_STOCK, 280.0)
    out, handled = _run(db)
    assert out == ["THC"] and len(handled) == 1


def test_zero_close_alone_before_expiry_passes_is_not_touched():
    """Only the $0 BUY_PUT (no stock) on expiry day: not an assignment, and NOT a worthless
    expiry yet either — leave the put OPEN for trade_sync to settle after expiry."""
    db = _session()
    p = _put(db, "ADBE", 300.0, TODAY)
    _fill(db, "ADBE", TradeType.BUY_PUT, 0.0, qty=1)
    out, handled = _run(db)
    assert out == [] and handled == []
    assert p.status == PositionStatus.OPEN


def test_live_put_with_no_fills_is_not_touched():
    db = _session()
    p = _put(db, "VRT", 150.0, TOMORROW)
    out, handled = _run(db)
    assert out == [] and handled == [] and p.status == PositionStatus.OPEN


def test_unrelated_stock_buy_is_not_an_assignment():
    """A covered-call lot bought far from the strike must not book the live put."""
    db = _session()
    _put(db, "MSFT", 500.0, TOMORROW)
    _fill(db, "MSFT", TradeType.BUY_STOCK, 430.0)
    out, handled = _run(db)
    assert out == [] and handled == []


def test_post_expiry_assignment_path_unchanged():
    """Regression: the original expiry-passed path still books a delivery."""
    db = _session()
    _put(db, "DECK", 101.0, YESTERDAY)
    _fill(db, "DECK", TradeType.BUY_STOCK, 101.0, qty=500)
    out, handled = _run(db)
    assert out == ["DECK"] and len(handled) == 1


def test_already_assigned_dedup_is_still_respected():
    """The dedup lives in _handle_assignment; the early path calls the same handler, so a
    re-fire on the next sync must hit the ASSIGNMENT-trade guard. Run the REAL handler once
    and a second time: one ASSIGNMENT row, one stock lot."""
    db = _session()
    p = _put(db, "THC", 280.0, TODAY)
    _fill(db, "THC", TradeType.BUY_STOCK, 280.0)

    @contextmanager
    def _db():
        yield db

    wm = WheelManager.__new__(WheelManager)
    with patch("src.strategy.wheel.get_db", _db), \
         patch("src.strategy.wheel.get_stock_positions", return_value=[]):
        first = wm.check_assignments()
        # trade_sync may reopen the put while IBKR still lists it; simulate that re-fire
        p.status = PositionStatus.OPEN
        second = wm.check_assignments()
    assert first == ["THC"] and second == ["THC"]
    rows = db.query(Trade).filter(Trade.trade_type == TradeType.ASSIGNMENT).all()
    assert len(rows) == 1
    lots = db.query(Position).filter(Position.symbol == "THC", Position.position_type == "stock",
                                     Position.status == PositionStatus.OPEN).all()
    assert len(lots) == 1 and lots[0].quantity == 100
    assert abs(lots[0].cost_basis - (280.0 - 1.54)) < 1e-6
