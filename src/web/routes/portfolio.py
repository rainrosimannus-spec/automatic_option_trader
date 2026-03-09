"""
Portfolio dashboard route — track long-term holdings, buy signals, and performance.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.web.template_engine import templates
from src.core.database import get_db
from src.portfolio.models import (
    PortfolioHolding, PortfolioTransaction, PortfolioWatchlist, PortfolioState,
    PortfolioPutEntry,
)
from src.core.logger import get_logger

log = get_logger(__name__)

router = APIRouter()


def _get_state(key: str) -> str:
    try:
        with get_db() as db:
            s = db.query(PortfolioState).filter(PortfolioState.key == key).first()
            return s.value if s else ""
    except Exception:
        return ""


def _build_portfolio_performance() -> dict:
    """
    Build portfolio NLV return % vs BRK-B benchmark % using daily snapshots.
    Both lines start at 0% from a fixed inception date.

    - Portfolio line: NLV % change from first snapshot's NLV
    - BRK-B line: actual BRK-B price % change from inception (fetched live),
      falls back to ~15% annualized estimate if price unavailable
    """
    from src.core.models import AccountSnapshot, SystemState
    from datetime import datetime

    INCEPTION_DATE = "2026-02-21"  # portfolio system went live

    with get_db() as db:
        # Get or set permanent start date
        start_row = db.query(SystemState).filter(
            SystemState.key == "portfolio_start_date"
        ).first()
        if start_row:
            start_date_str = start_row.value
        else:
            start_date_str = INCEPTION_DATE
            db.add(SystemState(key="portfolio_start_date", value=start_date_str))

        # Get or set BRK-B starting price (anchored at inception)
        brkb_start_row = db.query(SystemState).filter(
            SystemState.key == "brkb_start_price"
        ).first()
        brkb_start_price = float(brkb_start_row.value) if brkb_start_row else None

    start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    daily_brkb_estimate = (1.15 ** (1 / 365)) - 1  # fallback: 15% annual

    # Try to fetch and anchor BRK-B price if not yet set
    # Note: if not yet anchored, it will be set by the next Portfolio Price Update job.
    # We don't fetch live here because web threads can't call IBKR (event loop conflict).
    if brkb_start_price is None:
        pass  # will use estimated benchmark until background job sets it

    # Get current BRK-B price from DB (saved by background Portfolio Price Update job)
    brkb_current_price = None
    try:
        with get_db() as db:
            cached = db.query(SystemState).filter(
                SystemState.key == "brkb_current_price"
            ).first()
            if cached:
                brkb_current_price = float(cached.value)
    except Exception:
        pass

    with get_db() as db:
        snapshots = (
            db.query(AccountSnapshot)
            .order_by(AccountSnapshot.date.asc())
            .all()
        )

    # Find snapshots with portfolio market value data (NOT options NLV)
    port_snaps = [s for s in snapshots if s.portfolio_market_value and s.portfolio_market_value > 0]

    if port_snaps:
        first_mv = port_snaps[0].portfolio_market_value

        labels = []
        portfolio_line = []
        brkb_line = []

        # Always start from inception
        if port_snaps[0].date > start_date_str:
            labels.append(start_date_str)
            portfolio_line.append(0)
            brkb_line.append(0)

        for snap in port_snaps:
            labels.append(snap.date)

            # Portfolio return % based on market value
            mv_pct = ((snap.portfolio_market_value - first_mv) / first_mv) * 100
            portfolio_line.append(round(mv_pct, 2))

            # BRK-B return %
            snap_date = datetime.strptime(snap.date, "%Y-%m-%d")
            days = max((snap_date - start_date).days, 0)

            if brkb_start_price and brkb_current_price and snap == port_snaps[-1]:
                # Use real BRK-B price for latest point
                brkb_pct = ((brkb_current_price - brkb_start_price) / brkb_start_price) * 100
            else:
                # Estimated for historical points
                brkb_pct = ((1 + daily_brkb_estimate) ** days - 1) * 100
            brkb_line.append(round(brkb_pct, 2))

        return {"labels": labels, "portfolio": portfolio_line, "brkb": brkb_line}

    # ── No snapshots: show empty chart from inception ──
    today = datetime.utcnow().strftime("%Y-%m-%d")
    days = max((datetime.utcnow() - start_date).days, 0)
    brkb_pct = ((1 + daily_brkb_estimate) ** days - 1) * 100

    if brkb_start_price and brkb_current_price:
        brkb_pct = ((brkb_current_price - brkb_start_price) / brkb_start_price) * 100

    return {
        "labels": [start_date_str, today],
        "portfolio": [0, 0],
        "brkb": [0, round(brkb_pct, 2)],
    }


def _build_tier_breakdown(holdings) -> dict:
    """Build tier allocation data for pie/bar chart."""
    tiers = {"dividend": 0, "growth": 0, "breakthrough": 0}
    for h in holdings:
        tier = h.tier or "growth"
        tiers[tier] = tiers.get(tier, 0) + (h.market_value or 0)
    return tiers


def _build_top_performers(holdings) -> list[dict]:
    """Top and bottom performers by unrealized P&L %."""
    performers = []
    for h in holdings:
        if h.total_invested and h.total_invested > 0:
            pnl_pct = ((h.market_value or 0) - h.total_invested) / h.total_invested * 100
            performers.append({
                "symbol": h.symbol,
                "name": h.name or "",
                "pnl_pct": pnl_pct,
                "pnl_abs": (h.market_value or 0) - h.total_invested,
                "tier": h.tier or "growth",
            })
    performers.sort(key=lambda x: x["pnl_pct"], reverse=True)
    return performers


@router.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request):
    """Main portfolio dashboard."""
    with get_db() as db:
        holdings = db.query(PortfolioHolding).filter(
            PortfolioHolding.shares > 0
        ).order_by(PortfolioHolding.market_value.desc()).all()

        watchlist = db.query(PortfolioWatchlist).order_by(
            PortfolioWatchlist.buy_signal.desc(),
            PortfolioWatchlist.composite_score.desc(),
        ).all()

        transactions = db.query(PortfolioTransaction).order_by(
            PortfolioTransaction.created_at.desc()
        ).limit(20).all()

        put_entries = db.query(PortfolioPutEntry).filter(
            PortfolioPutEntry.status == "open"
        ).order_by(PortfolioPutEntry.expiry.asc()).all()

    total_invested = sum(h.total_invested for h in holdings)
    total_value = sum(h.market_value or 0 for h in holdings)
    total_pnl = total_value - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0

    # Dividend income
    with get_db() as db:
        div_txns = db.query(PortfolioTransaction).filter(
            PortfolioTransaction.action == "dividend"
        ).all()
    total_dividends = sum(t.amount for t in div_txns)

    market_status = _get_state("market_status") or "unknown"
    market_pct = _get_state("market_pct_above_sma") or ""

    # Performance data
    perf = _build_portfolio_performance()
    tiers = _build_tier_breakdown(holdings)
    performers = _build_top_performers(holdings)

    # Get portfolio account margin — live connection with cache fallback
    portfolio_margin_pct = 0.0
    portfolio_maintenance_margin = 0.0
    portfolio_nlv = 0.0
    portfolio_buying_power = 0.0
    try:
        from src.portfolio.scheduler import _portfolio_ib
        from src.portfolio.connection import get_cached_portfolio_account
        got_live = False
        if _portfolio_ib and _portfolio_ib.isConnected():
            values = _portfolio_ib.accountValues()
            for v in values:
                if v.tag == "NetLiquidation" and v.currency in ("BASE", "USD"):
                    portfolio_nlv = float(v.value)
                elif v.tag == "MaintMarginReq" and v.currency in ("BASE", "USD"):
                    portfolio_maintenance_margin = float(v.value)
                elif v.tag == "BuyingPower" and v.currency in ("BASE", "USD"):
                    portfolio_buying_power = float(v.value)
            if portfolio_nlv > 0:
                portfolio_margin_pct = (portfolio_maintenance_margin / portfolio_nlv) * 100
                got_live = True
        if not got_live:
            cached = get_cached_portfolio_account()
            portfolio_nlv = cached.get("nlv", 0.0)
            portfolio_maintenance_margin = cached.get("margin", 0.0)
            portfolio_buying_power = cached.get("buying_power", 0.0)
            portfolio_margin_pct = cached.get("margin_pct", 0.0)
    except Exception:
        pass

    return templates.TemplateResponse("portfolio.html", {
        "request": request,
        "holdings": holdings,
        "watchlist": watchlist,
        "transactions": transactions,
        "put_entries": put_entries,
        "total_invested": total_invested,
        "total_value": total_value,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "total_dividends": total_dividends,
        "num_holdings": len(holdings),
        "portfolio_margin_pct": portfolio_margin_pct,
        "portfolio_maintenance_margin": portfolio_maintenance_margin,
        "portfolio_nlv": portfolio_nlv,
        "portfolio_buying_power": portfolio_buying_power,
        "market_status": market_status,
        "market_pct": market_pct,
        # Performance chart
        "perf_labels": perf["labels"],
        "perf_portfolio": perf["portfolio"],
        "perf_brkb": perf["brkb"],
        # Tier breakdown
        "tier_dividend": tiers.get("dividend", 0),
        "tier_growth": tiers.get("growth", 0),
        "tier_breakthrough": tiers.get("breakthrough", 0),
        # Top performers
        "top_performers": performers[:5],
        "bottom_performers": list(reversed(performers[-5:])) if len(performers) > 5 else [],
    })


@router.get("/portfolio/trades", response_class=HTMLResponse)
async def portfolio_trades_page(request: Request):
    """Full trade history for long-term portfolio."""
    with get_db() as db:
        # All transactions, most recent first
        transactions = db.query(PortfolioTransaction).order_by(
            PortfolioTransaction.created_at.desc()
        ).all()

        # Summary stats
        buys = [t for t in transactions if t.action == "buy"]
        sells = [t for t in transactions if t.action == "sell"]
        dividends = [t for t in transactions if t.action == "dividend"]
        put_entries = [t for t in transactions if t.action in ("sell_put", "buy_put", "put_assigned", "put_expired")]

        total_bought = sum(t.amount for t in buys)
        total_sold = sum(t.amount for t in sells)
        total_dividends = sum(t.amount for t in dividends)
        total_premium = sum(t.premium_collected or 0 for t in put_entries)

        # Per-symbol summary
        from collections import defaultdict
        symbol_stats: dict[str, dict] = defaultdict(lambda: {
            "buys": 0, "sells": 0, "dividends": 0, "shares_bought": 0,
            "shares_sold": 0, "avg_buy": 0.0, "total_invested": 0.0,
        })
        for t in transactions:
            s = symbol_stats[t.symbol]
            if t.action == "buy":
                s["buys"] += 1
                s["shares_bought"] += t.shares
                s["total_invested"] += t.amount
            elif t.action == "sell":
                s["sells"] += 1
                s["shares_sold"] += t.shares
            elif t.action == "dividend":
                s["dividends"] += t.amount

        for sym, s in symbol_stats.items():
            if s["shares_bought"] > 0:
                s["avg_buy"] = s["total_invested"] / s["shares_bought"]

        # Sort by total invested descending
        symbol_summary = sorted(
            symbol_stats.items(),
            key=lambda x: x[1]["total_invested"],
            reverse=True,
        )

    return templates.TemplateResponse("portfolio_trades.html", {
        "request": request,
        "transactions": transactions,
        "total_count": len(transactions),
        "buy_count": len(buys),
        "sell_count": len(sells),
        "dividend_count": len(dividends),
        "put_entry_count": len(put_entries),
        "total_bought": total_bought,
        "total_sold": total_sold,
        "total_dividends": total_dividends,
        "total_premium": total_premium,
        "symbol_summary": symbol_summary,
    })


@router.post("/portfolio/trades/sync")
def sync_portfolio_trades():
    """Manually trigger IBKR trade sync for portfolio."""
    import asyncio
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    try:
        from src.portfolio.config import PortfolioConfig
        from src.portfolio.scheduler import job_portfolio_sync_trades
        pcfg = PortfolioConfig()
        job_portfolio_sync_trades(pcfg)
    except Exception:
        import traceback
        traceback.print_exc()
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/portfolio/trades", status_code=303)
