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
from src.strategy.risk import adaptive_max_positions

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
            "excess_liquidity": summary.excess_liquidity,
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
            "net_liquidation": 0, "buying_power": 0, "excess_liquidity": 0, "cash_balance": 0,
            "unrealized_pnl": 0, "realized_pnl": 0, "maintenance_margin": 0,
            "margin_used_pct": 0,
        }


def _get_options_start_date() -> str:
    """Inception date for the option-trader performance chart.

    Auto-anchors to *today* the first time the configured options account differs
    from the one the chart was last anchored to — i.e. the 2026-06 split onto the
    dedicated account (U25878705). This makes the graph begin at separation, from
    0%, instead of inheriting the merged account's history (Feb 2026). After that
    first boot the date is stable in SystemState; edit that row to override.
    """
    from src.core.models import SystemState
    from src.core.config import get_settings
    from datetime import datetime

    acct = get_settings().ibkr.account or ""
    today = datetime.utcnow().strftime("%Y-%m-%d")

    with get_db() as db:
        acct_row = db.query(SystemState).filter(
            SystemState.key == "options_perf_account"
        ).first()
        start_row = db.query(SystemState).filter(
            SystemState.key == "options_start_date"
        ).first()

        # First run under a new trading account → (re)anchor inception to today.
        if acct_row is None or acct_row.value != acct:
            if start_row:
                start_row.value = today
            else:
                db.add(SystemState(key="options_start_date", value=today))
            if acct_row:
                acct_row.value = acct
            else:
                db.add(SystemState(key="options_perf_account", value=acct))
            return today

        if start_row:
            return start_row.value
        db.add(SystemState(key="options_start_date", value=today))
        return today


