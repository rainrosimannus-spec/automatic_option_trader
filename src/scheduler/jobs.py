"""
Scheduled jobs — multi-market scans, position checks, assignment detection.
Each market gets its own scan job running during that market's hours.

Market sessions (local time):
  US   (SMART) : 09:30–16:00 US/Eastern
  Swiss (SWX)  : 09:00–17:30 Europe/Zurich
  Japan (TSE)  : 09:00–15:00 Asia/Tokyo
  Norway (OSE) : 09:00–16:20 Europe/Oslo
  Australia (ASX): 10:00–16:00 Australia/Sydney
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta
from functools import partial, wraps

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import pytz

from src.core.config import get_settings
from src.core.database import get_db
from src.core.models import SystemState
from src.core.logger import get_logger
from src.strategy.universe import UniverseManager
from src.strategy.risk import RiskManager
from src.strategy.put_seller import PutSeller
from src.strategy.wheel import WheelManager
from src.strategy.profit_taker import ProfitTaker
from src.strategy.hedge import TailHedge
from src.broker.connection import is_connected, reconnect

log = get_logger(__name__)

_scheduler: BackgroundScheduler | None = None


def _ensure_event_loop():
    """Ensure there's an asyncio event loop in the current thread.
    ib_insync requires this when running in APScheduler's thread pool."""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


def _is_paused() -> bool:
    """Check if trading is manually paused or halted."""
    with get_db() as db:
        # Check legacy pause
        paused = db.query(SystemState).filter(SystemState.key == "paused").first()
        if paused and paused.value == "true":
            return True
        # Check halt
        halted = db.query(SystemState).filter(SystemState.key == "halted").first()
        if halted and halted.value == "true":
            return True
        return False


def _ensure_connected() -> bool:
    """Ensure IBKR connection is live. Returns True if connected.
    Does a quick port check first to avoid long retry delays when TWS is down."""
    if is_connected():
        return True

    from src.core.config import get_settings
    from src.broker.connection import is_port_open
    cfg = get_settings().ibkr
    if not is_port_open(cfg.host, cfg.port):
        log.warning("options_tws_not_reachable", host=cfg.host, port=cfg.port)
        return False

    try:
        reconnect()
        return True
    except Exception as e:
        log.error("reconnect_failed", error=str(e))
        return False


_scan_connections: dict[str, "IB"] = {}  # market -> IB connection
_scan_locks: dict[str, threading.Lock] = {
    "SMART": threading.Lock(),
    "SMART_EU": threading.Lock(),
    "SMART_ASIA": threading.Lock(),
}

# Per-market client IDs — each market gets its own IBKR connection
# so one market's pacing violations don't block others
_SCAN_CLIENT_IDS = {
    "SMART": 50,
    "SMART_EU": 51,
    "SMART_ASIA": 52,
}

# ── Resilience tracking ────────────────────────────────────
import time as _time_mod
_app_start_time: float = _time_mod.time()
_last_successful_scan: float = _time_mod.time()
_consecutive_disconnect_checks: int = 0  # how many health checks found TWS down
_tws_unreachable_alerted: bool = False   # avoid spamming TWS-down alerts


def _get_scan_connection(market: str = "SMART"):
    """Get or create a dedicated IB connection for a specific market's scanner.
    
    Does a quick TCP port check first — if Options Trader TWS isn't running,
    fails immediately instead of retrying for ~100 seconds.
    """
    from ib_insync import IB
    from src.core.config import get_settings
    from src.broker.connection import is_port_open

    existing = _scan_connections.get(market)
    if existing is not None and existing.isConnected():
        return existing

    # Clean up dead/disconnected connection before creating a new one
    if existing is not None:
        try:
            existing.disconnect()
        except Exception:
            pass
        _scan_connections.pop(market, None)

    cfg = get_settings().ibkr

    # Quick check: is the port even open?
    if not is_port_open(cfg.host, cfg.port):
        raise ConnectionError(
            f"Options Trader TWS not reachable on {cfg.host}:{cfg.port}"
        )

    ib = IB()
    client_id = _SCAN_CLIENT_IDS.get(market, 50)

    import time as _time
    for attempt in range(1, 4):
        try:
            ib.connect(
                host=cfg.host,
                port=cfg.port,
                clientId=client_id,
                timeout=30,
                readonly=True,
            )
            ib.RequestTimeout = 15
            ib.reqMarketDataType(4)
            _scan_connections[market] = ib
            log.info("scan_connection_established", clientId=client_id, market=market)
            return ib
        except Exception as e:
            log.warning("scan_connect_retry", attempt=attempt, error=str(e), market=market)
            if attempt < 3:
                _time.sleep(35)

    raise ConnectionError(f"Failed to connect scanner for {market}")


