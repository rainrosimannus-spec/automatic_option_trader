"""The card lifetime (15:30 ET) was written for options cards; the compounder's US late-session pass
makes cards after 20:20 UTC that live minutes. POWL #3927 (2026-11-19): the suggestions page ran the
sweep at 20:54 and flipped a 'submitted' card to 'Expired (time limit)' while its IBKR bid was still
working (outside-RTH DAY order). The sweep now leaves portfolio 'submitted' cards to the scan's own
re-price pass; options cards and pending/approved portfolio cards expire as before."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.core.database as db_mod
from src.core.models import Base
from src.core.suggestions import TradeSuggestion, get_pending_suggestions


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    monkeypatch.setattr(db_mod, "_engine", eng)
    monkeypatch.setattr(db_mod, "_SessionLocal", sessionmaker(bind=eng))
    return eng


def _add(db, symbol, status, source, action="buy_stock", past=True):
    exp = datetime.utcnow() + timedelta(minutes=-10 if past else 60)
    db.add(TradeSuggestion(symbol=symbol, action=action, status=status, source=source,
                           quantity=1, limit_price=100.0, expires_at=exp))


def _status(symbol):
    with db_mod.get_db() as db:
        s = db.query(TradeSuggestion).filter(TradeSuggestion.symbol == symbol).first()
        return s.status, s.review_note


def test_submitted_portfolio_card_with_live_order_is_not_time_expired(temp_db):
    with db_mod.get_db() as db:
        _add(db, "POWL", "submitted", "portfolio")          # live bid resting after 15:30 ET
        _add(db, "ROL", "pending", "portfolio")             # never approved → expires as before
        _add(db, "AAPL", "submitted", "options")            # options side untouched
        _add(db, "MSFT", "submitted", "portfolio", past=False)
    get_pending_suggestions()
    assert _status("POWL") == ("submitted", None)
    assert _status("ROL") == ("expired", "Expired (time limit)")
    assert _status("AAPL") == ("expired", "Expired (time limit)")
    assert _status("MSFT") == ("submitted", None)
