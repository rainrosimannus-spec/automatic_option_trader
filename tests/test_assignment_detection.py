"""Discriminator for put assignment vs worthless expiry (2026-06-29 fix).

IBKR books a $0 buy-to-close on the option for BOTH a worthless expiry and an
assignment; only an assignment ALSO delivers stock at the strike. So the authoritative
signal is the BUY_STOCK-at-~strike delivery fill, not the absence of the $0 close.
`assignment_delivery_fill` is shared by trade_sync (defer the put) and wheel
(create the stock lot) so they agree. The old logic treated the $0 close as proof of
worthless and missed every real assignment.
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import Base, Trade, TradeType, OrderStatus
from src.broker.trade_sync import assignment_delivery_fill


def _session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


OPENED = datetime(2026, 6, 25, 14, 0, 0)
AFTER = datetime(2026, 6, 27, 3, 22, 0)   # IBKR booked the delivery post-expiry
BEFORE = datetime(2026, 6, 20, 10, 0, 0)


def _trade(db, ttype, fill_price, created_at, symbol="FTAI", qty=100):
    # BUY_STOCK rows carry strike=0.0/expiry='' in the live DB; the helper keys off
    # fill_price vs the PUT's strike, not Trade.strike.
    db.add(Trade(symbol=symbol, trade_type=ttype, strike=0.0, expiry="",
                 premium=0.0, fill_price=fill_price, order_status=OrderStatus.FILLED,
                 quantity=qty, created_at=created_at))
    db.flush()


def test_delivery_at_strike_is_detected():
    db = _session()
    _trade(db, TradeType.BUY_STOCK, 267.50, AFTER)          # assignment delivery @ strike
    _trade(db, TradeType.BUY_PUT, 0.0, AFTER)               # the $0 option close (also present)
    hit = assignment_delivery_fill(db, "FTAI", 267.5, OPENED, 1)
    assert hit is not None
    assert hit.trade_type == TradeType.BUY_STOCK


def test_worthless_expiry_no_delivery_returns_none():
    db = _session()
    _trade(db, TradeType.BUY_PUT, 0.0, AFTER)               # only the $0 close, no stock
    assert assignment_delivery_fill(db, "FTAI", 267.5, OPENED, 1) is None


def test_unrelated_stock_at_different_price_is_not_delivery():
    db = _session()
    # A pre-existing covered-call lot bought far from this put's strike must NOT match.
    _trade(db, TradeType.BUY_STOCK, 130.0, AFTER)
    assert assignment_delivery_fill(db, "FTAI", 267.5, OPENED, 1) is None


def test_stock_bought_before_put_opened_is_not_delivery():
    db = _session()
    _trade(db, TradeType.BUY_STOCK, 267.50, BEFORE)         # predates the put → not its delivery
    assert assignment_delivery_fill(db, "FTAI", 267.5, OPENED, 1) is None


def test_multi_contract_delivery_at_strike_detected():
    db = _session()
    _trade(db, TradeType.BUY_STOCK, 50.0, AFTER, symbol="IREN", qty=200)
    hit = assignment_delivery_fill(db, "IREN", 50.0, OPENED, 2)
    assert hit is not None