def job_scan_market(market: str):
    """Scan a specific market's stocks using a dedicated per-market connection."""
    _ensure_event_loop()
    if _is_paused():
        log.info("trading_paused_skipping_scan", market=market)
        return

    # Each market has its own lock — EU scan cannot block US scan
    lock = _scan_locks.get(market, threading.Lock())

    with lock:
        try:
            scan_ib = _get_scan_connection(market)
        except Exception as e:
            log.error("scan_connection_failed", error=str(e), market=market)
            return

        from ib_insync import Stock, Index
        import time as _time

        universe = UniverseManager()
        symbols = universe.symbols_for_market(market)
        if not symbols:
            return

        log.info("market_scan_starting", market=market, stocks=len(symbols))
        scan_start = _time.time()

        # Swap the global IB connection to use scan_ib during the scan
        # This way all get_ib() calls inside the scan use the dedicated connection
        from src.broker import connection as _conn
        original_ib = _conn._ib
        _conn._ib = scan_ib
        try:
            risk = RiskManager(universe)
            seller = PutSeller(universe, risk)
            seller.run_scan(market=market)

            # Track successful scan for heartbeat
            global _last_successful_scan
            _last_successful_scan = _time.time()
        finally:
            _conn._ib = original_ib

            # Always disconnect scan connection after each scan.
            # This prevents zombie connections that block future scans.
            # Next scan will create a fresh connection.
            try:
                scan_ib.disconnect()
            except Exception:
                pass
            _scan_connections.pop(market, None)


def job_check_assignments():
    """Check for put assignments and write covered calls."""
    _ensure_event_loop()
    if _is_paused():
        return
    if not _ensure_connected():
        return

    universe = UniverseManager()
    risk = RiskManager(universe)
    wheel = WheelManager(risk, universe=universe)

    assigned = wheel.check_assignments()
    called = wheel.check_called_away()

    if assigned:
        wheel.write_covered_calls()


def job_check_profit():
    """Check open positions for profit-taking opportunities."""
    _ensure_event_loop()
    if _is_paused():
        return
    if not is_connected():
        return

    taker = ProfitTaker()
    taker.check_positions()


def job_execute_queued():
    """Pick up manually approved (queued) suggestions and execute them."""
    _ensure_event_loop()
    if _is_paused():
        return
    if not is_connected():
        return
    try:
        from src.core.suggestions import TradeSuggestion, _execute_approved_order
        from src.core.database import get_db
        with get_db() as db:
            queued = db.query(TradeSuggestion).filter(TradeSuggestion.status == "queued").all()
            if not queued:
                return
            for s in queued:
                log.info("executing_queued_suggestion", id=s.id, symbol=s.symbol)
                s.status = "approved"
                db.commit()
                _execute_approved_order(s.id)
    except Exception as e:
        log.warning("queued_suggestion_error", error=str(e))


def _is_any_market_open() -> bool:
    """Check if at least one major market is currently open."""
    import pytz
    from datetime import datetime as dt
    market_hours = {
        "US/Eastern": (9, 16),
        "Europe/Berlin": (9, 17),
        "Europe/London": (8, 16),
        "Asia/Tokyo": (9, 15),
    }
    for tz_name, (open_h, close_h) in market_hours.items():
        try:
            tz = pytz.timezone(tz_name)
            now = dt.now(tz)
            if now.weekday() < 5 and open_h <= now.hour < close_h:
                return True
        except Exception:
            pass
    return False


# Track consecutive price fetch failures for stale detection
_stale_fail_count = 0
_stale_success_count = 0
_STALE_THRESHOLD = 10  # consecutive failures during market hours before reconnect


def record_price_success():
    """Called from market_data when a price fetch succeeds."""
    global _stale_fail_count, _stale_success_count
    _stale_fail_count = 0
    _stale_success_count += 1


def record_price_failure():
    """Called from market_data when a price fetch fails."""
    global _stale_fail_count
    _stale_fail_count += 1


def _detect_stale_connection():
    """Disabled — forced reconnects cause client ID conflicts in TWS."""
    global _stale_fail_count
    _stale_fail_count = 0
    return


