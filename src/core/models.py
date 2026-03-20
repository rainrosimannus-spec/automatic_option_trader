"""
SQLAlchemy ORM models for trade tracking.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    String, Float, Integer, Boolean, DateTime, Text, Enum as SAEnum,
    ForeignKey, Index,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
import enum


class Base(DeclarativeBase):
    pass


# ── Enums ───────────────────────────────────────────────────
class TradeType(str, enum.Enum):
    SELL_PUT = "sell_put"
    BUY_PUT = "buy_put"          # close put
    SELL_CALL = "sell_call"      # covered call
    BUY_CALL = "buy_call"       # close covered call
    ASSIGNMENT = "assignment"    # put assigned → stock received
    CALLED_AWAY = "called_away"  # covered call assigned → stock sold
    BUY_STOCK = "buy_stock"     # direct stock purchase
    SELL_STOCK = "sell_stock"   # direct stock sale


class PositionStatus(str, enum.Enum):
    OPEN = "open"
    CLOSED = "closed"
    ASSIGNED = "assigned"
    EXPIRED = "expired"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    CANCELLED = "cancelled"
    ERROR = "error"


# ── Tables ──────────────────────────────────────────────────
class Trade(Base):
    """Individual trade (order fill)."""
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    position_id: Mapped[Optional[int]] = mapped_column(ForeignKey("positions.id"), nullable=True)
    symbol: Mapped[str] = mapped_column(String(10), index=True)
    trade_type: Mapped[TradeType] = mapped_column(SAEnum(TradeType))
    strike: Mapped[float] = mapped_column(Float)
    expiry: Mapped[str] = mapped_column(String(10))  # YYYYMMDD
    premium: Mapped[float] = mapped_column(Float)     # per share
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    fill_price: Mapped[float] = mapped_column(Float)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    order_status: Mapped[OrderStatus] = mapped_column(SAEnum(OrderStatus), default=OrderStatus.FILLED)
    delta_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    iv_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    vix_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, default="system")
    # "system" = placed by M&W, "ibkr_sync" = imported from IBKR executions, "manual" = manual import
    ibkr_exec_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    position: Mapped[Optional["Position"]] = relationship(back_populates="trades")

    __table_args__ = (
        Index("ix_trades_symbol_expiry", "symbol", "expiry"),
    )


class Position(Base):
    """Logical position tracking (put → assignment → covered call cycle)."""
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(10), index=True)
    status: Mapped[PositionStatus] = mapped_column(SAEnum(PositionStatus), default=PositionStatus.OPEN)
    position_type: Mapped[str] = mapped_column(String(20))  # "short_put", "stock", "covered_call"
    strike: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expiry: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    entry_premium: Mapped[float] = mapped_column(Float, default=0.0)
    cost_basis: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # for stock after assignment
    quantity: Mapped[int] = mapped_column(Integer, default=1)  # contracts or shares (100x)
    total_premium_collected: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    is_wheel: Mapped[bool] = mapped_column(Boolean, default=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    trades: Mapped[list["Trade"]] = relationship(back_populates="position", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_positions_status", "status"),
    )


class SystemState(Base):
    """Key-value store for system state (VIX, pause flags, etc.)."""
    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AccountSnapshot(Base):
    """Daily snapshot of account values for performance tracking."""
    __tablename__ = "account_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(10), index=True, unique=True)  # YYYY-MM-DD
    net_liquidation: Mapped[float] = mapped_column(Float, default=0.0)
    # Options account values
    options_premium_collected: Mapped[float] = mapped_column(Float, default=0.0)  # cumulative
    # Portfolio (long-term) values
    portfolio_nlv: Mapped[float] = mapped_column(Float, default=0.0)  # portfolio account NLV
    portfolio_invested: Mapped[float] = mapped_column(Float, default=0.0)
    portfolio_market_value: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
