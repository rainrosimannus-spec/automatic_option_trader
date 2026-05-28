"""Cash-and-carry orchestrator.

When the high-vol-grind detector (RiskManager.evaluate_grind_detector) flips
ON, idle cash is rotated into a Treasury / money-market ETF (default SGOV).
When the detector flips OFF, the ETF is sold back to cash and the wheel
resumes put writing.

This module is reactive, not transition-tracking: each scan compares the
current detector state to the current SGOV position. If detector ON and
no SGOV position → buy. If detector OFF and SGOV held → sell. Idempotent
on repeated invocation within the same state.

Existing short-put / assigned-stock positions are NOT touched — they settle
naturally over their 1-3 DTE cycles. The wheel's put-selling pass is what's
halted (via risk.check_cash_carry_gate); CC writing on assigned positions
continues so the wheel's existing book unwinds normally.

The `position_type='cash_carry_stock'` tag on the SGOV Position keeps it
distinct from wheel-assigned stock so the CC writer doesn't try to write
calls against it (filter in wheel.write_covered_calls).

Pure orchestration — actual order placement is in src.broker.orders
(buy_treasury_etf / sell_treasury_etf). Account/cash queries are in
src.broker.account.
"""
from __future__ import annotations

from datetime import datetime

from src.core.config import get_settings
from src.core.database import get_db
from src.core.logger import get_logger
from src.core.models import Position, PositionStatus

log = get_logger("strategy.cash_carry")


def _existing_carry_position() -> Position | None:
    """Return the open cash_carry_stock position, if any."""
    with get_db() as db:
        return (
            db.query(Position)
            .filter(
                Position.position_type == "cash_carry_stock",
                Position.status == PositionStatus.OPEN,
            )
            .first()
        )


def _get_last_price(symbol: str) -> float | None:
    """Best-effort last price for an ETF. Uses a 2-day daily-bar fetch via
    the existing market_data plumbing. Returns None on failure."""
    try:
        from ib_insync import Stock
        from src.broker.connection import get_ib, get_ib_lock
        from src.broker.market_data import _ensure_market_data_type
        with get_ib_lock():
            _ensure_market_data_type()
            ib = get_ib()
            contract = Stock(symbol, "SMART", "USD")
            ib.qualifyContracts(contract)
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="2 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                timeout=10,
            )
            if bars and bars[-1].close > 0:
                return float(bars[-1].close)
    except Exception as e:
        log.warning("cash_carry_price_fetch_failed", symbol=symbol, error=str(e))
    return None


def maybe_rotate(detector_active: bool) -> None:
    """Reactive orchestrator. Call once per scan after the detector evaluates.

    detector_active: current grind-detector state (True = high-vol grind ON).

    Acts only when the wanted state (have SGOV or not) differs from current.
    No-op if cash_carry is disabled.
    """
    cfg = get_settings().risk
    if not cfg.cash_carry_enabled:
        return

    existing = _existing_carry_position()
    ticker = cfg.cash_carry_ticker

    if detector_active and existing is None:
        # Need to BUY into the cash-carry ticker.
        _rotate_into(ticker, cfg)
    elif not detector_active and existing is not None:
        # Need to SELL out of the cash-carry ticker.
        _rotate_out(existing, ticker)
    # else: state already matches — no-op.


def _rotate_into(ticker: str, cfg) -> None:
    """Buy `ticker` with all idle cash above the configured buffer."""
    from src.broker.account import get_account_summary
    from src.broker.orders import buy_treasury_etf

    summary = get_account_summary()
    deployable = summary.cash_balance - cfg.cash_carry_min_cash_buffer
    if deployable <= 0:
        log.info("cash_carry_skip_no_cash",
                 cash=summary.cash_balance, buffer=cfg.cash_carry_min_cash_buffer)
        return

    price = _get_last_price(ticker)
    if not price or price <= 0:
        log.warning("cash_carry_skip_no_price", ticker=ticker)
        return

    shares = int(deployable / price)
    if shares < 1:
        log.info("cash_carry_skip_too_small",
                 deployable=deployable, price=price)
        return

    log.info("cash_carry_rotating_in",
             ticker=ticker, shares=shares, price=price,
             deployable=round(deployable, 2))
    trade = buy_treasury_etf(ticker, shares)
    if trade is None:
        log.warning("cash_carry_buy_failed", ticker=ticker, shares=shares)
        return

    # Record the position. Cost basis is the limit price (or expected fill);
    # the actual fill price will update via trade_sync downstream.
    with get_db() as db:
        pos = Position(
            symbol=ticker,
            status=PositionStatus.OPEN,
            position_type="cash_carry_stock",
            quantity=shares,
            cost_basis=price,
            opened_at=datetime.utcnow(),
            is_wheel=False,
        )
        db.add(pos)
        db.commit()