def job_health_check():
    """
    Periodic health check — reconnect on disconnect, circuit breaker on daily loss,
    refresh VIX/SPY for dashboard, detect prolonged TWS outages.
    """
    _ensure_event_loop()
    global _consecutive_disconnect_checks, _tws_unreachable_alerted

    if not is_connected():
        _consecutive_disconnect_checks += 1
        log.warning("ibkr_disconnected_attempting_reconnect",
                     consecutive_failures=_consecutive_disconnect_checks)
        try:
            _ensure_connected()
            if is_connected():
                log.info("ibkr_reconnected_successfully")
                _consecutive_disconnect_checks = 0
                _tws_unreachable_alerted = False
        except Exception as e:
            log.error("ibkr_reconnect_failed", error=str(e))

        # Alert after 3 consecutive failed health checks (15 min of downtime)
        if _consecutive_disconnect_checks >= 3 and not _tws_unreachable_alerted:
            try:
                from src.core.alerts import get_alert_manager
                alerts = get_alert_manager()
                alerts.tws_unreachable_alert(
                    minutes_down=_consecutive_disconnect_checks * 5
                )
                _tws_unreachable_alerted = True
            except Exception:
                pass
        return
    else:
        # Connected — reset counter
        if _consecutive_disconnect_checks > 0:
            _consecutive_disconnect_checks = 0
            _tws_unreachable_alerted = False

    # Stale connection detector: if connected but price fetches keep failing
    # during market hours, force reconnect
    if is_connected():
        _detect_stale_connection()

    # Check scan connections — disconnect zombie scan connections so they reconnect
    # on the next scan cycle
    for market, scan_ib in list(_scan_connections.items()):
        if scan_ib is not None and scan_ib.isConnected():
            try:
                # Quick liveness test: request current time from TWS
                scan_ib.reqCurrentTime()
            except Exception:
                log.warning("scan_connection_zombie_detected", market=market)
                try:
                    scan_ib.disconnect()
                except Exception:
                    pass
                _scan_connections.pop(market, None)

    # Refresh VIX and SPY for dashboard display — only every 5 minutes
    # Skip if a scan is currently running to avoid pacing violations
    if is_connected() and not any(lk.locked() for lk in _scan_locks.values()):
        import time
        _last_regime = getattr(job_health_check, '_last_regime', 0)
        if time.time() - _last_regime > 300:  # 5 minutes
            try:
                from src.strategy.risk import RiskManager
                from src.strategy.universe import UniverseManager
                universe = UniverseManager()
                risk = RiskManager(universe)
                risk.get_regime(force_refresh=True)
                job_health_check._last_regime = time.time()
            except Exception:
                pass  # non-critical, dashboard just shows stale data

    # Circuit breaker: check daily P&L
    # Skip if already halted — no need to keep checking
    if is_connected():
        already_halted = False
        with get_db() as db:
            h = db.query(SystemState).filter(SystemState.key == "halted").first()
            already_halted = h is not None and h.value == "true"

        if already_halted:
            return  # already halted, don't spam

        try:
            from src.broker.connection import get_ib
            ib = get_ib()

            # Use ib.pnl() for accurate daily P&L
            # Falls back to accountValues if pnl() not available
            daily_pnl = None
            net_liq = None

            # Try the PnL API first (most accurate for daily)
            try:
                pnl_list = ib.pnl()
                if pnl_list:
                    p = pnl_list[0]
                    daily_pnl = p.dailyPnL if hasattr(p, 'dailyPnL') and p.dailyPnL is not None else None
            except Exception:
                pass

            # Get net liquidation from account values
            values = ib.accountValues()
            for v in values:
                if v.tag == "NetLiquidation" and v.currency in ("EUR", "BASE", "USD"):
                    net_liq = float(v.value)
                # Fallback: look for DailyPnL in account values
                if daily_pnl is None and v.tag == "DailyPnL" and v.currency in ("EUR", "BASE", "USD"):
                    daily_pnl = float(v.value)

            if daily_pnl is not None and net_liq and net_liq > 0:
                daily_loss_pct = abs(daily_pnl) / net_liq * 100 if daily_pnl < 0 else 0
                if daily_loss_pct >= 5.0:
                    log.warning("CIRCUIT_BREAKER_TRIGGERED",
                                daily_loss_pct=round(daily_loss_pct, 2),
                                daily_pnl=round(daily_pnl, 2),
                                net_liq=round(net_liq, 2))
                    with get_db() as db:
                        state = db.query(SystemState).filter(
                            SystemState.key == "halted"
                        ).first()
                        if state:
                            state.value = "true"
                        else:
                            db.add(SystemState(key="halted", value="true"))
                        reason_text = f"circuit_breaker_{daily_loss_pct:.1f}pct"
                        reason_state = db.query(SystemState).filter(
                            SystemState.key == "halt_reason"
                        ).first()
                        if reason_state:
                            reason_state.value = reason_text
                        else:
                            db.add(SystemState(
                                key="halt_reason",
                                value=reason_text
                            ))

                    # Send ONE critical alert
                    try:
                        from src.core.alerts import get_alert_manager
                        get_alert_manager().halt_alert(
                            f"Circuit breaker: {daily_loss_pct:.1f}% daily loss "
                            f"(P&L: {daily_pnl:,.2f}, Net Liq: {net_liq:,.2f})"
                        )
                    except Exception:
                        pass
        except Exception:
            pass  # don't let circuit breaker check crash the health check


def job_check_hedge():
    """Check and maintain SPY tail hedge."""
    _ensure_event_loop()
    if _is_paused():
        return
    if not _ensure_connected():
        return

    hedge = TailHedge()
    result = hedge.check_and_maintain_hedge()
    if result:
        log.info("hedge_action", result=result)


