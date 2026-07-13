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
_options_engine = None
_SessionLocal = None


def get_engine():
    """Main DB engine (data/trades.db) — portfolio tables + shared infra
    (system_state, account_snapshots, trade_suggestions, earnings_cache,
    ipo_watchlist) and the frozen legacy positions/trades."""
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


def get_options_engine():
    """Option-trader ledger engine (data/options.db) — holds ONLY the live
    `positions` and `trades` tables. Sessions route those two models here via
    `binds`; everything else uses the main engine. This keeps the option
    trader's books physically separate from the portfolio's while both run in
    one process behind the shared overview dashboard."""
    global _options_engine
    if _options_engine is None:
        settings = get_settings()
        opt_path = Path(settings.app.options_db_path)
        opt_path.parent.mkdir(parents=True, exist_ok=True)
        _options_engine = create_engine(
            f"sqlite:///{opt_path}",
            echo=False,
            connect_args={"check_same_thread": False},
        )
    return _options_engine


def _ledger_tables():
    """The option-ledger tables that live in the options DB."""
    from src.core.models import Position, Trade
    return [Position.__table__, Trade.__table__]


def init_db() -> None:
    """Create all tables if they don't exist, and migrate new columns.

    The options engine gets ONLY the option-ledger tables (positions, trades);
    the main engine gets everything (the legacy positions/trades tables stay
    physically present there but are no longer read — sessions route those two
    models to the options engine via binds)."""
    engine = get_engine()
    opt_engine = get_options_engine()

    Base.metadata.create_all(engine)
    Base.metadata.create_all(opt_engine, tables=_ledger_tables())

    # Migrate new columns on both DBs (idempotent; ALTERs no-op when the column
    # already exists or the table is absent).
    _migrate_columns(engine)
    _migrate_columns(opt_engine)


def _migrate_columns(engine):
    """Add new columns to existing tables (idempotent)."""
    migrations = [
        ("trades", "source", "VARCHAR(20) DEFAULT 'system'"),
        ("trades", "ibkr_exec_id", "VARCHAR(50)"),
        # Decision-time option quote for execution-quality analysis (Consigliere)
        ("trades", "bid_at_entry", "REAL"),
        ("trades", "ask_at_entry", "REAL"),
        ("trades", "mid_at_entry", "REAL"),
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
        ("portfolio_watchlist", "raw_score", "REAL DEFAULT 0.0"),
        # Augmentation diagnostics — full Claude proposal JSON per audit row
        ("augmentation_audit", "raw_proposal_json", "TEXT DEFAULT ''"),
        ("portfolio_watchlist", "forward_growth_score", "REAL DEFAULT 0.0"),
        # Options exchange routing (v19+)
        ("trade_suggestions", "opt_exchange", "VARCHAR(15)"),
        ("trade_suggestions", "opt_currency", "VARCHAR(5)"),
        ("trade_suggestions", "trailing_stop_pct", "REAL"),
        ("trade_suggestions", "trailing_peak_price", "REAL"),
        # Intraday-loss halt support (May 2026)
        ("positions", "unrealized_pnl", "REAL DEFAULT 0.0"),
        # Per-account capital injections (May 2026)
        ("portfolio_capital_injections", "account_id", "VARCHAR(20)"),
        # Consigliere dollarize / n-confidence framework (May 2026)
        ("consigliere_memos", "impact_eur_month", "REAL"),
        ("consigliere_memos", "sample_n", "INTEGER"),
        ("consigliere_memos", "confidence", "VARCHAR(10)"),
        # IPO rider — SEC-parsed lock-up confidence (June 2026)
        ("ipo_watchlist", "lockup_confidence", "VARCHAR(12)"),
        ("ipo_watchlist", "lockup_source", "VARCHAR(20)"),
        # IPO rider — SEC-parsed ticker + first-trading-day source/confidence (June 2026)
        ("ipo_watchlist", "date_confidence", "VARCHAR(12)"),
        ("ipo_watchlist", "date_source", "VARCHAR(20)"),
        # Execution-quality measurement — decision-time option quote on the suggestion (June 2026)
        ("trade_suggestions", "bid_at_entry", "REAL"),
        ("trade_suggestions", "ask_at_entry", "REAL"),
        ("trade_suggestions", "mid_at_entry", "REAL"),
        # Foreign-currency FX funding — count failed pre-buy conversion attempts so an unfundable
        # foreign buy expires+alerts instead of churning 'approved' forever (June 2026)
        ("trade_suggestions", "funding_attempts", "INTEGER DEFAULT 0"),
        # Foreign-option identity resolved AT SCAN TIME (real derivatives exchange e.g. FTA/EUREX
        # + conId + tradingClass) so the executor places a foreign option with no placement-time
        # IBKR lookup / event-loop race (July 2026)
        ("trade_suggestions", "opt_con_id", "INTEGER"),
        ("trade_suggestions", "trading_class", "VARCHAR(20)"),
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
            ("portfolio_watchlist", "raw_score", "0.0"),
            ("portfolio_watchlist", "forward_growth_score", "0.0"),
            # Intraday-loss halt support (May 2026)
            ("positions", "unrealized_pnl", "0.0"),
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
        from src.core.models import Position, Trade
        # Route the option-ledger models to the options DB; default everything
        # else to the main DB. A session can span both engines because no single
        # SQL statement joins these two models against any other table.
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            binds={Position: get_options_engine(), Trade: get_options_engine()},
            expire_on_commit=False,
        )
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
