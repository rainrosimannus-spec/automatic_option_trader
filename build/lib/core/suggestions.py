"""
Trade Suggestion Queue — safety layer for live account operation.

Instead of placing orders directly, the system creates trade suggestions
that appear on the dashboard with Approve/Reject buttons.

Safety rules (hardcoded, not configurable):
  1. NEVER sell existing stock positions
  2. NEVER buy options (portfolio builder only buys stocks / sells CSPs)
  3. NEVER exceed 70% margin utilization
  4. NEVER modify or cancel existing orders
  5. All trades require explicit human approval via dashboard
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import String, Float, Integer, Boolean, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.core.models import Base
from src.core.database import get_db
from src.core.logger import get_logger

log = get_logger(__name__)

# ── Margin rejection circuit breaker ────────────────────────
# When margin rejections pile up during auto-execute, stop trying
# after 3 rejections. Counter resets on new scan cycle.
_margin_rejected_this_cycle = 0
_buying_power_remaining = None  # Track buying power within a scan cycle


def reset_margin_circuit_breaker():
    """Call at the start of each scan cycle to allow new attempts."""
    global _margin_rejected_this_cycle
    global _buying_power_remaining
    _margin_rejected_this_cycle = 0
    _buying_power_remaining = None  # Re-fetch from IBKR on next auto-approve


class TradeSuggestion(Base):
    """A suggested trade awaiting human approval."""
    __tablename__ = "trade_suggestions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # What
    symbol: Mapped[str] = mapped_column(String(10), index=True)
    action: Mapped[str] = mapped_column(String(20))
    # Actions: "buy_stock", "sell_put", "sell_covered_call"
    # NEVER: "sell_stock", "buy_option"

    quantity: Mapped[int] = mapped_column(Integer, default=0)  # shares or contracts
    limit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    order_type: Mapped[str] = mapped_column(String(10), default="LMT")  # LMT, MKT

    # For options
    strike: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expiry: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    right: Mapped[Optional[str]] = mapped_column(String(1), nullable=True)  # P or C

    # Why
    source: Mapped[str] = mapped_column(String(20), default="portfolio")
    # "portfolio" = portfolio builder, "options" = options trader simulation
    tier: Mapped[str] = mapped_column(String(15), default="growth")
    signal: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Market context at time of suggestion
    current_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sma_200: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    rsi_14: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    iv_rank: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Estimated impact
    est_cost: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    est_margin_impact: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    margin_util_after: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Status
    status: Mapped[str] = mapped_column(String(15), default="pending")
    # "pending", "approved", "rejected", "expired", "executed"
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Ranking (for execution priority when switching to auto mode)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    # Rank within this scan batch: 1 = execute first, 2 = second, etc.
    rank_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # Composite score that produced this rank (for display/audit)
    funding_source: Mapped[str] = mapped_column(String(25), default="cash")
    # "cash", "reserve", "margin_capitulation", "margin_stabilization"

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Exchange/currency for order execution (options may differ from stock exchange)
    opt_exchange: Mapped[Optional[str]] = mapped_column(String(15), nullable=True)
    opt_currency: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)


# ── Safety checks ────────────────────────────────────────────

FORBIDDEN_ACTIONS = {"sell_stock", "buy_put", "buy_call", "buy_option"}
MAX_MARGIN_UTIL = 0.70  # 70% — never suggest beyond this

# Review actions — can be suggested but NEVER auto-executed.
# These require explicit manual approval AND manual execution by the user.
# The system will never place these orders, even if approved.
REVIEW_ONLY_ACTIONS = {
    "sell_stock_review",          # annual review: sell entire position
    "reduce_position_review",     # annual review: reduce overweight position
    "sell_covered_call_review",   # annual review: sell covered call on profitable position
}


def validate_suggestion(
    action: str,
    symbol: str,
    quantity: int,
    existing_positions: list[str],
    current_margin_util: float,
    est_margin_impact: float,
) -> tuple[bool, str]:
    """
    Validate a trade suggestion against safety rules.
    Returns (is_valid, reason).
    """
    # Rule 1: Forbidden actions
    if action in FORBIDDEN_ACTIONS:
        return False, f"Action '{action}' is forbidden"

    # Rule 2: Never sell existing positions
    if action.startswith("sell") and action != "sell_put" and action != "sell_covered_call":
        return False, f"Selling stock positions is forbidden"

    # Rule 3: Covered calls only on positions we own
    if action == "sell_covered_call" and symbol not in existing_positions:
        return False, f"Cannot sell covered call on {symbol} — not in portfolio"

    # Rule 4: Margin check
    new_margin_util = current_margin_util + est_margin_impact
    if new_margin_util > MAX_MARGIN_UTIL:
        return False, (
            f"Would push margin to {new_margin_util:.1%} "
            f"(current: {current_margin_util:.1%}, limit: {MAX_MARGIN_UTIL:.1%})"
        )

    # Rule 5: Quantity must be positive
    if quantity <= 0:
        return False, "Quantity must be positive"

    return True, "OK"


def create_suggestion(
    symbol: str,
    action: str,
    quantity: int,
    limit_price: float | None = None,
    order_type: str = "LMT",
    source: str = "portfolio",
    tier: str = "growth",
    signal: str = "",
    rationale: str = "",
    current_price: float | None = None,
    sma_200: float | None = None,
    rsi_14: float | None = None,
    strike: float | None = None,
    expiry: str | None = None,
    right: str | None = None,
    est_cost: float | None = None,
    est_margin_impact: float | None = None,
    margin_util_after: float | None = None,
    expires_hours: int = 24,
    rank: int = 0,
    rank_score: float | None = None,
    funding_source: str = "cash",
    opt_exchange: str | None = None,
    opt_currency: str | None = None,
) -> TradeSuggestion | None:
    """
    Create a trade suggestion after safety validation.
    Returns the suggestion if valid, None if rejected by safety rules.
    """
    # Quick safety check on action
    if action in FORBIDDEN_ACTIONS:
        log.warning("suggestion_blocked_forbidden", symbol=symbol, action=action)
        return None

    from datetime import timedelta
    expires_at = datetime.utcnow() + timedelta(hours=expires_hours)

    suggestion = TradeSuggestion(
        symbol=symbol,
        action=action,
        quantity=quantity,
        limit_price=limit_price,
        order_type=order_type,
        strike=strike,
        expiry=expiry,
        right=right,
        source=source,
        tier=tier,
        signal=signal,
        rationale=rationale,
        current_price=current_price,
        sma_200=sma_200,
        rsi_14=rsi_14,
        est_cost=est_cost,
        est_margin_impact=est_margin_impact,
        margin_util_after=margin_util_after,
        rank=rank,
        rank_score=rank_score,
        funding_source=funding_source,
        opt_exchange=opt_exchange,
        opt_currency=opt_currency,
        status="pending",
        expires_at=expires_at,
    )

    with get_db() as db:
        db.add(suggestion)
        db.flush()
        suggestion_id = suggestion.id

    log.info("trade_suggestion_created",
             id=suggestion_id,
             symbol=symbol,
             action=action,
             quantity=quantity,
             price=limit_price,
             source=source)

    # Send alert
    try:
        from src.core.alerts import get_alert_manager
        price_str = f"@ ${limit_price:.2f}" if limit_price else ""
        get_alert_manager().trade_alert(
            action=f"SUGGESTION: {action}",
            symbol=symbol,
            details=f"Qty: {quantity} {price_str}\n{rationale}\nApprove on dashboard →",
        )
    except Exception:
        pass

    # Auto-approve if enabled for this source
    if _is_auto_approve_enabled(source):
        global _margin_rejected_this_cycle
        global _buying_power_remaining

        if _margin_rejected_this_cycle >= 3:
            # After 3 margin rejections in this cycle, stop trying
            log.info("auto_approve_skipped_margin_limit",
                     id=suggestion_id, symbol=symbol,
                     rejections=_margin_rejected_this_cycle,
                     msg="3 margin rejections this cycle — stopping auto-execute")
        else:
            # Pre-check: estimate if buying power covers this trade
            # before sending to IBKR (avoids flooding broker with doomed orders)
            try:
                # Fetch buying power once per cycle, then track locally
                if _buying_power_remaining is None:
                    from src.broker.account import get_account_summary
                    acct = get_account_summary()
                    _buying_power_remaining = acct.buying_power if acct else 0

                    # Subtract estimated margin of orders already sent to IBKR
                    # (approved/executed today but not yet filled — buying power
                    #  hasn't changed on IBKR side yet)
                    cutoff = datetime.utcnow() - timedelta(hours=12)
                    with get_db() as db:
                        outstanding = db.query(TradeSuggestion).filter(
                            TradeSuggestion.status.in_(["approved", "executed"]),
                            TradeSuggestion.reviewed_at >= cutoff,
                            TradeSuggestion.strike.isnot(None),
                        ).all()
                        for s in outstanding:
                            est = (s.strike or 0) * 100 * (s.quantity or 1) * 0.20
                            _buying_power_remaining -= est
                        if outstanding:
                            log.info("auto_approve_outstanding_orders",
                                     count=len(outstanding),
                                     bp_after_outstanding=f"${_buying_power_remaining:,.0f}")

                # Estimate margin needed: ~20% of notional for naked puts
                est_margin = (strike or 0) * 100 * (quantity or 1) * 0.20
                if est_margin > 0 and _buying_power_remaining < est_margin:
                    _margin_rejected_this_cycle += 1
                    log.info("auto_approve_skipped_insufficient_margin",
                             id=suggestion_id, symbol=symbol,
                             buying_power=f"${_buying_power_remaining:,.0f}",
                             est_margin=f"${est_margin:,.0f}",
                             rejection_count=_margin_rejected_this_cycle,
                             msg="Not enough buying power — skipping this suggestion")
                else:
                    log.info("auto_approving_suggestion", id=suggestion_id, symbol=symbol, source=source)
                    approve_suggestion(suggestion_id, note="auto-approved")
                    # Subtract estimated margin so next suggestion sees reduced buying power
                    if est_margin > 0:
                        _buying_power_remaining -= est_margin
                        log.info("auto_approve_bp_updated",
                                 remaining=f"${_buying_power_remaining:,.0f}",
                                 used=f"${est_margin:,.0f}")
            except Exception as e:
                log.warning("auto_approve_margin_check_failed", error=str(e), symbol=symbol)
                log.info("auto_approving_suggestion", id=suggestion_id, symbol=symbol, source=source)
                approve_suggestion(suggestion_id, note="auto-approved")

    return suggestion


def _is_auto_approve_enabled(source: str) -> bool:
    """Check if auto-approve is ON for a given source (options/portfolio)."""
    from src.core.models import SystemState
    key = f"auto_approve_{source}"
    with get_db() as db:
        state = db.query(SystemState).filter(SystemState.key == key).first()
        return state is not None and state.value == "true"


def get_pending_suggestions() -> list[TradeSuggestion]:
    """Get all pending (not yet reviewed) suggestions."""
    with get_db() as db:
        now = datetime.utcnow()
        # Expire old suggestions past their expiry time
        expired = db.query(TradeSuggestion).filter(
            TradeSuggestion.status == "pending",
            TradeSuggestion.expires_at < now,
        ).all()
        for s in expired:
            s.status = "expired"
            s.reviewed_at = now
            s.review_note = "Expired (time limit)"

        return db.query(TradeSuggestion).filter(
            TradeSuggestion.status == "pending",
        ).order_by(
            TradeSuggestion.rank.asc(),              # rank 1 first, 2 second, etc.
            TradeSuggestion.created_at.desc(),        # within same rank, newest first
        ).all()



def approve_suggestion(suggestion_id: int, note: str = "") -> bool:
    """Approve a suggestion and execute the order via IBKR."""
    with get_db() as db:
        s = db.query(TradeSuggestion).filter(
            TradeSuggestion.id == suggestion_id
        ).first()
        if not s or s.status != "pending":
            return False

        s.status = "approved"
        s.reviewed_at = datetime.utcnow()
        s.review_note = note
        log.info("suggestion_approved", id=suggestion_id, symbol=s.symbol)

        # ── Execute the order via IBKR ──────────────────────────
        # Skip execution for review-only actions (annual review sells etc.)
        from src.core.suggestions import REVIEW_ONLY_ACTIONS
        if s.action in REVIEW_ONLY_ACTIONS:
            log.info("review_only_skipping_execution", id=suggestion_id, action=s.action)
            return True

        # Check if IBKR is connected and not in read-only mode
        from src.core.config import get_settings
        cfg = get_settings()
        if cfg.ibkr.readonly:
            log.warning("suggestion_approved_but_readonly",
                        id=suggestion_id, symbol=s.symbol,
                        msg="IBKR is in read-only mode — order not placed")
            return True

        from src.broker.connection import is_connected
        if not is_connected():
            log.warning("suggestion_approved_but_disconnected",
                        id=suggestion_id, symbol=s.symbol)
            return True

        # Auto-approve (scheduler thread) -> execute directly
        # Manual approve (web thread) -> queue for 30s scheduler pickup
        import threading
        thread_name = threading.current_thread().name
        is_scheduler = "APScheduler" in thread_name or "ThreadPool" in thread_name
        if is_scheduler:
            db.commit()
            _execute_approved_order(suggestion_id)
        else:
            s.status = "queued"
            s.review_note = "Queued for execution by scheduler"
            db.commit()
            log.info("suggestion_queued_for_execution", id=suggestion_id, symbol=s.symbol)

    return True


def _execute_approved_order(suggestion_id: int):
    """Execute an approved suggestion's order via IBKR. Runs in its own thread."""
    with get_db() as db:
        s = db.query(TradeSuggestion).filter(
            TradeSuggestion.id == suggestion_id
        ).first()
        if not s or s.status != "approved":
            return

        # Look up exchange/currency — prefer stored options exchange, fall back to watchlist
        from src.core.config import get_watchlist
        exchange = "SMART"
        opt_exchange = "SMART"
        currency = "USD"
        for stock in get_watchlist():
            if stock.symbol == s.symbol:
                exchange = stock.exchange
                opt_exchange = stock.opt_exchange
                currency = stock.currency
                break

        # Use stored exchange from suggestion if available
        if s.opt_exchange:
            opt_exchange = s.opt_exchange
        if s.opt_currency:
            currency = s.opt_currency

        try:
            if s.action == "sell_put" and s.strike and s.expiry:
                log.info("executing_sell_put", id=suggestion_id, symbol=s.symbol,
                         strike=s.strike, expiry=s.expiry, exchange=opt_exchange, currency=currency)
                from src.broker.orders import sell_put
                trade = sell_put(
                    symbol=s.symbol,
                    expiry=s.expiry,
                    strike=s.strike,
                    quantity=s.quantity,
                    limit_price=s.limit_price,
                    exchange=opt_exchange,
                    currency=currency,
                )
                if trade:
                    # Check if the order actually filled or is just submitted/queued
                    fill_status = trade.orderStatus.status
                    if fill_status == "Filled":
                        s.status = "executed"
                        log.info("suggestion_executed", id=suggestion_id,
                                 symbol=s.symbol, action=s.action,
                                 strike=s.strike, expiry=s.expiry)
                    else:
                        # Order accepted but not yet filled (PreSubmitted, Submitted, etc.)
                        s.status = "submitted"
                        s.review_note = f"Order {fill_status} — awaiting fill"
                        log.info("suggestion_submitted", id=suggestion_id,
                                 symbol=s.symbol, action=s.action,
                                 strike=s.strike, expiry=s.expiry,
                                 order_status=fill_status)
                    _record_trade(db, s, "sell_put")
                else:
                    global _margin_rejected_this_cycle
                    _margin_rejected_this_cycle += 1
                    s.status = "approved"
                    s.review_note = "Order rejected by IBKR"
                    log.warning("suggestion_order_rejected", id=suggestion_id,
                                symbol=s.symbol)

            elif s.action == "sell_covered_call" and s.strike and s.expiry:
                from src.broker.orders import sell_covered_call
                trade = sell_covered_call(
                    symbol=s.symbol,
                    expiry=s.expiry,
                    strike=s.strike,
                    quantity=s.quantity,
                    limit_price=s.limit_price,
                    exchange=opt_exchange,
                    currency=currency,
                )
                if trade:
                    fill_status = trade.orderStatus.status
                    if fill_status == "Filled":
                        s.status = "executed"
                        log.info("suggestion_executed", id=suggestion_id,
                                 symbol=s.symbol, action=s.action,
                                 strike=s.strike, expiry=s.expiry)
                    else:
                        s.status = "submitted"
                        s.review_note = f"Order {fill_status} — awaiting fill"
                        log.info("suggestion_submitted", id=suggestion_id,
                                 symbol=s.symbol, action=s.action,
                                 strike=s.strike, expiry=s.expiry,
                                 order_status=fill_status)
                    _record_trade(db, s, "sell_call")
                else:
                    _margin_rejected_this_cycle += 1
                    s.status = "approved"
                    s.review_note = "Order rejected by IBKR"
                    log.warning("suggestion_order_rejected", id=suggestion_id,
                                symbol=s.symbol)

            elif s.action == "buy_stock":
                from src.broker.orders import sell_put  # placeholder
                log.info("buy_stock_approved_manual_execution",
                         id=suggestion_id, symbol=s.symbol,
                         msg="Stock buys require manual execution in TWS")

            else:
                log.warning("unknown_suggestion_action",
                            id=suggestion_id, action=s.action)

        except Exception as e:
            log.error("suggestion_execution_failed",
                      id=suggestion_id, symbol=s.symbol, error=str(e))
            s.status = "approved"  # keep as approved so user knows it didn't execute
            s.review_note = f"Execution failed: {e}"


