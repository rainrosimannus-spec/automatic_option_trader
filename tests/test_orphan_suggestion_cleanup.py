"""Portfolio cleans its OWN orphaned buy cards (orders that died on their own).

A 'submitted' portfolio buy_stock card whose IBKR order is no longer working (rejected/Inactive/filled,
NOT cancelled by _cancel_stale) used to linger 'submitted' until the EOD sweep, inflating the pending
count. _expire_orphan_buy_suggestions expires those whose symbol has no live BUY order this scan, on the
portfolio's own account (never via the options-account reconciler), while keeping ones still working.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.core.database as db_mod
from src.core.models import Base
from src.core.suggestions import TradeSuggestion  # noqa: F401 (registers table)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    monkeypatch.setattr(db_mod, "_engine", eng)
    monkeypatch.setattr(db_mod, "_SessionLocal", sessionmaker(bind=eng))
    return eng


def _buyer():
    from src.core.config import get_settings
    from src.portfolio.buyer import PortfolioBuyer
    return PortfolioBuyer(ib=SimpleNamespace(), cfg=get_settings().portfolio)


def _add(db, symbol, status, source="portfolio", action="buy_stock"):
    db.add(TradeSuggestion(symbol=symbol, action=action, status=status, source=source,
                           quantity=1, limit_price=100.0))


def test_expires_only_cards_without_a_working_order(temp_db):
    with db_mod.get_db() as db:
        _add(db, "LIVE", "submitted")     # has a working order this scan → keep
        _add(db, "DEAD", "submitted")     # order died on its own → expire
        db.commit()

    n = _buyer()._expire_orphan_buy_suggestions(working_syms={"LIVE"})
    assert n == 1
    with db_mod.get_db() as db:
        by = {s.symbol: s.status for s in db.query(TradeSuggestion).all()}
        assert by == {"LIVE": "submitted", "DEAD": "expired"}


def test_leaves_options_and_non_submitted_alone(temp_db):
    with db_mod.get_db() as db:
        _add(db, "OPT", "submitted", source="options", action="sell_put")  # not portfolio → ignore
        _add(db, "PEND", "pending")                                         # not submitted → ignore
        _add(db, "GHOST", "submitted")                                      # portfolio ghost → expire
        db.commit()

    n = _buyer()._expire_orphan_buy_suggestions(working_syms=set())
    assert n == 1
    with db_mod.get_db() as db:
        by = {s.symbol: s.status for s in db.query(TradeSuggestion).all()}
        assert by["OPT"] == "submitted"      # options-account card untouched
        assert by["PEND"] == "pending"       # pending untouched
        assert by["GHOST"] == "expired"
