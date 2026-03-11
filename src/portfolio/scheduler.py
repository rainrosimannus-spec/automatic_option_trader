"""
Portfolio scheduler jobs — periodic buy scans, price updates, annual rescreen.

Uses a dedicated IBKR connection (clientId=99) to avoid conflicts
with the options trader's health checks and data requests.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from datetime import datetime
from typing import Optional

from ib_insync import IB

from src.core.logger import get_logger
from src.portfolio.config import PortfolioConfig
from src.portfolio.buyer import PortfolioBuyer

log = get_logger(__name__)

_portfolio_ib: Optional[IB] = None
_portfolio_lock = threading.Lock()


def _is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Quick TCP check — is TWS/Gateway listening on this port?"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (ConnectionRefusedError, TimeoutError, OSError):
        return False


def _ensure_event_loop():
    import threading
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed() or threading.current_thread() is not threading.main_thread():
            asyncio.set_event_loop(asyncio.new_event_loop())
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _get_portfolio_connection(cfg: PortfolioConfig) -> IB:
    """Get or create a dedicated portfolio IB connection.
    
    Does a quick TCP port check first — if Portfolio TWS isn't running,
    fails immediately instead of retrying for ~100 seconds.
    """
    global _portfolio_ib

    if _portfolio_ib is not None and _portfolio_ib.isConnected():
        return _portfolio_ib

    # Quick check: is the port even open?
    if not _is_port_open(cfg.ibkr_host, cfg.ibkr_port):
        raise ConnectionError(
            f"Portfolio TWS not reachable on {cfg.ibkr_host}:{cfg.ibkr_port}"
        )

    _portfolio_ib = IB()

    for attempt in range(1, 4):
        try:
            _portfolio_ib.connect(
                host=cfg.ibkr_host,
                port=cfg.ibkr_port,
                clientId=99,  # dedicated client ID for portfolio
                timeout=30,
                readonly=True,
            )
            _portfolio_ib.RequestTimeout = 15
            _portfolio_ib.reqMarketDataType(4)
            log.info("portfolio_connection_established", clientId=99)
            return _portfolio_ib
        except Exception as e:
            log.warning("portfolio_connect_retry", attempt=attempt, error=str(e))
            if attempt < 3:
                time.sleep(35)  # wait for TWS 30-second rule

    raise ConnectionError("Failed to connect portfolio scanner")


def job_portfolio_scan(cfg: PortfolioConfig):
    """Scan watchlist for buy opportunities."""
    _ensure_event_loop()

    if not cfg.enabled:
        return

    with _portfolio_lock:
        try:
            ib = _get_portfolio_connection(cfg)
            buyer = PortfolioBuyer(ib, cfg)
            bought = buyer.run_scan()
            log.info("portfolio_scan_job_done", bought=bought)
        except Exception as e:
            log.error("portfolio_scan_job_error", error=str(e))


def job_portfolio_update_prices(cfg: PortfolioConfig):
    """Update holdings with current market prices."""
    _ensure_event_loop()

    if not cfg.enabled:
        return

    with _portfolio_lock:
        try:
            ib = _get_portfolio_connection(cfg)
            buyer = PortfolioBuyer(ib, cfg)
            buyer.update_holdings_prices()
            log.info("portfolio_prices_updated")
            # Refresh cached account data for dashboard
            try:
                from src.portfolio.connection import refresh_portfolio_account_cache_from
                refresh_portfolio_account_cache_from(ib)
            except Exception:
                pass
        except Exception as e:
            log.error("portfolio_price_update_error", error=str(e))


def job_portfolio_update_metrics(cfg: PortfolioConfig):
    """Update watchlist metrics (SMA, RSI, discount) independently from buy scan."""
    _ensure_event_loop()

    if not cfg.enabled:
        return

    with _portfolio_lock:
        try:
            ib = _get_portfolio_connection(cfg)
            buyer = PortfolioBuyer(ib, cfg)
            # Always recalc scores from existing DB data first (instant, no IBKR)
            buyer.recalc_scores_from_db()
            # Then try to refresh metrics from IBKR (may timeout outside market hours)
            buyer.update_watchlist_metrics()
        except Exception as e:
            log.error("portfolio_metrics_update_error", error=str(e))


def job_portfolio_annual_rescreen(cfg: PortfolioConfig):
    """
    Annual rescreen — December 1st.

    Three phases:
      1. Screen universe ($1B+ market cap) → refresh watchlist
      2. Review existing holdings → suggest sell/reduce/sell-call for underperformers
      3. Send alert with summary + all pending suggestions needing approval

    CRITICAL: sell/reduce suggestions are NEVER auto-executed.
    They always require manual approval via dashboard.
    """
    _ensure_event_loop()

    if not cfg.enabled:
        return

    with _portfolio_lock:
        log.info("portfolio_annual_rescreen_started", date=f"Dec {cfg.rescreen_day}")

        try:
            ib = _get_portfolio_connection(cfg)

            # ══════════════════════════════════════════════════════
            # PHASE 1: Screen universe for new watchlist
            # ══════════════════════════════════════════════════════
            from tools.screen_universe import UniverseScreener

            regions = [r.strip() for r in cfg.rescreen_regions.split(",") if r.strip()] or None

            screener = UniverseScreener(ib)
            results = screener.screen_all(
                regions=regions,
                min_market_cap=cfg.rescreen_min_market_cap,
                top_n=cfg.rescreen_top_n,
                growth_count=40,
                dividend_count=10,
            )

            # Update portfolio watchlist in DB
            from src.core.database import get_db
            from src.portfolio.models import PortfolioWatchlist, PortfolioHolding

            new_symbols = set()
            with get_db() as db:
                for score in results:
                    new_symbols.add(score.symbol)
                    existing = db.query(PortfolioWatchlist).filter(
                        PortfolioWatchlist.symbol == score.symbol
                    ).first()

                    if existing:
                        existing.composite_score = score.composite_score
                        existing.growth_score = score.growth_score
                        existing.valuation_score = score.valuation_score
                        existing.quality_score = score.quality_score
                        existing.category = score.category
                        existing.screened_at = datetime.utcnow()
                    else:
                        db.add(PortfolioWatchlist(
                            symbol=score.symbol,
                            name=score.name,
                            exchange=score.exchange,
                            currency=score.currency,
                            sector=score.sector,
                            composite_score=score.composite_score,
                            growth_score=score.growth_score,
                            valuation_score=score.valuation_score,
                            quality_score=score.quality_score,
                            category=score.category,
                        ))

            log.info("portfolio_rescreen_phase1_done",
                     screened=len(results),
                     new_symbols=len(new_symbols))

            # ══════════════════════════════════════════════════════
            # PHASE 2: Review existing holdings
            # ══════════════════════════════════════════════════════
            review_suggestions = _review_existing_holdings(ib, cfg, new_symbols)

            log.info("portfolio_rescreen_phase2_done",
                     review_suggestions=len(review_suggestions))

            # ══════════════════════════════════════════════════════
            # PHASE 3: Alert with summary
            # ══════════════════════════════════════════════════════
            _send_rescreen_alert(len(results), review_suggestions)

            log.info("portfolio_annual_rescreen_done",
                     stocks_screened=len(results),
                     sell_suggestions=len(review_suggestions))

        except Exception as e:
            log.error("portfolio_rescreen_error", error=str(e))
            # Alert on failure too
            try:
                from src.core.alerts import get_alert_manager
                get_alert_manager().critical(
                    "Annual Rescreen FAILED",
                    f"Error: {str(e)}\nManual intervention required."
                )
            except Exception:
                pass


def _review_existing_holdings(
    ib: IB,
    cfg: PortfolioConfig,
    new_watchlist_symbols: set[str],
) -> list[dict]:
    """
    Review all existing portfolio holdings and create suggestions for:
      - SELL: stock dropped off new watchlist AND has poor fundamentals
      - REDUCE: overweight position (>12% of portfolio) or deteriorating trend
      - SELL COVERED CALL: stock above SMA, not growing, harvest premium

    CRITICAL: All suggestions are created as "pending" with source="rescreen".
    They are NEVER auto-executed. Manual approval required.

    Returns list of suggestion summaries for the alert.
    """
    from src.core.database import get_db
    from src.portfolio.models import PortfolioHolding
    from src.core.suggestions import create_suggestion
    from src.broker.market_data import get_stock_price

    suggestions = []

    with get_db() as db:
        holdings = db.query(PortfolioHolding).filter(
            PortfolioHolding.shares > 0
        ).all()

        if not holdings:
            return suggestions

        # Calculate portfolio totals
        total_value = sum(h.market_value or h.total_invested or 0 for h in holdings)
        if total_value <= 0:
            return suggestions

    # Analyze each holding
    for holding in holdings:
        symbol = holding.symbol
        shares = holding.shares
        avg_cost = holding.avg_cost
        market_value = holding.market_value or (shares * (holding.current_price or avg_cost))
        position_pct = market_value / total_value if total_value > 0 else 0
        pnl_pct = holding.unrealized_pnl_pct or 0
        tier = holding.tier

        # Get fresh price data
        try:
            current_price = get_stock_price(
                symbol,
                exchange=holding.exchange or "SMART",
                currency=holding.currency or "USD",
            )
        except Exception:
            current_price = holding.current_price

        if not current_price or current_price <= 0:
            continue

        # Get SMA for trend analysis
        sma_200 = None
        try:
            from src.portfolio.analyzer import PortfolioAnalyzer
            analyzer = PortfolioAnalyzer(ib)
            analysis = analyzer.analyze_stock(
                symbol,
                holding.exchange or "SMART",
                holding.currency or "USD",
                tier=tier,
            )
            if analysis:
                sma_200 = analysis.sma_200
        except Exception:
            pass

        pct_vs_sma = 0
        if sma_200 and sma_200 > 0:
            pct_vs_sma = ((current_price - sma_200) / sma_200) * 100

        # ── Decision logic ──

        # 1. SELL: Dropped off watchlist + losing money + below SMA
        dropped_off = symbol not in new_watchlist_symbols
        if dropped_off and pnl_pct < -10 and pct_vs_sma < -10:
            rationale = (
                f"ANNUAL REVIEW: {symbol} dropped off new watchlist. "
                f"P&L: {pnl_pct:+.1f}%, price {pct_vs_sma:+.1f}% vs SMA. "
                f"Position: {shares} shares @ ${avg_cost:.2f}, "
                f"now ${current_price:.2f}. "
                f"Consider selling — fundamentals no longer qualify."
            )
            create_suggestion(
                symbol=symbol,
                action="sell_stock_review",  # special action — requires manual execution
                quantity=shares,
                limit_price=round(current_price * 0.998, 2),
                source="rescreen",
                tier=tier,
                signal="annual_review_sell",
                rationale=rationale,
                current_price=current_price,
                sma_200=sma_200,
                rank=0,  # review suggestions don't have buy-ranks
                funding_source="n/a",
                expires_hours=720,  # 30 days to review
            )
            suggestions.append({
                "symbol": symbol, "action": "SELL",
                "reason": f"Dropped off watchlist, P&L {pnl_pct:+.1f}%",
            })
            continue

        # 2. REDUCE: Position >12% of portfolio (overconcentrated)
        if position_pct > 0.12:
            target_pct = 0.08  # reduce to 8%
            target_value = total_value * target_pct
            reduce_value = market_value - target_value
            reduce_shares = int(reduce_value / current_price)
            if reduce_shares > 0:
                rationale = (
                    f"ANNUAL REVIEW: {symbol} is {position_pct:.1%} of portfolio "
                    f"(above 12% concentration limit). "
                    f"Suggest reducing by {reduce_shares} shares to bring to ~8%. "
                    f"P&L: {pnl_pct:+.1f}%, current ${current_price:.2f}."
                )
                create_suggestion(
                    symbol=symbol,
                    action="reduce_position_review",
                    quantity=reduce_shares,
                    limit_price=round(current_price * 0.998, 2),
                    source="rescreen",
                    tier=tier,
                    signal="annual_review_reduce",
                    rationale=rationale,
                    current_price=current_price,
                    sma_200=sma_200,
                    rank=0,
                    funding_source="n/a",
                    expires_hours=720,
                )
                suggestions.append({
                    "symbol": symbol, "action": "REDUCE",
                    "reason": f"Position {position_pct:.0%} > 12% limit",
                })
                continue

        # 3. SELL COVERED CALL: Stock well above SMA + in profit + not breakthrough tier
        # (Don't cap upside on breakthrough stocks — they're the moonshots)
        if (pct_vs_sma > 15 and pnl_pct > 20 and tier != "breakthrough"
                and shares >= 100):
            rationale = (
                f"ANNUAL REVIEW: {symbol} is {pct_vs_sma:+.1f}% above 200d SMA "
                f"and up {pnl_pct:+.1f}%. Consider selling covered call to "
                f"harvest premium while holding. "
                f"Position: {shares} shares @ ${avg_cost:.2f}, "
                f"now ${current_price:.2f}."
            )
            create_suggestion(
                symbol=symbol,
                action="sell_covered_call_review",
                quantity=shares // 100,  # contracts = shares / 100
                source="rescreen",
                tier=tier,
                signal="annual_review_cc",
                rationale=rationale,
                current_price=current_price,
                sma_200=sma_200,
                rank=0,
                funding_source="n/a",
                expires_hours=720,
            )
            suggestions.append({
                "symbol": symbol, "action": "SELL CC",
                "reason": f"+{pct_vs_sma:.0f}% above SMA, P&L +{pnl_pct:.0f}%",
            })
            continue

        # 4. SELL: Dropped off watchlist but still in profit — gentle suggestion
        if dropped_off and pnl_pct > 5:
            rationale = (
                f"ANNUAL REVIEW: {symbol} no longer in screened watchlist "
                f"but still profitable ({pnl_pct:+.1f}%). "
                f"Consider trimming or selling while in profit. "
                f"Position: {shares} shares @ ${avg_cost:.2f}, "
                f"now ${current_price:.2f}."
            )
            create_suggestion(
                symbol=symbol,
                action="sell_stock_review",
                quantity=shares,
                limit_price=round(current_price * 0.998, 2),
                source="rescreen",
                tier=tier,
                signal="annual_review_trim_profit",
                rationale=rationale,
                current_price=current_price,
                sma_200=sma_200,
                rank=0,
                funding_source="n/a",
                expires_hours=720,
            )
            suggestions.append({
                "symbol": symbol, "action": "CONSIDER SELL",
                "reason": f"Off watchlist but profitable ({pnl_pct:+.1f}%)",
            })

    return suggestions


def _send_rescreen_alert(stocks_screened: int, review_suggestions: list[dict]):
    """Send alert summarizing rescreen results and pending suggestions."""
    from src.core.alerts import get_alert_manager
    from src.core.suggestions import get_pending_suggestions

    alert = get_alert_manager()

    # Count all pending suggestions (both buy and review)
    pending = get_pending_suggestions()
    pending_count = len(pending)

    lines = [
        f"📋 Annual Portfolio Rescreen Complete",
        f"",
        f"Screened: {stocks_screened} stocks ($1B+ market cap)",
        f"Date: {datetime.utcnow().strftime('%Y-%m-%d')}",
    ]

    if review_suggestions:
        lines.append(f"")
        lines.append(f"⚠️ {len(review_suggestions)} holding review suggestions:")
        for s in review_suggestions[:10]:  # max 10 in alert
            lines.append(f"  • {s['symbol']}: {s['action']} — {s['reason']}")
        if len(review_suggestions) > 10:
            lines.append(f"  ... and {len(review_suggestions) - 10} more")

    if pending_count > 0:
        lines.append(f"")
        lines.append(f"🔔 {pending_count} total suggestions awaiting approval")
        lines.append(f"Review on dashboard → Approve or Reject each one")
    else:
        lines.append(f"")
        lines.append(f"✅ No actions needed — portfolio looks healthy")

    alert._send("\n".join(lines), priority="high", tags="clipboard")


def job_portfolio_sync_trades(cfg: PortfolioConfig):
    """
    Sync IBKR executions into PortfolioTransaction for watchlist symbols.

    Imports put sells, put buys (close), stock buys, and stock sells
    that involve symbols on the portfolio watchlist.
    This ensures portfolio trade history reflects all activity on
    watchlist stocks regardless of whether option trader or portfolio
    manager initiated the trade.
    """
    _ensure_event_loop()

    if not cfg.enabled:
        return

    with _portfolio_lock:
        try:
            from src.core.database import get_db
            from src.portfolio.models import PortfolioTransaction, PortfolioWatchlist
            from datetime import datetime

            # Use portfolio connection (port 7496), NOT options connection
            ib = _get_portfolio_connection(cfg)
            if ib is None:
                log.debug("portfolio_trade_sync_not_connected")
                return

            # Get fills from IBKR
            try:
                fills = ib.fills()
                if not fills:
                    ib.reqExecutions()
                    ib.sleep(2)
                    fills = ib.fills()
            except Exception as e:
                log.error("portfolio_trade_sync_fetch_error", error=str(e))
                return

            if not fills:
                return

            # Get watchlist symbols
            with get_db() as db:
                wl_rows = db.query(PortfolioWatchlist).all()
                watchlist_symbols = {w.symbol: w for w in wl_rows}

                # Existing exec IDs to skip duplicates
                existing = db.query(PortfolioTransaction.ibkr_exec_id).filter(
                    PortfolioTransaction.ibkr_exec_id.isnot(None)
                ).all()
                existing_ids = {row[0] for row in existing}

            imported = 0

            for fill in fills:
                exec_id = fill.execution.execId
                if not exec_id or exec_id in existing_ids:
                    continue

                contract = fill.contract
                symbol = contract.symbol
                execution = fill.execution
                side = execution.side  # "BOT" or "SLD"

                # Only sync watchlist symbols
                if symbol not in watchlist_symbols:
                    continue

                # Only sync trades from the portfolio account
                trade_account = getattr(execution, 'acctNumber', '')
                if trade_account and cfg.ibkr_account and trade_account != cfg.ibkr_account:
                    continue

                wl = watchlist_symbols[symbol]

                # Parse execution time
                try:
                    exec_time = datetime.strptime(
                        execution.time, "%Y%m%d %H:%M:%S"
                    ) if isinstance(execution.time, str) else execution.time
                except Exception:
                    exec_time = datetime.utcnow()

                # Commission
                commission = 0.0
                if fill.commissionReport:
                    commission = fill.commissionReport.commission or 0.0

                sec_type = contract.secType
                right = getattr(contract, 'right', '')
                strike = getattr(contract, 'strike', 0.0) or 0.0
                expiry = getattr(contract, 'lastTradeDateOrContractMonth', '') or ''
                price = execution.price
                qty = abs(int(execution.shares))

                # Classify into portfolio transaction action
                action = None
                shares = 0
                amount = 0.0
                premium_collected = None

                if sec_type == "STK":
                    if side == "BOT":
                        action = "buy"
                        shares = qty
                        amount = qty * price
                    else:
                        action = "sell"
                        shares = qty
                        amount = qty * price

                elif sec_type in ("OPT", "FOP") and right == "P":
                    if side == "SLD":
                        action = "sell_put"
                        premium_collected = price * qty * 100
                        amount = premium_collected
                    else:
                        # Buying back a put — check if it's a close or assignment
                        if price <= 0.01:
                            # Price ~0 means assignment or expiry, not a buyback
                            action = "put_assigned"
                            shares = qty
                            price = strike
                            amount = strike * qty  # cost basis for assigned shares
                        else:
                            action = "buy_put"
                            amount = price * qty * 100

                else:
                    # Call options or other — skip for portfolio history
                    continue

                if action is None:
                    continue

                # Build notes
                if sec_type in ("OPT", "FOP"):
                    notes = (
                        f"IBKR sync: {side} {qty} {symbol} "
                        f"{expiry} ${strike}{right} @ ${price:.2f}"
                    )
                else:
                    notes = f"IBKR sync: {side} {qty} {symbol} @ ${price:.2f}"

                with get_db() as db:
                    db.add(PortfolioTransaction(
                        symbol=symbol,
                        action=action,
                        shares=shares,
                        price=price,
                        amount=amount,
                        commission=commission,
                        currency=wl.currency or "USD",
                        strike=strike if strike else None,
                        expiry=expiry if expiry else None,
                        premium_collected=premium_collected,
                        tier=wl.tier or "growth",
                        notes=notes,
                        source="ibkr_sync",
                        ibkr_exec_id=exec_id,
                        created_at=exec_time,
                    ))
                    existing_ids.add(exec_id)
                    imported += 1

                    log.info("portfolio_trade_synced",
                             symbol=symbol, action=action,
                             price=price, qty=qty, exec_id=exec_id)

            if imported > 0:
                log.info("portfolio_trade_sync_done", imported=imported)

        except Exception as e:
            log.error("portfolio_trade_sync_error", error=str(e))
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass
