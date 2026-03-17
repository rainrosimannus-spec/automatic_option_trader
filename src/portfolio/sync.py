"""
Portfolio holdings sync — sync IBKR positions into the database.
Accepts an already-connected IB instance — no connection logic here.
"""
from __future__ import annotations

import math
from datetime import datetime

from ib_insync import IB

from src.core.database import get_db
from src.core.logger import get_logger

log = get_logger(__name__)


def sync_ibkr_holdings(ib: IB) -> int:
    """
    Sync real IBKR portfolio positions into the holdings database.
    Reads all stock positions and creates/updates PortfolioHolding entries.
    Returns count of synced positions.
    """
    from src.portfolio.models import PortfolioHolding, PortfolioWatchlist

    ib.reqPositions()
    ib.sleep(2)
    portfolio_items = ib.portfolio()

    if not portfolio_items:
        positions = ib.positions()
        if not positions:
            log.warning("no_ibkr_positions_found")
            return 0
        portfolio_items = []
        for pos in positions:
            portfolio_items.append(type('Item', (), {
                'contract': pos.contract,
                'position': pos.position,
                'averageCost': pos.avgCost,
                'marketPrice': 0,
                'marketValue': 0,
                'unrealizedPNL': 0,
            })())

    count = 0
    with get_db() as db:
        for item in portfolio_items:
            contract = item.contract
            if contract.secType != "STK":
                continue

            symbol = contract.symbol
            shares = int(item.position)
            avg_cost = float(item.averageCost)

            market_price = float(item.marketPrice) if hasattr(item, 'marketPrice') and item.marketPrice else 0
            market_value = float(item.marketValue) if hasattr(item, 'marketValue') and item.marketValue else 0
            unrealized_pnl = float(item.unrealizedPNL) if hasattr(item, 'unrealizedPNL') and item.unrealizedPNL else 0

            if math.isnan(market_price): market_price = 0
            if math.isnan(market_value): market_value = 0
            if math.isnan(unrealized_pnl): unrealized_pnl = 0

            wl = db.query(PortfolioWatchlist).filter(PortfolioWatchlist.symbol == symbol).first()
            tier = wl.tier if wl else "growth"
            name = wl.name if wl else contract.localSymbol or symbol
            sector = wl.sector if wl else ""

            total_invested = abs(shares * avg_cost)
            pnl_pct = (unrealized_pnl / total_invested * 100) if total_invested > 0 else 0

            existing = db.query(PortfolioHolding).filter(PortfolioHolding.symbol == symbol).first()
            if existing:
                existing.shares = shares
                existing.avg_cost = avg_cost
                existing.total_invested = total_invested
                existing.current_price = market_price if market_price > 0 else None
                existing.market_value = market_value if market_value != 0 else None
                existing.unrealized_pnl = unrealized_pnl if unrealized_pnl != 0 else None
                existing.unrealized_pnl_pct = pnl_pct if total_invested > 0 else None
                existing.exchange = contract.exchange or contract.primaryExchange or "SMART"
                existing.currency = contract.currency or "USD"
            else:
                db.add(PortfolioHolding(
                    symbol=symbol,
                    name=name,
                    exchange=contract.exchange or contract.primaryExchange or "SMART",
                    currency=contract.currency or "USD",
                    sector=sector,
                    tier=tier,
                    shares=shares,
                    avg_cost=avg_cost,
                    total_invested=total_invested,
                    current_price=market_price if market_price > 0 else None,
                    market_value=market_value if market_value != 0 else None,
                    unrealized_pnl=unrealized_pnl if unrealized_pnl != 0 else None,
                    unrealized_pnl_pct=pnl_pct if total_invested > 0 else None,
                    entry_method="existing",
                ))
            count += 1

        # Zero out stale holdings
        synced_symbols = {item.contract.symbol for item in portfolio_items if item.contract.secType == "STK"}
        stale = db.query(PortfolioHolding).filter(
            PortfolioHolding.shares > 0,
            ~PortfolioHolding.symbol.in_(synced_symbols) if synced_symbols else True
        ).all()
        for h in stale:
            log.info("holding_zeroed_out", symbol=h.symbol, old_shares=h.shares)
            h.shares = 0
            h.market_value = 0
            h.unrealized_pnl = 0
            h.unrealized_pnl_pct = 0

        # Auto-add holdings to watchlist if not already there
        wl_added = 0
        for h in db.query(PortfolioHolding).all():
            if h.shares <= 0:
                continue
            if not db.query(PortfolioWatchlist).filter(PortfolioWatchlist.symbol == h.symbol).first():
                db.add(PortfolioWatchlist(
                    symbol=h.symbol, name=h.name, exchange=h.exchange,
                    currency=h.currency, sector=h.sector, tier=h.tier,
                    category="existing_holding",
                    composite_score=0, growth_score=0,
                    valuation_score=0, quality_score=0,
                    screened_at=datetime.utcnow(),
                    buy_signal=False,
                ))
                wl_added += 1

        if wl_added:
            log.info("holdings_added_to_watchlist", count=wl_added)

    log.info("ibkr_holdings_synced", count=count)
    return count
