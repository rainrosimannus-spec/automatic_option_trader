"""2026-11-24 (Rain): lot-sale execution — marketable limits, per-scan re-pricing, earnings-eve unwind.

ADSK that day: the exit sale was priced at the bid of one snapshot (257.59), reached IBKR 12 s
later under the market and rested 47 minutes; every scan returned early on its "submitted"
marker. Earlier the same morning the unwind's sale had been cancelled by a sweep and its
marker blocked the lot for the rest of the day.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.config import RiskConfig
from src.core.models import Base, Trade, TradeType, OrderStatus
from src.core.suggestions import TradeSuggestion
from src.strategy.lot_sale import sale_limit, reprice_decision, unwind_min_profit
from src.strategy import wheel


# ── pure rules ───────────────────────────────────────────────────────────────────────────
def test_sale_limit_is_a_hair_under_the_bid():
    assert sale_limit(257.59, 0.0005) == 257.46        # 0.05% ≈ 13c on ADSK
    assert sale_limit(5.00, 0.0005) == 4.99             # never closer than one tick
    assert sale_limit(100.0, 0.0) == 99.99
    assert sale_limit(None, 0.0005) is None and sale_limit(0, 0.0005) is None


def test_reprice_stop_follows_the_bid_both_ways():
    assert reprice_decision("wheel_stop", 165.00, 165.10, 165.01) == ("modify", 164.92)
    assert reprice_decision("wheel_stop", 170.00, 170.10, 165.01) == ("modify", 169.92)   # bounce → up too
    assert reprice_decision("wheel_stop", 165.00, 165.10, 164.92) == ("keep", None)        # already there


def test_reprice_exit_respects_floor_and_cancels_when_condition_gone():
    # ADSK exit: strike 250 + 0.04 fee → floor 250.04
    assert reprice_decision("wheel_exit", 257.20, 257.30, 257.59, exit_floor=250.04) == ("modify", 257.07)
    # bid near the bar: limit is clamped AT the bar, not under it
    assert reprice_decision("wheel_exit", 250.05, 250.15, 257.59, exit_floor=250.04) == ("modify", 250.04)
    # mid under the bar → condition gone → cancel (and the scan writes the CC)
    assert reprice_decision("wheel_exit", 249.50, 249.60, 257.59, exit_floor=250.04) == ("cancel", None)
    # no floor known → plain follow-the-bid
    assert reprice_decision("wheel_exit", 257.20, 257.30, 257.59) == ("modify", 257.07)


def test_reprice_keeps_on_unusable_quote():
    assert reprice_decision("wheel_stop", None, 10.0, 9.9) == ("keep", None)
    assert reprice_decision("wheel_stop", 252.0, 257.9, 255.0) == ("keep", None)    # 2.3% spread (ADSK 09:35)
    assert reprice_decision("wheel_stop", 10.0, 9.0, 9.9) == ("keep", None)         # crossed


def test_unwind_bar_drops_to_flat_on_earnings_eve_only_when_enabled():
    assert unwind_min_profit(0.005, True, True) == 0.0
    assert unwind_min_profit(0.005, False, True) == 0.005
    assert unwind_min_profit(0.005, True, False) == 0.005


def test_live_defaults():
    rc = RiskConfig()
    assert rc.wheel_sale_bid_pad_pct == 0.0005 and rc.wheel_sale_reprice_enabled is True
    assert rc.cc_package_unwind_earnings_eve_enabled is True and rc.cc_package_unwind_earnings_eve_days == 1


# ── wiring ───────────────────────────────────────────────────────────────────────────────
def _session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    TradeSuggestion.__table__.create(eng, checkfirst=True)
    return sessionmaker(bind=eng)()


def _marker(db, symbol, source, limit, qty=100):
    sg = TradeSuggestion(symbol=symbol, action="sell_stock", source=source, status="submitted",
                         quantity=qty, limit_price=limit, rank=1, rank_score=1.0,
                         created_at=datetime.utcnow() - timedelta(minutes=30))
    db.add(sg)
    db.add(Trade(symbol=symbol, trade_type=TradeType.SELL_STOCK, strike=0.0, expiry="", premium=0.0,
                 quantity=qty, fill_price=limit, commission=0.0, order_id=301440,
                 order_status=OrderStatus.SUBMITTED, created_at=datetime.utcnow() - timedelta(minutes=30)))
    db.flush()
    return sg


def _ib_order(symbol, limit, qty=100, order_id=301440, client_id=12, sec="STK", action="SELL"):
    return SimpleNamespace(contract=SimpleNamespace(symbol=symbol, secType=sec),
                           order=SimpleNamespace(orderId=order_id, clientId=client_id, action=action,
                                                 totalQuantity=qty, lmtPrice=limit))


def _run(db, open_orders, quote, assignment_strike=250.0):
    modified, cancelled = [], []
    with patch("src.broker.orders.get_open_orders", return_value=open_orders), \
         patch("src.broker.orders.modify_limit_price", lambda t, p: modified.append((t.order.orderId, p))), \
         patch("src.broker.orders.cancel_order", lambda t: cancelled.append(t.order.orderId)), \
         patch("src.broker.market_data.get_stock_live_quote", return_value=quote), \
         patch("src.scheduler.jobs._order_is_ours", lambda t: t.order.clientId == 12), \
         patch.object(wheel, "_assignment_strike", lambda db_, s, n: assignment_strike):
        out = wheel.reprice_lot_sales(db)
    return out, modified, cancelled


def test_resting_exit_is_walked_to_the_bid():
    """ADSK 17:00 UTC: order rests at 257.59, bid 257.20 → modified to 257.07; marker + Trade row follow."""
    db = _session()
    sg = _marker(db, "ADSK", "wheel_exit", 257.59)
    out, modified, cancelled = _run(db, [_ib_order("ADSK", 257.59)], (257.20, 257.30, 257.25))
    assert out == {"ADSK": "modify"} and modified == [(301440, 257.07)] and cancelled == []
    assert sg.status == "submitted" and sg.limit_price == 257.07
    assert db.query(Trade).filter(Trade.order_id == 301440).one().fill_price == 257.07


def test_exit_whose_condition_is_gone_is_cancelled_and_marker_expired():
    db = _session()
    sg = _marker(db, "ADSK", "wheel_exit", 257.59)
    out, modified, cancelled = _run(db, [_ib_order("ADSK", 257.59)], (249.50, 249.60, 249.55))
    assert out == {"ADSK": "cancel"} and cancelled == [301440] and modified == []
    assert sg.status == "expired"
    assert db.query(Trade).filter(Trade.order_id == 301440).one().order_status == OrderStatus.CANCELLED
    assert wheel._liquidation_in_flight(db, "ADSK") is False


def test_stop_sale_follows_bid_without_a_floor():
    db = _session()
    _marker(db, "VRT", "wheel_stop", 165.01, qty=200)
    out, modified, _ = _run(db, [_ib_order("VRT", 165.01, qty=200)], (160.00, 160.10, 160.05))
    assert out == {"VRT": "modify"} and modified == [(301440, 159.92)]


def test_marker_whose_order_is_gone_is_expired_so_the_lot_unblocks():
    """The ADSK morning case: sale swept at 15:10, marker stayed 'submitted' all day."""
    db = _session()
    sg = _marker(db, "ADSK", "wheel_unwind", 261.61)
    assert wheel._liquidation_in_flight(db, "ADSK") is True
    out, modified, cancelled = _run(db, [], (257.0, 257.1, 257.05))
    assert out == {"ADSK": "gone"} and modified == [] and cancelled == []
    assert sg.status == "expired" and wheel._liquidation_in_flight(db, "ADSK") is False


def test_marker_with_a_fill_is_marked_filled():
    db = _session()
    sg = _marker(db, "ADSK", "wheel_exit", 257.59)
    db.add(Trade(symbol="ADSK", trade_type=TradeType.SELL_STOCK, strike=0.0, expiry="", premium=0.0,
                 quantity=100, fill_price=257.59, commission=0.0, order_status=OrderStatus.FILLED,
                 created_at=datetime.utcnow()))
    db.flush()
    out, *_ = _run(db, [], (257.0, 257.1, 257.05))
    assert out == {"ADSK": "filled"} and sg.status == "filled"


def test_foreign_and_option_orders_are_not_touched():
    db = _session()
    sg = _marker(db, "XFFE", "wheel_stop", 217.9)
    orders = [_ib_order("XFFE", 217.9, client_id=19),                 # Rain's hand order, client 19
              _ib_order("XFFE", 2.0, sec="OPT", action="SELL")]       # an option order
    out, modified, cancelled = _run(db, orders, (210.0, 210.1, 210.05))
    assert modified == [] and cancelled == [] and out == {"XFFE": "gone"}   # none of ours → marker dead


def test_knob_off_is_a_noop():
    db = _session()
    _marker(db, "ADSK", "wheel_exit", 257.59)
    with patch("src.strategy.wheel.get_settings") as gs:
        gs.return_value.risk.wheel_sale_reprice_enabled = False
        assert wheel.reprice_lot_sales(db) == {}


def test_sell_lot_direct_prices_under_the_bid():
    db = _session()
    placed = []

    def _sell(symbol, shares, limit_price=None, currency="USD"):
        placed.append(limit_price)
        return SimpleNamespace(order=SimpleNamespace(orderId=7))
    with patch("src.broker.orders.sell_stock", _sell):
        t = wheel._sell_lot_direct(db, "ADSK", 100, 261.61, source="wheel_unwind", rationale="r",
                                   signal="s", current_price=261.7)
    assert t is not None and placed == [261.48]
    sg = db.query(TradeSuggestion).filter(TradeSuggestion.symbol == "ADSK").one()
    assert sg.limit_price == 261.48


def test_live_exit_limit_is_marketable_but_never_under_the_bar():
    src = open("src/strategy/wheel.py").read()
    assert "exit_limit = max(sale_limit(bid" in src and "limit_price=exit_limit" in src
    # reprice runs at both the CC writer and the stop pass (right after the marker reconcile)
    assert src.count("reprice_lot_sales(db)") == 2
    pt = open("src/strategy/profit_taker.py").read()
    assert "unwind_min_profit(" in pt and "cc_package_unwind_earnings_eve_enabled" in pt
    html = open("src/web/templates/dashboard.html").read()
    assert "re-priced to the live bid every scan" in html and "on earnings eve the bar drops to flat" in html
