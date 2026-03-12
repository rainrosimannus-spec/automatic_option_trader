"""
Portfolio database models — three-tier long-term portfolio.

Tiers: dividend (25%), breakthrough (25%), growth (50%)
Entry methods: direct buy OR sell cash-secured put at target strike
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    String, Float, Integer, Boolean, DateTime, Text, Index,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import Base


class PortfolioHolding(Base):
    """Current stock holdings in the long-term portfolio."""
    __tablename__ = "portfolio_holdings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(10), index=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    exchange: Mapped[str] = mapped_column(String(10), default="SMART")
    currency: Mapped[str] = mapped_column(String(5), default="USD")
    sector: Mapped[str] = mapped_column(String(50), default="")
    tier: Mapped[str] = mapped_column(String(15), default="growth")  # "dividend", "breakthrough", "growth"

    shares: Mapped[int] = mapped_column(Integer, default=0)
    avg_cost: Mapped[float] = mapped_column(Float, default=0.0)
    total_invested: Mapped[float] = mapped_column(Float, default=0.0)
    current_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    market_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unrealized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    unrealized_pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    total_dividends: Mapped[float] = mapped_column(Float, default=0.0)

    # Entry method tracking
    entry_method: Mapped[str] = mapped_column(String(20), default="direct_buy")  # "direct_buy" or "put_entry"
    target_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # desired entry price

    first_bought: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_bought: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Pending removal flag — set when stock drops off screened universe but has open position
    # Cleared automatically when position is closed or stock re-qualifies
    pending_removal: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_removal_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PortfolioTransaction(Base):
    """All portfolio buy/sell/dividend/put-entry transactions."""
    __tablename__ = "portfolio_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(10), index=True)
    action: Mapped[str] = mapped_column(String(20))
    # Actions: "buy", "sell", "dividend", "interest", "sell_put", "put_assigned", "put_expired"
    shares: Mapped[int] = mapped_column(Integer, default=0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(5), default="USD")

    # Buy signal info
    signal: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    sma_200: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rsi: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    discount_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Put-entry fields
    strike: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expiry: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    premium_collected: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    tier: Mapped[str] = mapped_column(String(15), default="growth")
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, default="system")
    ibkr_exec_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("ix_ptx_symbol_date", "symbol", "created_at"),
    )


class PortfolioWatchlist(Base):
    """Screened stocks for the portfolio — three tiers, refreshed annually."""
    __tablename__ = "portfolio_watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(10), index=True, unique=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    exchange: Mapped[str] = mapped_column(String(10), default="SMART")
    currency: Mapped[str] = mapped_column(String(5), default="USD")
    sector: Mapped[str] = mapped_column(String(50), default="")
    tier: Mapped[str] = mapped_column(String(15), default="growth")  # "dividend", "breakthrough", "growth"
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    composite_score: Mapped[float] = mapped_column(Float, default=0.0)
    growth_score: Mapped[float] = mapped_column(Float, default=0.0)
    valuation_score: Mapped[float] = mapped_column(Float, default=0.0)
    quality_score: Mapped[float] = mapped_column(Float, default=0.0)
    category: Mapped[str] = mapped_column(String(15), default="growth")  # legacy compat
    screened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Current buy metrics (updated on each scan)
    current_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sma_200: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    discount_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rsi_14: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    buy_signal: Mapped[bool] = mapped_column(Boolean, default=False)
    signal_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # Put-entry state
    has_open_put: Mapped[bool] = mapped_column(Boolean, default=False)
    put_strike: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    put_expiry: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    target_buy_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # ── Structural risk flags ──
    # Each is "none", "low", "medium", "high"
    risk_ai_disruption: Mapped[str] = mapped_column(String(10), default="none")
    risk_regulatory: Mapped[str] = mapped_column(String(10), default="none")
    risk_geopolitical: Mapped[str] = mapped_column(String(10), default="none")
    risk_single_product: Mapped[str] = mapped_column(String(10), default="none")
    risk_profitability: Mapped[str] = mapped_column(String(10), default="none")
    risk_total_penalty: Mapped[float] = mapped_column(Float, default=0.0)  # sum of all penalties

    # Pending removal flag — set when stock drops off screened universe but has open position
    # Cleared automatically when position is closed or stock re-qualifies
    pending_removal: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_removal_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PortfolioPutEntry(Base):
    """Active put-entry positions — CSPs sold to enter stock positions."""
    __tablename__ = "portfolio_put_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(10), index=True)
    tier: Mapped[str] = mapped_column(String(15), default="growth")
    exchange: Mapped[str] = mapped_column(String(10), default="SMART")
    currency: Mapped[str] = mapped_column(String(5), default="USD")

    strike: Mapped[float] = mapped_column(Float)
    expiry: Mapped[str] = mapped_column(String(10))
    contracts: Mapped[int] = mapped_column(Integer, default=1)
    premium: Mapped[float] = mapped_column(Float, default=0.0)  # premium collected per contract
    total_premium: Mapped[float] = mapped_column(Float, default=0.0)

    status: Mapped[str] = mapped_column(String(20), default="open")
    # "open", "assigned", "expired", "closed"

    # If assigned, link to the resulting holding
    assigned_shares: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    effective_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # effective_cost = strike - premium_collected (real entry price)

    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Pending removal flag — set when stock drops off screened universe but has open position
    # Cleared automatically when position is closed or stock re-qualifies
    pending_removal: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_removal_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PortfolioState(Base):
    """Key-value store for portfolio state."""
    __tablename__ = "portfolio_state"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    # Pending removal flag — set when stock drops off screened universe but has open position
    # Cleared automatically when position is closed or stock re-qualifies
    pending_removal: Mapped[bool] = mapped_column(Boolean, default=False)
    pending_removal_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PortfolioCapitalInjection(Base):
    """Tracks every cash injection (deposit) into the portfolio account in USD."""
    __tablename__ = "portfolio_capital_injections"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String, nullable=False)
    amount_original = Column(Float, nullable=False)
    currency = Column(String, nullable=False)
    eur_usd_rate = Column(Float, nullable=True)
    amount_usd = Column(Float, nullable=False)
    notes = Column(String, nullable=True)
    source = Column(String, default="manual")
    created_at = Column(DateTime, default=datetime.utcnow)
