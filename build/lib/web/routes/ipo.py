"""
IPO Rider web route — manage upcoming IPOs and monitor trades.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from src.web.template_engine import templates
from src.core.database import get_db
from src.ipo.models import IpoWatchlist
from src.core.logger import get_logger

router = APIRouter()
log = get_logger(__name__)


@router.get("/", response_class=HTMLResponse)
def ipo_page(request: Request):
    """Main IPO Rider page."""
    with get_db() as db:
        # Active / watching
        active = db.query(IpoWatchlist).filter(
            IpoWatchlist.status.in_(["watching", "ipo_trading", "lockup_waiting", "lockup_trading"]),
        ).order_by(IpoWatchlist.created_at.desc()).all()

        # Completed
        completed = db.query(IpoWatchlist).filter(
            IpoWatchlist.status.in_(["flip_done", "lockup_done", "cancelled"]),
        ).order_by(IpoWatchlist.updated_at.desc()).limit(20).all()

    return templates.TemplateResponse("ipo.html", {
        "request": request,
        "active": active,
        "completed": completed,
    })


@router.post("/add")
def add_ipo(
    request: Request,
    company_name: str = Form(...),
    expected_ticker: str = Form(...),
    exchange: str = Form("SMART"),
    currency: str = Form("USD"),
    expected_date: str = Form(""),
    flip_enabled: bool = Form(False),
    flip_amount: float = Form(5000),
    flip_trailing_pct: float = Form(8.0),
    flip_stop_loss_pct: float = Form(12.0),
    flip_max_hold_days: int = Form(5),
    lockup_enabled: bool = Form(False),
    lockup_date: str = Form(""),
    lockup_dip_pct: float = Form(10.0),
    lockup_trailing_buy_pct: float = Form(5.0),
    lockup_amount: float = Form(10000),
    notes: str = Form(""),
):
    """Add a new IPO to the watchlist."""
    with get_db() as db:
        ipo = IpoWatchlist(
            company_name=company_name.strip(),
            expected_ticker=expected_ticker.strip().upper(),
            exchange=exchange.strip() or "SMART",
            currency=currency.strip() or "USD",
            expected_date=expected_date.strip() or None,
            flip_enabled=flip_enabled,
            flip_amount=flip_amount,
            flip_trailing_pct=flip_trailing_pct,
            flip_stop_loss_pct=flip_stop_loss_pct,
            flip_max_hold_days=flip_max_hold_days,
            lockup_enabled=lockup_enabled,
            lockup_date=lockup_date.strip() or None,
            lockup_dip_pct=lockup_dip_pct,
            lockup_trailing_buy_pct=lockup_trailing_buy_pct,
            lockup_amount=lockup_amount,
            notes=notes.strip() or None,
        )
        db.add(ipo)

    log.info("ipo_added", ticker=expected_ticker, company=company_name)
    return RedirectResponse(url="/ipo", status_code=303)


@router.post("/edit/{ipo_id}")
def edit_ipo(
    ipo_id: int,
    company_name: str = Form(...),
    expected_ticker: str = Form(...),
    exchange: str = Form("SMART"),
    currency: str = Form("USD"),
    expected_date: str = Form(""),
    flip_enabled: bool = Form(False),
    flip_amount: float = Form(5000),
    flip_trailing_pct: float = Form(8.0),
    flip_stop_loss_pct: float = Form(12.0),
    flip_max_hold_days: int = Form(5),
    lockup_enabled: bool = Form(False),
    lockup_date: str = Form(""),
    lockup_dip_pct: float = Form(10.0),
    lockup_trailing_buy_pct: float = Form(5.0),
    lockup_amount: float = Form(10000),
    notes: str = Form(""),
):
    """Edit an existing IPO entry."""
    with get_db() as db:
        ipo = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo_id).first()
        if ipo and ipo.status in ("watching", "lockup_waiting"):
            ipo.company_name = company_name.strip()
            ipo.expected_ticker = expected_ticker.strip().upper()
            ipo.exchange = exchange.strip() or "SMART"
            ipo.currency = currency.strip() or "USD"
            ipo.expected_date = expected_date.strip() or None
            ipo.flip_enabled = flip_enabled
            ipo.flip_amount = flip_amount
            ipo.flip_trailing_pct = flip_trailing_pct
            ipo.flip_stop_loss_pct = flip_stop_loss_pct
            ipo.flip_max_hold_days = flip_max_hold_days
            ipo.lockup_enabled = lockup_enabled
            ipo.lockup_date = lockup_date.strip() or None
            ipo.lockup_dip_pct = lockup_dip_pct
            ipo.lockup_trailing_buy_pct = lockup_trailing_buy_pct
            ipo.lockup_amount = lockup_amount
            ipo.notes = notes.strip() or None
            ipo.updated_at = datetime.utcnow()

    return RedirectResponse(url="/ipo", status_code=303)


@router.post("/cancel/{ipo_id}")
def cancel_ipo(ipo_id: int):
    """Cancel an IPO watchlist entry."""
    with get_db() as db:
        ipo = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo_id).first()
        if ipo:
            ipo.status = "cancelled"
            ipo.updated_at = datetime.utcnow()

    return RedirectResponse(url="/ipo", status_code=303)


@router.post("/reactivate/{ipo_id}")
def reactivate_ipo(ipo_id: int):
    """Reactivate a cancelled IPO entry."""
    with get_db() as db:
        ipo = db.query(IpoWatchlist).filter(IpoWatchlist.id == ipo_id).first()
        if ipo and ipo.status == "cancelled":
            ipo.status = "watching"
            ipo.updated_at = datetime.utcnow()

    return RedirectResponse(url="/ipo", status_code=303)
