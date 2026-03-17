"""
Options Trader — Application entry point.

Starts the IBKR connection, scheduler, and web dashboard.
"""
from __future__ import annotations

import signal
import sys
import threading

import uvicorn

from src.core.config import get_settings
from src.core.database import init_db
from src.core.logger import setup_logging, get_logger
from src.broker.connection import get_ib, initial_connect, disconnect
from src.scheduler.jobs import create_scheduler
from src.web.app import create_app


def _load_portfolio_watchlist():
    """Load portfolio watchlist from YAML into database (if not already populated)."""
    import yaml
    from pathlib import Path
    from src.core.database import get_db
    from src.portfolio.models import PortfolioWatchlist

    yaml_path = Path("config/portfolio_watchlist.yaml")
    if not yaml_path.exists():
        return

    with open(yaml_path) as f:
        data = yaml.safe_load(f) or {}

    with get_db() as db:
        # Fix known YAML parsing issues
        bad_on = db.query(PortfolioWatchlist).filter(
            PortfolioWatchlist.symbol == "1"
        ).first()
        if bad_on:
            bad_on.symbol = "ON"
            bad_on.name = "ON Semiconductor"
            get_logger("main").info("fixed_symbol", old="1", new="ON")

        # Fix wrong currencies for European stocks
        currency_fixes = {
            "BESI": "EUR",
            "VWS": "DKK",
            "ORSTED": "DKK",
            "CRCL": "USD",
        }
        for sym, cur in currency_fixes.items():
            entry = db.query(PortfolioWatchlist).filter(
                PortfolioWatchlist.symbol == sym
            ).first()
            if entry and entry.currency != cur:
                entry.currency = cur
                get_logger("main").info("fixed_currency", symbol=sym, currency=cur)

        # Remove delisted/acquired stocks
        remove_symbols = ["SWAV"]
        for sym in remove_symbols:
            entry = db.query(PortfolioWatchlist).filter(
                PortfolioWatchlist.symbol == sym
            ).first()
            if entry:
                db.delete(entry)
                get_logger("main").info("removed_unavailable_stock", symbol=sym)

        # Rename ABB -> ABBN (EBS/CHF)
        abb = db.query(PortfolioWatchlist).filter(
            PortfolioWatchlist.symbol.in_(["ABB", "ABBNY"])
        ).first()
        if abb:
            abb.symbol = "ABBN"
            abb.name = "ABB Ltd"
            abb.currency = "CHF"
            get_logger("main").info("fixed_symbol", old=abb.symbol, new="ABBN")

        existing_count = db.query(PortfolioWatchlist).count()
        if existing_count > 0:
            get_logger("main").info("portfolio_watchlist_loaded")
            return  # already loaded

        count = 0
        for tier_name in ["dividend", "breakthrough", "growth"]:
            stocks = data.get(tier_name, [])
            for s in stocks:
                db.add(PortfolioWatchlist(
                    symbol=s.get("symbol", ""),
                    name=s.get("name", ""),
                    exchange=s.get("exchange", "SMART"),
                    currency=s.get("currency", "USD"),
                    sector=s.get("sector", ""),
                    tier=tier_name,
                    category=tier_name,
                ))
                count += 1

    get_logger("main").info("portfolio_watchlist_imported", count=count)


# ── Penalty map: risk level → score penalty ──
_RISK_PENALTY = {"none": 0, "low": 5, "medium": 10, "high": 20}


