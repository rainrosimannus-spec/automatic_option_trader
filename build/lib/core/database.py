"""
SQLite database engine and session management.
"""
from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.core.config import get_settings
from src.core.models import Base
import src.portfolio.models  # noqa: F401 — registers portfolio tables with Base
import src.consigliere.models  # noqa: F401 — registers consigliere tables with Base
import src.ipo.models  # noqa: F401 — registers IPO tables with Base


_engine = None
_SessionLocal = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        db_path = Path(settings.app.db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{db_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
    return _engine


def init_db() -> None:
    """Create all tables if they don't exist, and migrate new columns."""
    engine = get_engine()
    Base.metadata.create_all(engine)

    # Migrate: add columns that may be missing on existing DBs
    _migrate_columns(engine)


def _migrate_columns(engine):
    """Add new columns to existing tables (idempotent)."""
    migrations = [
        ("trades", "source", "VARCHAR(20) DEFAULT 'system'"),
        ("trades", "ibkr_exec_id", "VARCHAR(50)"),
        ("trade_suggestions", "rank", "INTEGER DEFAULT 0"),
        ("trade_suggestions", "rank_score", "REAL"),
        ("trade_suggestions", "funding_source", "VARCHAR(30)"),
        ("portfolio_transactions", "source", "VARCHAR(20) DEFAULT 'system'"),
        ("portfolio_transactions", "ibkr_exec_id", "VARCHAR(50)"),
        # Structural risk flags
        ("portfolio_watchlist", "risk_ai_disruption", "VARCHAR(10) DEFAULT 'none'"),
        ("portfolio_watchlist", "risk_regulatory", "VARCHAR(10) DEFAULT 'none'"),
        ("portfolio_watchlist", "risk_geopolitical", "VARCHAR(10) DEFAULT 'none'"),
        ("portfolio_watchlist", "risk_single_product", "VARCHAR(10) DEFAULT 'none'"),
        ("portfolio_watchlist", "risk_profitability", "VARCHAR(10) DEFAULT 'none'"),
        ("portfolio_watchlist", "risk_total_penalty", "REAL DEFAULT 0.0"),
        # Options exchange routing (v19+)
        ("trade_suggestions", "opt_exchange", "VARCHAR(15)"),
        ("trade_suggestions", "opt_currency", "VARCHAR(5)"),
    ]
    from sqlalchemy import text
    with engine.connect() as conn:
        for table, column, col_type in migrations:
            try:
                conn.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"
                ))
                conn.commit()
            except Exception:
                conn.rollback()

        # Backfill NULLs on risk columns (SQLite ALTER ADD doesn't set defaults on existing rows)
        risk_backfills = [
            ("portfolio_watchlist", "risk_ai_disruption", "'none'"),
            ("portfolio_watchlist", "risk_regulatory", "'none'"),
            ("portfolio_watchlist", "risk_geopolitical", "'none'"),
            ("portfolio_watchlist", "risk_single_product", "'none'"),
            ("portfolio_watchlist", "risk_profitability", "'none'"),
            ("portfolio_watchlist", "risk_total_penalty", "0.0"),
        ]
        for table, column, default_val in risk_backfills:
            try:
                from sqlalchemy import text
                conn.execute(text(
                    f"UPDATE {table} SET {column} = {default_val} WHERE {column} IS NULL"
                ))
                conn.commit()
            except Exception:
                conn.rollback()


def get_session_factory() -> sessionmaker[Session]:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionLocal


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Provide a transactional session scope."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