def job_daily_summary():
    """Send end-of-day summary notification after US market close."""
    _ensure_event_loop()
    if not _ensure_connected():
        return

    try:
        from src.broker.connection import get_ib
        from src.core.alerts import get_alert_manager

        ib = get_ib()
        alerts = get_alert_manager()
        values = ib.accountValues()

        net_liq = 0.0
        realized = 0.0
        unrealized = 0.0
        daily_pnl = 0.0

        for v in values:
            if v.tag == "NetLiquidation" and v.currency in ("EUR", "BASE", "USD"):
                net_liq = float(v.value)
            elif v.tag == "RealizedPnL" and v.currency in ("EUR", "BASE", "USD"):
                realized = float(v.value)
            elif v.tag == "UnrealizedPnL" and v.currency in ("EUR", "BASE", "USD"):
                unrealized = float(v.value)

        daily_pnl = realized + unrealized

        # Count open positions and today's trades
        from src.core.models import Position, PositionStatus, TradeLog
        open_count = 0
        trades_today = 0
        with get_db() as db:
            open_count = db.query(Position).filter(
                Position.status == PositionStatus.OPEN
            ).count()
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            trades_today = db.query(TradeLog).filter(
                TradeLog.timestamp >= today_start
            ).count() if hasattr(TradeLog, 'timestamp') else 0

        # Calculate annualized return (simple: daily P&L / net_liq * 252)
        annual_return = None
        if net_liq > 0 and daily_pnl != 0:
            # This is a rough estimate — proper tracking needs inception date
            daily_return_pct = daily_pnl / net_liq * 100
            annual_return = daily_return_pct * 252  # rough annualization

        alerts.daily_summary(
            net_liq=net_liq,
            daily_pnl=daily_pnl,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            open_positions=open_count,
            trades_today=trades_today,
            annual_return_pct=annual_return,
        )

    except Exception as e:
        log.error("daily_summary_error", error=str(e))


def job_heartbeat():
    """💚 Daily heartbeat — proof of life sent every morning.
    If you stop receiving this, something is wrong."""
    _ensure_event_loop()

    try:
        from src.core.alerts import get_alert_manager
        from src.broker.connection import get_ib
        import time

        alerts = get_alert_manager()
        connected = is_connected()

        net_liq = 0.0
        margin_pct = 0.0
        open_positions = 0

        if connected:
            try:
                ib = get_ib()
                values = ib.accountValues()
                for v in values:
                    if v.tag == "NetLiquidation" and v.currency in ("EUR", "BASE", "USD"):
                        net_liq = float(v.value)
                    elif v.tag == "FullMaintMarginReq" and v.currency in ("EUR", "BASE", "USD"):
                        margin = float(v.value)
                        if net_liq > 0:
                            margin_pct = margin / net_liq * 100
            except Exception:
                pass

            from src.core.models import Position, PositionStatus
            with get_db() as db:
                open_positions = db.query(Position).filter(
                    Position.status == PositionStatus.OPEN
                ).count()

        uptime_hours = (time.time() - _app_start_time) / 3600
        scan_ago_sec = time.time() - _last_successful_scan
        if scan_ago_sec < 3600:
            last_scan = f"{int(scan_ago_sec / 60)}m ago"
        else:
            last_scan = f"{scan_ago_sec / 3600:.1f}h ago"

        alerts.heartbeat_alert(
            net_liq=net_liq,
            margin_pct=margin_pct,
            open_positions=open_positions,
            uptime_hours=uptime_hours,
            last_scan_ago=last_scan,
            connection_ok=connected,
        )

    except Exception as e:
        log.error("heartbeat_error", error=str(e))


def job_margin_monitor():
    """Check margin and NLV every health check cycle.
    Alert on high margin (>85%) or large drawdown (>5% single day)."""
    _ensure_event_loop()
    if not is_connected():
        return

    try:
        from src.broker.connection import get_ib
        from src.core.alerts import get_alert_manager
        from src.core.models import AccountSnapshot

        ib = get_ib()
        alerts = get_alert_manager()
        values = ib.accountValues()

        net_liq = 0.0
        margin = 0.0
        for v in values:
            if v.tag == "NetLiquidation" and v.currency in ("EUR", "BASE", "USD"):
                net_liq = float(v.value)
            elif v.tag == "FullMaintMarginReq" and v.currency in ("EUR", "BASE", "USD"):
                margin = float(v.value)

        if net_liq <= 0:
            return

        margin_pct = margin / net_liq * 100

        # Margin warning at 85%
        if margin_pct > 85:
            # Only alert once per hour (use a simple timestamp tracker)
            last_margin_alert = getattr(job_margin_monitor, '_last_alert', 0)
            import time
            if time.time() - last_margin_alert > 3600:
                alerts.margin_warning_alert(margin_pct, net_liq)
                job_margin_monitor._last_alert = time.time()

        # Drawdown check: compare current NLV to yesterday's snapshot
        with get_db() as db:
            from datetime import date as _date, timedelta
            yesterday = (_date.today() - timedelta(days=1)).isoformat()
            snap = db.query(AccountSnapshot).filter(
                AccountSnapshot.date == yesterday
            ).first()
            if snap and snap.net_liquidation and snap.net_liquidation > 0:
                drop_pct = (snap.net_liquidation - net_liq) / snap.net_liquidation * 100
                if drop_pct > 5:
                    last_dd_alert = getattr(job_margin_monitor, '_last_dd_alert', 0)
                    import time
                    if time.time() - last_dd_alert > 3600:
                        alerts.drawdown_alert(drop_pct, net_liq, snap.net_liquidation)
                        job_margin_monitor._last_dd_alert = time.time()

    except Exception as e:
        log.error("margin_monitor_error", error=str(e))