def _load_structural_risks():
    """
    Load structural risk flags from config/structural_risks.yaml
    and apply them to existing PortfolioWatchlist entries.
    Runs every startup so risk assessments stay current.
    Uses raw SQL to avoid ORM column caching issues after migration.
    """
    import yaml
    from pathlib import Path

    yaml_path = Path("config/structural_risks.yaml")
    if not yaml_path.exists():
        return

    with open(yaml_path) as f:
        risks = yaml.safe_load(f) or {}

    log = get_logger("main")
    updated = 0

    # Use raw SQL to avoid ORM not knowing about new columns
    from src.core.database import get_engine
    from sqlalchemy import text

    engine = get_engine()
    with engine.connect() as conn:
        for sym_raw, flags in risks.items():
            symbol = str(sym_raw)
            if not flags:
                flags = {}

            ai = flags.get("ai_disruption", "none")
            reg = flags.get("regulatory", "none")
            geo = flags.get("geopolitical", "none")
            prod = flags.get("single_product", "none")
            prof = flags.get("profitability", "none")

            total = 0
            for level in [ai, reg, geo, prod, prof]:
                total += _RISK_PENALTY.get(level, 0)

            try:
                result = conn.execute(text(
                    "UPDATE portfolio_watchlist SET "
                    "risk_ai_disruption = :ai, "
                    "risk_regulatory = :reg, "
                    "risk_geopolitical = :geo, "
                    "risk_single_product = :prod, "
                    "risk_profitability = :prof, "
                    "risk_total_penalty = :penalty "
                    "WHERE symbol = :symbol"
                ), {
                    "ai": ai, "reg": reg, "geo": geo,
                    "prod": prod, "prof": prof,
                    "penalty": total, "symbol": symbol,
                })
                if result.rowcount > 0:
                    updated += 1
            except Exception as e:
                log.warning("risk_update_failed", symbol=symbol, error=str(e))

        conn.commit()

    log.info("structural_risks_loaded", updated=updated)


