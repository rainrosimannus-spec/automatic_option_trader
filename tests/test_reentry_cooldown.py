"""Post-sale re-entry cooldown (Rain, 2026-11-10).

Live fills: of 11 puts re-sold within a week of selling the same name's stock, 4 were
assigned and two became the CIEN 425 and VRT 225 bags. With the −8% stop live, a name
that just stopped out must not be re-assigned on the bounce. Shared predicate, live gate
in RiskManager.can_open_put, MarsWalk section-4 gate, default OFF everywhere (sweep sets it).
"""
from datetime import date, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.config import RiskConfig
from src.core.models import Base, Trade, TradeType, OrderStatus
from src.marswalk.engine import Params
from src.strategy.wheel_rules import reentry_cooldown_active


def test_predicate_counts_weekdays():
    mon = date(2026, 11, 9)
    assert reentry_cooldown_active(mon, mon, 5) is True            # sale day itself
    assert reentry_cooldown_active(mon, date(2026, 11, 13), 5) is True   # Fri: 4 weekdays passed
    assert reentry_cooldown_active(mon, date(2026, 11, 16), 5) is False  # next Mon: 5 passed
    assert reentry_cooldown_active(datetime(2026, 11, 9, 20, 1), datetime(2026, 11, 16, 9), 5) is False


def test_predicate_off_and_missing():
    assert reentry_cooldown_active(date(2026, 11, 9), date(2026, 11, 9), 0) is False
    assert reentry_cooldown_active(None, date(2026, 11, 9), 5) is False
    assert reentry_cooldown_active(date(2026, 11, 9), None, 5) is False


def test_defaults_are_off_live_and_sim():
    rc = RiskConfig()
    assert rc.put_reentry_cooldown_days == 0 and rc.put_reentry_cooldown_scope == "stop"
    p = Params()
    assert p.put_reentry_cooldown_days == 0 and p.put_reentry_cooldown_scope == "stop"


def _session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)


def _rm(monkeypatch, days, scope, factory):
    from src.strategy import risk as risk_mod
    from contextlib import contextmanager

    @contextmanager
    def _get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()
    monkeypatch.setattr(risk_mod, "get_db", _get_db)
    rm = risk_mod.RiskManager.__new__(risk_mod.RiskManager)
    rm.cfg = RiskConfig(put_reentry_cooldown_days=days, put_reentry_cooldown_scope=scope)
    return rm


def _sale(db, sym, notes, days_ago):
    db.add(Trade(symbol=sym, trade_type=TradeType.SELL_STOCK, strike=0.0, expiry="",
                 premium=0.0, quantity=100, fill_price=100.0, order_status=OrderStatus.FILLED,
                 notes=notes, created_at=datetime.utcnow() - timedelta(days=days_ago)))
    db.commit()


def test_live_gate_scope_stop_blocks_only_stop_sales(monkeypatch):
    factory = _session()
    db = factory()
    _sale(db, "VRT", "IBKR sync: SLD 200.0 VRT @ $251.10", 1)      # manual sale
    _sale(db, "BKNG", "wheel_stop: sell 200 BKNG @ 162.14 — mid=162.19", 1)
    rm = _rm(monkeypatch, 10, "stop", factory)
    assert rm.check_reentry_cooldown("VRT").allowed is True
    chk = rm.check_reentry_cooldown("BKNG")
    assert chk.allowed is False and "cooldown" in chk.reason
    assert rm.check_reentry_cooldown("AAPL").allowed is True        # never sold


def test_live_gate_scope_any_sale_blocks_manual_too(monkeypatch):
    factory = _session()
    db = factory()
    _sale(db, "VRT", "IBKR sync: SLD 200.0 VRT @ $251.10", 1)
    rm = _rm(monkeypatch, 10, "any_sale", factory)
    assert rm.check_reentry_cooldown("VRT").allowed is False


def test_live_gate_expires_and_off_at_zero(monkeypatch):
    factory = _session()
    db = factory()
    _sale(db, "BKNG", "wheel_stop: sell 200 BKNG @ 162.14", 30)    # a month ago
    rm = _rm(monkeypatch, 10, "stop", factory)
    assert rm.check_reentry_cooldown("BKNG").allowed is True
    _sale(db, "DECK", "wheel_stop: sell 500 DECK @ 90", 0)
    rm0 = _rm(monkeypatch, 0, "stop", factory)
    assert rm0.check_reentry_cooldown("DECK").allowed is True


def test_can_open_put_runs_the_gate(monkeypatch):
    from src.strategy import risk as risk_mod
    src = open(risk_mod.__file__).read()
    body = src[src.index("def can_open_put("):]
    assert body.index("check_reentry_cooldown") < body.index("check_iv_rank")


def test_dashboard_chip_present():
    html = open("src/web/templates/dashboard.html").read()
    assert "Re-entry cooldown" in html
