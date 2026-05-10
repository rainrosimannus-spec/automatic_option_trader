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
    Graph starts today at 0% and grows forward from there.
    Formula: (NLV / total_invested_usd - 1) * 100, anchored to first point = 0%.
    BRK-B benchmark anchored the same way.
    """
    from src.core.models import AccountSnapshot, SystemState
    from src.portfolio.capital_injections import get_total_invested_usd
    from datetime import date as date_type

    total_invested_usd = get_total_invested_usd()
    today_str = str(date_type.today())

    with get_db() as db:
        # Store today as graph start date if not already set
        start_row = db.query(SystemState).filter(
            SystemState.key == "graph_start_date"
        ).first()
        if not start_row:
            db.add(SystemState(key="graph_start_date", value=today_str))
            db.commit()
            start_date = today_str
        else:
            start_date = start_row.value

        snapshots = (
            db.query(AccountSnapshot)
            .filter(AccountSnapshot.date >= start_date)
            .order_by(AccountSnapshot.date.asc())
            .all()
        )

    labels = []
    raw_returns = []
    for snap in snapshots:
        nlv = snap.portfolio_nlv if snap.portfolio_nlv and snap.portfolio_nlv > 0 else None
        if not nlv or nlv <= 0:
            continue
        labels.append(str(snap.date)[:10])
        raw_returns.append((nlv / total_invested_usd - 1.0) * 100.0)

    if not raw_returns:
        return {
            "labels": [today_str],
            "portfolio_data": [0.0],
            "brkb_data": [0.0],
            "total_invested_usd": total_invested_usd,
            "current_return_pct": 0.0,
        }

    # Anchor first point to 0% — graph grows forward from today
    anchor = raw_returns[0]
    portfolio_data = [round(r - anchor, 4) for r in raw_returns]
    current_return_pct = round(raw_returns[-1], 2)

    brkb_data = []  # loaded async via /portfolio/brkb-data

    return {
        "labels": labels,
        "portfolio_data": portfolio_data,
        "brkb_data": brkb_data,
        "total_invested_usd": total_invested_usd,
        "current_return_pct": current_return_pct,
    }






@router.get("/portfolio/brkb-data")
async def brkb_data_endpoint(request: Request):
    """Return BRK-B benchmark series as JSON for async chart loading."""
    from fastapi.responses import JSONResponse
    from src.portfolio.connection import get_cached_portfolio_account
    import json, os
    # Read from cache file directly (populated hourly by scheduler)
    try:
        cache_file = "data/portfolio_account_cache.json"
        with open(cache_file) as f:
            cache = json.load(f)
        brkb_history = cache.get("brkb_history", {})
    except Exception:
        brkb_history = {}
    perf = _build_portfolio_performance()
    labels = perf.get("labels", [])
    if brkb_history and labels:
        sorted_dates = sorted(brkb_history.keys())
        prices = []
        for label in labels:
            p = brkb_history.get(label)
            if p is None:
                for d in reversed(sorted_dates):
                    if d <= label:
                        p = brkb_history[d]
                        break
            prices.append(p)
        if prices and prices[0]:
            anchor = prices[0]
            brkb = [round((p / anchor - 1.0) * 100.0, 4) if p else None for p in prices]
        else:
            brkb = []
    else:
        brkb = []
    return JSONResponse({"labels": labels, "brkb": brkb})

# FX rate cache — refreshed once per page load
_fx_cache: dict = {}
_fx_cache_time: float = 0.0
_FX_CACHE_TTL = 300  # seconds

def _get_fx_rates(currencies: list) -> dict:
    """Get FX rates from IBKR cache file (populated hourly by scheduler)."""
    import json
    global _fx_cache, _fx_cache_time
    import time
    now = time.time()
    if _fx_cache and (now - _fx_cache_time) < _FX_CACHE_TTL:
        return _fx_cache
    try:
        with open("data/portfolio_account_cache.json") as f:
            cache = json.load(f)
        rates = cache.get("fx_rates", {})
        if rates:
            _fx_cache = rates
            _fx_cache_time = now
        return rates
    except Exception:
        return _fx_cache

def _to_usd(amount: float, currency: str, fx_rates: dict = None) -> float:
    """Convert an amount in the given currency to USD."""
    if not amount or currency in ("USD", None):
        return amount or 0.0
    rates = fx_rates if fx_rates is not None else _fx_cache
    rate = rates.get(currency)
    if rate:
        return amount * rate
    return amount

def _build_tier_breakdown(holdings, fx_rates=None) -> dict:
    """Build tier allocation data for pie/bar chart."""
    tiers = {"dividend": 0, "growth": 0, "breakthrough": 0}
    for h in holdings:
        tier = h.tier or "growth"
        tiers[tier] = tiers.get(tier, 0) + _to_usd(h.market_value or 0, h.currency, fx_rates)
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
            PortfolioWatchlist.compound_quality_pct.desc(),
        ).all()

        transactions = db.query(PortfolioTransaction).order_by(
            PortfolioTransaction.created_at.desc()
        ).limit(20).all()

        put_entries = db.query(PortfolioPutEntry).filter(
            PortfolioPutEntry.status == "open"
        ).order_by(PortfolioPutEntry.expiry.asc()).all()

    # FX conversion: convert non-USD holdings to USD for display


    # Total invested = capital deposited (from injections table, not cost basis)
    from src.portfolio.capital_injections import get_total_invested_usd
    # Pre-fetch all FX rates in one API call
    currencies = list(set(h.currency for h in holdings if h.currency not in ("USD", None)))
    fx_rates = _get_fx_rates(currencies)
    total_invested = get_total_invested_usd()
    total_value = sum(_to_usd(h.market_value or 0, h.currency, fx_rates) for h in holdings)
    ibkr_unrealized_pnl = None
    try:
        from src.portfolio.connection import get_cached_portfolio_account
        _acct = get_cached_portfolio_account()
        ibkr_unrealized_pnl = _acct.get("unrealized_pnl")
    except Exception:
        pass

    if ibkr_unrealized_pnl is not None:
        total_pnl = ibkr_unrealized_pnl
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0
    else:
        total_pnl = total_value - total_invested
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0

    # Dividend income — from IBKR cache (populated by Flex Query)
    dividends_accrued = 0.0
    total_dividends = 0.0
    try:
        from src.portfolio.connection import get_cached_portfolio_account
        _div_cache = get_cached_portfolio_account()
        dividends_accrued = _div_cache.get("accrued_dividends", 0.0)
        total_dividends = _div_cache.get("dividends_ytd", 0.0)
    except Exception:
        pass

    # Dividend yield YTD — dividend-tier holdings only
    dividends_yield_pct = 0.0
    try:
        from src.portfolio.models import PortfolioHolding as _PH
        with get_db() as db:
            div_holdings = db.query(_PH).filter(
                _PH.tier == "dividend",
                _PH.shares > 0
            ).all()
        div_cost_basis = sum(h.total_invested for h in div_holdings)
        if div_cost_basis > 0:
            dividends_yield_pct = ((total_dividends + dividends_accrued) / div_cost_basis) * 100
    except Exception:
        pass

    market_status = _get_state("market_status") or "unknown"
    market_pct = _get_state("market_pct_above_sma") or ""

    # Performance data
    perf = _build_portfolio_performance()
    tiers = _build_tier_breakdown(holdings, fx_rates)
    performers = _build_top_performers(holdings)

    # Get portfolio account data from cache (populated hourly by scheduler)
    portfolio_margin_pct = 0.0
    portfolio_maintenance_margin = 0.0
    portfolio_nlv = 0.0
    portfolio_buying_power = 0.0
    portfolio_loans = 0.0
    portfolio_accrued_interest = 0.0
    try:
        from src.portfolio.connection import get_cached_portfolio_account
        cached = get_cached_portfolio_account()
        portfolio_nlv = cached.get("nlv", 0.0)
        portfolio_maintenance_margin = cached.get("margin", 0.0)
        portfolio_buying_power = cached.get("buying_power", 0.0)
        portfolio_margin_pct = cached.get("margin_pct", 0.0)
        portfolio_loans = cached.get("loans", 0.0)
        portfolio_accrued_interest = cached.get("accrued_interest", 0.0)
    except Exception:
        pass

    # Compute adaptive cap values for risk panel display
    _nlv = portfolio_nlv or 0.0
    try:
        from src.core.config import get_settings
        _pcfg = get_settings().portfolio
        position_cap = round(min(_nlv * _pcfg.position_cap_pct, _pcfg.position_cap_max_usd), 0)
        total_exposure_cap = round(min(_nlv * _pcfg.total_exposure_pct, _pcfg.total_exposure_max_usd), 0)
        daily_deployment_cap = round(min(_nlv * _pcfg.daily_deployment_pct, _pcfg.daily_deployment_max_usd), 0)
    except Exception:
        position_cap = 0.0
        total_exposure_cap = 0.0
        daily_deployment_cap = 0.0

    # Open orders from portfolio IBKR (cached, non-blocking)
    portfolio_open_orders = []
    try:
        from src.portfolio.connection import get_cached_portfolio_open_orders
        portfolio_open_orders = get_cached_portfolio_open_orders()
    except Exception:
        pass

    portfolio_pending_orders = []
    try:
        from src.portfolio.connection import get_cached_portfolio_pending_orders
        portfolio_pending_orders = get_cached_portfolio_pending_orders()
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
        "dividends_accrued": dividends_accrued,
        "dividends_yield_pct": dividends_yield_pct,
        "num_holdings": len(holdings),
        "portfolio_margin_pct": portfolio_margin_pct,
        "portfolio_maintenance_margin": portfolio_maintenance_margin,
        "portfolio_nlv": portfolio_nlv,
        "portfolio_buying_power": portfolio_buying_power,
        "portfolio_loans": portfolio_loans,
        "portfolio_accrued_interest": portfolio_accrued_interest,
        "market_status": market_status,
        "market_pct": market_pct,
        # Performance chart
        "perf_labels": perf["labels"],
        "perf_portfolio": perf["portfolio_data"],
        "perf_brkb": perf["brkb_data"],
        "current_return_pct": perf.get("current_return_pct", 0.0),
        "total_invested_usd": perf.get("total_invested_usd", 0.0),
        # Tier breakdown
        "tier_dividend": tiers.get("dividend", 0),
        "tier_growth": tiers.get("growth", 0),
        "tier_breakthrough": tiers.get("breakthrough", 0),
        # Top performers
        "top_performers": performers[:5],
        "bottom_performers": list(reversed(performers[-5:])) if len(performers) > 5 else [],
        "portfolio_open_orders": portfolio_open_orders,
        "portfolio_pending_orders": portfolio_pending_orders,
        "position_cap": position_cap,
        "total_exposure_cap": total_exposure_cap,
        "daily_deployment_cap": daily_deployment_cap,
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
        call_entries = [t for t in transactions if t.action in ("sell_call", "buy_call", "call_expired", "call_assigned")]
        option_entries = put_entries + call_entries

        total_bought = sum(t.amount for t in buys)
        total_sold = sum(t.amount for t in sells)
        total_dividends = sum(t.amount for t in dividends)
        total_premium = sum(t.premium_collected or 0 for t in option_entries)

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
async def sync_portfolio_trades():
    """Manually trigger IBKR trade sync for portfolio — fires in background, returns immediately."""
    import threading
    from fastapi.responses import RedirectResponse
    from src.core.config import get_settings

    def _run_sync():
        try:
            from src.portfolio.scheduler import job_portfolio_sync_trades
            pcfg = get_settings().portfolio
            job_portfolio_sync_trades(pcfg)
        except Exception:
            import traceback
            traceback.print_exc()

    threading.Thread(target=_run_sync, daemon=True).start()
    return RedirectResponse(url="/portfolio/trades", status_code=303)


@router.post("/api/portfolio/sync-injections")
async def sync_capital_injections():
    """Sync deposit history from IBKR Flex Web Service."""
    try:
        from src.portfolio.capital_injections import sync_injections_from_ibkr
        added = sync_injections_from_ibkr()
        return {"status": "ok", "added": added,
                "message": f"Synced {added} new deposit row(s) from IBKR"}
    except ValueError as e:
        return {"status": "setup_needed", "message": str(e)}
    except Exception as e:
        log.error("sync_injections_error", error=str(e))
        return {"status": "error", "message": str(e)}