def _seed_ipo_watchlist():
    """
    Seed the IPO rider watchlist with known upcoming IPOs.
    Only adds entries that don't already exist (by ticker).
    """
    from src.ipo.models import IpoWatchlist
    from src.core.database import get_db

    # ── Known upcoming IPOs for 2026 ──
    # Companies that already IPO'd (CRWV, KLAR, CRCL, etc.) are NOT included.
    upcoming = [
        {
            "company_name": "Cerebras Systems",
            "expected_ticker": "CBRS",
            "exchange": "NASDAQ",
            "currency": "USD",
            "expected_date": "2026-06-01",  # targeting Q2 2026
            "flip_enabled": True,
            "lockup_enabled": True,
            "notes": "AI chipmaker, wafer-scale engine. S-1 refiled. $23B valuation.",
        },
        {
            "company_name": "SpaceX",
            "expected_ticker": "SPACEX",
            "exchange": "NASDAQ",
            "currency": "USD",
            "expected_date": "2026-09-01",  # targeting H2 2026
            "flip_enabled": True,
            "lockup_enabled": True,
            "notes": "Full company IPO (not just Starlink spin-off). ~$350B valuation.",
        },
        {
            "company_name": "Anthropic",
            "expected_ticker": "ANTH",
            "exchange": "NASDAQ",
            "currency": "USD",
            "expected_date": "2026-12-01",  # possibly 2026, could be later
            "flip_enabled": True,
            "lockup_enabled": True,
            "notes": "Claude AI maker. Early IPO prep, Wilson Sonsini hired.",
        },
        {
            "company_name": "Stripe",
            "expected_ticker": "STRPE",
            "exchange": "NASDAQ",
            "currency": "USD",
            "expected_date": "",
            "flip_enabled": True,
            "lockup_enabled": True,
            "notes": "Payments infra. No rush but widely expected. ~$90B valuation.",
        },
        {
            "company_name": "Databricks",
            "expected_ticker": "DBX2",
            "exchange": "NASDAQ",
            "currency": "USD",
            "expected_date": "",
            "flip_enabled": True,
            "lockup_enabled": True,
            "notes": "Data + AI platform. $62B valuation. CFO says ready when timing right.",
        },
        {
            "company_name": "Kraken",
            "expected_ticker": "KRKN",
            "exchange": "NASDAQ",
            "currency": "USD",
            "expected_date": "2026-06-01",  # confidentially filed, targeting H1 2026
            "flip_enabled": True,
            "lockup_enabled": True,
            "notes": "Crypto exchange. Confidentially filed Nov 2025. $20B valuation.",
        },
        {
            "company_name": "OpenAI",
            "expected_ticker": "OAIA",
            "exchange": "NASDAQ",
            "currency": "USD",
            "expected_date": "",  # likely late 2026 or 2027
            "flip_enabled": True,
            "lockup_enabled": True,
            "notes": "ChatGPT maker. Targeting $1T valuation. CFO says ~2027.",
        },
        {
            "company_name": "Lambda",
            "expected_ticker": "LMDA",
            "exchange": "NASDAQ",
            "currency": "USD",
            "expected_date": "2026-06-01",  # H1 2026
            "flip_enabled": True,
            "lockup_enabled": True,
            "notes": "GPU cloud provider. $6.2B valuation. Banks hired for H1 2026.",
        },
        {
            "company_name": "Dataiku",
            "expected_ticker": "DIKU",
            "exchange": "NASDAQ",
            "currency": "USD",
            "expected_date": "2026-06-01",  # H1 2026
            "flip_enabled": True,
            "lockup_enabled": True,
            "notes": "Enterprise AI/data analytics. Morgan Stanley + Citi as underwriters.",
        },
        {
            "company_name": "Bolt",
            "expected_ticker": "BOLT",
            "exchange": "NASDAQ",
            "currency": "USD",
            "expected_date": "2026-06-01",
            "flip_enabled": True,
            "lockup_enabled": False,
            "notes": "Ride-hailing/delivery. 45+ countries. Uber competitor.",
        },
        {
            "company_name": "Xanadu Quantum",
            "expected_ticker": "XNDU",
            "exchange": "NASDAQ",
            "currency": "USD",
            "expected_date": "2026-06-01",
            "flip_enabled": True,
            "lockup_enabled": False,
            "notes": "Quantum computing. $3.6B SPAC deal with Crane Harbor.",
        },
        {
            "company_name": "Canva",
            "expected_ticker": "CNVA",
            "exchange": "NASDAQ",
            "currency": "USD",
            "expected_date": "",  # 2026 or 2027
            "flip_enabled": True,
            "lockup_enabled": True,
            "notes": "Design platform. COO says IPO 'probably imminent'. $26B valuation.",
        },
    ]

    log = get_logger("main")
    added = 0

    with get_db() as db:
        existing_tickers = set()
        for r in db.query(IpoWatchlist).all():
            existing_tickers.add(r.expected_ticker)

        log.info("ipo_seed_check", existing=len(existing_tickers), to_seed=len(upcoming))

        for ipo_data in upcoming:
            if ipo_data["expected_ticker"] in existing_tickers:
                continue

            db.add(IpoWatchlist(
                company_name=ipo_data["company_name"],
                expected_ticker=ipo_data["expected_ticker"],
                exchange=ipo_data.get("exchange", "SMART"),
                currency=ipo_data.get("currency", "USD"),
                expected_date=ipo_data.get("expected_date") or None,
                flip_enabled=ipo_data.get("flip_enabled", True),
                lockup_enabled=ipo_data.get("lockup_enabled", True),
                notes=ipo_data.get("notes"),
            ))
            added += 1

    log.info("ipo_watchlist_seeded", added=added)