def job_db_cleanup():
    """Weekly database cleanup — purge old suggestions, vacuum SQLite."""
    try:
        from src.core.suggestions import TradeSuggestion

        cutoff = datetime.utcnow() - timedelta(days=7)
        with get_db() as db:
            # Delete expired/rejected suggestions older than 7 days
            old_suggestions = db.query(TradeSuggestion).filter(
                TradeSuggestion.status.in_(["expired", "rejected"]),
                TradeSuggestion.created_at < cutoff,
            ).all()
            count = len(old_suggestions)
            for s in old_suggestions:
                db.delete(s)

        if count > 0:
            log.info("db_cleanup_suggestions", deleted=count)

        # Vacuum SQLite to reclaim space
        import sqlite3
        from src.core.config import get_settings
        db_path = get_settings().app.db_path
        conn = sqlite3.connect(db_path)
        conn.execute("VACUUM")
        conn.close()
        log.info("db_cleanup_vacuum_complete")

    except Exception as e:
        log.error("db_cleanup_error", error=str(e))


def create_scheduler() -> BackgroundScheduler:
    """
    Create and configure the multi-market job scheduler.
    Each market gets scan jobs that run every 30 min during its trading hours.
    """
    global _scheduler
    cfg = get_settings().schedule

    # Use UTC internally — each job specifies its own timezone
    scheduler = BackgroundScheduler(timezone=pytz.UTC)

    universe = UniverseManager()

    # ── Per-market scan jobs ────────────────────────────────
    enabled_markets = cfg.enabled_markets  # empty list = all markets
    for exchange in universe.markets:
        # Skip markets not in enabled list (if list is set)
        if enabled_markets and exchange not in enabled_markets:
            log.info("market_scan_skipped", exchange=exchange, reason="not in enabled_markets")
            continue

        session = universe.get_market_session(exchange)
        if not session:
            log.warning("unknown_market_session", exchange=exchange)
            continue

        tz_name, open_h, open_m, close_h, close_m = session
        tz = pytz.timezone(tz_name)
        stock_count = len(universe.symbols_for_market(exchange))

        # CronTrigger that fires every 30 min during market hours
        # hour range: open_h to close_h-1 (last scan starts before close)
        # Stagger minutes per market to avoid race condition on global _ib swap
        market_minutes = {
            "SMART": "0,30",
            "SMART_EU": "10,40",
            "SMART_ASIA": "20,50",
        }
        scan_minute = market_minutes.get(exchange, "0,30")
        scan_hour = f"{open_h}-{close_h - 1}" if close_h > open_h else f"{open_h}"

        scheduler.add_job(
            partial(job_scan_market, exchange),
            CronTrigger(
                hour=scan_hour,
                minute=scan_minute,
                day_of_week="mon-fri",
                timezone=tz,
            ),
            id=f"scan_{exchange}",
            name=f"Scan {exchange} ({stock_count} stocks)",
            max_instances=1,
            replace_existing=True,
        )

        # If market is currently open, also fire an immediate scan
        from datetime import datetime as _dt, timedelta as _td
        now_local = _dt.now(tz)
        if (now_local.weekday() < 5
                and open_h <= now_local.hour < close_h):
            # Stagger startup scans so they don't all fire at once
            if not hasattr(create_scheduler, '_startup_delay'):
                create_scheduler._startup_delay = 45
            else:
                create_scheduler._startup_delay += 60

            scheduler.add_job(
                partial(job_scan_market, exchange),
                'date',
                run_date=_dt.now(pytz.UTC) + _td(seconds=create_scheduler._startup_delay),
                id=f"scan_{exchange}_startup",
                name=f"Startup Scan {exchange}",
                max_instances=1,
            )

        log.info(
            "market_scan_scheduled",
            exchange=exchange,
            timezone=tz_name,
            hours=f"{open_h:02d}:{open_m:02d}–{close_h:02d}:{close_m:02d}",
            stocks=stock_count,
        )

    # ── Assignment checks — run twice daily at fixed US times ──
    us_tz = pytz.timezone("US/Eastern")

    scheduler.add_job(
        job_check_assignments,
        CronTrigger(hour=10, minute=0, day_of_week="mon-fri", timezone=us_tz),
        id="check_assignments",
        name="Check Assignments (AM)",
        max_instances=1,
    )

    scheduler.add_job(
        job_check_assignments,
        CronTrigger(hour=15, minute=30, day_of_week="mon-fri", timezone=us_tz),
        id="check_assignments_eod",
        name="Check Assignments (EOD)",
        max_instances=1,
    )

    # ── Profit check — every 5 min ──
    scheduler.add_job(
        job_check_profit,
        IntervalTrigger(minutes=cfg.position_check_minutes),
        id="check_profit",
        name="Check Profit Targets",
        max_instances=1,
    )

    # ── Queued suggestion executor — every 30s (manual approve pickup) ──
    scheduler.add_job(
        job_execute_queued,
        IntervalTrigger(seconds=30),
        id="execute_queued",
        name="Execute Queued Suggestions",
        max_instances=1,
    )

    # ── Health check — every 5 min (reduced to avoid pacing violations) ──
    scheduler.add_job(
        job_health_check,
        IntervalTrigger(minutes=5),
        id="health_check",
        name="IBKR Health Check",
        max_instances=1,
    )

    # ── IBKR Trade Sync — every 15 min, pulls real executions ──
    scheduler.add_job(
        _job_trade_sync,
        IntervalTrigger(minutes=15),
        id="trade_sync",
        name="IBKR Trade Sync",
        max_instances=1,
    )

    # ── Hedge check — once daily at 10:30 AM ET ──
    scheduler.add_job(
        job_check_hedge,
        CronTrigger(hour=10, minute=30, day_of_week="mon-fri", timezone=us_tz),
        id="check_hedge",
        name="Check/Roll SPY Hedge",
        max_instances=1,
    )

    # ── Daily summary alert — 16:15 ET (after US close) ──
    scheduler.add_job(
        job_daily_summary,
        CronTrigger(hour=16, minute=15, day_of_week="mon-fri", timezone=us_tz),
        id="daily_summary",
        name="Daily Summary Alert",
        max_instances=1,
    )

    # ── Daily heartbeat — 8:00 AM ET every day (including weekends) ──
    scheduler.add_job(
        job_heartbeat,
        CronTrigger(hour=8, minute=0, timezone=us_tz),
        id="heartbeat",
        name="Daily Heartbeat",
        max_instances=1,
    )

    # ── Margin & drawdown monitor — every 30 min during US hours ──
    scheduler.add_job(
        job_margin_monitor,
        CronTrigger(hour="9-16", minute="15,45", day_of_week="mon-fri", timezone=us_tz),
        id="margin_monitor",
        name="Margin & Drawdown Monitor",
        max_instances=1,
    )

    # ── Database cleanup — Sunday 3:00 AM ET ──
    scheduler.add_job(
        job_db_cleanup,
        CronTrigger(hour=3, minute=0, day_of_week="sun", timezone=us_tz),
        id="db_cleanup",
        name="Weekly DB Cleanup",
        max_instances=1,
    )

    # ── Portfolio jobs (long-term builder) — runs 24/7 ────────
    portfolio_cfg = get_settings().portfolio
    if portfolio_cfg.enabled:
        from datetime import datetime as dt, timedelta
        from src.portfolio.scheduler import (
            job_portfolio_scan, job_portfolio_update_prices,
            job_portfolio_update_metrics, job_portfolio_annual_rescreen,
            job_portfolio_sync_trades,
        )

        # Buy scan — every N hours, 24/7
        # Stagger: prices first (60s), trade sync (90s), metrics (120s), scan later (180s)
        prices_first_run = dt.now(pytz.UTC) + timedelta(seconds=60)
        trade_sync_first_run = dt.now(pytz.UTC) + timedelta(seconds=90)
        metrics_first_run = dt.now(pytz.UTC) + timedelta(seconds=120)
        scan_first_run = dt.now(pytz.UTC) + timedelta(seconds=180)

        scheduler.add_job(
            partial(job_portfolio_scan, portfolio_cfg),
            IntervalTrigger(hours=portfolio_cfg.check_interval_hours),
            id="portfolio_scan",
            name="Portfolio Buy Scan (24/7)",
            max_instances=1,
            next_run_time=scan_first_run,
        )

        # Price updates — every hour, 24/7
        scheduler.add_job(
            partial(job_portfolio_update_prices, portfolio_cfg),
            IntervalTrigger(hours=1),
            id="portfolio_prices",
            name="Portfolio Price Update",
            max_instances=1,
            next_run_time=prices_first_run,
        )

        # Trade sync — import IBKR put/stock trades for watchlist symbols
        scheduler.add_job(
            partial(job_portfolio_sync_trades, portfolio_cfg),
            IntervalTrigger(minutes=30),
            id="portfolio_trade_sync",
            name="Portfolio Trade Sync (IBKR)",
            max_instances=1,
            next_run_time=trade_sync_first_run,
        )

        # Metrics updates (SMA, RSI, discount) — every 4 hours
        # Runs independently from buy scan so metrics show even when margin blocks buying
        scheduler.add_job(
            partial(job_portfolio_update_metrics, portfolio_cfg),
            IntervalTrigger(hours=portfolio_cfg.check_interval_hours),
            id="portfolio_metrics",
            name="Portfolio Watchlist Metrics",
            max_instances=1,
            next_run_time=metrics_first_run,
        )

        # Annual rescreen — once a year
        scheduler.add_job(
            partial(job_portfolio_annual_rescreen, portfolio_cfg),
            CronTrigger(
                month=portfolio_cfg.rescreen_month,
                day=portfolio_cfg.rescreen_day,
                hour=10,
                minute=0,
                timezone=us_tz,
            ),
            id="portfolio_rescreen",
            name="Portfolio Annual Rescreen",
            max_instances=1,
        )

        log.info("portfolio_scheduler_enabled",
                 account=portfolio_cfg.ibkr_account,
                 scan_interval=f"{portfolio_cfg.check_interval_hours}h",
                 mode="24/7 extended hours")

    # ── Bridge job (annual harvest) ─────────────────────────
    # Runs daily at noon ET, but only acts on the configured date
    from src.portfolio.bridge import CashBridge, BridgeConfig
    bridge_cfg = BridgeConfig()  # defaults; overridden by dashboard state at runtime
    scheduler.add_job(
        _job_bridge_check,
        CronTrigger(hour=12, minute=0, timezone=us_tz),
        id="bridge_check",
        name="Cash Bridge Daily Check",
        max_instances=1,
    )

    _scheduler = scheduler

    # ── Consigliere daily review ─────────────────────────────
    scheduler.add_job(
        _job_consigliere_review,
        CronTrigger(hour=17, minute=30, timezone=us_tz),  # 5:30 PM ET, after market close
        id="consigliere_review",
        name="Consigliere Daily Review",
        max_instances=1,
    )
    log.info("consigliere_scheduled", time="17:30 ET daily")

    # ── Daily Account Snapshot ────────────────────────────────
    def _job_account_snapshot():
        """Save daily NLV snapshot for performance charts."""
        _ensure_event_loop()
        try:
            from src.core.models import AccountSnapshot, Trade, TradeType
            from src.core.database import get_db
            from src.broker.account import get_account_summary
            from src.portfolio.models import PortfolioHolding
            from datetime import datetime

            today = datetime.utcnow().strftime("%Y-%m-%d")

            # Get account NLV
            summary = get_account_summary()
            nlv = summary.net_liquidation if summary and summary.net_liquidation > 0 else 0

            if nlv <= 0:
                return

            # Get cumulative options premium
            with get_db() as db:
                trades = db.query(Trade).filter(
                    Trade.order_status.in_(["FILLED", "filled"])
                ).all()

            cum_premium = 0.0
            for t in trades:
                if t.trade_type in (TradeType.SELL_PUT, TradeType.SELL_CALL):
                    cum_premium += (t.premium or 0) * (t.quantity or 1) * 100 - (t.commission or 0)
                elif t.trade_type in (TradeType.BUY_PUT, TradeType.BUY_CALL):
                    cum_premium -= (t.premium or 0) * (t.quantity or 1) * 100 + (t.commission or 0)

            # Get portfolio values
            with get_db() as db:
                holdings = db.query(PortfolioHolding).filter(
                    PortfolioHolding.shares > 0
                ).all()

            port_invested = sum(h.total_invested or 0 for h in holdings)
            port_value = sum(h.market_value or 0 for h in holdings)

            # Upsert today's snapshot
            with get_db() as db:
                existing = db.query(AccountSnapshot).filter(
                    AccountSnapshot.date == today
                ).first()

                if existing:
                    existing.net_liquidation = nlv
                    existing.options_premium_collected = round(cum_premium, 2)
                    existing.portfolio_invested = round(port_invested, 2)
                    existing.portfolio_market_value = round(port_value, 2)
                else:
                    db.add(AccountSnapshot(
                        date=today,
                        net_liquidation=round(nlv, 2),
                        options_premium_collected=round(cum_premium, 2),
                        portfolio_invested=round(port_invested, 2),
                        portfolio_market_value=round(port_value, 2),
                    ))

            log.info("account_snapshot_saved", date=today, nlv=round(nlv, 2))
        except Exception as e:
            log.error("account_snapshot_error", error=str(e))

    # Snapshot at market open and market close (to catch both)
    scheduler.add_job(
        _job_account_snapshot,
        CronTrigger(hour=9, minute=35, timezone=us_tz),  # shortly after US market open
        id="account_snapshot_open",
        name="Account Snapshot (Open)",
        max_instances=1,
    )
    scheduler.add_job(
        _job_account_snapshot,
        CronTrigger(hour=16, minute=5, timezone=us_tz),  # shortly after US market close
        id="account_snapshot_close",
        name="Account Snapshot (Close)",
        max_instances=1,
    )
    log.info("account_snapshot_scheduled")

    # Also take a snapshot right now at startup
    try:
        _job_account_snapshot()
    except Exception:
        pass

    # ── IPO Rider jobs ────────────────────────────────────────
    from src.ipo.trader import IpoTrader
    from src.ipo.scanner import scan_ipo_calendar

    def _job_ipo_scan():
        """Scan for newly tradeable IPO tickers."""
        _ensure_event_loop()
        try:
            from src.broker.connection import get_ib
            ib = get_ib()
            trader = IpoTrader(ib)
            trader.scan_for_new_ipos()
        except Exception as e:
            log.error("ipo_scan_error", error=str(e))

    def _job_ipo_check_exits():
        """Check if any IPO flip orders have filled."""
        _ensure_event_loop()
        try:
            from src.broker.connection import get_ib
            ib = get_ib()
            trader = IpoTrader(ib)
            trader.check_flip_exits()
            trader.check_lockup_entries()
        except Exception as e:
            log.error("ipo_exit_check_error", error=str(e))

    def _job_ipo_date_scan():
        """Check Finnhub for upcoming IPO dates."""
        _ensure_event_loop()
        try:
            scan_ipo_calendar()
        except Exception as e:
            log.error("ipo_date_scan_error", error=str(e))

    # IPO ticker scan — every 5 minutes during market hours
    scheduler.add_job(
        _job_ipo_scan,
        IntervalTrigger(minutes=5),
        id="ipo_scan",
        name="IPO Ticker Scan",
        max_instances=1,
    )

    # IPO exit/lockup check — every 5 minutes
    scheduler.add_job(
        _job_ipo_check_exits,
        IntervalTrigger(minutes=5),
        id="ipo_exits",
        name="IPO Exit & Lockup Check",
        max_instances=1,
    )

    # IPO date calendar scan — daily at 8 AM ET
    scheduler.add_job(
        _job_ipo_date_scan,
        CronTrigger(hour=8, minute=0, timezone=us_tz),
        id="ipo_date_scan",
        name="IPO Date Calendar Scan",
        max_instances=1,
    )

    log.info("ipo_rider_scheduled")

    return scheduler


