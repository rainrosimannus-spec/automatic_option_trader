"""
JSON API endpoints — used by HTMX for partial updates.
"""
from __future__ import annotations

from fastapi import APIRouter

from src.core.database import get_db
from src.core.models import Position, Trade, PositionStatus, SystemState
from src.broker.connection import is_connected

router = APIRouter()


@router.get("/status")
def system_status():
    with get_db() as db:
        vix = db.query(SystemState).filter(SystemState.key == "current_vix").first()
        paused = db.query(SystemState).filter(SystemState.key == "paused").first()
        spy_bullish = db.query(SystemState).filter(SystemState.key == "spy_bullish").first()
        spy_fast_ma = db.query(SystemState).filter(SystemState.key == "spy_fast_ma").first()
        spy_slow_ma = db.query(SystemState).filter(SystemState.key == "spy_slow_ma").first()
        open_count = (
            db.query(Position).filter(Position.status == PositionStatus.OPEN).count()
        )
        from datetime import datetime, date
        today_start = datetime.combine(date.today(), datetime.min.time())
        daily_count = (
            db.query(Position)
            .filter(Position.opened_at >= today_start, Position.position_type == "short_put")
            .count()
        )
    return {
        "connected": is_connected(),
        "paused": paused and paused.value == "true",
        "vix": float(vix.value) if vix else None,
        "spy_trend": "bullish" if (spy_bullish and spy_bullish.value == "true") else "bearish" if spy_bullish else None,
        "spy_fast_ma": float(spy_fast_ma.value) if spy_fast_ma else None,
        "spy_slow_ma": float(spy_slow_ma.value) if spy_slow_ma else None,
        "open_positions": open_count,
        "daily_trades": daily_count,
        "daily_limit": 10,
    }


@router.get("/positions")
def api_positions():
    with get_db() as db:
        positions = (
            db.query(Position)
            .filter(Position.status == PositionStatus.OPEN)
            .order_by(Position.opened_at.desc())
            .all()
        )
    return [
        {
            "id": p.id,
            "symbol": p.symbol,
            "type": p.position_type,
            "strike": p.strike,
            "expiry": p.expiry,
            "premium": p.entry_premium,
            "quantity": p.quantity,
            "opened_at": p.opened_at.isoformat() if p.opened_at else None,
        }
        for p in positions
    ]


@router.get("/pnl")
def api_pnl():
    with get_db() as db:
        closed = (
            db.query(Position)
            .filter(Position.status.in_([PositionStatus.CLOSED, PositionStatus.EXPIRED]))
            .all()
        )
    total = sum(p.realized_pnl for p in closed)
    count = len(closed)
    return {
        "total_realized_pnl": round(total, 2),
        "closed_positions": count,
        "avg_pnl": round(total / count, 2) if count > 0 else 0,
    }


@router.get("/trades/recent")
def api_recent_trades():
    with get_db() as db:
        recent = (
            db.query(Trade)
            .order_by(Trade.created_at.desc())
            .limit(20)
            .all()
        )
    return [
        {
            "id": t.id,
            "symbol": t.symbol,
            "type": t.trade_type.value,
            "strike": t.strike,
            "expiry": t.expiry,
            "premium": t.premium,
            "quantity": t.quantity,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        }
        for t in recent
    ]
