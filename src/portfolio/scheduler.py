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

    # Clear dead connection before attempting reconnect
    if _portfolio_ib is not None and not _portfolio_ib.isConnected():
        try:
            _portfolio_ib.disconnect()
        except Exception:
            pass
        _portfolio_ib = None

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
            # Immediately populate cache so dashboard shows margin without waiting for hourly scan
            try:
                from src.portfolio.connection import refresh_portfolio_account_cache_from
                refresh_portfolio_account_cache_from(_portfolio_ib)
            except Exception:
                pass
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


def job_portfolio_monthly_screen(cfg: PortfolioConfig):
    """
    Monthly screener — first Monday of each month, 2 AM ET.

    Four phases:
      1. Screen global universe → update screened_universe.yaml + options_universe.yaml
      2. Diff against current watchlist → add new stocks, flag removals
         (never remove stocks with open positions — mark as pending_removal instead)
      3. Review existing holdings → CC suggestions, sell suggestions, reclassification
      4. Send alert with summary

    CRITICAL: sell/reduce/CC suggestions are NEVER auto-executed.
    They always require manual approval via dashboard.
    Breakthrough tier: always manual regardless of auto mode setting.
    """
    _ensure_event_loop()

    if not cfg.enabled:
        return

    with _portfolio_lock:
        log.info("portfolio_monthly_screen_started",
                 date=datetime.utcnow().strftime("%Y-%m-%d"))

        try:
            ib = _get_portfolio_connection(cfg)

            # ══════════════════════════════════════════════════════
            # PHASE 1: Screen universe
            # ══════════════════════════════════════════════════════
            from tools.screen_universe import (
                UniverseScreener, write_screened_universe, write_options_universe
            )
            from pathlib import Path

            regions = [r.strip() for r in cfg.rescreen_regions.split(",") if r.strip()] or None

            screener = UniverseScreener(ib)
            portfolio_universe, options_universe = screener.screen_all(
                regions=regions,
                min_market_cap=cfg.rescreen_min_market_cap,
                growth_count=50,
                dividend_count=25,
                breakthrough_count=25,
                options_count=50,
            )

            # Write output files
            write_screened_universe(
                portfolio_universe,
                Path("config/screened_universe.yaml"),
            )
            write_options_universe(
                options_universe,
                Path("config/options_universe.yaml"),
            )

            # Invalidate options universe cache so trader picks up new universe
            from src.core.config import get_options_universe
            get_options_universe.cache_clear()

            new_symbols = {s.symbol for s in portfolio_universe}
            new_tiers = {s.symbol: s.tier for s in portfolio_universe}

            log.info("portfolio_monthly_screen_phase1_done",
                     portfolio=len(portfolio_universe),
                     options=len(options_universe))

            # ══════════════════════════════════════════════════════
            # PHASE 2: Diff watchlist — add new, flag removals
            # ══════════════════════════════════════════════════════
            from src.core.database import get_db
            from src.portfolio.models import PortfolioWatchlist, PortfolioHolding

            added = []
            flagged_removal = []

            with get_db() as db:
                # Get current watchlist symbols
                current_watchlist = {
                    w.symbol: w
                    for w in db.query(PortfolioWatchlist).all()
                }

                # Get symbols with open positions
                open_positions = {
                    h.symbol
                    for h in db.query(PortfolioHolding).filter(
                        PortfolioHolding.shares > 0
                    ).all()
                }

                reclassified = []

                # Add new stocks from screener
                for score in portfolio_universe:
                    sym = score.symbol
                    # breakthrough stored as tier="breakthrough", category="growth"
                    # (category only has growth/dividend for legacy compat)
                    new_category = "dividend" if score.tier == "dividend" else "growth"
                    if sym not in current_watchlist:
                        db.add(PortfolioWatchlist(
                            symbol=sym,
                            name=score.name,
                            exchange=score.exchange,
                            currency=score.currency,
                            sector=score.sector,
                            tier=score.tier,
                            rationale=score.rationale if score.tier == "breakthrough" else None,
                            composite_score=score.portfolio_score,
                            growth_score=score.growth_score,
                            valuation_score=score.valuation_score,
                            quality_score=score.quality_score,
                            category=new_category,
                            screened_at=datetime.utcnow(),
                        ))
                        added.append(sym)
                    else:
                        # Update scores for existing watchlist entries
                        w = current_watchlist[sym]
                        # Detect reclassification
                        old_tier = w.tier or w.category or "growth"
                        if old_tier != score.tier:
                            reclassified.append({
                                "symbol": sym,
                                "from_tier": old_tier,
                                "to_tier": score.tier,
                                "reason": f"Screener reclassified from {old_tier} to {score.tier}",
                            })
                            w.tier = score.tier
                            w.category = new_category
                        # Update rationale for breakthrough stocks
                        if score.tier == "breakthrough" and score.rationale:
                            w.rationale = score.rationale
                        w.composite_score = score.portfolio_score
                        w.growth_score = score.growth_score
                        w.valuation_score = score.valuation_score
                        w.quality_score = score.quality_score
                        w.screened_at = datetime.utcnow()
                        # Clear pending_removal if stock re-qualifies
                        if w.pending_removal:
                            w.pending_removal = False
                            w.pending_removal_reason = None

                # Flag stocks no longer in screener results
                for sym, wl_entry in current_watchlist.items():
                    if sym not in new_symbols:
                        if sym in open_positions:
                            # Cannot remove — open position exists
                            if hasattr(wl_entry, "pending_removal"):
                                wl_entry.pending_removal = True
                                wl_entry.pending_removal_reason = (
                                    "No longer in screened universe. "
                                    "Pending removal — open position exists."
                                )
                            flagged_removal.append(sym)
                        else:
                            # Safe to remove — no open position
                            db.delete(wl_entry)
                            flagged_removal.append(sym)

            log.info("portfolio_monthly_screen_phase2_done",
                     added=len(added),
                     flagged_removal=len(flagged_removal))

            # ══════════════════════════════════════════════════════
            # PHASE 3: Review existing holdings
            # ══════════════════════════════════════════════════════
            review_suggestions = _review_existing_holdings_monthly(
                ib, cfg, new_symbols, new_tiers
            )

            log.info("portfolio_monthly_screen_phase3_done",
                     review_suggestions=len(review_suggestions))

            # ══════════════════════════════════════════════════════
            # PHASE 4: Alert
            # ══════════════════════════════════════════════════════
            _send_monthly_screen_alert(
                len(portfolio_universe),
                added,
                flagged_removal,
                review_suggestions,
            )

            # ══════════════════════════════════════════════════════
            # Write run log for Screener page
            # ══════════════════════════════════════════════════════
            import json as _json
            from pathlib import Path as _Path
            _log_path = _Path("data/screener_last_run.json")
            _log_path.parent.mkdir(parents=True, exist_ok=True)
            _log_path.write_text(_json.dumps({
                "status": "success",
                "run_date": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                "stocks_screened": len(portfolio_universe),
                "added": [
                    {
                        "symbol": s.symbol,
                        "name": s.name,
                        "tier": s.tier,
                        "megatrend": s.megatrend if s.tier == "breakthrough" else "",
                        "rationale": s.rationale if s.tier == "breakthrough" else "",
                    }
                    for s in portfolio_universe if s.symbol in added
                ],
                "removed": [
                    sym for sym in flagged_removal
                    if sym not in [h.symbol for h in
                        __import__("src.portfolio.models", fromlist=["PortfolioHolding"]).PortfolioHolding.__table__.c]
                ],
                "flagged_removal": flagged_removal,
                "reclassified": reclassified,
                "suggestions_created": review_suggestions,
            }, indent=2))

            log.info("portfolio_monthly_screen_done",
                     screened=len(portfolio_universe),
                     added=len(added),
                     flagged=len(flagged_removal),
                     suggestions=len(review_suggestions))

        except Exception as e:
            log.error("portfolio_monthly_screen_error", error=str(e))
            import json as _json
            from pathlib import Path as _Path
            _log_path = _Path("data/screener_last_run.json")
            _log_path.parent.mkdir(parents=True, exist_ok=True)
            _log_path.write_text(_json.dumps({
                "status": "error",
                "run_date": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                "error": str(e),
            }, indent=2))
            try:
                from src.core.alerts import get_alert_manager
                get_alert_manager().critical(
                    "Monthly Screener FAILED",
                    f"Error: {str(e)}\nManual intervention required."
                )
            except Exception:
                pass


