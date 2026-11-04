"""scripts/screener_restore.py puts the watchlist rows of a before-snapshot back, datetimes included."""
import importlib.util
import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.portfolio.models import PortfolioWatchlist


def _load_script():
    spec = importlib.util.spec_from_file_location("screener_restore", Path("scripts/screener_restore.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_restore_watchlist_replaces_rows(monkeypatch):
    engine = create_engine("sqlite://")
    PortfolioWatchlist.__table__.create(engine)
    Session = sessionmaker(bind=engine)

    @contextmanager
    def get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    import src.core.database as dbmod
    monkeypatch.setattr(dbmod, "get_db", get_db)
    with get_db() as s:
        s.add(PortfolioWatchlist(symbol="BROKEN", name="x", tier="growth"))
        s.commit()
    mod = _load_script()
    rows = [{"symbol": "A", "name": "Alpha", "tier": "growth", "pending_removal": False,
             "screened_at": "2026-11-03T01:51:55.319599", "growth_score": 70.5, "not_a_column": 1},
            {"symbol": "B", "name": "Beta", "tier": "dividend", "pending_removal": True, "screened_at": None}]
    assert mod._restore_watchlist(rows) == 2
    with get_db() as s:
        got = {r.symbol: r for r in s.query(PortfolioWatchlist).all()}
    assert set(got) == {"A", "B"}
    assert got["A"].screened_at == datetime(2026, 11, 3, 1, 51, 55, 319599)
    assert got["A"].growth_score == 70.5 and got["B"].pending_removal is True
