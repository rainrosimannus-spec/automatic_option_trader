"""
Main dashboard — overview of system status, P&L, positions, market regime,
account balances, and AI decision performance chart.
"""
from __future__ import annotations

from datetime import datetime, date, timedelta

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from src.web.template_engine import templates
from src.core.database import get_db
from src.core.models import Position, Trade, PositionStatus, SystemState, TradeType, OrderStatus
from src.broker.connection import is_connected

router = APIRouter()


def _get_state_val(db, key: str) -> str | None:
    state = db.query(SystemState).filter(SystemState.key == key).first()
    return state.value if state else None


def _get_account_data() -> dict:
    """Safely fetch account data — returns zeros if unavailable."""
    try:
        from src.broker.account import get_account_summary
        summary = get_account_summary()
        return {
            "net_liquidation": summary.net_liquidation,
            "buying_power": summary.buying_power,
            "cash_balance": summary.cash_balance,
            "unrealized_pnl": summary.unrealized_pnl,
            "realized_pnl": summary.realized_pnl,
            "maintenance_margin": summary.maintenance_margin,
            "margin_used_pct": (
                summary.maintenance_margin / summary.net_liquidation * 100
                if summary.net_liquidation > 0 else 0
            ),
        }
    except Exception:
        return {
            "net_liquidation": 0, "buying_power": 0, "cash_balance": 0,
            "unrealized_pnl": 0, "realized_pnl": 0, "maintenance_margin": 0,
            "margin_used_pct": 0,
        }


def _get_performance_data() -> dict:
    """
    Build performance chart as % return using daily account snapshots.
    - "actual" line: options premium earned as % of starting NLV (deposit-proof)
    - "target" line: 24% annualized return % over same period

    Uses AccountSnapshot table. Falls back to trade-based calc if no snapshots yet.
    """
    from src.core.models import AccountSnapshot, Trade, TradeType
    from datetime import datetime

    with get_db() as db:
        snapshots = (
            db.query(AccountSnapshot)
            .order_by(AccountSnapshot.date.asc())
            .all()
        )

    # ── Get or set the permanent start date ──
    from src.core.models import SystemState
    INCEPTION_DATE = "2026-02-20"  # system went live

    with get_db() as db:
        start_row = db.query(SystemState).filter(
            SystemState.key == "options_start_date"
        ).first()
        if start_row:
            start_date_str = start_row.value
        else:
            start_date_str = INCEPTION_DATE
            db.add(SystemState(key="options_start_date", value=start_date_str))

    start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    daily_target_rate = (1.24 ** (1 / 365)) - 1

    # ── Snapshot-based chart (preferred, handles deposits correctly) ──
    if len(snapshots) >= 1:
        labels = []
        actual_line = []
        target_line = []

        first_nlv = snapshots[0].net_liquidation
        first_premium = snapshots[0].options_premium_collected

        # Always start from inception date, even if first snapshot is later
        if snapshots[0].date > start_date_str:
            labels.append(start_date_str)
            actual_line.append(0)
            target_line.append(0)

        for snap in snapshots:
            labels.append(snap.date)

            premium_gain = snap.options_premium_collected - first_premium
            actual_pct = (premium_gain / first_nlv) * 100 if first_nlv > 0 else 0
            actual_line.append(round(actual_pct, 2))

            snap_date = datetime.strptime(snap.date, "%Y-%m-%d")
            days = max((snap_date - start_date).days, 0)
            target_pct = ((1 + daily_target_rate) ** days - 1) * 100
            target_line.append(round(target_pct, 2))

        # Ensure at least 2 points so the chart renders
        if len(labels) < 2:
            today = datetime.utcnow().strftime("%Y-%m-%d")
            if today == labels[-1]:
                # Same day — add "Day 1" as next day
                from datetime import timedelta
                tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
                labels.append(tomorrow)
            else:
                labels.append(today)
            actual_line.append(actual_line[-1])
            days = max((datetime.utcnow() - start_date).days + 1, 1)
            target_line.append(round(((1 + daily_target_rate) ** days - 1) * 100, 2))

        return {"labels": labels, "actual": actual_line, "target": target_line}

    # ── No snapshots at all: use trade-based with fixed start date ──
    with get_db() as db:
        trades = (
            db.query(Trade)
            .filter(Trade.order_status.in_(["FILLED", "filled"]))
            .order_by(Trade.created_at.asc())
            .all()
        )

    if not trades:
        # Show empty chart starting from inception
        today = datetime.utcnow().strftime("%Y-%m-%d")
        days = max((datetime.utcnow() - start_date).days, 0)
        target_pct = ((1 + daily_target_rate) ** days - 1) * 100
        return {
            "labels": [start_date_str, today],
            "actual": [0, 0],
            "target": [0, round(target_pct, 2)],
        }

    try:
        from src.broker.account import get_account_summary
        summary = get_account_summary()
        net_liq = summary.net_liquidation if summary and summary.net_liquidation > 0 else 100000
    except Exception:
        net_liq = 100000

    total_premium = 0.0
    for t in trades:
        if t.trade_type in (TradeType.SELL_PUT, TradeType.SELL_CALL):
            total_premium += (t.premium or 0) * (t.quantity or 1) * 100 - (t.commission or 0)
        elif t.trade_type in (TradeType.BUY_PUT, TradeType.BUY_CALL):
            total_premium -= (t.premium or 0) * (t.quantity or 1) * 100 + (t.commission or 0)

    starting_nlv = max(net_liq - total_premium, net_liq * 0.8)

    # Start from inception
    labels = [start_date_str]
    actual_line = [0]
    target_line = [0]
    cum_pnl = 0.0
    current_date = None

    for t in trades:
        t_date = t.created_at.strftime("%Y-%m-%d") if t.created_at else ""
        if not t_date:
            continue
        if t.trade_type in (TradeType.SELL_PUT, TradeType.SELL_CALL):
            cum_pnl += (t.premium or 0) * (t.quantity or 1) * 100 - (t.commission or 0)
        elif t.trade_type in (TradeType.BUY_PUT, TradeType.BUY_CALL):
            cum_pnl -= (t.premium or 0) * (t.quantity or 1) * 100 + (t.commission or 0)

        if t_date != current_date:
            labels.append(t_date)
            actual_line.append(round((cum_pnl / starting_nlv) * 100, 2))
            days = max((t.created_at.date() - start_date.date()).days, 0) if t.created_at else 0
            target_line.append(round(((1 + daily_target_rate) ** days - 1) * 100, 2))
            current_date = t_date
        else:
            if actual_line:
                actual_line[-1] = round((cum_pnl / starting_nlv) * 100, 2)

    return {"labels": labels, "actual": actual_line, "target": target_line}