def _job_trade_sync():
    """Periodic IBKR trade sync — imports real executions."""
    _ensure_event_loop()
    if not _ensure_connected():
        return
    try:
        from src.broker.trade_sync import sync_ibkr_trades, sync_ibkr_positions
        imported = sync_ibkr_trades()
        if imported:
            log.info("trade_sync_job_done", imported=imported)
        # Also sync positions
        pos_changes = sync_ibkr_positions()
        if pos_changes:
            log.info("position_sync_job_done", changes=pos_changes)
    except Exception as e:
        log.error("trade_sync_job_error", error=str(e))


def _job_bridge_check():
    """Daily bridge check — reads config from dashboard state."""
    _ensure_event_loop()
    if not _ensure_connected():
        return

    from src.portfolio.bridge import CashBridge, BridgeConfig
    from src.broker.connection import get_ib

    # Read bridge settings from DB state (set via dashboard)
    enabled = _get_state_value("bridge_enabled") == "true"
    if not enabled:
        return

    try:
        cfg = BridgeConfig(
            enabled=True,
            transfer_pct=float(_get_state_value("bridge_transfer_pct") or "10") / 100,
            min_portfolio_value=float(_get_state_value("bridge_min_value") or "1000000"),
            transfer_month=int(_get_state_value("bridge_month") or "7"),
            transfer_day=int(_get_state_value("bridge_day") or "31"),
            source_account=_get_state_value("bridge_source_account") or "",
            target_account=_get_state_value("bridge_target_account") or "",
            dry_run=_get_state_value("bridge_dry_run") != "false",
        )
        bridge = CashBridge(get_ib(), cfg)
        result = bridge.check_and_transfer()
        if result:
            log.info("bridge_check_result", **result)
    except Exception as e:
        log.error("bridge_check_error", error=str(e))


