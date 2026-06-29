"""Purge-date guard for the Flex deposit sync (2026-06-29).

Flex publishes cash transactions with a ~1-day lag, so a deposit made TODAY isn't in the statement
yet and must be hand-bridged so the return chart isn't a fake gain. The nightly sync purges manual
rows before re-adding from Flex — but it must only purge rows UP TO the latest date Flex actually
covers, or it wipes today's bridge before Flex has the real row (the deposit-as-return recurrence).
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.core.database as db_mod
from src.core.models import Base
from src.portfolio.models import PortfolioCapitalInjection  # noqa: F401 (registers table)
import src.portfolio.capital_injections as ci


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}",
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    monkeypatch.setattr(db_mod, "_engine", eng)
    monkeypatch.setattr(db_mod, "_SessionLocal", sessionmaker(bind=eng))
    return eng


def _row(db, date, amount, source):
    db.add(PortfolioCapitalInjection(
        date=date, amount_original=amount, currency="EUR",
        eur_usd_rate=1.06, amount_usd=amount * 1.06,
        notes="t", source=source, account_id="U26413485"))


def test_bridge_after_flex_coverage_survives_purge(temp_db, monkeypatch):
    # Seed: an OLD manual row (covered by Flex) + a TODAY bridge (beyond Flex's coverage).
    with db_mod.get_db() as db:
        _row(db, "2026-06-20", 40000.0, "manual_bootstrap")   # should be purged (<= flex latest)
        _row(db, "2026-06-29", 100000.0, "manual_bootstrap")  # bridge — must SURVIVE
        db.commit()

    # Flex returns deposits only through 2026-06-25 (today's 6/29 not published yet).
    monkeypatch.setattr(ci, "fetch_flex_statement", lambda *a, **k: "<xml/>")
    monkeypatch.setattr(ci, "parse_deposits_from_flex", lambda *a, **k: [
        {"date": "2026-06-24", "amount_original": 10000.0, "currency": "EUR", "notes": "f"},
        {"date": "2026-06-25", "amount_original": 200000.0, "currency": "EUR", "notes": "f"},
    ])
    monkeypatch.setattr(ci, "_get_fx_rate_to_usd", lambda *a, **k: 1.06)

    added = ci.sync_injections_from_ibkr(
        account_id="U26413485", flex_token="t", flex_query_id="q", include_withdrawals=True)
    assert added == 2  # the two Flex rows

    with db_mod.get_db() as db:
        rows = db.query(PortfolioCapitalInjection).all()
        by = {(r.date, r.source) for r in rows}
        # OLD manual row purged; bridge survives; Flex rows added.
        assert ("2026-06-20", "manual_bootstrap") not in by
        assert ("2026-06-29", "manual_bootstrap") in by, "today's bridge must survive until Flex covers it"
        assert ("2026-06-25", "ibkr_flex") in by
        assert ("2026-06-24", "ibkr_flex") in by
        # Total invested = bridge 100k + Flex 10k + 200k = 310k (old 40k bootstrap replaced/dropped).
        assert sum(r.amount_original for r in rows) == pytest.approx(310000.0)


def test_no_deposits_keeps_all_bootstrap(temp_db, monkeypatch):
    # If Flex returns nothing (still broken/throttled), KEEP the safety net — never wipe to empty.
    with db_mod.get_db() as db:
        _row(db, "2026-06-20", 40000.0, "manual_bootstrap")
        db.commit()
    monkeypatch.setattr(ci, "fetch_flex_statement", lambda *a, **k: "<xml/>")
    monkeypatch.setattr(ci, "parse_deposits_from_flex", lambda *a, **k: [])

    added = ci.sync_injections_from_ibkr(
        account_id="U26413485", flex_token="t", flex_query_id="q")
    assert added == 0
    with db_mod.get_db() as db:
        assert db.query(PortfolioCapitalInjection).count() == 1  # bootstrap preserved
