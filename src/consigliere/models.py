"""
Consigliere data model — the advisor's memory.

Stores observations, improvement suggestions, and performance insights.
The Consigliere never executes — only observes and advises.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import String, Float, Integer, Boolean, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import Base


class ConsigliereMemo(Base):
    """An observation or suggestion from the Consigliere."""
    __tablename__ = "consigliere_memos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Classification
    category: Mapped[str] = mapped_column(String(30))
    # Categories:
    #   "performance"  — P&L patterns, win rate trends
    #   "risk"         — concentration, exposure, margin usage
    #   "strategy"     — delta/DTE optimization, entry timing
    #   "portfolio"    — tier balance, sector rotation, rebalancing
    #   "missed"       — rejected suggestions that would have been profitable
    #   "market"       — macro regime observations
    #   "improvement"  — specific parameter change recommendations

    severity: Mapped[str] = mapped_column(String(15), default="info")
    # "info", "suggestion", "warning", "critical"

    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)

    # What it's about (optional context)
    symbol: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    metric_name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    metric_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    metric_benchmark: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Status
    status: Mapped[str] = mapped_column(String(15), default="new")
    # "new", "read", "acted_on", "dismissed"
    dismissed_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