def _sync_ibkr_holdings():
    """
    Sync real IBKR portfolio positions into the holdings database.
    Reads all stock positions from the PORTFOLIO account and creates/updates
    PortfolioHolding entries. Uses a dedicated portfolio connection so that
    after the account split, it reads from the correct account (U17562704).
    """
    from src.portfolio.connection import get_portfolio_ib, is_portfolio_connected
    from src.core.database import get_db
    from src.portfolio.models import PortfolioHolding, PortfolioWatchlist
    import math

    settings = get_settings()
    pcfg = settings.portfolio

    # Use a fresh connection (same approach as portfolio scheduler)
    # Background thread needs its own event loop for ib_insync
    from ib_insync import IB
    import asyncio
    import time

    asyncio.set_event_loop(asyncio.new_event_loop())

    ib = IB()
    connected = False
    for attempt in range(1, 3):
        try:
            ib.connect(
                host=pcfg.ibkr_host,
                port=pcfg.ibkr_port,
                clientId=98,  # use 98 for startup sync — 99 reserved for portfolio scheduler
                timeout=30,
                readonly=True,
            )
            ib.RequestTimeout = 15
            connected = True
            get_logger("main").info("portfolio_holdings_sync_connected", clientId=pcfg.ibkr_client_id)
            break
        except Exception as e:
            get_logger("main").warning("portfolio_holdings_sync_connect_failed",
                                       attempt=attempt, error=str(e) or repr(e))
            if attempt < 2:
                time.sleep(5)

    if not connected:
        get_logger("main").warning("portfolio_connection_failed_for_sync",
                                    error="Could not connect after 2 attempts")
        return

    # portfolio() gives us market value and unrealized P&L
    ib.reqPositions()
    ib.sleep(2)
    portfolio_items = ib.portfolio()

    if not portfolio_items:
        # Fallback to positions()
        positions = ib.positions()
        if not positions:
            get_logger("main").warning("no_ibkr_positions_found")
            return
        # Convert to a simple format
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
            # Only sync stock positions (not options, futures, etc.)
            if contract.secType != "STK":
                continue

            symbol = contract.symbol
            shares = int(item.position)
            avg_cost = float(item.averageCost)

            # Market data from portfolio
            market_price = float(item.marketPrice) if hasattr(item, 'marketPrice') and item.marketPrice else 0
            market_value = float(item.marketValue) if hasattr(item, 'marketValue') and item.marketValue else 0
            unrealized_pnl = float(item.unrealizedPNL) if hasattr(item, 'unrealizedPNL') and item.unrealizedPNL else 0

            # Clean NaN values
            if math.isnan(market_price):
                market_price = 0
            if math.isnan(market_value):
                market_value = 0
            if math.isnan(unrealized_pnl):
                unrealized_pnl = 0

            # Try to find tier from watchlist
            wl = db.query(PortfolioWatchlist).filter(
                PortfolioWatchlist.symbol == symbol
            ).first()
            tier = wl.tier if wl else "growth"
            name = wl.name if wl else contract.localSymbol or symbol
            sector = wl.sector if wl else ""

            total_invested = abs(shares * avg_cost)
            pnl_pct = (unrealized_pnl / total_invested * 100) if total_invested > 0 else 0

            # Check if already in holdings
            existing = db.query(PortfolioHolding).filter(
                PortfolioHolding.symbol == symbol
            ).first()

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

        # Zero out holdings that IBKR no longer reports (fully sold positions)
        synced_symbols = {item.contract.symbol for item in portfolio_items
                         if item.contract.secType == "STK"}
        stale_holdings = db.query(PortfolioHolding).filter(
            PortfolioHolding.shares > 0,
            ~PortfolioHolding.symbol.in_(synced_symbols) if synced_symbols else True
        ).all()
        for h in stale_holdings:
            get_logger("main").info("holding_zeroed_out", symbol=h.symbol,
                                    old_shares=h.shares,
                                    reason="not in IBKR portfolio")
            h.shares = 0
            h.market_value = 0
            h.unrealized_pnl = 0
            h.unrealized_pnl_pct = 0

        # Auto-add holdings to watchlist if not already there
        wl_added = 0
        holdings = db.query(PortfolioHolding).all()
        for h in holdings:
            if h.shares <= 0:
                continue  # skip short positions
            exists_in_wl = db.query(PortfolioWatchlist).filter(
                PortfolioWatchlist.symbol == h.symbol
            ).first()
            if not exists_in_wl:
                db.add(PortfolioWatchlist(
                    symbol=h.symbol,
                    name=h.name,
                    exchange=h.exchange,
                    currency=h.currency,
                    sector=h.sector,
                    tier=h.tier,
                    category="existing_holding",
                ))
                wl_added += 1

        if wl_added:
            get_logger("main").info("holdings_added_to_watchlist", count=wl_added)

    get_logger("main").info("ibkr_holdings_synced", count=count)

    # Disconnect — frees the client ID for scheduled jobs
    try:
        ib.disconnect()
    except Exception:
        pass


