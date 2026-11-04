"""Assigned-lot stop (Rain, 2026-11-04): a wheel lot is sold outright once its mark is
wheel_stop_loss_pct (8%) below the share-weighted assignment strike.

Evidence: over the account's first 36 assignments, holding the bags cost $37k more than
selling the next day; a −8% daily-close stop would have cut the stock loss from −$86k to
−$51k. The predicate is shared with the MarsWalk engine (section 2e); the live path buys
back any covered call first and sells only after that fill shows Filled.
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import Base, Trade, TradeType, OrderStatus
from src.core.config import RiskConfig
from src.strategy.wheel_rules import stop_loss_triggered
from src.strategy.wheel import _assignment_strike, _liquidation_in_flight
from src.core.suggestions import TradeSuggestion


def _session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    TradeSuggestion.__table__.create(eng, checkfirst=True)
    return sessionmaker(bind=eng)()


def test_predicate_boundaries():
    # exactly −8% fires; −7.9% does not; stop is "at or below"
    assert stop_loss_triggered(92.0, 100.0, 0.08) is True
    assert stop_loss_triggered(92.1, 100.0, 0.08) is False
    assert stop_loss_triggered(80.0, 100.0, 0.08) is True


def test_predicate_fails_closed_on_missing_data():
    assert stop_loss_triggered(None, 100.0, 0.08) is False
    assert stop_loss_triggered(0.0, 100.0, 0.08) is False
    assert stop_loss_triggered(50.0, None, 0.08) is False
    assert stop_loss_triggered(50.0, 100.0, 0.0) is False   # disabled pct never fires


def test_live_defaults_are_on_at_eight_percent():
    rc = RiskConfig()
    assert rc.wheel_stop_loss_enabled is True
    assert rc.wheel_stop_loss_pct == 0.08


def test_assignment_strike_is_share_weighted_over_held_lots():
    db = _session()
    now = datetime.utcnow()
    # older 2 lots at 215, newest 1 lot at 105 → holding 3 lots: (215*2 + 105) / 3
    db.add(Trade(symbol="X", trade_type=TradeType.ASSIGNMENT, strike=215.0, expiry="20260801",
                 premium=0, quantity=2, fill_price=215.0, commission=0,
                 order_status=OrderStatus.FILLED, created_at=now - timedelta(days=10)))
    db.add(Trade(symbol="X", trade_type=TradeType.ASSIGNMENT, strike=105.0, expiry="20260815",
                 premium=0, quantity=1, fill_price=105.0, commission=0,
                 order_status=OrderStatus.FILLED, created_at=now - timedelta(days=1)))
    db.flush()
    assert _assignment_strike(db, "X", 3) == round((215 * 2 + 105) / 3, 2)
    # only the newest lot is still held → its strike alone
    assert _assignment_strike(db, "X", 1) == 105.0
    assert _assignment_strike(db, "NOPE", 1) is None


def test_liquidation_in_flight_blocks_new_calls():
    db = _session()
    assert _liquidation_in_flight(db, "X") is False
    db.add(TradeSuggestion(symbol="X", action="sell_stock", source="wheel_stop", status="submitted",
                           quantity=100, rank=1, rank_score=1.0))
    db.flush()
    assert _liquidation_in_flight(db, "X") is True
    # a plain wheel_exit suggestion is not a liquidation marker
    db2 = _session()
    db2.add(TradeSuggestion(symbol="Y", action="sell_stock", source="wheel_exit", status="submitted",
                            quantity=100, rank=1, rank_score=1.0))
    db2.flush()
    assert _liquidation_in_flight(db2, "Y") is False
