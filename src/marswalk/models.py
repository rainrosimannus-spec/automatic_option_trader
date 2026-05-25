"""
MarsWalk DB models — FULLY ISOLATED from the live trading DB.

Uses its own SQLite file (data/marswalk.db) with its own engine/sessionmaker,
so a backtest can never read or write trades.db. Nothing here imports
src.core.database.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Index, Text,
    create_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

Base = declarative_base()

_DB_PATH = Path("data/marswalk.db")
_engine = None
_Session = None


def get_engine():
    global _engine
    if _engine is None:
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(
            f"sqlite:///{_DB_PATH}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(_engine)
    return _engine


def get_session_factory():
    global _Session
    if _Session is None:
        _Session = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _Session


@contextmanager
def get_mw_db():
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class MarketBar(Base):
    """Cached daily underlying close + IV, per symbol per regime window.

    Param-invariant: fetched once from IBKR historical data, reused across every
    Run (different DTE/delta) of the same regime.
    """
    __tablename__ = "mw_market_bars"
    id = Column(Integer, primary_key=True, autoincrement=True)
    regime_id = Column(String(40), index=True)
    symbol = Column(String(12), index=True)
    date = Column(String(10))  # YYYY-MM-DD
    close = Column(Float)
    iv = Column(Float)
    __table_args__ = (
        Index("ix_mw_bar_unique", "regime_id", "symbol", "date", unique=True),
    )


class Run(Base):
    """One backtest of one regime under one parameter set."""
    __tablename__ = "mw_runs"
    id = Column(Integer, primary_key=True, autoincrement=True)
    regime_id = Column(String(40), index=True)
    regime_name = Column(String(120))
    category = Column(String(20))
    rank = Column(Integer)

    # Parameter snapshot (what was tested)
    dte_min = Column(Integer)
    dte_max = Column(Integer)
    delta_min = Column(Float)
    delta_max = Column(Float)
    params_json = Column(Text)  # full param dict for forward-compat

    # Results
    start_capital = Column(Float)
    final_nlv = Column(Float)
    final_return_pct = Column(Float)
    target_return_pct = Column(Float)
    max_drawdown_pct = Column(Float)
    n_trades = Column(Integer, default=0)
    n_assignments = Column(Integer, default=0)
    status = Column(String(20), default="done")  # done | error | running
    error = Column(String(400), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    points = relationship("Point", back_populates="run",
                          cascade="all, delete-orphan")


class Point(Base):
    """One equity-curve point for a Run: date, NLV, return %, target %."""
    __tablename__ = "mw_points"
    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(Integer, ForeignKey("mw_runs.id"), index=True)
    date = Column(String(10))
    nlv = Column(Float)
    return_pct = Column(Float)
    target_pct = Column(Float)

    run = relationship("Run", back_populates="points")