def _rotate_out(position: Position, ticker: str) -> None:
    """Sell the existing cash_carry_stock position back to cash."""
    from src.broker.orders import sell_treasury_etf

    shares = int(position.quantity or 0)
    if shares < 1:
        log.warning("cash_carry_rotate_out_no_shares", pos_id=position.id)
        return

    log.info("cash_carry_rotating_out",
             ticker=ticker, shares=shares, pos_id=position.id)
    trade = sell_treasury_etf(ticker, shares)
    if trade is None:
        log.warning("cash_carry_sell_failed", ticker=ticker, shares=shares)
        return

    # Close the position record. trade_sync will update final cost basis / pnl.
    with get_db() as db:
        pos = db.query(Position).filter(Position.id == position.id).first()
        if pos:
            pos.status = PositionStatus.CLOSED
            pos.closed_at = datetime.utcnow()
            db.commit()


def close_naked_calls_before_assignment() -> None:
    """Daily ITM-avoidance check for naked short calls (strangle leg).

    For each open `short_call_naked` position:
      - if DTE > strangle_itm_close_dte → leave alone (let theta work)
      - if DTE <= threshold AND spot > strike (ITM) → buy-to-close now to
        avoid IBKR auto-assigning us into a short stock position
      - if DTE <= threshold AND spot <= strike (OTM) → leave alone (will expire
        worthless overnight; broker auto-closes)

    Called by the scheduler each market day (suggest 30 min before close).
    Idempotent — won't re-close already-closed positions.
    """
    from datetime import date, datetime as _dt
    from src.broker.orders import buy_to_close_call_naked
    from src.broker.market_data import get_stock_price

    cfg = get_settings().risk
    if not cfg.strangle_when_grind:
        # If strangle mode is disabled, no naked calls should exist. Skip.
        return

    today = date.today()
    with get_db() as db:
        positions = (
            db.query(Position)
            .filter(
                Position.position_type == "short_call_naked",
                Position.status == PositionStatus.OPEN,
            )
            .all()
        )

    if not positions:
        return

    closed = 0
    for pos in positions:
        try:
            # Parse expiry (YYYYMMDD) and compute DTE
            exp_date = _dt.strptime(pos.expiry, "%Y%m%d").date()
            dte = (exp_date - today).days
            if dte > cfg.strangle_itm_close_dte:
                continue  # Plenty of time; let theta decay
            spot = get_stock_price(pos.symbol)
            if not spot or spot <= 0:
                log.warning("strangle_itm_check_no_price", symbol=pos.symbol)
                continue
            if spot <= pos.strike:
                # OTM — will expire worthless overnight. Leave it.
                continue
            # ITM AND about to expire → buy-to-close NOW.
            log.warning(
                "strangle_call_itm_closing",
                symbol=pos.symbol, strike=pos.strike, expiry=pos.expiry,
                spot=spot, dte=dte,
            )
            trade = buy_to_close_call_naked(
                symbol=pos.symbol,
                expiry=pos.expiry,
                strike=pos.strike,
                quantity=pos.quantity or 1,
            )
            if trade is not None:
                with get_db() as db:
                    p = db.query(Position).filter(Position.id == pos.id).first()
                    if p:
                        p.status = PositionStatus.CLOSED
                        p.closed_at = _dt.utcnow()
                        db.commit()
                closed += 1
        except Exception as e:
            log.error("strangle_itm_check_error",
                      symbol=pos.symbol, error=str(e))

    log.info("strangle_itm_check_done",
             scanned=len(positions), closed=closed)