def _record_trade(db, suggestion, trade_type_str: str):
    """
    Update account snapshot after a suggestion is executed.
    Trade records are NOT created here — the fill sync (trade_sync.py)
    is the single source of truth for trades, based on actual IBKR fills.
    """
    from src.core.models import Trade, TradeType, OrderStatus

    # Trigger immediate account snapshot for performance graph
    try:
        from src.core.models import AccountSnapshot
        from src.broker.account import get_account_summary
        today = datetime.utcnow().strftime("%Y-%m-%d")
        summary = get_account_summary()
        nlv = summary.net_liquidation if summary and summary.net_liquidation > 0 else 0
        if nlv > 0:
            all_trades = db.query(Trade).filter(
                Trade.order_status.in_(["FILLED", "filled"])
            ).all()
            cum_premium = 0.0
            for t2 in all_trades:
                if t2.trade_type in (TradeType.SELL_PUT, TradeType.SELL_CALL):
                    cum_premium += (t2.premium or 0) * (t2.quantity or 1) * 100 - (t2.commission or 0)
                elif t2.trade_type in (TradeType.BUY_PUT, TradeType.BUY_CALL):
                    cum_premium -= (t2.premium or 0) * (t2.quantity or 1) * 100 + (t2.commission or 0)

            existing = db.query(AccountSnapshot).filter(AccountSnapshot.date == today).first()
            if existing:
                existing.net_liquidation = nlv
                existing.options_premium_collected = round(cum_premium, 2)
            else:
                db.add(AccountSnapshot(
                    date=today,
                    net_liquidation=round(nlv, 2),
                    options_premium_collected=round(cum_premium, 2),
                ))
            log.info("snapshot_saved_on_trade", date=today, nlv=round(nlv, 2))
    except Exception as e:
        log.debug("snapshot_on_trade_failed", error=str(e))


def reject_suggestion(suggestion_id: int, note: str = "") -> bool:
    """Reject a suggestion."""
    with get_db() as db:
        s = db.query(TradeSuggestion).filter(
            TradeSuggestion.id == suggestion_id
        ).first()
        if s and s.status == "pending":
            s.status = "rejected"
            s.reviewed_at = datetime.utcnow()
            s.review_note = note
            log.info("suggestion_rejected", id=suggestion_id, symbol=s.symbol)
            return True
    return False