def _get_performance_data() -> dict:
    """
    Build performance chart as % return using daily account snapshots.
    - "actual" line: NLV return vs invested capital, anchored to 0% at inception
    - "target" line: 24% annualized return % over same period

    Capital deposits are deposit-proof: each snapshot's return divides NLV by the
    cumulative deposits *as of that date*, so fresh cash lifts NLV and invested
    capital together and the return dilutes instead of spiking. Falls back to
    trade-based calc if no snapshots yet.
    """
    from src.core.models import AccountSnapshot, Trade, TradeType
    from src.core.config import get_settings
    from datetime import datetime

    # ── Inception (separation day) — also filters out pre-split snapshots ──
    start_date_str = _get_options_start_date()
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    daily_target_rate = (1.24 ** (1 / 365)) - 1

    options_account = get_settings().ibkr.account

    with get_db() as db:
        snapshots = (
            db.query(AccountSnapshot)
            .filter(AccountSnapshot.date >= start_date_str)
            .order_by(AccountSnapshot.date.asc())
            .all()
        )

    # Drop snapshots with no/zero NLV. These are transient rows written right after a restart,
    # before the IBKR account summary has loaded — net_liquidation comes back 0, and (0/invested - 1)
    # = -100% spikes the chart straight down (and, if it's the first point, anchors everything there).
    # The graph "recovers" only when a real NLV snapshot lands. Filtering them removes the false fall.
    snapshots = [s for s in snapshots if s.net_liquidation and s.net_liquidation > 0]

    # ── Snapshot-based chart (preferred, handles deposits correctly) ──
    if len(snapshots) >= 1:
        labels = []
        actual_line = []
        target_line = []

        # Time-aware invested capital: cumulative deposits (USD) as of each date.
        # A deposit raises both NLV and the divisor, so the return % dilutes rather
        # than jumping — fresh cash hasn't earned anything yet.
        from src.portfolio.models import PortfolioCapitalInjection
        with get_db() as db:
            inj_rows = (
                db.query(PortfolioCapitalInjection)
                .filter(PortfolioCapitalInjection.account_id == options_account)
                .order_by(PortfolioCapitalInjection.date.asc())
                .all()
            )
        injections = [(r.date, r.amount_usd or 0.0) for r in inj_rows]

        first_nlv = snapshots[0].net_liquidation or 1

        def _invested_as_of(date_str: str) -> float:
            # Cumulative deposits recorded on/before this snapshot date; falls back
            # to the first snapshot's NLV before any deposit row exists.
            total = sum(amt for d, amt in injections if d <= date_str)
            return total if total > 0 else first_nlv

        # Compute raw returns, then anchor first point to 0%
        raw_returns = []
        for snap in snapshots:
            invested = _invested_as_of(snap.date)
            nlv_return = (snap.net_liquidation / invested - 1) * 100
            raw_returns.append(nlv_return)
        anchor = raw_returns[0] if raw_returns else 0

        # Always start from inception date, even if first snapshot is later
        if snapshots[0].date > start_date_str:
            labels.append(start_date_str)
            actual_line.append(0)
            target_line.append(0)

        for snap, raw in zip(snapshots, raw_returns):
            labels.append(snap.date)
            actual_line.append(round(raw - anchor, 2))

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
        # Hide expired-but-still-OPEN options: once expiry has passed (strict <,
        # matching the reconciler), the option is gone from IBKR (assigned or
        # expired worthless) and only lingers as OPEN until the background sync
        # catches up. Dropping it here removes the display lag in both the Open
        # Positions table and the Short-puts card without waiting for that sync.
        _today = date.today().strftime("%Y%m%d")
        open_positions = [
            p for p in open_positions
            if not (
                p.position_type in ("short_put", "short_call", "covered_call")
                and p.expiry and p.expiry < _today
            )
        ]
        closed_positions = (
            db.query(Position)
            .filter(Position.status.in_([PositionStatus.CLOSED, PositionStatus.EXPIRED]))
            .all()
        )

        # System state
        current_vix = _get_state_val(db, "current_vix")
        vix_spike = _get_state_val(db, "vix_spike")
        effective_vix_tier = _get_state_val(db, "effective_vix_tier")
        spy_ma50 = _get_state_val(db, "spy_ma50")
        spy_distance_below_ma50 = _get_state_val(db, "spy_distance_below_ma50")
        drawdown_5d = _get_state_val(db, "drawdown_5d")
        paused_state = _get_state_val(db, "paused")
        spy_bullish = _get_state_val(db, "spy_bullish")
        spy_fast_ma = _get_state_val(db, "spy_fast_ma")
        spy_slow_ma = _get_state_val(db, "spy_slow_ma")
        spy_price = _get_state_val(db, "spy_price")
        eu_bullish = _get_state_val(db, "eu_bullish")
        eu_price = _get_state_val(db, "eu_price")
        asia_bullish = _get_state_val(db, "asia_bullish")
        asia_price = _get_state_val(db, "asia_price")
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

        # Daily theta estimate — sum across all open short positions
        # Approximation: premium / DTE remaining gives daily decay rate
        # This is conservative (actual theta accelerates as DTE shrinks)
        from datetime import datetime as _dt
        daily_theta = 0.0
        for p in open_puts + open_calls:
            try:
                if p.expiry and p.entry_premium and p.quantity:
                    exp_date = _dt.strptime(p.expiry, "%Y%m%d").date()
                    dte = (exp_date - _dt.utcnow().date()).days
                    if dte > 0:
                        contract_size = 100
                        # Daily theta = remaining premium value / DTE
                        # Use 50% of entry premium as proxy for current value
                        # (assumes we're roughly at midpoint of the trade)
                        remaining = p.entry_premium * 0.5
                        daily_theta += (remaining / dte) * contract_size * p.quantity
            except Exception:
                pass

        _all_recent = (
            db.query(Trade)
            .order_by(Trade.created_at.desc())
            .limit(50)
            .all()
        )
        # Suppress IBKR-side BUY_PUT (@$0) + BUY_STOCK rows when a wheel-side
        # ASSIGNMENT row exists for the same conceptual event. Keyed on
        # (symbol, strike, expiry) so it works across days — the wheel's
        # overnight cron writes ASSIGNMENT a day later than ibkr_sync writes
        # the IBKR rows.
        _assignment_keys = set()
        _assignment_symbols = set()
        for t in _all_recent:
            if t.trade_type == TradeType.ASSIGNMENT:
                _assignment_keys.add((t.symbol, t.strike, t.expiry))
                _assignment_symbols.add(t.symbol)
        recent_trades = []
        for t in _all_recent:
            if t.trade_type == TradeType.BUY_PUT:
                if (t.symbol, t.strike, t.expiry) in _assignment_keys:
                    continue
            elif t.trade_type == TradeType.BUY_STOCK:
                # IBKR's assignment BUY_STOCK has strike=0/expiry='' — can't
                # match on the full triple. Fall back to symbol-only.
                if t.symbol in _assignment_symbols:
                    continue
            recent_trades.append(t)
        recent_trades = recent_trades[:15]

        # Portfolio (Winston) recent transactions
        from src.portfolio.models import PortfolioTransaction
        recent_portfolio_trades = (
            db.query(PortfolioTransaction)
            .order_by(PortfolioTransaction.created_at.desc())
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

    # NLV-net-of-debt card intentionally DISABLED on the options dashboard.
    # After the 2026-06 account split this page shows the dedicated options
    # account (U25878705), which carries no external/shareholder borrowings —
    # its NLV needs no debt correction. The Bruno loan book (data/bruno.db) is
    # the LENDER PORTAL's data, operated separately (and is the son's), so
    # netting it against the options NLV showed the wrong party's debt. The
    # borrower loan book stays accessible under /borrower/*; it just no longer
    # bleeds into the options NLV here. Template hides the card when None.
    debt_card = None

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "connected": is_connected(),
        "paused": paused_state == "true",
        "current_vix": float(current_vix) if current_vix else None,
        "vix_spike": float(vix_spike) if vix_spike else None,
        "effective_vix_tier": effective_vix_tier or None,
        "spy_ma50": float(spy_ma50) if spy_ma50 else None,
        "spy_distance_below_ma50": float(spy_distance_below_ma50) if spy_distance_below_ma50 else None,
        "drawdown_5d": float(drawdown_5d) if drawdown_5d else None,
        "spy_bullish": spy_bullish == "true" if spy_bullish else None,
        "spy_fast_ma": float(spy_fast_ma) if spy_fast_ma else None,
        "spy_slow_ma": float(spy_slow_ma) if spy_slow_ma else None,
        "spy_price": float(spy_price) if spy_price else None,
        "eu_bullish": eu_bullish == "true" if eu_bullish else None,
        "eu_price": float(eu_price) if eu_price else None,
        "asia_bullish": asia_bullish == "true" if asia_bullish else None,
        "asia_price": float(asia_price) if asia_price else None,
        "market_regime": market_regime,
        "daily_count": daily_count,
        "daily_limit": 10,
        "open_puts": len(open_puts),
        "open_stock": len(open_stock),
        "open_calls": len(open_calls),
        # Position slots (NLV-tiered cap; counts only slot-consuming short_put + stock,
        # matching the live check_position_limit gate)
        "slots_used": len(open_puts) + len(open_stock),
        "slots_max": adaptive_max_positions(account["net_liquidation"] or 0),
        "total_open": len(open_positions),
        "total_closed": len(closed_positions),
        "total_realized": total_realized,
        "total_premium": total_premium,
        "daily_theta": round(daily_theta, 2),
        "open_positions": open_positions,
        "recent_trades": recent_trades,
        "recent_portfolio_trades": recent_portfolio_trades,
        "pending_count": pending_options + pending_portfolio,
        "pending_options": pending_options,
        "pending_portfolio": pending_portfolio,
        "ipo_confirmed": ipo_confirmed,
        "open_orders": open_orders,
        # Account
        "net_liquidation": account["net_liquidation"],
        "buying_power": account["buying_power"],
        "excess_liquidity": account["excess_liquidity"],
        "cash_balance": account["cash_balance"],
        "unrealized_pnl": account["unrealized_pnl"],
        "margin_used_pct": account["margin_used_pct"],
        "maintenance_margin": account["maintenance_margin"],
        "debt_card": debt_card,
        # Performance chart
        "perf_labels": performance["labels"],
        "perf_actual": performance["actual"],
        "perf_target": performance["target"],
    })
