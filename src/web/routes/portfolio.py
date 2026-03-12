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
    Portfolio return % = (NLV / total_invested_usd - 1) * 100

    Anchored so the most recent snapshot = 0% (today is day 0).
    Earlier dates show what the return was then, relative to today's capital base.
    BRK-B benchmark uses the same anchor.
    """
    from src.core.models import AccountSnapshot
    from src.portfolio.capital_injections import get_total_invested_usd

    total_invested_usd = get_total_invested_usd()

    with get_db() as db:
        snapshots = (
            db.query(AccountSnapshot)
            .order_by(AccountSnapshot.date.asc())
            .all()
        )

    if not snapshots:
        return {
            "labels": [],
            "portfolio_data": [],
            "brkb_data": [],
            "total_invested_usd": total_invested_usd,
            "current_return_pct": 0.0,
        }

    labels = []
    raw_returns = []

    for snap in snapshots:
        nlv = getattr(snap, "portfolio_market_value", None) or getattr(snap, "port_value", None)
        if not nlv or nlv <= 0:
            continue
        labels.append(str(snap.date)[:10])
        raw_returns.append((nlv / total_invested_usd - 1.0) * 100.0)

    if not raw_returns:
        return {
            "labels": [],
            "portfolio_data": [],
            "brkb_data": [],
            "total_invested_usd": total_invested_usd,
            "current_return_pct": 0.0,
        }

    # Shift so most recent point = 0%
    anchor = raw_returns[-1]
    portfolio_data = [round(r - anchor, 4) for r in raw_returns]
    current_return_pct = round(anchor, 2)

    brkb_data = _build_brkb_series(labels)

    return {
        "labels": labels,
        "portfolio_data": portfolio_data,
        "brkb_data": brkb_data,
        "total_invested_usd": total_invested_usd,
        "current_return_pct": current_return_pct,
    }


def _build_brkb_series(labels: list) -> list:
    """BRK-B % change series, anchored so most recent date = 0%."""
    if not labels:
        return []
    try:
        from src.core.config import get_settings
        import requests

        api_key = get_settings().fmp_api_key
        url = (
            f"https://financialmodelingprep.com/stable/historical-price-eod/full"
            f"?symbol=BRK-B&from={labels[0]}&to={labels[-1]}&apikey={api_key}"
        )
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        hist = r.json()
        if not hist or not isinstance(hist, list):
            return []

        price_map = {row["date"]: row["close"] for row in hist if "date" in row and "close" in row}

        prices = []
        for label in labels:
            p = price_map.get(label)
            if p is None:
                for row in sorted(hist, key=lambda x: x["date"], reverse=True):
                    if row["date"] <= label:
                        p = row["close"]
                        break
            prices.append(p)

        if not prices or prices[-1] is None:
            return []

        anchor_price = prices[-1]
        return [
            round((p / anchor_price - 1.0) * 100.0, 4) if p is not None else None
            for p in prices
        ]
    except Exception as e:
        from src.core.logger import get_logger
        get_logger(__name__).warning("brkb_series_failed", error=str(e))
        return []


def _to_usd(amount: float, currency: str) -> float:
    """Convert an amount in the given currency to USD using FMP FX rates."""
    if not amount or currency in ("USD", None):
        return amount or 0.0
    try:
        import requests
        from src.core.config import load_config
        cfg = load_config()
        api_key = cfg.get("fmp", {}).get("api_key", "")
        if not api_key:
            return amount
        pair = f"{currency}USD"
        url = f"https://financialmodelingprep.com/stable/quote?symbol={pair}&apikey={api_key}"
        r = requests.get(url, timeout=5)
        d = r.json()
        if d and isinstance(d, list) and "price" in d[0]:
            return amount * float(d[0]["price"])
    except Exception:
        pass
    return amount

def _build_tier_breakdown(holdings) -> dict:
    """Build tier allocation data for pie/bar chart."""
    tiers = {"dividend": 0, "growth": 0, "breakthrough": 0}
    for h in holdings:
        tier = h.tier or "growth"
        tiers[tier] = tiers.get(tier, 0) + _to_usd(h.market_value or 0, h.currency)
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

    # FX conversion: convert non-USD holdings to USD for display


    total_invested = sum(_to_usd(h.total_invested, h.currency) for h in holdings)
    total_value = sum(_to_usd(h.market_value or 0, h.currency) for h in holdings)
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
    portfolio_loans = 0.0
    portfolio_accrued_interest = 0.0
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
            # Loans: negative cash balances per currency, converted to USD
            # DEBUG: log all cash/accrued tags so we can verify tag names
            import logging as _log
            for _v in values:
                if any(x in _v.tag.lower() for x in ['cash', 'interest', 'loan', 'accrued', 'borrow']):
                    _log.getLogger(__name__).info(f"[LOANS_DEBUG] tag={_v.tag} currency={_v.currency} value={_v.value}")
            try:
                import requests
                from src.core.config import load_config
                cfg = load_config()
                fmp_key = cfg.get("fmp", {}).get("api_key", "")
                def _fx_to_usd(amount, currency):
                    if currency in ("USD", "BASE") or not fmp_key:
                        return amount
                    try:
                        pair = f"{currency}USD"
                        r = requests.get(
                            f"https://financialmodelingprep.com/stable/quote?symbol={pair}&apikey={fmp_key}",
                            timeout=5)
                        d = r.json()
                        if d and isinstance(d, list) and "price" in d[0]:
                            return amount * float(d[0]["price"])
                    except Exception:
                        pass
                    return amount
                for v in values:
                    if v.tag == "CashBalance" and v.currency not in ("BASE",):
                        val = float(v.value)
                        if val < 0:
                            portfolio_loans += _fx_to_usd(val, v.currency)
                    elif v.tag == "AccruedInterest" and v.currency not in ("BASE",):
                        val = float(v.value)
                        portfolio_accrued_interest += _fx_to_usd(val, v.currency)
            except Exception:
                pass
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
