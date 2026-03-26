"""
IBKR Trade Sync — imports real executions from IBKR into the Trade table.

Runs periodically, pulls recent fills via ib.executions(), maps them to
Trade records, and skips duplicates using IBKR's execution ID.

Covers: options (puts, calls), stock buys/sells, assignments.
Both manual trades and system-placed trades appear in one unified history.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from src.broker.connection import get_ib, is_connected
from src.core.database import get_db
from src.core.models import Trade, TradeType, OrderStatus, Position, PositionStatus
from src.core.logger import get_logger

log = get_logger(__name__)


def _classify_trade(fill) -> TradeType | None:
    """
    Classify an IBKR fill into a TradeType.

    fill.contract: Stock, Option, etc.
    fill.execution: side (BOT/SLD), shares, price, etc.
    """
    contract = fill.contract
    execution = fill.execution
    sec_type = contract.secType  # "STK", "OPT", "FOP"
    side = execution.side        # "BOT" or "SLD"

    if sec_type == "STK":
        return TradeType.BUY_STOCK if side == "BOT" else TradeType.SELL_STOCK

    if sec_type in ("OPT", "FOP"):
        right = contract.right  # "P" or "C"
        if right == "P":
            # Selling a put = sell_put, buying a put = buy_put (closing)
            return TradeType.SELL_PUT if side == "SLD" else TradeType.BUY_PUT
        elif right == "C":
            return TradeType.SELL_CALL if side == "SLD" else TradeType.BUY_CALL

    return None


def sync_ibkr_trades() -> int:
    """
    Pull recent executions from IBKR and insert missing ones into Trade table.
    Returns number of new trades imported.
    """
    # Use the main connection — creating a separate connection corrupts the event loop
    if not is_connected():
        log.warning("trade_sync_not_connected")
        return 0

    ib = get_ib()

    # Get account ID for filtering
    account_id = ""
    try:
        from src.core.config import get_settings
        cfg = get_settings()
        if cfg.ibkr.account:
            account_id = cfg.ibkr.account
    except Exception:
        pass

    try:
        # First try fills() which returns cached fills from this session
        fills = ib.fills()

        # If empty, request executions explicitly
        if not fills:
            from ib_insync import ExecutionFilter
            exec_filter = ExecutionFilter()
            if account_id:
                exec_filter.acctCode = account_id
            ib.reqExecutions(exec_filter)
            ib.sleep(3)
            fills = ib.fills()

        # Filter to our account only (TWS may return fills from other accounts)
        if fills and account_id:
            filtered = [f for f in fills
                        if getattr(f.execution, 'acctNumber', '') == account_id
                        or not getattr(f.execution, 'acctNumber', '')]
            log.info("trade_sync_account_filter", total=len(fills),
                     after_filter=len(filtered), account=account_id)
            fills = filtered

        log.info("trade_sync_query_done", fills_count=len(fills) if fills else 0)
    except Exception as e:
        log.error("trade_sync_fetch_error", error=str(e) or repr(e), type=type(e).__name__)
        return 0

    if not fills:
        log.info("trade_sync_no_fills")
        return 0

    log.info("trade_sync_fills_found", count=len(fills))

    imported = 0

    with get_db() as db:
        # Get existing IBKR exec IDs to skip duplicates
        existing_ids = set()
        existing = db.query(Trade.ibkr_exec_id).filter(
            Trade.ibkr_exec_id.isnot(None)
        ).all()
        existing_ids = {row[0] for row in existing}

        for fill in fills:
            exec_id = fill.execution.execId
            if not exec_id or exec_id in existing_ids:
                continue

            trade_type = _classify_trade(fill)
            if trade_type is None:
                log.debug("trade_sync_unknown_type",
                          sec_type=fill.contract.secType,
                          symbol=fill.contract.symbol)
                continue

            contract = fill.contract
            execution = fill.execution

            # Extract option fields
            strike = getattr(contract, 'strike', 0.0) or 0.0
            expiry = getattr(contract, 'lastTradeDateOrContractMonth', '') or ''
            right = getattr(contract, 'right', '')

            # Premium: for options it's the fill price, for stocks it's 0
            is_option = contract.secType in ("OPT", "FOP")
            premium = execution.price if is_option else 0.0
            fill_price = execution.price

            # Commission from commissionReport if available
            commission = 0.0
            if fill.commissionReport:
                commission = fill.commissionReport.commission or 0.0

            # Parse execution time
            try:
                exec_time = datetime.strptime(
                    execution.time, "%Y%m%d %H:%M:%S"
                ) if isinstance(execution.time, str) else execution.time
            except Exception:
                exec_time = datetime.utcnow()

            # Build description
            if is_option:
                notes = (
                    f"IBKR sync: {execution.side} {abs(execution.shares)} "
                    f"{contract.symbol} {expiry} ${strike}{right} @ ${fill_price:.2f}"
                )
            else:
                notes = (
                    f"IBKR sync: {execution.side} {abs(execution.shares)} "
                    f"{contract.symbol} @ ${fill_price:.2f}"
                )

            trade = Trade(
                symbol=contract.symbol,
                trade_type=trade_type,
                strike=strike,
                expiry=expiry,
                premium=premium,
                quantity=abs(int(execution.shares)),
                fill_price=fill_price,
                commission=commission,
                order_id=execution.orderId,
                order_status=OrderStatus.FILLED,
                notes=notes,
                source="ibkr_sync",
                ibkr_exec_id=exec_id,
                created_at=exec_time,
            )
            db.add(trade)
            existing_ids.add(exec_id)
            imported += 1

            log.info("trade_synced",
                     symbol=contract.symbol,
                     type=trade_type.value,
                     price=fill_price,
                     qty=abs(int(execution.shares)),
                     exec_id=exec_id)

    if imported:
        log.info("trade_sync_complete", imported=imported, total_fills=len(fills))
        _mark_submitted_suggestions_executed(fills)
    else:
        log.debug("trade_sync_no_new", total_fills=len(fills))

    # Also sync stock trades to PortfolioTransaction table
    stock_imported = _sync_stock_to_portfolio(fills)
    if stock_imported:
        log.info("portfolio_trade_sync", imported=stock_imported)

    return imported


def _mark_submitted_suggestions_executed(fills):
    """When fills arrive, update matching 'submitted' suggestions to 'executed'."""
    try:
        from src.core.suggestions import TradeSuggestion
        from datetime import datetime

        # Map trade types to suggestion actions
        type_to_action = {
            "sell_put": "sell_put",
            "sell_call": "sell_covered_call",
            "buy_put": "buy_put",
            "buy_call": "buy_call",
        }

        with get_db() as db:
            submitted = db.query(TradeSuggestion).filter(
                TradeSuggestion.status == "submitted"
            ).all()
            if not submitted:
                return

            updated = 0
            for s in submitted:
                for fill in fills:
                    contract = fill.contract
                    execution = fill.execution
                    is_option = contract.secType in ("OPT", "FOP")
                    if not is_option:
                        continue

                    strike = getattr(contract, 'strike', 0.0) or 0.0
                    expiry = getattr(contract, 'lastTradeDateOrContractMonth', '') or ''

                    # Match by symbol, strike, expiry
                    if (contract.symbol == s.symbol
                            and abs((strike or 0) - (s.strike or 0)) < 0.01
                            and expiry == (s.expiry or '')):
                        s.status = "executed"
                        s.reviewed_at = datetime.utcnow()
                        s.review_note = (s.review_note or "") + " | Filled via trade sync"
                        updated += 1
                        log.info("suggestion_marked_executed",
                                 id=s.id, symbol=s.symbol,
                                 strike=s.strike, expiry=s.expiry)
                        break

            if updated:
                db.commit()
                log.info("suggestions_updated_from_fills", count=updated)
    except Exception as e:
        log.warning("suggestion_fill_match_error", error=str(e))


def _sync_stock_to_portfolio(fills) -> int:
    """Sync stock buy/sell fills to the PortfolioTransaction table."""
    from src.portfolio.models import PortfolioTransaction, PortfolioHolding

    imported = 0

    with get_db() as db:
        existing_ids = set()
        existing = db.query(PortfolioTransaction.ibkr_exec_id).filter(
            PortfolioTransaction.ibkr_exec_id.isnot(None)
        ).all()
        existing_ids = {row[0] for row in existing}

        for fill in fills:
            contract = fill.contract
            execution = fill.execution
            exec_id = execution.execId

            # Only stock trades from the portfolio account
            if contract.secType != "STK" or not exec_id:
                continue
            if exec_id in existing_ids:
                continue
            # Skip fills from options account — only portfolio account stocks here
            from src.core.config import get_settings as _gs
            try:
                _pa = _gs().portfolio.ibkr_account
            except Exception:
                _pa = ""
            if _pa and hasattr(execution, "acctNumber") and execution.acctNumber != _pa:
                continue

            side = execution.side  # "BOT" or "SLD"
            action = "buy" if side == "BOT" else "sell"
            shares = abs(int(execution.shares))
            price = execution.price
            amount = shares * price

            commission = 0.0
            if fill.commissionReport:
                commission = fill.commissionReport.commission or 0.0

            try:
                exec_time = datetime.strptime(
                    execution.time, "%Y%m%d %H:%M:%S"
                ) if isinstance(execution.time, str) else execution.time
            except Exception:
                exec_time = datetime.utcnow()

            # Try to find tier from holdings or watchlist
            tier = "growth"
            holding = db.query(PortfolioHolding).filter(
                PortfolioHolding.symbol == contract.symbol
            ).first()
            if holding:
                tier = holding.tier or "growth"

            txn = PortfolioTransaction(
                symbol=contract.symbol,
                action=action,
                shares=shares,
                price=price,
                amount=amount,
                commission=commission,
                currency=contract.currency or "USD",
                tier=tier,
                notes=f"IBKR sync: {side} {shares} {contract.symbol} @ ${price:.2f}",
                source="ibkr_sync",
                ibkr_exec_id=exec_id,
                created_at=exec_time,
            )
            db.add(txn)
            existing_ids.add(exec_id)
            imported += 1

    return imported


def sync_ibkr_trades_extended() -> int:
    """
    Extended sync — also pulls from ib.trades() for orders placed during
    this session that may not yet appear in fills.
    """
    count = sync_ibkr_trades()

    if not is_connected():
        return count

    ib = get_ib()

    try:
        # ib.trades() returns Trade objects for orders placed in this session
        open_trades = ib.trades()
        with get_db() as db:
            existing_ids = {
                row[0] for row in
                db.query(Trade.ibkr_exec_id).filter(
                    Trade.ibkr_exec_id.isnot(None)
                ).all()
            }

        for t in open_trades:
            if not t.fills:
                continue
            for fill in t.fills:
                exec_id = fill.execution.execId
                if exec_id in existing_ids:
                    continue
                # Will be picked up by next sync_ibkr_trades() call
    except Exception as e:
        log.debug("trade_sync_extended_error", error=str(e))

    return count


def sync_ibkr_positions() -> int:
    """
    Sync open positions from IBKR into the Position table.
    Creates new Position records for IBKR positions not already tracked.
    Closes Position records for positions no longer in IBKR.
    Returns number of changes made.
    """
    if not is_connected():
        log.warning("position_sync_not_connected")
        return 0

    ib = get_ib()
    cfg = None
    try:
        from src.core.config import get_settings
        cfg = get_settings()
    except Exception:
        pass

    account_id = cfg.ibkr.account if cfg else ""

    try:
        portfolio_items = ib.portfolio()
    except Exception as e:
        log.error("position_sync_fetch_error", error=str(e))
        return 0

    changes = 0

    # Collect IBKR option and stock positions
    ibkr_positions = {}
    ibkr_stock_positions = {}
    for item in portfolio_items:
        contract = item.contract
        if account_id and hasattr(item, 'account') and item.account != account_id:
            continue

        if contract.secType in ("OPT", "FOP"):
            symbol = contract.symbol
            strike = contract.strike
            expiry = contract.lastTradeDateOrContractMonth
            right = contract.right
            qty = abs(int(item.position))
            avg_cost = item.averageCost

            key = (symbol, strike, expiry, right)
            ibkr_positions[key] = {
                "symbol": symbol,
                "strike": strike,
                "expiry": expiry,
                "right": right,
                "quantity": qty,
                "avg_cost": avg_cost,
                "position_size": item.position,
                "market_value": item.marketValue,
            }
        elif contract.secType == "STK":
            symbol = contract.symbol
            qty = int(item.position)
            ibkr_stock_positions[symbol] = qty

    with get_db() as db:
        open_positions = db.query(Position).filter(
            Position.status == PositionStatus.OPEN,
            Position.position_type.in_(["short_put", "short_call", "covered_call"]),
        ).all()

        tracked_keys = set()
        for pos in open_positions:
            right = "P" if "put" in pos.position_type else "C"
            key = (pos.symbol, pos.strike, pos.expiry, right)
            tracked_keys.add(key)

            if key not in ibkr_positions:
                # Skip if there is a live open order at IBKR for this position
                from src.broker.orders import get_cached_open_orders
                open_orders = get_cached_open_orders()
                has_open_order = any(
                    o.get("symbol") == pos.symbol and
                    o.get("strike") == pos.strike and
                    o.get("expiry") == pos.expiry
                    for o in open_orders
                )
                if has_open_order:
                    log.info("position_sync_skipped_open_order",
                             symbol=pos.symbol, strike=pos.strike, expiry=pos.expiry)
                    tracked_keys.add(key)
                    continue
                # If stock appeared in account, this is an assignment — let wheel handle it
                # Do NOT mark expired here or the wheel will never see it
                if ibkr_stock_positions.get(pos.symbol, 0) >= 100 * pos.quantity:
                    log.info("position_sync_skipped_assignment",
                             symbol=pos.symbol, strike=pos.strike,
                             stock_qty=ibkr_stock_positions.get(pos.symbol, 0))
                    tracked_keys.add(key)
                    continue
                pos.status = PositionStatus.EXPIRED
                pos.closed_at = datetime.utcnow()

                # Calculate realized PnL from trades for this position
                pos_trades = db.query(Trade).filter(
                    Trade.symbol == pos.symbol,
                    Trade.strike == pos.strike,
                    Trade.expiry == pos.expiry,
                    Trade.order_status == OrderStatus.FILLED,
                ).all()
                realized = 0.0
                for t in pos_trades:
                    if t.trade_type in (TradeType.SELL_PUT, TradeType.SELL_CALL):
                        realized += (t.premium or 0) * (t.quantity or 1) * 100 - (t.commission or 0)
                    elif t.trade_type in (TradeType.BUY_PUT, TradeType.BUY_CALL):
                        realized -= (t.premium or 0) * (t.quantity or 1) * 100 + (t.commission or 0)
                pos.realized_pnl = round(realized, 2)

                changes += 1
                log.info("position_expired_by_sync",
                         symbol=pos.symbol, strike=pos.strike,
                         expiry=pos.expiry, realized_pnl=pos.realized_pnl)

        # Close stock positions no longer in IBKR
        open_stock_positions = db.query(Position).filter(
            Position.status == PositionStatus.OPEN,
            Position.position_type == "stock",
        ).all()
        for pos in open_stock_positions:
            if pos.symbol not in ibkr_stock_positions or ibkr_stock_positions[pos.symbol] == 0:
                pos.status = PositionStatus.CLOSED
                pos.closed_at = datetime.utcnow()
                # Calculate realized PnL from stock trades
                stock_trades = db.query(Trade).filter(
                    Trade.symbol == pos.symbol,
                    Trade.order_status == OrderStatus.FILLED,
                    Trade.trade_type.in_([
                        TradeType.BUY_STOCK, TradeType.SELL_STOCK,
                        TradeType.ASSIGNMENT, TradeType.CALLED_AWAY,
                    ]),
                ).all()
                realized = 0.0
                for t in stock_trades:
                    qty = t.quantity or 1
                    price = t.fill_price or t.premium or 0
                    commission = t.commission or 0
                    if t.trade_type in (TradeType.SELL_STOCK, TradeType.CALLED_AWAY):
                        realized += price * qty - commission
                    elif t.trade_type in (TradeType.BUY_STOCK, TradeType.ASSIGNMENT):
                        realized -= price * qty + commission
                pos.realized_pnl = round(realized, 2)
                changes += 1
                log.info("stock_position_closed_by_sync",
                         symbol=pos.symbol, realized_pnl=pos.realized_pnl)

        for key, data in ibkr_positions.items():
            if key in tracked_keys:
                continue
            if data["position_size"] >= 0:
                continue

            # Check for any existing position with same symbol+strike+expiry
            # (could be already created by suggestion execution or a previous sync)
            if data["right"] == "P":
                pos_type = "short_put"
            else:
                # Use covered_call if we have an open stock position for this symbol
                has_stock = db.query(Position).filter(
                    Position.symbol == data["symbol"],
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "stock",
                ).first()
                pos_type = "covered_call" if has_stock else "short_call"
            existing = db.query(Position).filter(
                Position.symbol == data["symbol"],
                Position.strike == data["strike"],
                Position.expiry == data["expiry"],
                Position.position_type == pos_type,
            ).first()
            if existing:
                # If it exists but was expired, reopen it
                if existing.status != PositionStatus.OPEN:
                    existing.status = PositionStatus.OPEN
                    existing.closed_at = None
                    changes += 1
                    log.info("position_reopened_by_sync",
                             symbol=data["symbol"], strike=data["strike"])
                # Even if position exists, ensure a trade record exists too
                trade_type = TradeType.SELL_PUT if data["right"] == "P" else TradeType.SELL_CALL
                existing_trade = db.query(Trade).filter(
                    Trade.symbol == data["symbol"],
                    Trade.strike == data["strike"],
                    Trade.expiry == data["expiry"],
                    Trade.trade_type == trade_type,
                ).first()
                if not existing_trade:
                    premium_ps = abs(data["avg_cost"]) / 100.0 if data["avg_cost"] else 0.0
                    db.add(Trade(
                        symbol=data["symbol"],
                        trade_type=trade_type,
                        strike=data["strike"],
                        expiry=data["expiry"],
                        premium=premium_ps,
                        quantity=data["quantity"],
                        fill_price=premium_ps,
                        commission=0,
                        order_status=OrderStatus.FILLED,
                        notes="Synced from IBKR position",
                        source="ibkr_sync",
                    ))
                    log.info("trade_created_for_existing_position",
                             symbol=data["symbol"], strike=data["strike"])
                continue
            premium_per_share = abs(data["avg_cost"]) / 100.0 if data["avg_cost"] else 0.0

            new_pos = Position(
                symbol=data["symbol"],
                status=PositionStatus.OPEN,
                position_type=pos_type,
                strike=data["strike"],
                expiry=data["expiry"],
                entry_premium=premium_per_share,
                quantity=data["quantity"],
                total_premium_collected=premium_per_share * data["quantity"] * 100,
                is_wheel=True,
                opened_at=datetime.utcnow(),
            )
            db.add(new_pos)

            # Also create a Trade record so it shows in trade history + graph
            trade_type = TradeType.SELL_PUT if data["right"] == "P" else TradeType.SELL_CALL
            existing_trade = db.query(Trade).filter(
                Trade.symbol == data["symbol"],
                Trade.strike == data["strike"],
                Trade.expiry == data["expiry"],
                Trade.trade_type == trade_type,
            ).first()
            if not existing_trade:
                db.add(Trade(
                    symbol=data["symbol"],
                    trade_type=trade_type,
                    strike=data["strike"],
                    expiry=data["expiry"],
                    premium=premium_per_share,
                    quantity=data["quantity"],
                    fill_price=premium_per_share,
                    commission=0,
                    order_status=OrderStatus.FILLED,
                    notes=f"Synced from IBKR position",
                    source="ibkr_sync",
                ))
                log.info("trade_created_from_position",
                         symbol=data["symbol"], strike=data["strike"],
                         premium=premium_per_share)

            # Update matching submitted suggestion to executed (covered call fill)
            if data["right"] == "C":
                try:
                    from src.core.suggestions import TradeSuggestion
                    pending_sugg = db.query(TradeSuggestion).filter(
                        TradeSuggestion.symbol == data["symbol"],
                        TradeSuggestion.strike == data["strike"],
                        TradeSuggestion.expiry == data["expiry"],
                        TradeSuggestion.action == "sell_covered_call",
                        TradeSuggestion.status == "submitted",
                    ).first()
                    if pending_sugg:
                        pending_sugg.status = "executed"
                        pending_sugg.reviewed_at = datetime.utcnow()
                        pending_sugg.review_note = "Filled — synced from IBKR"
                        log.info("covered_call_suggestion_marked_executed",
                                 symbol=data["symbol"], strike=data["strike"])
                    # Also update the submitted trade record to FILLED
                    submitted_trade = db.query(Trade).filter(
                        Trade.symbol == data["symbol"],
                        Trade.strike == data["strike"],
                        Trade.expiry == data["expiry"],
                        Trade.trade_type == TradeType.SELL_CALL,
                        Trade.order_status == OrderStatus.SUBMITTED,
                    ).first()
                    if submitted_trade:
                        submitted_trade.order_status = OrderStatus.FILLED
                        submitted_trade.fill_price = premium_per_share
                        log.info("covered_call_trade_marked_filled",
                                 symbol=data["symbol"], strike=data["strike"])
                except Exception as e:
                    log.warning("covered_call_suggestion_update_failed", error=str(e))

            changes += 1
            log.info("position_created_by_sync",
                     symbol=data["symbol"], type=pos_type,
                     strike=data["strike"], expiry=data["expiry"],
                     qty=data["quantity"])

    if changes:
        log.info("position_sync_complete", changes=changes)

    # Create/update account snapshot for performance graph
    try:
        from src.core.models import AccountSnapshot
        from src.broker.account import get_account_summary
        today = datetime.utcnow().strftime("%Y-%m-%d")
        summary = get_account_summary()
        nlv = summary.net_liquidation if summary and summary.net_liquidation > 0 else 0
        if nlv > 0:
            # Calculate cumulative premium from all trades
            with get_db() as db:
                all_trades = db.query(Trade).filter(
                    Trade.order_status == OrderStatus.FILLED
                ).all()
                cum_premium = 0.0
                for t in all_trades:
                    if t.trade_type in (TradeType.SELL_PUT, TradeType.SELL_CALL):
                        cum_premium += (t.premium or 0) * (t.quantity or 1) * 100 - (t.commission or 0)
                    elif t.trade_type in (TradeType.BUY_PUT, TradeType.BUY_CALL):
                        cum_premium -= (t.premium or 0) * (t.quantity or 1) * 100 + (t.commission or 0)

                existing = db.query(AccountSnapshot).filter(
                    AccountSnapshot.date == today
                ).first()
                if existing:
                    existing.net_liquidation = nlv
                    existing.options_premium_collected = round(cum_premium, 2)
                else:
                    db.add(AccountSnapshot(
                        date=today,
                        net_liquidation=round(nlv, 2),
                        options_premium_collected=round(cum_premium, 2),
                    ))
                log.info("snapshot_saved_on_sync", date=today, nlv=round(nlv, 2),
                         premium=round(cum_premium, 2))
    except Exception as e:
        log.debug("snapshot_on_sync_failed", error=str(e))

    return changes
