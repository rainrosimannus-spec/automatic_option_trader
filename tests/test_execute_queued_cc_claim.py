"""Covered-call executor must atomically claim a pending CC before dispatching it.

Regression guard for the 'margin-free shortcut skips the atomic claim' landmine
(the son's clone hit the live version; ported here as an inert-but-real defect):

  job_execute_queued's CC fast-path used to do `selected = s; break` WITHOUT flipping
  the row queued/pending -> executing. _execute_approved_order_inner only runs
  approved/executing rows, so it silently returned and the order was never placed --
  the row hung forever. The shortcut also keyed on the dead action string "sell_call"
  while real covered calls are created as "sell_covered_call" (wheel.py), so the same
  bug sat latent on the cc_pending margin-exemption too.

These tests exercise the real job_execute_queued with the connection/loop/lock and
IBKR order placement stubbed, asserting the claim happens and the row is dispatched.
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
    # Match production (database.py): expire_on_commit=False, so a committed row
    # stays usable after its session closes (job_execute_queued logs selected.id
    # after the `with get_db()` block).
    monkeypatch.setattr(db_mod, "_SessionLocal", sessionmaker(bind=eng, expire_on_commit=False))
    return eng


def _add(db, symbol, action, status="pending", source="options", rank=1):
    db.add(TradeSuggestion(symbol=symbol, action=action, status=status, source=source,
                           quantity=1, limit_price=1.0, strike=100.0, rank=rank))


def _acct(net_liq, maint):
    return SimpleNamespace(net_liquidation=net_liq, maintenance_margin=maint)


@pytest.fixture
def stub_exec(monkeypatch):
    """Neutralize connection/loop/lock/market gates; capture dispatched suggestion ids.

    Returns the (mutable) dispatched list plus a setter for the account summary so
    each test can pick healthy vs over-ceiling margin.
    """
    import time as _time

    import src.scheduler.jobs as jobs
    import src.broker.connection as conn
    import src.broker.account as account
    import src.strategy.risk as risk_mod
    import src.strategy.universe as universe_mod
    import src.core.suggestions as suggestions

    monkeypatch.setattr(jobs, "_ensure_event_loop", lambda: None)
    monkeypatch.setattr(jobs, "_is_paused", lambda: False)
    monkeypatch.setattr(jobs, "is_connected", lambda: True)

    class _Lock:
        def acquire(self, timeout=None):
            return True

        def release(self):
            pass

    monkeypatch.setattr(conn, "get_ib_lock", lambda: _Lock())

    # Fixed dynamic margin ceiling so headroom is deterministic and no market data
    # is touched.
    class _RM:
        def __init__(self, *a, **k):
            pass

        def dynamic_margin_ceiling(self):
            return 0.60

    class _UM:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(risk_mod, "RiskManager", _RM)
    monkeypatch.setattr(universe_mod, "UniverseManager", _UM)

    monkeypatch.setattr(_time, "sleep", lambda *a, **k: None)

    dispatched = []
    monkeypatch.setattr(suggestions, "_execute_approved_order", lambda sid: dispatched.append(sid))

    state = {"acct": _acct(500_000.0, 25_000.0)}  # healthy by default (5% margin)
    monkeypatch.setattr(account, "get_account_summary", lambda: state["acct"])

    return SimpleNamespace(dispatched=dispatched, set_acct=lambda a: state.__setitem__("acct", a))


def test_pending_covered_call_is_claimed_and_dispatched(temp_db, stub_exec):
    """The core regression: a pending sell_covered_call is atomically claimed to
    'executing' (not left 'pending') and handed to the order executor."""
    from src.scheduler.jobs import job_execute_queued

    with db_mod.get_db() as db:
        _add(db, "NVDA", "sell_covered_call", status="pending")
        db.commit()
        cc_id = db.query(TradeSuggestion).filter_by(symbol="NVDA").first().id

    job_execute_queued()

    assert stub_exec.dispatched == [cc_id]
    with db_mod.get_db() as db:
        row = db.query(TradeSuggestion).filter_by(symbol="NVDA").first()
        assert row.status == "executing"


def test_covered_call_exempt_from_margin_ceiling(temp_db, stub_exec):
    """Over the margin ceiling, a pending sell_covered_call must NOT block the
    executor (CCs are margin-free) -- proving the cc_pending exemption now matches
    the real 'sell_covered_call' action string, and the CC still gets claimed."""
    from src.scheduler.jobs import job_execute_queued

    stub_exec.set_acct(_acct(500_000.0, 490_000.0))  # 98% margin, over any ceiling
    with db_mod.get_db() as db:
        _add(db, "NVDA", "sell_covered_call", status="pending")
        db.commit()
        cc_id = db.query(TradeSuggestion).filter_by(symbol="NVDA").first().id

    job_execute_queued()

    assert stub_exec.dispatched == [cc_id]
    with db_mod.get_db() as db:
        assert db.query(TradeSuggestion).filter_by(symbol="NVDA").first().status == "executing"


def test_margin_ceiling_blocks_when_no_cc_pending(temp_db, stub_exec):
    """Over the margin ceiling with only a put pending (no CC), the executor blocks:
    nothing dispatched and the put stays pending -- the exemption is CC-specific."""
    from src.scheduler.jobs import job_execute_queued

    stub_exec.set_acct(_acct(500_000.0, 490_000.0))  # 98% margin, over any ceiling
    with db_mod.get_db() as db:
        _add(db, "AAPL", "sell_put", status="pending")
        db.commit()

    job_execute_queued()

    assert stub_exec.dispatched == []
    with db_mod.get_db() as db:
        assert db.query(TradeSuggestion).filter_by(symbol="AAPL").first().status == "pending"
