"""Double-buy guard: fold TODAY's executed-but-not-yet-synced compounder buys into `cur`
(2026-08-26 VRT).

VRT was bought by two green late-session scans ~17min apart and overshot its target by ~$14k: the
first order FILLED between the scans, but the holdings position-sync hadn't run yet, so the second
scan saw the name still underweight (shares not in `held`, order no longer resting, and the pending
map only covers queued/executing — never executed) and bought it again. `_unsynced_executed_buy_map`
folds those fills back in, and drops each one the moment the holdings sync picks it up (holding
.updated_at advances past the fill) so it can never over-count and wrongly block buying.
"""
import datetime as dt
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import Base
from src.core.suggestions import TradeSuggestion
from src.portfolio.models import PortfolioWatchlist, PortfolioHolding
from src.portfolio.buyer import PortfolioBuyer
import src.portfolio.buyer as buyer_mod
import src.portfolio.fx as pfx

TODAY = dt.date.today()
def _t(h, m=0):
    return dt.datetime(TODAY.year, TODAY.month, TODAY.day, h, m)


def _setup(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    sess = sessionmaker(bind=eng)()

    @contextmanager
    def fake_get_db():
        yield sess
    monkeypatch.setattr(buyer_mod, "get_db", fake_get_db)
    monkeypatch.setattr(pfx, "load_fx_rates", lambda: {})
    monkeypatch.setattr(pfx, "to_base", lambda n, ccy, rates: n)
    return sess


def _buyer():
    b = PortfolioBuyer.__new__(PortfolioBuyer)
    class _Cfg:
        cash_yield_symbol = "XEON"
    b.cfg = _Cfg()
    return b


def _wl(sess, sym, ccy="USD"):
    sess.add(PortfolioWatchlist(symbol=sym, currency=ccy)); sess.flush()

def _sugg(sess, sym, status, qty=100, price=100.0, created=None, reviewed=None):
    sess.add(TradeSuggestion(symbol=sym, source="portfolio", action="buy_stock", status=status,
             quantity=qty, limit_price=price, est_cost=qty * price,
             created_at=created or _t(10), reviewed_at=reviewed)); sess.flush()

def _hold(sess, sym, updated):
    sess.add(PortfolioHolding(symbol=sym, shares=1, updated_at=updated)); sess.flush()


def test_unsynced_fill_is_folded(monkeypatch):
    # Filled at 10:00, holdings last synced 09:00 (before) → shares not in `held` yet → fold.
    sess = _setup(monkeypatch)
    _wl(sess, "VRT")
    _sugg(sess, "VRT", "executed", qty=318, price=265.67, reviewed=_t(10))
    _hold(sess, "VRT", _t(9))
    m = _buyer()._unsynced_executed_buy_map()
    assert round(m.get("VRT", 0.0)) == round(318 * 265.67)


def test_synced_fill_not_folded(monkeypatch):
    # Holdings synced at 11:00 (after the 10:00 fill) → shares already in `held` → must NOT fold
    # (folding here would double-count and wrongly block further buying).
    sess = _setup(monkeypatch)
    _wl(sess, "VRT")
    _sugg(sess, "VRT", "executed", qty=318, price=265.67, reviewed=_t(10))
    _hold(sess, "VRT", _t(11))
    assert "VRT" not in _buyer()._unsynced_executed_buy_map()


def test_new_position_no_holding_row_is_folded(monkeypatch):
    # First-ever buy of a name: no holding row yet → definitely not in `held` → fold.
    sess = _setup(monkeypatch)
    _wl(sess, "FIX")
    _sugg(sess, "FIX", "executed", qty=40, price=1680.0, reviewed=_t(10))
    assert round(_buyer()._unsynced_executed_buy_map().get("FIX", 0.0)) == round(40 * 1680.0)


def test_executing_status_not_folded(monkeypatch):
    # 'executing' is the pending map's job; this map is executed-only (else double-count).
    sess = _setup(monkeypatch)
    _wl(sess, "VRT")
    _sugg(sess, "VRT", "executing", qty=100, price=100.0, reviewed=_t(10))
    assert "VRT" not in _buyer()._unsynced_executed_buy_map()


def test_park_symbol_excluded(monkeypatch):
    sess = _setup(monkeypatch)
    _wl(sess, "XEON")
    _sugg(sess, "XEON", "executed", qty=1000, price=100.0, reviewed=_t(10))
    assert "XEON" not in _buyer()._unsynced_executed_buy_map()


def test_falls_back_to_created_at_when_no_reviewed_at(monkeypatch):
    # reviewed_at NULL → use created_at (10:00); holdings at 09:00 (before) → fold.
    sess = _setup(monkeypatch)
    _wl(sess, "VRT")
    _sugg(sess, "VRT", "executed", qty=10, price=100.0, created=_t(10), reviewed=None)
    _hold(sess, "VRT", _t(9))
    assert round(_buyer()._unsynced_executed_buy_map().get("VRT", 0.0)) == 1000