# Keep old name as alias for backward compatibility during transition
job_portfolio_annual_rescreen = job_portfolio_monthly_screen


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


def _review_existing_holdings_monthly(
    ib: IB,
    cfg: PortfolioConfig,
    new_watchlist_symbols: set[str],
    new_tiers: dict[str, str],
) -> list[dict]:
    """
    Monthly review of existing holdings. Creates suggestions for:
      - SELL: dropped off watchlist + losing money + below SMA
      - SELL (profit): dropped off watchlist but still profitable
      - REDUCE: overweight position (>12% of portfolio)
      - SELL COVERED CALL: dividend tier above SMA + profitable
                           growth tier: only when FMP shows growth slowing (future)
      - RECLASSIFY: growth→dividend when dividends started

    Rules:
      - Breakthrough tier: never suggest sell/CC based on price/metrics alone
      - Growth tier: CC only when revenue growth slows (pending FMP integration)
      - Dividend tier: CC when above SMA + profitable
      - Never auto-execute any suggestion here
      - Skip CC if open covered call already exists on that symbol
    """
    from src.core.database import get_db
    from src.portfolio.models import PortfolioHolding
    from src.core.suggestions import create_suggestion, TradeSuggestion
    from src.broker.market_data import get_stock_price
    from src.portfolio.fmp import get_full_fundamentals
    from datetime import datetime, timedelta

    suggestions = []

    with get_db() as db:
        holdings = db.query(PortfolioHolding).filter(
            PortfolioHolding.shares > 0
        ).all()

        if not holdings:
            return suggestions

        total_value = sum(
            h.market_value or h.total_invested or 0 for h in holdings
        )
        if total_value <= 0:
            return suggestions

        # Check which symbols already have open covered call suggestions
        open_cc_symbols = {
            s.symbol for s in db.query(TradeSuggestion).filter(
                TradeSuggestion.action == "sell_covered_call_review",
                TradeSuggestion.status == "pending",
            ).all()
        }

    for holding in holdings:
        symbol = holding.symbol
        shares = holding.shares
        avg_cost = holding.avg_cost
        market_value = holding.market_value or (
            shares * (holding.current_price or avg_cost)
        )
        position_pct = market_value / total_value if total_value > 0 else 0
        pnl_pct = holding.unrealized_pnl_pct or 0

        # Use tier from new screener results if available, else from holding
        tier = new_tiers.get(symbol, holding.tier or "growth")

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

        # SMA analysis
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

        dropped_off = symbol not in new_watchlist_symbols

        # ── 1. Breakthrough: never touch based on metrics ──
        if tier == "breakthrough":
            continue

        # ── 1b. Dividend disqualification ──────────────────
        # Primary: dividend cut OR payout >90% OR revenue declining 2yr
        # Secondary (2+ triggers): dividend growth stopped 3yr, FCF negative 2yr, D/E deteriorating
        if tier == "dividend":
            try:
                fmp = get_full_fundamentals(symbol)
                if fmp:
                    payout = fmp.get("payout_ratio", 0)
                    div_cut = fmp.get("dividend_cut", False)
                    rev_yoy = fmp.get("revenue_yoy_pct", 0)
                    rev_avg = fmp.get("revenue_avg_pct", 0)
                    fcf_neg = fmp.get("fcf_negative_years", 0)
                    de = fmp.get("debt_to_equity", 0)

                    # Primary disqualifiers
                    primary_fail = (
                        div_cut
                        or payout > 90
                        or (rev_yoy < 0 and rev_avg < 0)  # declining 2+ years
                    )

                    # Secondary disqualifiers
                    secondary_count = sum([
                        fcf_neg >= 2,          # FCF negative 2+ years
                        de > 2.0,              # high and rising debt
                    ])

                    if primary_fail or secondary_count >= 2:
                        reason_parts = []
                        if div_cut:
                            reason_parts.append("dividend cut detected")
                        if payout > 90:
                            reason_parts.append(f"payout ratio {payout:.0f}%")
                        if rev_yoy < 0 and rev_avg < 0:
                            reason_parts.append(f"revenue declining YoY {rev_yoy:+.1f}%")
                        if fcf_neg >= 2:
                            reason_parts.append(f"FCF negative {fcf_neg} years")
                        if de > 2.0:
                            reason_parts.append(f"D/E ratio {de:.1f}")

                        rationale = (
                            f"MONTHLY REVIEW: {symbol} (dividend) failing health check. "
                            f"{', '.join(reason_parts)}. "
                            f"Position: {shares} shares @ ${avg_cost:.2f}, now ${current_price:.2f}. "
                            f"P&L: {pnl_pct:+.1f}%."
                        )
                        create_suggestion(
                            symbol=symbol,
                            action="sell_stock_review",
                            quantity=shares,
                            limit_price=round(current_price * 0.998, 2),
                            source="rescreen",
                            tier=tier,
                            signal="monthly_dividend_disqualified",
                            rationale=rationale,
                            current_price=current_price,
                            sma_200=sma_200,
                            rank=0,
                            funding_source="n/a",
                            expires_hours=720,
                        )
                        suggestions.append({
                            "symbol": symbol, "action": "SELL",
                            "reason": f"Dividend disqualified: {', '.join(reason_parts)}",
                        })
                        continue
            except Exception:
                pass

        # ── 1c. Growth reclassification check ─────────────
        # Growth → Dividend: revenue slowing (<15% YoY) + dividend started (yield >2.5%)
        # Growth → Exit suggestion: revenue slowing + NO dividend after holding 6+ months
        if tier == "growth":
            try:
                fmp = get_full_fundamentals(symbol)
                if fmp:
                    rev_yoy = fmp.get("revenue_yoy_pct", 999)
                    div_yield = fmp.get("dividend_yield", 0)
                    growth_slowing = rev_yoy < 15

                    if growth_slowing:
                        if div_yield > 2.5:
                            # Reclassify to dividend — update DB directly
                            from src.core.database import get_db as _get_db
                            from src.portfolio.models import PortfolioWatchlist as _PWL
                            with _get_db() as _db:
                                _w = _db.query(_PWL).filter(_PWL.symbol == symbol).first()
                                if _w and _w.tier == "growth":
                                    _w.tier = "dividend"
                                    _w.category = "dividend"
                                    _db.commit()
                            suggestions.append({
                                "symbol": symbol, "action": "RECLASSIFIED",
                                "reason": f"Growth→Dividend: rev growth {rev_yoy:+.1f}%, div yield {div_yield:.1f}%",
                            })
                        else:
                            # Growth slowing, no dividend — check how long held
                            # Use earliest buy transaction for this symbol
                            held_days = 999
                            try:
                                from src.core.database import get_db as _get_db2
                                from src.portfolio.models import PortfolioTransaction as _PTX
                                from datetime import date as _date
                                from sqlalchemy import func as _func
                                with _get_db2() as _db2:
                                    first_tx = _db2.query(_func.min(_PTX.created_at)).filter(
                                        _PTX.symbol == symbol,
                                        _PTX.action.in_(["buy_stock", "put_assigned"]),
                                    ).scalar()
                                    if first_tx:
                                        held_days = (_date.today() - first_tx.date()).days
                            except Exception:
                                pass
                            if held_days > 180:  # held 6+ months with slowing growth, no dividend
                                rationale = (
                                    f"MONTHLY REVIEW: {symbol} (growth) revenue growth slowing "
                                    f"({rev_yoy:+.1f}% YoY, below 15% threshold). "
                                    f"No dividend started after {held_days} days. "
                                    f"Consider exiting — growth thesis weakening. "
                                    f"Position: {shares} shares @ ${avg_cost:.2f}, now ${current_price:.2f}. "
                                    f"P&L: {pnl_pct:+.1f}%."
                                )
                                create_suggestion(
                                    symbol=symbol,
                                    action="sell_stock_review",
                                    quantity=shares,
                                    limit_price=round(current_price * 0.998, 2),
                                    source="rescreen",
                                    tier=tier,
                                    signal="monthly_growth_thesis_weak",
                                    rationale=rationale,
                                    current_price=current_price,
                                    sma_200=sma_200,
                                    rank=0,
                                    funding_source="n/a",
                                    expires_hours=720,
                                )
                                suggestions.append({
                                    "symbol": symbol, "action": "CONSIDER SELL",
                                    "reason": f"Growth slowing {rev_yoy:+.1f}% YoY, no dividend after {held_days}d",
                                })
            except Exception:
                pass

        # ── 2. SELL: dropped off + losing + below SMA ──────
        if dropped_off and pnl_pct < -10 and pct_vs_sma < -10:
            rationale = (
                f"MONTHLY REVIEW: {symbol} dropped off screened universe. "
                f"P&L: {pnl_pct:+.1f}%, price {pct_vs_sma:+.1f}% vs 200d SMA. "
                f"Position: {shares} shares @ ${avg_cost:.2f}, now ${current_price:.2f}. "
                f"Consider selling — fundamentals no longer qualify."
            )
            create_suggestion(
                symbol=symbol,
                action="sell_stock_review",
                quantity=shares,
                limit_price=round(current_price * 0.998, 2),
                source="rescreen",
                tier=tier,
                signal="monthly_review_sell",
                rationale=rationale,
                current_price=current_price,
                sma_200=sma_200,
                rank=0,
                funding_source="n/a",
                expires_hours=720,
            )
            suggestions.append({
                "symbol": symbol, "action": "SELL",
                "reason": f"Off watchlist, P&L {pnl_pct:+.1f}%, below SMA",
            })
            continue

        # ── 3. REDUCE: overconcentrated (>12%) ─────────────
        if position_pct > 0.12:
            target_value = total_value * 0.08
            reduce_value = market_value - target_value
            reduce_shares = int(reduce_value / current_price)
            if reduce_shares > 0:
                rationale = (
                    f"MONTHLY REVIEW: {symbol} is {position_pct:.1%} of portfolio "
                    f"(above 12% concentration limit). "
                    f"Suggest reducing by {reduce_shares} shares to ~8%. "
                    f"P&L: {pnl_pct:+.1f}%, current ${current_price:.2f}."
                )
                create_suggestion(
                    symbol=symbol,
                    action="reduce_position_review",
                    quantity=reduce_shares,
                    limit_price=round(current_price * 0.998, 2),
                    source="rescreen",
                    tier=tier,
                    signal="monthly_review_reduce",
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

        # ── 4. SELL COVERED CALL ────────────────────────────
        # Dividend tier: above SMA + profitable + no open CC + enough shares
        # Growth tier: above SMA + profitable + revenue growth slowing (<15% YoY)
        growth_slowing = False
        if tier == "growth":
            try:
                fmp = get_full_fundamentals(symbol)
                if fmp:
                    rev_yoy = fmp.get("revenue_yoy_pct", 999)
                    growth_slowing = rev_yoy < 15
            except Exception:
                pass

        cc_trigger = (
            pct_vs_sma > 15
            and pnl_pct > 20
            and shares >= 100
            and symbol not in open_cc_symbols
            and (
                tier == "dividend"
                or (tier == "growth" and growth_slowing)
            )
        )
        if cc_trigger:
            # Calculate strike: 5% OTM from current price
            strike = round(current_price * 1.05, 0)
            # Target ~30 DTE: find nearest monthly expiry
            from datetime import date
            import calendar
            today = date.today()
            # Third Friday of next month as target expiry
            next_month = today.replace(day=1)
            if today.day > 15:
                # If past mid-month, target month after next
                if next_month.month == 12:
                    next_month = next_month.replace(year=next_month.year + 1, month=1)
                else:
                    next_month = next_month.replace(month=next_month.month + 1)
            if next_month.month == 12:
                month_after = next_month.replace(year=next_month.year + 1, month=1)
            else:
                month_after = next_month.replace(month=next_month.month + 1)
            # Find third Friday of target month
            target_month = month_after
            first_day = target_month.replace(day=1)
            first_friday = first_day + timedelta(days=(4 - first_day.weekday()) % 7)
            third_friday = first_friday + timedelta(weeks=2)
            expiry_str = third_friday.strftime("%Y%m%d")

            rationale = (
                f"MONTHLY REVIEW: {symbol} ({tier}) is {pct_vs_sma:+.1f}% above "
                f"200d SMA and up {pnl_pct:+.1f}%. "
                f"Suggest selling covered call to harvest premium. "
                f"Position: {shares} shares @ ${avg_cost:.2f}, now ${current_price:.2f}. "
                f"Suggested: {shares // 100} contract(s), strike ${strike:.0f}, "
                f"expiry {third_friday.strftime('%b %d %Y')} (~30 DTE)."
            )
            create_suggestion(
                symbol=symbol,
                action="sell_covered_call_review",
                quantity=shares // 100,
                source="rescreen",
                tier=tier,
                signal="monthly_cc_review",
                rationale=rationale,
                current_price=current_price,
                sma_200=sma_200,
                strike=strike,
                expiry=expiry_str,
                right="C",
                rank=0,
                funding_source="n/a",
                expires_hours=720,
            )
            suggestions.append({
                "symbol": symbol, "action": "SELL CC",
                "reason": f"+{pct_vs_sma:.0f}% above SMA, P&L +{pnl_pct:.0f}%, strike ${strike:.0f} exp {third_friday.strftime('%b %d')}",
            })
            open_cc_symbols.add(symbol)  # prevent duplicate within same run
            continue

        # ── 5. SELL: dropped off but profitable ────────────
        if dropped_off and pnl_pct > 5:
            rationale = (
                f"MONTHLY REVIEW: {symbol} no longer in screened universe "
                f"but still profitable ({pnl_pct:+.1f}%). "
                f"Consider trimming or selling while in profit. "
                f"Position: {shares} shares @ ${avg_cost:.2f}, now ${current_price:.2f}."
            )
            create_suggestion(
                symbol=symbol,
                action="sell_stock_review",
                quantity=shares,
                limit_price=round(current_price * 0.998, 2),
                source="rescreen",
                tier=tier,
                signal="monthly_review_trim_profit",
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


def _send_monthly_screen_alert(
    stocks_screened: int,
    added: list[str],
    flagged_removal: list[str],
    review_suggestions: list[dict],
):
    """Send alert summarizing monthly screen results."""
    from src.core.alerts import get_alert_manager
    from src.core.suggestions import get_pending_suggestions

    alert = get_alert_manager()
    pending_count = len(get_pending_suggestions())

    lines = [
        f"📋 Monthly Portfolio Screen Complete",
        f"Date: {datetime.utcnow().strftime('%Y-%m-%d')}",
        f"Screened: {stocks_screened} stocks globally",
    ]

    if added:
        lines.append(f"")
        lines.append(f"✅ {len(added)} new stocks added to watchlist:")
        lines.append(f"  {', '.join(added[:10])}")
        if len(added) > 10:
            lines.append(f"  ... and {len(added) - 10} more")

    if flagged_removal:
        lines.append(f"")
        lines.append(f"⚠️ {len(flagged_removal)} stocks flagged for removal:")
        lines.append(f"  {', '.join(flagged_removal[:10])}")

    if review_suggestions:
        lines.append(f"")
        lines.append(f"⚠️ {len(review_suggestions)} holding review suggestions:")
        for s in review_suggestions[:10]:
            lines.append(f"  • {s['symbol']}: {s['action']} — {s['reason']}")
        if len(review_suggestions) > 10:
            lines.append(f"  ... and {len(review_suggestions) - 10} more")

    if pending_count > 0:
        lines.append(f"")
        lines.append(f"🔔 {pending_count} total suggestions awaiting approval on dashboard")
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

                elif sec_type in ("OPT", "FOP") and right == "C":
                    if side == "SLD":
                        action = "sell_call"
                        premium_collected = price * qty * 100
                        amount = premium_collected
                    else:
                        # Buying back a call
                        action = "buy_call"
                        amount = price * qty * 100
                else:
                    # Other instrument types — skip
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
