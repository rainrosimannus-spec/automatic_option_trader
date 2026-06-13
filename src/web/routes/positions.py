"""
Open positions view.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.web.template_engine import templates
from src.core.database import get_db
from src.core.models import Position, PositionStatus

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def list_positions(request: Request):
    with get_db() as db:
        positions = (
            db.query(Position)
            .filter(Position.status == PositionStatus.OPEN)
            .order_by(Position.opened_at.desc())
            .all()
        )
        # Show only positions IBKR actually holds — hides phantom OPEN options
        # (e.g. an assigned-but-not-yet-reconciled put). See dashboard helper.
        from src.web.routes.dashboard import filter_open_positions_to_ibkr_truth
        from src.broker.trade_sync import get_cached_ibkr_positions
        try:
            from src.broker.orders import get_cached_open_orders
            _cached_oo = get_cached_open_orders()
        except Exception:
            _cached_oo = []
        positions = filter_open_positions_to_ibkr_truth(
            positions, get_cached_ibkr_positions(), _cached_oo
        )
    return templates.TemplateResponse("positions.html", {
        "request": request,
        "positions": positions,
    })


@router.post("/sync")
def sync_positions():
    """Manually trigger IBKR position sync."""
    try:
        from src.broker.trade_sync import sync_ibkr_trades, sync_ibkr_positions
        sync_ibkr_trades()
        sync_ibkr_positions()
    except Exception as e:
        import traceback
        traceback.print_exc()
    return RedirectResponse(url="/positions", status_code=303)
