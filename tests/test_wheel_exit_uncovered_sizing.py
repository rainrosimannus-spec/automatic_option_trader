"""The live-exit sell must be sized to the UNCOVERED shares only (2026-08-25 CTAS).

_live_exit_opportunity used to create a sell_stock suggestion for `total_shares` — every
held share, including those collateralising open covered calls. On a partially-covered lot
that strips the collateral off the CCs and leaves them naked. The exit now sells only the
uncovered portion (total_shares minus what open/pending CCs back); on a fully-uncovered lot
that equals total_shares, so nothing changes there.
"""
import src.core.suggestions as suggestions_mod
import src.broker.market_data as market_data_mod
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import Base, Trade, TradeType, OrderStatus
from src.strategy.wheel import WheelManager


def _session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _assignment(db, strike, contracts):
    db.add(Trade(symbol="CTAS", trade_type=TradeType.ASSIGNMENT, strike=strike,
                 expiry="", premium=0, quantity=contracts, fill_price=strike,
                 order_status=OrderStatus.FILLED))
    db.flush()


def _run(monkeypatch, total_shares, lots_needed, sell_shares):
    """Call _live_exit_opportunity with a firing quote; return the captured sell quantity
    (or None if no suggestion was created)."""
    db = _session()
    _assignment(db, strike=200.0, contracts=lots_needed)  # threshold 200.04, well below mid

    captured = {}

    def _fake_create(**kwargs):
        captured.update(kwargs)

    # Mid 205.1, tight spread → above the 200.04 threshold, passes the 2% spread gate.
    monkeypatch.setattr(market_data_mod, "get_stock_live_quote", lambda sym: (205.0, 205.2, 205.1))
    monkeypatch.setattr(suggestions_mod, "create_suggestion", _fake_create)

    wm = WheelManager.__new__(WheelManager)   # method uses no __init__ state
    fired = wm._live_exit_opportunity(db, "CTAS", total_shares, lots_needed,
                                      sell_shares=sell_shares)
    return fired, captured.get("quantity")


def test_partial_coverage_sells_only_uncovered(monkeypatch):
    # 400 shares, 2 CCs back 200 → uncovered 200. The exit must sell 200, NOT 400,
    # so the two covered calls keep their collateral.
    fired, qty = _run(monkeypatch, total_shares=400, lots_needed=4, sell_shares=200)
    assert fired is True
    assert qty == 200


def test_fully_uncovered_sells_everything(monkeypatch):
    # No CCs → uncovered == total. Behaviour is unchanged: sell all 200.
    fired, qty = _run(monkeypatch, total_shares=200, lots_needed=2, sell_shares=200)
    assert fired is True
    assert qty == 200


def test_odd_lot_remainder_included_when_uncovered(monkeypatch):
    # 250 shares fully uncovered (caller passes total_shares as sell_shares): the 50-share
    # odd lot is NOT stranded — it sells with the rest, as before.
    fired, qty = _run(monkeypatch, total_shares=250, lots_needed=2, sell_shares=250)
    assert fired is True
    assert qty == 250


def test_nothing_uncovered_does_not_fire(monkeypatch):
    # Defensive: if the uncovered count is 0, there is nothing to exit — no suggestion.
    fired, qty = _run(monkeypatch, total_shares=200, lots_needed=2, sell_shares=0)
    assert fired is False
    assert qty is None
