"""
IPO Rider database models.

Two-phase IPO strategy:
  Phase 1 — Day-one flip: buy after opening auction, trailing stop sell
  Phase 2 — Lockup re-entry: trailing stop buy after lockup dip, hold long-term
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    String, Float, Integer, Boolean, DateTime, Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import Base


class IpoWatchlist(Base):
    """IPO companies to monitor and trade."""
    __tablename__ = "ipo_watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_name: Mapped[str] = mapped_column(String(100))
    expected_ticker: Mapped[str] = mapped_column(String(10), index=True)
    exchange: Mapped[str] = mapped_column(String(10), default="SMART")
    currency: Mapped[str] = mapped_column(String(5), default="USD")

    # Expected IPO date (user enters best estimate)
    expected_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD

    # Phase 1 — Day-one flip settings
    flip_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    flip_amount: Mapped[float] = mapped_column(Float, default=5000.0)  # EUR/USD to invest
    flip_trailing_pct: Mapped[float] = mapped_column(Float, default=8.0)  # trailing stop %
    flip_stop_loss_pct: Mapped[float] = mapped_column(Float, default=12.0)  # hard floor stop-loss %
    flip_max_hold_days: Mapped[int] = mapped_column(Integer, default=5)  # sell at market after N trading days

    # Phase 2 — Lockup re-entry settings
    lockup_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    lockup_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # YYYY-MM-DD
    lockup_dip_pct: Mapped[float] = mapped_column(Float, default=10.0)  # buy when price dips x% from pre-lockup
    lockup_trailing_buy_pct: Mapped[float] = mapped_column(Float, default=5.0)  # trailing buy triggers on x% bounce
    lockup_amount: Mapped[float] = mapped_column(Float, default=10000.0)  # EUR/USD to invest

    # Status tracking
    status: Mapped[str] = mapped_column(String(20), default="watching")
    # "watching"       — scanning IBKR for ticker to become available
    # "ipo_trading"    — ticker found, phase 1 in progress
    # "flip_done"      — day-one flip complete (sold)
    # "lockup_waiting" — waiting for lockup expiry
    # "lockup_trading" — lockup expired, monitoring for dip/re-entry
    # "lockup_done"    — re-entry complete, now in long-term portfolio
    # "cancelled"      — user cancelled

    # Phase 1 execution tracking
    flip_entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    flip_shares: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    flip_entry_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    flip_exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    flip_exit_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    flip_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    flip_order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # IBKR trailing stop order
    flip_stop_order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # hard stop-loss order

    # Phase 2 execution tracking
    pre_lockup_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # price just before lockup
    lockup_entry_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lockup_shares: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    lockup_entry_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    lockup_order_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # IBKR trailing stop buy order

    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