def main():
    settings = get_settings()
    setup_logging(settings.app.log_level)
    log = get_logger("main")

    log.info(
        "starting_options_trader",
        mode=settings.app.mode,
        port=settings.web.port,
    )

    # Initialize database
    init_db()
    log.info("database_initialized", path=settings.app.db_path)

    # ── Startup database cleanup ──
    try:
        from src.core.database import get_engine
        from sqlalchemy import text
        engine = get_engine()
        with engine.connect() as conn:
            # Remove duplicate positions (keep lowest id per symbol+strike+expiry+type)
            conn.execute(text(
                "DELETE FROM positions WHERE id NOT IN "
                "(SELECT MIN(id) FROM positions GROUP BY symbol, strike, expiry, position_type)"
            ))
            # Remove all suggestion-source trades — IBKR fills are the single source of truth
            conn.execute(text("DELETE FROM trades WHERE source = 'suggestion'"))
            # Remove duplicate IBKR trades (keep lowest id per exec_id)
            conn.execute(text(
                "DELETE FROM trades WHERE ibkr_exec_id IS NOT NULL AND id NOT IN "
                "(SELECT MIN(id) FROM trades WHERE ibkr_exec_id IS NOT NULL GROUP BY ibkr_exec_id)"
            ))
            # Remove snapshots with wrong NLV (portfolio NLV leaked)
            conn.execute(text(
                "DELETE FROM account_snapshots WHERE net_liquidation > 100000 OR net_liquidation = 0.0"
            ))

            # Seed historical trades from IBKR if missing
            # These are the actual IBKR trades for this account
            ibkr_history = [
                ("AVGO", "SELL_PUT", 327.5, "20260223", 1.70, 1, 4.00, "2026-02-20 16:57:32", "ibkr_sync"),
                ("BHP",  "SELL_PUT", 52.01, "20260226", 0.055, 1, 6.00, "2026-02-23 02:00:17", "ibkr_sync"),
                ("AVGO", "BUY_PUT",  327.5, "20260223", 0.00, 1, 0.00, "2026-02-24 04:35:54", "ibkr_sync"),
                ("DDOG", "SELL_PUT", 98.0,  "20260227", 2.33, 1, 4.00, "2026-02-24 16:30:01", "ibkr_sync"),
                ("TTD",  "SELL_PUT", 23.0,  "20260227", 1.13, 1, 4.00, "2026-02-24 16:30:01", "ibkr_sync"),
                ("AVGO", "SELL_PUT", 310.0, "20260227", 3.32, 1, 4.00, "2026-02-24 22:07:45", "ibkr_sync"),
            ]
            for symbol, ttype, strike, expiry, price, qty, comm, ts, src in ibkr_history:
                exists = conn.execute(text(
                    "SELECT 1 FROM trades WHERE symbol = :s AND strike = :k AND expiry = :e AND trade_type = :t LIMIT 1"
                ), {"s": symbol, "k": strike, "e": expiry, "t": ttype}).fetchone()
                if not exists:
                    conn.execute(text(
                        "INSERT INTO trades (symbol, trade_type, strike, expiry, premium, quantity, "
                        "fill_price, commission, order_status, notes, source, created_at) "
                        "VALUES (:s, :t, :k, :e, :p, :q, :p, :c, 'FILLED', 'Seeded from IBKR history', :src, :ts)"
                    ), {"s": symbol, "t": ttype, "k": strike, "e": expiry, "p": price, "q": qty, "c": comm, "src": src, "ts": ts})

            conn.commit()
        log.info("startup_cleanup_complete")
    except Exception as e:
        log.warning("startup_cleanup_failed", error=str(e))

    # Initialize alerts
    try:
        from src.core.alerts import init_alerts, AlertConfig
        alert_cfg = AlertConfig(
            enabled=True,
            ntfy_enabled=settings.raw.get("alerts", {}).get("ntfy_enabled", False),
            ntfy_topic=settings.raw.get("alerts", {}).get("ntfy_topic", ""),
            ntfy_server=settings.raw.get("alerts", {}).get("ntfy_server", "https://ntfy.sh"),
            telegram_enabled=settings.raw.get("alerts", {}).get("telegram_enabled", False),
            telegram_bot_token=settings.raw.get("alerts", {}).get("telegram_bot_token", ""),
            telegram_chat_id=settings.raw.get("alerts", {}).get("telegram_chat_id", ""),
            alert_critical=settings.raw.get("alerts", {}).get("alert_critical", True),
            alert_daily=settings.raw.get("alerts", {}).get("alert_daily", True),
            alert_trades=settings.raw.get("alerts", {}).get("alert_trades", False),
            alert_bridge=settings.raw.get("alerts", {}).get("alert_bridge", True),
            alert_assignments=settings.raw.get("alerts", {}).get("alert_assignments", True),
        )
        alerts = init_alerts(alert_cfg)
        alerts._send("🟢 Options Trader v14 started\nMode: {}\nAccount: {}".format(
            settings.app.mode, settings.ibkr.account or "auto"))
        log.info("alerts_initialized",
                 ntfy=alert_cfg.ntfy_enabled,
                 telegram=alert_cfg.telegram_enabled)
    except Exception as e:
        log.warning("alerts_init_failed", error=str(e))

    # Connect to IBKR (Options Trader account)
    from src.broker.connection import is_port_open
    if is_port_open(settings.ibkr.host, settings.ibkr.port):
        try:
            ib = initial_connect()
            log.info("ibkr_ready", accounts=ib.managedAccounts())
        except Exception as e:
            log.error("ibkr_connection_failed", error=str(e))
            log.warning("starting_without_broker_connection")
    else:
        log.warning("options_tws_not_running",
                     port=settings.ibkr.port,
                     msg=f"Options Trader TWS not found on port {settings.ibkr.port} — "
                         f"options trading disabled until TWS is started")

    # Load portfolio watchlist from YAML into database
    try:
        _load_portfolio_watchlist()
        log.info("portfolio_watchlist_loaded")
    except Exception as e:
        log.warning("portfolio_watchlist_load_failed", error=str(e))

    # Load structural risk flags
    try:
        _load_structural_risks()
    except Exception as e:
        log.warning("structural_risks_load_failed", error=str(e))

    # Seed IPO rider watchlist with known upcoming IPOs
    try:
        _seed_ipo_watchlist()
    except Exception as e:
        log.warning("ipo_watchlist_seed_failed", error=str(e))

    # Sync real IBKR holdings from portfolio account into database
    # Run in background so it doesn't block the web server startup
    pcfg = settings.portfolio
    if is_port_open(pcfg.ibkr_host, pcfg.ibkr_port):
        import threading
        def _bg_portfolio_sync():
            try:
                _sync_ibkr_holdings()
            except Exception as e:
                log.warning("ibkr_holdings_sync_failed", error=str(e))
        threading.Thread(target=_bg_portfolio_sync, daemon=True).start()
        log.info("portfolio_sync_started_in_background")
    else:
        log.warning("portfolio_tws_not_running",
                     port=pcfg.ibkr_port,
                     msg=f"Portfolio TWS not found on port {pcfg.ibkr_port} — "
                         f"holdings sync skipped, portfolio features limited")

    # Start scheduler
    scheduler = create_scheduler()
    scheduler.start()
    log.info("scheduler_started", jobs=[j.name for j in scheduler.get_jobs()])

    # Graceful shutdown
    def shutdown(signum, frame):
        log.info("shutdown_requested")
        scheduler.shutdown(wait=False)
        disconnect()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start web dashboard
    app = create_app()
    uvicorn.run(
        app,
        host=settings.web.host,
        port=settings.web.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