def _estimate_suggestion_pnl(s) -> float:
    """
    Estimate the P&L that would have resulted from a suggestion.

    For buy_stock: (current_price - suggested_price) * quantity
    For sell_put: premium collected × quantity × 100 (assumes expired worthless)
    For sell_covered_call: similar premium collection estimate
    For review actions: 0 (can't estimate)
    """
    if not s.limit_price or not s.quantity:
        return 0.0

    if s.action == "buy_stock" and s.current_price:
        # Try to get current price from watchlist metrics
        try:
            from src.core.database import get_db
            from src.portfolio.models import PortfolioWatchlist
            with get_db() as db:
                wl = db.query(PortfolioWatchlist).filter(
                    PortfolioWatchlist.symbol == s.symbol
                ).first()
                if wl and wl.current_price and wl.current_price > 0:
                    return (wl.current_price - s.limit_price) * s.quantity
        except Exception:
            pass
        return 0.0

    elif s.action == "sell_put" and s.limit_price:
        # Premium collected (assumes expired worthless — best case)
        return s.limit_price * s.quantity * 100

    elif s.action in ("sell_covered_call", "sell_call"):
        return (s.limit_price or 0) * (s.quantity or 0) * 100

    return 0.0


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with get_db() as db:
        open_positions = (
            db.query(Position)
            .filter(Position.status == PositionStatus.OPEN)
            .all()
        )
        closed_positions = (
            db.query(Position)
            .filter(Position.status.in_([PositionStatus.CLOSED, PositionStatus.EXPIRED]))
            .all()
        )

        # System state
        current_vix = _get_state_val(db, "current_vix")
        paused_state = _get_state_val(db, "paused")
        spy_bullish = _get_state_val(db, "spy_bullish")
        spy_fast_ma = _get_state_val(db, "spy_fast_ma")
        spy_slow_ma = _get_state_val(db, "spy_slow_ma")
        spy_price = _get_state_val(db, "spy_price")
        market_regime = _get_state_val(db, "market_regime") or "normal"

        # Daily trade count
        today_start = datetime.combine(date.today(), datetime.min.time())
        daily_count = (
            db.query(Position)
            .filter(
                Position.opened_at >= today_start,
                Position.position_type == "short_put",
            )
            .count()
        )

        total_realized = sum(p.realized_pnl for p in closed_positions)

        # Calculate premium from actual trades (IBKR fills = source of truth)
        all_trades = db.query(Trade).filter(Trade.order_status == OrderStatus.FILLED).all()
        total_premium = 0.0
        for t in all_trades:
            if t.trade_type in (TradeType.SELL_PUT, TradeType.SELL_CALL):
                total_premium += (t.premium or 0) * (t.quantity or 1) * 100 - (t.commission or 0)
            elif t.trade_type in (TradeType.BUY_PUT, TradeType.BUY_CALL):
                total_premium -= (t.premium or 0) * (t.quantity or 1) * 100 + (t.commission or 0)

        open_puts = [p for p in open_positions if p.position_type == "short_put"]
        open_stock = [p for p in open_positions if p.position_type == "stock"]
        open_calls = [p for p in open_positions if p.position_type == "covered_call"]

        recent_trades = (
            db.query(Trade)
            .order_by(Trade.created_at.desc())
            .limit(15)
            .all()
        )

        # Pending suggestions count — split by source
        from src.core.suggestions import TradeSuggestion
        pending_options = db.query(TradeSuggestion).filter(
            TradeSuggestion.status.in_(["pending", "submitted", "approved", "queued"]),
            TradeSuggestion.source == "options",
        ).count()
        pending_portfolio = db.query(TradeSuggestion).filter(
            TradeSuggestion.status.in_(["pending", "submitted", "approved", "queued"]),
            TradeSuggestion.source == "portfolio",
        ).count()

        # IPO watchlist — count IPOs with confirmed dates within 30 days
        from src.ipo.models import IpoWatchlist
        from datetime import timedelta
        ipo_soon_cutoff = (datetime.utcnow() + timedelta(days=30)).strftime("%Y-%m-%d")
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        ipo_confirmed = db.query(IpoWatchlist).filter(
            IpoWatchlist.status == "watching",
            IpoWatchlist.expected_date.isnot(None),
            IpoWatchlist.expected_date != "",
            IpoWatchlist.expected_date <= ipo_soon_cutoff,
            IpoWatchlist.expected_date >= today_str,
        ).count()

    # Account data (separate to avoid DB session issues)
    account = _get_account_data()
    try:
        performance = _get_performance_data()
    except Exception as e:
        from src.core.logger import get_logger
        get_logger(__name__).warning("performance_data_error", error=str(e))
        today = datetime.utcnow().strftime("%Y-%m-%d")
        performance = {
            "labels": ["2026-02-20", today],
            "actual": [0, 0],
            "target": [0, 0],
        }

    # Open orders — use cached data to prevent dashboard freeze
    # The cache is updated by the health check job every 5 minutes
    open_orders = []
    try:
        from src.broker.orders import get_cached_open_orders
        open_orders = get_cached_open_orders()
    except Exception:
        pass

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "connected": is_connected(),
        "paused": paused_state == "true",
        "current_vix": float(current_vix) if current_vix else None,
        "spy_bullish": spy_bullish == "true" if spy_bullish else None,
        "spy_fast_ma": float(spy_fast_ma) if spy_fast_ma else None,
        "spy_slow_ma": float(spy_slow_ma) if spy_slow_ma else None,
        "spy_price": float(spy_price) if spy_price else None,
        "market_regime": market_regime,
        "daily_count": daily_count,
        "daily_limit": 10,
        "open_puts": len(open_puts),
        "open_stock": len(open_stock),
        "open_calls": len(open_calls),
        "total_open": len(open_positions),
        "total_closed": len(closed_positions),
        "total_realized": total_realized,
        "total_premium": total_premium,
        "open_positions": open_positions,
        "recent_trades": recent_trades,
        "pending_count": pending_options + pending_portfolio,
        "pending_options": pending_options,
        "pending_portfolio": pending_portfolio,
        "ipo_confirmed": ipo_confirmed,
        "open_orders": open_orders,
        # Account
        "net_liquidation": account["net_liquidation"],
        "buying_power": account["buying_power"],
        "cash_balance": account["cash_balance"],
        "unrealized_pnl": account["unrealized_pnl"],
        "margin_used_pct": account["margin_used_pct"],
        "maintenance_margin": account["maintenance_margin"],
        # Performance chart
        "perf_labels": performance["labels"],
        "perf_actual": performance["actual"],
        "perf_target": performance["target"],
    })