def _get_state_value(key: str) -> str | None:
    """Read a value from SystemState."""
    with get_db() as db:
        state = db.query(SystemState).filter(SystemState.key == key).first()
        return state.value if state else None


def _job_consigliere_review():
    """Daily Consigliere analysis — runs after market close."""
    _ensure_event_loop()
    try:
        from src.consigliere.advisor import Consigliere
        advisor = Consigliere()
        findings = advisor.run_daily_review()
        log.info("consigliere_daily_complete", findings=len(findings))

        # Send alert if there are new suggestions
        if findings:
            from src.core.alerts import AlertManager, AlertConfig
            try:
                cfg = get_settings()
                alerts = AlertManager(AlertConfig(
                    ntfy_topic=cfg.alerts.ntfy_topic,
                    enabled=cfg.alerts.enabled,
                ))
                warning_count = len([f for f in findings if f.severity in ("warning", "critical")])
                alerts.send(
                    title="🤵 Consigliere Review Complete",
                    body=(
                        f"{len(findings)} new insights generated.\n"
                        f"{'⚠ ' + str(warning_count) + ' require attention.' if warning_count else 'No urgent items.'}\n"
                        f"Review at dashboard → Consigliere"
                    ),
                    priority="high" if warning_count else "default",
                    tags="consigliere",
                )
            except Exception:
                pass  # alerts are best-effort
    except Exception as e:
        log.error("consigliere_review_error", error=str(e))


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler
