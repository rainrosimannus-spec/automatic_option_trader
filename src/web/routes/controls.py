"""
Trading controls — halt/resume, bridge settings, force close.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from src.web.template_engine import templates
from src.core.config import get_settings
from src.core.database import get_db
from src.core.models import SystemState, Position, PositionStatus
from src.core.logger import get_logger
from src.broker.connection import is_connected

router = APIRouter()
log = get_logger(__name__)


def _get_state(key: str) -> str | None:
    with get_db() as db:
        state = db.query(SystemState).filter(SystemState.key == key).first()
        return state.value if state else None


def _set_state(key: str, value: str):
    with get_db() as db:
        state = db.query(SystemState).filter(SystemState.key == key).first()
        if state:
            state.value = value
            state.updated_at = datetime.utcnow()
        else:
            db.add(SystemState(key=key, value=value))


@router.get("/", response_class=HTMLResponse)
def controls_page(request: Request):
    cfg = get_settings()

    halted = _get_state("halted") == "true"
    halt_reason = _get_state("halt_reason") or ""

    # Bridge settings from state (overrides config if set)
    bridge_enabled = _get_state("bridge_enabled")
    bridge_pct = _get_state("bridge_transfer_pct")
    bridge_min = _get_state("bridge_min_value")
    bridge_month = _get_state("bridge_month")
    bridge_day = _get_state("bridge_day")
    bridge_dry_run = _get_state("bridge_dry_run")

    return templates.TemplateResponse("controls.html", {
        "request": request,
        "halted": halted,
        "halt_reason": halt_reason,
        "ibkr_connected": is_connected(),
        "account": cfg.ibkr.account or "auto-detect",
        "mode": cfg.app.mode,
        "auto_restart": True,  # supervisor is always recommended
        "bridge_enabled": (bridge_enabled == "true") if bridge_enabled else False,
        "bridge_transfer_pct": int(float(bridge_pct)) if bridge_pct else 10,
        "bridge_min_value": int(float(bridge_min)) if bridge_min else 1000000,
        "bridge_month": int(bridge_month) if bridge_month else 7,
        "bridge_day": int(bridge_day) if bridge_day else 31,
        "bridge_dry_run": (bridge_dry_run != "false") if bridge_dry_run else True,
        "bridge_last_check": _get_state("bridge_last_check_date"),
        "bridge_last_net_liq": _get_state("bridge_last_check_net_liq"),
    })


@router.post("/halt")
def halt_trading():
    """Immediately halt all new trades."""
    _set_state("halted", "true")
    _set_state("halt_reason", "manual")
    _set_state("halt_time", datetime.utcnow().isoformat())
    log.warning("TRADING_HALTED", reason="manual")
    return RedirectResponse(url="/controls", status_code=303)


@router.post("/resume")
def resume_trading():
    """Resume normal trading."""
    _set_state("halted", "false")
    _set_state("halt_reason", "")
    log.info("TRADING_RESUMED")
    return RedirectResponse(url="/controls", status_code=303)


@router.post("/close-all")
def close_all_positions():
    """Mark all open positions as closed (user must handle broker side)."""
    with get_db() as db:
        open_positions = db.query(Position).filter(
            Position.status == PositionStatus.OPEN
        ).all()
        count = 0
        for pos in open_positions:
            pos.status = PositionStatus.CLOSED
            pos.closed_at = datetime.utcnow()
            count += 1

    log.warning("ALL_POSITIONS_CLOSED", count=count)
    return RedirectResponse(url="/controls", status_code=303)


@router.post("/pause")
def toggle_pause():
    """Legacy pause toggle — maps to halt/resume."""
    halted = _get_state("halted") == "true"
    if halted:
        return resume_trading()
    else:
        return halt_trading()


@router.post("/bridge")
def save_bridge_settings(
    transfer_pct: int = Form(10),
    min_portfolio_value: int = Form(1000000),
    transfer_month: int = Form(7),
    transfer_day: int = Form(31),
    enabled: bool = Form(False),
    dry_run: bool = Form(False),
):
    """Save bridge settings to database state."""
    _set_state("bridge_enabled", "true" if enabled else "false")
    _set_state("bridge_transfer_pct", str(transfer_pct))
    _set_state("bridge_min_value", str(min_portfolio_value))
    _set_state("bridge_month", str(transfer_month))
    _set_state("bridge_day", str(transfer_day))
    _set_state("bridge_dry_run", "true" if dry_run else "false")

    log.info("bridge_settings_updated",
             enabled=enabled,
             transfer_pct=transfer_pct,
             min_value=min_portfolio_value,
             month=transfer_month,
             day=transfer_day,
             dry_run=dry_run)

    return RedirectResponse(url="/controls", status_code=303)


@router.post("/force-close/{position_id}")
async def force_close_position(position_id: int):
    """Force close a single position — sends market order to IBKR then marks DB closed."""
    import asyncio
    from fastapi.responses import JSONResponse

    def _do_close():
        order_sent = False
        with get_db() as db:
            pos = db.query(Position).filter(Position.id == position_id).first()
            if not pos or pos.status != PositionStatus.OPEN:
                return {"status": "error", "message": "Position not found or not open"}
            try:
                from src.broker.connection import get_ib, get_ib_lock, is_connected
                from src.strategy.universe import UniverseManager
                from ib_insync import Option, Stock, Order

                if is_connected():
                    universe = UniverseManager()
                    stock = universe.get_stock(pos.symbol)
                    opt_exchange = stock.opt_exchange if stock else "SMART"
                    currency = stock.currency if stock else "USD"

                    with get_ib_lock():
                        ib = get_ib()

                        if pos.position_type in ("short_put", "covered_call", "short_call"):
                            right = "P" if pos.position_type == "short_put" else "C"
                            contract = Option(
                                symbol=pos.symbol,
                                lastTradeDateOrContractMonth=pos.expiry,
                                strike=pos.strike,
                                right=right,
                                exchange=opt_exchange,
                                currency=currency,
                            )
                            action = "BUY"
                        elif pos.position_type == "stock":
                            contract = Stock(pos.symbol, "SMART", currency)
                            action = "SELL"
                        else:
                            contract = None

                        if contract:
                            ib.qualifyContracts(contract)
                            order = Order(
                                action=action,
                                totalQuantity=pos.quantity if pos.position_type == "stock" else 1,
                                orderType="MKT",
                            )
                            trade = ib.placeOrder(contract, order)
                            ib.sleep(1)
                            order_sent = True
                            log.info("force_close_order_sent",
                                     position_id=position_id,
                                     symbol=pos.symbol,
                                     action=action,
                                     status=trade.orderStatus.status)
            except Exception as e:
                log.error("force_close_ibkr_error", position_id=position_id, error=str(e))
                return {"status": "error", "message": str(e)}

            # Only mark closed if order was successfully sent
            if order_sent:
                pos.status = PositionStatus.CLOSED
                pos.closed_at = datetime.utcnow()
                pos.realized_pnl = pos.realized_pnl or 0.0
                log.info("force_closed", position_id=position_id, symbol=pos.symbol)
                return {"status": "ok", "message": f"Close order sent for {pos.symbol}"}
            else:
                return {"status": "error", "message": "IBKR not connected"}

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _do_close)
    return JSONResponse(result)


@router.post("/cancel-order/{order_id}")
def cancel_ibkr_order(order_id: int):
    """Cancel an open order on IBKR and update DB record. Returns JSON."""
    from fastapi.responses import JSONResponse
    try:
        from src.broker.connection import ensure_main_event_loop
        from src.broker.orders import cancel_order, get_open_orders
        from src.core.database import get_db
        from src.core.models import Trade, OrderStatus
        ensure_main_event_loop()
        for trade in get_open_orders():
            if trade.order.orderId == order_id:
                cancel_order(trade)
                break
        with get_db() as db:
            t = db.query(Trade).filter(Trade.order_id == order_id).first()
            if t:
                t.order_status = OrderStatus.CANCELLED
                log.info("order_cancelled_db_updated", order_id=order_id)
        return JSONResponse({"status": "ok", "message": f"Order {order_id} cancelled"})
    except Exception as e:
        log.warning("cancel_order_failed", order_id=order_id, error=str(e))
        return JSONResponse({"status": "error", "message": str(e)})
