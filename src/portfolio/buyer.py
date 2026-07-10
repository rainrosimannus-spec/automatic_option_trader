"""
Portfolio Buyer — three-tier long-term accumulation with put-entry mechanism.

Entry decision per stock:
  1. If price is already at/below target → direct buy
  2. Otherwise → sell cash-secured put at target strike price
     - If assigned → stock acquired at desired price minus premium
     - If expired → premium collected, re-sell put at same/lower strike

Tier-aware: different buy criteria for dividend, breakthrough, growth stocks.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ib_insync import IB, Stock, Option, LimitOrder, MarketOrder, Forex

from src.core.logger import get_logger
from src.core.database import get_db
from src.portfolio.models import (
    PortfolioHolding, PortfolioTransaction, PortfolioWatchlist,
    PortfolioPutEntry, PortfolioState,
)
from src.portfolio.analyzer import PortfolioAnalyzer, StockAnalysis
from src.portfolio.config import PortfolioConfig
from src.portfolio.connection import _ensure_event_loop, get_portfolio_lock
from src.portfolio.ranker import (
    MarketRegime, CashPolicy, RankedSignal,
    rank_signals, detect_market_regime,
)

log = get_logger(__name__)


# outsideRth is only honoured by North-American and European venues. Asia/AU/ZA reject out-of-hours
# orders and park them in PendingSubmit indefinitely (the HK 3690 case), so those markets are RTH-only
# — their orders rest and transmit when the local market opens.
_OUTSIDE_RTH_CCY = {"USD", "CAD", "EUR", "GBP", "CHF", "NOK", "SEK", "DKK"}


def _outside_rth_ok(currency: str | None) -> bool:
    return (currency or "USD").upper() in _OUTSIDE_RTH_CCY


# (timezone, open_hour, close_hour, trading_weekdays) — weekday() is Mon=0 … Sun=6.
# A currency MISSING from this map used to mean "scan anyway" (always open), which is how NICE (ILS)
# got ordered into a shut Tel Aviv exchange at 00:09 UTC on a Friday: the order can't reach the venue,
# sits PendingSubmit, and our own stuck-detector cancels it + venue-blocks the symbol for 6h. Every
# currency the watchlist can hold must be listed here. TASE trades Sun–Thu, not Mon–Fri.
_MON_FRI = (0, 1, 2, 3, 4)
_MARKET_HOURS = {
    "USD": ("US/Eastern", 9, 16, _MON_FRI),
    "CAD": ("US/Eastern", 9, 16, _MON_FRI),
    "EUR": ("Europe/Berlin", 9, 17, _MON_FRI),
    "CHF": ("Europe/Berlin", 9, 17, _MON_FRI),
    "GBP": ("Europe/London", 8, 16, _MON_FRI),
    "NOK": ("Europe/Berlin", 9, 17, _MON_FRI),
    "SEK": ("Europe/Berlin", 9, 17, _MON_FRI),
    "DKK": ("Europe/Berlin", 9, 17, _MON_FRI),
    "JPY": ("Asia/Tokyo", 9, 15, _MON_FRI),
    "AUD": ("Australia/Sydney", 10, 16, _MON_FRI),
    "HKD": ("Asia/Hong_Kong", 9, 16, _MON_FRI),
    "SGD": ("Asia/Singapore", 9, 17, _MON_FRI),
    "ZAR": ("Africa/Johannesburg", 9, 17, _MON_FRI),
    "ILS": ("Asia/Jerusalem", 10, 17, (6, 0, 1, 2, 3)),   # TASE: Sun–Thu
}


def _market_open(currency: str | None) -> bool:
    """True when the venue that settles `currency` is in its regular session right now.

    Unknown currency → True (scan anyway), matching the long-standing behaviour for names whose
    venue we haven't mapped. Prefer adding the currency above over relying on that fallback."""
    import pytz
    hours = _MARKET_HOURS.get((currency or "").upper())
    if not hours:
        return True
    tz_name, open_h, close_h, days = hours
    now = datetime.now(pytz.timezone(tz_name))
    return now.weekday() in days and open_h <= now.hour < close_h


# ── Permission-block registry ────────────────────────────────────────────────────────────
# When an order is rejected because the account isn't permissioned to trade that exchange yet
# (IBKR Error 200 "No security definition" / "no trading permission" — e.g. Hong Kong/3690
# before worldwide perms activate), we pause the SYMBOL for a cooldown so the compounder scan
# stops re-creating doomed suggestions for it and the day's deploy budget flows to names that
# can actually fill. The name stays ranked/visible in the universe — it's just not given budget
# until the block expires (auto-retries) or perms are granted. In-memory: a restart clears it,
# so a blocked name gets one fresh attempt post-restart, then re-blocks if still failing.
_PERMISSION_BLOCK_HOURS = 24.0
_permission_blocked: dict[str, datetime] = {}


def _mark_permission_blocked(symbol: str, hours: float = _PERMISSION_BLOCK_HOURS) -> None:
    if symbol:
        _permission_blocked[symbol] = datetime.utcnow() + timedelta(hours=hours)
        log.warning("portfolio_symbol_permission_blocked", symbol=symbol, hours=hours)


def _is_permission_blocked(symbol: str) -> bool:
    until = _permission_blocked.get(symbol)
    if not until:
        return False
    if datetime.utcnow() < until:
        return True
    _permission_blocked.pop(symbol, None)   # cooldown elapsed — allow a retry
    return False


def _order_blocked_by_permission(trade) -> bool:
    """True if an order's cancel/rejection was a permission / no-security-definition error
    (the account can't trade that exchange yet), vs a transient cancel. Reads the IBKR
    TradeLogEntry codes/messages — Error 200 is the Hong Kong/3690 case."""
    try:
        for entry in (getattr(trade, "log", None) or []):
            code = getattr(entry, "errorCode", 0) or 0
            msg = (getattr(entry, "message", "") or "").lower()
            if code == 200 or "no security definition" in msg or "permission" in msg:
                return True
    except Exception:
        pass
    return False


# _ensure_event_loop imported from connection.py


# Tracks when each symbol was FIRST seen stuck in 'PendingSubmit' (so a freshly-placed order that's
# briefly PendingSubmit before it transmits isn't mistaken for stuck). In-memory; cleared on restart.
_pending_submit_since: dict[str, datetime] = {}
_STUCK_PENDING_SECONDS = 240.0      # PendingSubmit longer than this → never reached the exchange


def _cancel_portfolio_order_id(ib, order_id) -> bool:
    """Cancel a single portfolio order by id. LOOP-SAFE: ib.trades() is a pure cached-list read and
    ib.cancelOrder() is fire-and-forget (sends, no await) — NEITHER drives the asyncio loop (the wedge
    came from the analyzer's reqHistoricalData run_until_complete, not from cancel/trades). Serialized
    under get_portfolio_lock like every other IBKR call."""
    from src.portfolio.connection import get_portfolio_lock, _ensure_event_loop
    try:
        _ensure_event_loop()
        with get_portfolio_lock():
            for t in ib.trades():                       # pure property read — loop-free
                if getattr(getattr(t, "order", None), "orderId", None) == order_id:
                    ib.cancelOrder(t.order)             # fire-and-forget — loop-free
                    return True
    except Exception as e:
        log.warning("compounder_stuck_cancel_failed", order_id=order_id, error=str(e))
    return False


def detect_stuck_orders_from_cache(ib=None) -> int:
    """Prompt detector for orders that never reach the exchange (RACE/BVME PendingSubmit).

    DETECTION is loop-safe — reads ONLY the already-refreshed in-memory portfolio pending-orders cache
    (no IBKR calls). A compounder STK BUY that stays 'PendingSubmit' longer than _STUCK_PENDING_SECONDS
    can't fill at any price → venue-block the name (the deploy queue skips it and routes the budget to
    the next buy) + cancel its orphaned suggestion card. When `ib` is provided, ALSO cancel the stuck
    IBKR order(s) for the just-blocked names via _cancel_portfolio_order_id (loop-free fire-and-forget
    cancel — see that helper) so the order leaves the dashboard within ~5 min instead of lingering until
    the 4h scan. Do NOT reintroduce ib.trades()/cancelOrder into the *detection* path or run them for
    every order each tick — the earlier sweep that did so contended on the shared loop and wedged it
    (reverted 14ad4e0); here IBKR is touched only on the rare block event. Runs every ~5 min from
    job_portfolio_health_check.
    """
    from src.portfolio.connection import get_cached_portfolio_pending_orders
    from src.core.suggestions import TradeSuggestion
    now = datetime.utcnow()
    blocked: list[str] = []
    stuck_ids: dict[str, list] = {}     # sym -> [order_id] of its stuck PendingSubmit orders
    try:
        pending = get_cached_portfolio_pending_orders() or []
        cur_stuck: set[str] = set()
        for o in pending:
            if (o.get("sec_type") == "STK" and str(o.get("action") or "").upper() == "BUY"
                    and o.get("status") == "PendingSubmit"):
                sym = o.get("symbol")
                if not sym:
                    continue
                cur_stuck.add(sym)
                stuck_ids.setdefault(sym, []).append(o.get("order_id"))
                first = _pending_submit_since.setdefault(sym, now)
                if (now - first).total_seconds() >= _STUCK_PENDING_SECONDS and not _is_permission_blocked(sym):
                    _mark_permission_blocked(sym, hours=6.0)
                    blocked.append(sym)
        # Forget symbols no longer stuck (transmitted / filled / cancelled) so they get a clean retry.
        for sym in list(_pending_submit_since):
            if sym not in cur_stuck:
                _pending_submit_since.pop(sym, None)
        if blocked:
            with get_db() as db:
                ghosts = db.query(TradeSuggestion).filter(
                    TradeSuggestion.source == "portfolio",
                    TradeSuggestion.action == "buy_stock",
                    TradeSuggestion.status.in_(("submitted", "approved", "queued")),
                    TradeSuggestion.symbol.in_(blocked),
                ).all()
                for g in ghosts:
                    g.status = "cancelled"
                    g.review_note = "Order stuck PendingSubmit (venue rights) — auto-blocked, budget to next"
            cancelled = 0
            if ib is not None:                          # targeted loop-free cancel of the stuck order(s)
                for sym in blocked:
                    for oid in stuck_ids.get(sym, []):
                        if oid is not None and _cancel_portfolio_order_id(ib, oid):
                            cancelled += 1
            log.warning("compounder_stuck_pending_blocked", symbols=sorted(blocked),
                        cancelled=cancelled,
                        note="never reached exchange — venue-blocked + order cancelled; budget to next")
    except Exception as e:
        log.warning("compounder_stuck_detect_failed", error=str(e))
    return len(blocked)


class PortfolioBuyer:
    """Execute buy decisions for the three-tier long-term portfolio."""

    def __init__(self, ib: IB, cfg: PortfolioConfig):
        self.ib = ib
        self.cfg = cfg
        self.analyzer = PortfolioAnalyzer(
            ib,
            sma_period=cfg.sma_period,
            rsi_period=cfg.rsi_period,
            min_discount_pct=cfg.min_discount_pct,
            rsi_oversold=cfg.rsi_oversold,
        )
        self.cash_policy = CashPolicy(
            cash_reserve_pct=cfg.cash_reserve_pct,
            margin_max_pct=cfg.margin_max_pct,
        )

    # ── Main scan ────────────────────────────────────────────
    def run_scan(self) -> list[str]:
        """
        Main scan loop:
        1. Detect market regime (VIX, SPY conditions)
        2. Determine deployable capital (cash / reserve override / margin)
        3. Analyze watchlist stocks per tier
        4. Rank signals with portfolio-aware scoring
        5. Execute entries in rank order until capital exhausted
        6. Check open put-entries for assignment/expiry
        """
        _ensure_event_loop()
        log.info("portfolio_scan_started")

        bought = []

        # ── Manual HALT check — same system_state key as options trader ──
        try:
            from src.core.database import get_db
            from src.core.models import SystemState
            with get_db() as db:
                halt_state = db.query(SystemState).filter(
                    SystemState.key == "halted"
                ).first()
                if halt_state and halt_state.value == "true":
                    log.warning("portfolio_scan_halted", reason="manual halt active")
                    return bought
        except Exception as e:
            log.warning("portfolio_halt_check_failed", error=str(e))

        # ── Strategy branch: long-horizon compounder accumulation ──
        if getattr(self.cfg, "strategy", "classic") == "compounder":
            return self.run_compounder_scan()

        # Step 1: Detect market regime
        regime = self._detect_regime()
        self._store_state("market_regime", regime.regime_name)
        self._store_state("market_vix", str(round(regime.vix, 1)))

        # Step 1b: Market overbought guard (still applies — don't buy into euphoria)
        is_overbought, pct_above = self.analyzer.check_market_overbought(
            self.cfg.market_overbought_pct
        )

        if is_overbought and regime.regime_name == "normal":
            log.info("portfolio_market_overbought_parking_cash", pct_above_sma=pct_above)
            self._park_cash()
            self._store_state("market_status", "overbought")
            self._store_state("market_pct_above_sma", str(pct_above or ""))
            return bought

        self._store_state("market_status", regime.regime_name)
        self._store_state("market_pct_above_sma", str(pct_above or ""))

        # Step 2: Get account info and determine deployable capital
        available_cash = self._get_available_cash()
        if available_cash is None:
            log.warning("portfolio_cannot_get_cash")
            return bought

        net_liquidation = self._get_net_liquidation()
        if not net_liquidation:
            net_liquidation = available_cash

        # Step 2b: Margin/leverage gate — block buys when leverage is too high
        margin_used = self._get_maintenance_margin()
        if margin_used and net_liquidation and net_liquidation > 0:
            margin_pct = margin_used / net_liquidation * 100
            self._store_state("margin_utilization", str(round(margin_pct, 1)))

            # Hard gate: no new buys above 40% margin (except in special regimes)
            regime_name = regime.regime_name if regime else "normal"
            margin_limit = 40.0  # default

            # Allow slightly higher margin during capitulation/stabilization
            # (policy: up to 15% NLV margin usage = ~57% margin util)
            if regime_name in ("capitulation", "stabilization"):
                margin_limit = 55.0

            if margin_pct > margin_limit:
                log.warning("portfolio_margin_gate_blocked",
                            margin_pct=round(margin_pct, 1),
                            limit=margin_limit,
                            regime=regime_name)
                self._store_state("portfolio_blocked_reason",
                                  f"margin {margin_pct:.0f}% > {margin_limit:.0f}% limit")
                return bought

            # Soft gate: reduce deployable capital if margin is elevated
            if margin_pct > 25:
                # Scale down: at 25% margin use full cash, at 40% use 0
                scale = max(0.0, (margin_limit - margin_pct) / (margin_limit - 25))
                original_cash = available_cash
                available_cash = available_cash * scale
                log.info("portfolio_margin_reduced_capital",
                         margin_pct=round(margin_pct, 1),
                         scale=round(scale, 2),
                         original_cash=round(original_cash),
                         reduced_to=round(available_cash))

        # Also check: if cash balance is negative, don't buy on margin
        # (unless regime explicitly allows it)
        if available_cash < 0:
            regime_name = regime.regime_name if regime else "normal"
            if regime_name not in ("capitulation", "stabilization"):
                log.warning("portfolio_negative_cash_blocked",
                            cash=round(available_cash),
                            regime=regime_name)
                self._store_state("portfolio_blocked_reason", "negative cash balance")
                return bought

        # Step 3: Check open put-entries for assignment/expiry
        self._check_put_entries()

        # Step 4: Analyze watchlist stocks per tier
        watchlist = self._get_watchlist()
        if not watchlist:
            log.warning("portfolio_empty_watchlist")
            return bought

        signals: list[tuple[PortfolioWatchlist, StockAnalysis]] = []
        scanned = 0
        failed = 0
        failed_exchanges: dict[str, int] = {}
        skipped_exchanges: set[str] = set()

        for stock in watchlist:
            if stock.exchange in skipped_exchanges:
                failed += 1
                continue

            if not self._is_market_open(stock.currency):
                continue

            try:
                analysis = self.analyzer.analyze_stock(
                    stock.symbol, stock.exchange, stock.currency,
                    tier=stock.tier,
                )
                if analysis:
                    self._update_watchlist_metrics(stock, analysis)
                    scanned += 1
                    failed_exchanges[stock.exchange] = 0
                    if analysis.buy_signal:
                        signals.append((stock, analysis))
                else:
                    failed += 1
                    failed_exchanges[stock.exchange] = failed_exchanges.get(stock.exchange, 0) + 1

            except Exception as e:
                failed += 1
                failed_exchanges[stock.exchange] = failed_exchanges.get(stock.exchange, 0) + 1
                log.warning("portfolio_analysis_error", symbol=stock.symbol,
                            error=str(e), error_type=type(e).__name__)

            if failed_exchanges.get(stock.exchange, 0) >= 3:
                log.warning("portfolio_exchange_skipped",
                            exchange=stock.exchange,
                            reason="3 consecutive timeouts — data farm likely unavailable")
                skipped_exchanges.add(stock.exchange)

        log.info("portfolio_scan_progress", scanned=scanned, failed=failed,
                 total=len(watchlist), skipped_exchanges=list(skipped_exchanges))

        if not signals:
            log.info("portfolio_no_signals")
            return bought

        # Step 5: Portfolio-aware ranking
        holdings_map = self._get_holdings_map()
        tier_weights = {
            "dividend": self.cfg.tier_allocation.dividend,
            "breakthrough": self.cfg.tier_allocation.breakthrough,
            "growth": self.cfg.tier_allocation.growth,
        }
        tier_values = self._get_tier_values()
        sector_counts = self._get_sector_counts()

        ranked = rank_signals(
            signals=signals,
            holdings=holdings_map,
            tier_weights=tier_weights,
            tier_values=tier_values,
            sector_counts=sector_counts,
            net_liquidation=net_liquidation,
            regime=regime,
            cash_policy=self.cash_policy,
        )

        # Determine deployable capital based on regime
        best_score = ranked[0].composite_score if ranked else 0
        deployable, funding_source = self.cash_policy.get_deployable(
            available_cash, net_liquidation, regime, best_score,
        )

        log.info("portfolio_capital_decision",
                 regime=regime.regime_name,
                 funding_source=funding_source,
                 available_cash=round(available_cash, 2),
                 deployable=round(deployable, 2),
                 net_liquidation=round(net_liquidation, 2),
                 signals_count=len(ranked))

        if deployable < self.cfg.min_single_buy_eur:
            log.info("portfolio_insufficient_deployable", deployable=round(deployable, 2))
            return bought

        # Step 6: Execute entries in rank order with sequential numbering
        suggestion_seq = 0  # sequential counter — no gaps in suggestion ranks

        for rs in ranked:
            if deployable < self.cfg.min_single_buy_eur:
                log.info("portfolio_capital_exhausted", remaining=round(deployable, 2))
                break

            # Find the matching signal pair
            stock_analysis = None
            for stock, analysis in signals:
                if stock.symbol == rs.symbol:
                    stock_analysis = (stock, analysis)
                    break
            if not stock_analysis:
                continue

            stock, analysis = stock_analysis

            # Skip if already has open put-entry
            if stock.has_open_put:
                log.debug("portfolio_skip_has_open_put", symbol=stock.symbol)
                continue

            # Apply tier-specific margin limit if using margin
            if rs.is_margin_trade:
                tier_limit = self.cash_policy.get_tier_margin_limit(
                    stock.tier, net_liquidation, regime
                )
                max_for_this = min(deployable, tier_limit)
            else:
                max_for_this = deployable

            # ── $5M scaling safeguards ──
            # 1. Hard dollar cap per position
            position_cap = min(
                (net_liquidation or 0) * self.cfg.position_cap_pct,
                self.cfg.position_cap_max_usd,
            )
            if position_cap > self.cfg.min_single_buy_eur and max_for_this > position_cap:
                log.info("portfolio_position_cap_applied",
                         symbol=stock.symbol,
                         cap=round(position_cap, 0),
                         was=round(max_for_this, 0))
                max_for_this = position_cap

            # 2. Total exposure cap — how much is already deployed today + open positions
            if not self._check_total_exposure(net_liquidation or 0):
                log.info("portfolio_total_exposure_cap_reached",
                         symbol=stock.symbol,
                         nlv=round(net_liquidation or 0, 0))
                break  # no point checking more stocks

            # 3. Daily deployment limit — how much new capital deployed today
            daily_deployed = self._get_daily_deployed()
            daily_cap = min(
                (net_liquidation or 0) * self.cfg.daily_deployment_pct,
                self.cfg.daily_deployment_max_usd,
            )
            if daily_cap > self.cfg.min_single_buy_eur and daily_deployed >= daily_cap:
                log.info("portfolio_daily_deployment_cap_reached",
                         symbol=stock.symbol,
                         deployed_today=round(daily_deployed, 0),
                         cap=round(daily_cap, 0))
                break  # no point checking more stocks

            buy_amount = self._calculate_buy_amount(
                analysis, net_liquidation, max_for_this
            )

            if buy_amount < self.cfg.min_single_buy_eur:
                log.info("portfolio_buy_amount_too_small",
                         symbol=stock.symbol, amount=round(buy_amount, 2))
                continue

            # This stock will produce a suggestion — assign sequential rank
            suggestion_seq += 1

            log.info("portfolio_ranked_entry",
                     suggestion_rank=suggestion_seq,
                     symbol=rs.symbol,
                     tier=rs.tier,
                     rank_score=round(rs.final_rank_score, 1),
                     signal_score=round(rs.composite_score, 1),
                     tier_bonus=round(rs.tier_underweight_bonus, 1),
                     sector_bonus=round(rs.sector_diversity_bonus, 1),
                     conc_penalty=round(rs.concentration_penalty, 1),
                     funding=rs.funding_source,
                     buy_amount=round(buy_amount, 2))

            # Decision: direct buy or put-entry?
            entry_method = self._choose_entry_method(stock, analysis)
            log.info("portfolio_entry_method_chosen",
                     symbol=stock.symbol, method=entry_method,
                     suggestion_mode=self.cfg.suggestion_mode)

            # Earnings guard — skip if earnings within 3 days (same rule as Maggy)
            try:
                from src.broker.market_data import has_upcoming_earnings
                if has_upcoming_earnings(stock.symbol, stock.exchange, stock.currency, within_days=3):
                    log.info("portfolio_earnings_skip", symbol=stock.symbol,
                             reason="earnings within 3 days")
                    continue
            except Exception as _e:
                log.debug("portfolio_earnings_check_failed", symbol=stock.symbol, error=str(_e))

            # Sentiment guard — skip if strongly negative news sentiment in last 7 days
            try:
                from src.portfolio.sentiment import get_news_sentiment
                from src.core.config import get_settings as _gs
                _api_key = _gs().raw.get("finnhub", {}).get("api_key", "")
                if _api_key:
                    _sent = get_news_sentiment(stock.symbol, _api_key, days=7)
                    if _sent["signal"] == "negative" and _sent["score"] < -0.3:
                        log.info("portfolio_sentiment_skip", symbol=stock.symbol,
                                 score=_sent["score"], articles=_sent["articles"])
                        continue
            except Exception as _e:
                log.debug("portfolio_sentiment_check_failed", symbol=stock.symbol, error=str(_e))

            # Chronos forecast guard — skip if model predicts down trend with confidence
            try:
                from src.portfolio.models import PortfolioForecast
                from src.core.database import get_db as _get_db
                import datetime as _dt
                today_str = _dt.date.today().strftime("%Y-%m-%d")
                with _get_db() as _db:
                    _fc = _db.query(PortfolioForecast).filter(
                        PortfolioForecast.symbol == stock.symbol,
                        PortfolioForecast.forecast_date == today_str,
                    ).first()
                    if _fc and _fc.trend == "down" and _fc.confidence < 0.05:
                        log.info("portfolio_forecast_skip", symbol=stock.symbol,
                                 trend=_fc.trend, day10=_fc.forecast_day10,
                                 confidence=_fc.confidence)
                        continue
            except Exception as _e:
                log.debug("portfolio_forecast_check_failed", symbol=stock.symbol, error=str(_e))

            if entry_method == "direct_buy":
                success = self._execute_buy(stock, analysis, buy_amount,
                                            funding_source=rs.funding_source,
                                            rank=suggestion_seq,
                                            rank_score=rs.final_rank_score)
                if success:
                    bought.append(stock.symbol)
                    deployable -= buy_amount
            elif entry_method == "put_entry":
                success = self._execute_put_entry(
                    stock, analysis,
                    rank=suggestion_seq,
                    rank_score=rs.final_rank_score,
                    funding_source=rs.funding_source,
                )
                if success:
                    bought.append(f"{stock.symbol}(P)")

        # Step 7: Reinvest dividends
        if self.cfg.reinvest_dividends:
            self._reinvest_dividends(deployable)

        log.info("portfolio_scan_completed", bought=bought, count=len(bought),
                 regime=regime.regime_name, funding=funding_source)
        return bought

    # ── Tier-specific criteria ───────────────────────────────
    # ── Market regime detection ─────────────────────────────────
    # ── Compounder accumulation strategy ─────────────────────
    def run_compounder_scan(self) -> list[str]:
        """
        Long-horizon 10x compounder accumulation (see src/portfolio/compounder.py):
        rank the universe by quality/growth + momentum, build conviction-weighted capped
        targets to the 25/15/60 tier proportions, deploy a base tranche steadily plus a
        crash reserve that fires on market drawdowns, choosing direct-buy vs put-sell by
        price intensity. Hold; never trim winners.
        """
        _ensure_event_loop()
        from src.portfolio import compounder as cmp
        cc = self.cfg.compounder
        log.info("compounder_scan_started")
        bought: list[str] = []

        cash = self._get_available_cash()
        if cash is None:
            log.warning("compounder_cannot_get_cash"); return bought
        nlv = self._get_net_liquidation() or cash
        if not nlv or nlv <= 0:
            log.warning("compounder_zero_nlv"); return bought

        # Resolve expiring/assigned put-entries first
        self._check_put_entries()

        # Market drawdown gauge (SPY) -> crash-reserve tranche state
        spy = self._get_market_price("SPY")
        rstate = self._load_reserve_state()
        dd = 0.0
        if spy and spy > 0:
            rstate, dd = cmp.reserve_update(rstate, spy, tuple(cc.drawdown_tranches))
            self._save_reserve_state(rstate)
        unlocked_dd = cmp.reserve_unlocked_fraction(rstate.tranches_fired, len(cc.drawdown_tranches))
        # Crash dump (deploy the parked cash reserve fast) on ANY real drawdown tranche.
        crash_active = unlocked_dd > 0 and dd >= (cc.drawdown_tranches[0] if cc.drawdown_tranches else 1.0)
        # Capitulation = the DEEPEST tranche has been hit. The margin facility (below) is gated to this,
        # NOT to crash_active — margin is a last-resort supplement, the parked cash reserve is primary.
        deepest_dd = (cc.drawdown_tranches[-1] if cc.drawdown_tranches else 1.0)
        capitulation = dd >= deepest_dd
        # Time-based backstop bleed: if no crash within backstop_start_days, deploy the reserve
        # slowly anyway so we're never permanently under-invested in a melt-up.
        import datetime as _dt
        start_str = self._get_state_value("compounder_start_date")
        if not start_str:
            start_str = _dt.date.today().isoformat()
            self._store_state("compounder_start_date", start_str)
        try:
            days_since = (_dt.date.today() - _dt.date.fromisoformat(start_str)).days
        except Exception:
            days_since = 0
        backstop = cmp.backstop_unlocked_fraction(days_since, cc.backstop_start_days, cc.backstop_bleed_days)
        unlocked = max(unlocked_dd, backstop)

        # ── Leverage gate (margin account) ───────────────────────────────────────────────
        # Cash-FIRST: the deployable base is genuine SETTLED cash (TotalCashValue), NOT the broker's
        # AvailableFunds — the latter already bakes in margin buying power, which silently levered the
        # book in calm markets. Margin is used ONLY when a crash drawdown-tranche is active, bounded by
        # crash_margin_pct, and the whole path is hard-stopped / soft-de-rated by the maintenance-margin
        # level. (The classic path's margin gate never ran for the compounder branch — this wires it in.)
        settled = self._get_settled_cash()
        base_cash = settled if settled is not None else cash
        maint = self._get_maintenance_margin() or 0.0
        margin_pct = (maint / nlv * 100.0) if nlv > 0 else 0.0
        self._store_state("margin_utilization", str(round(margin_pct, 1)))
        hard_limit = cc.margin_hard_limit_crash_pct if capitulation else cc.margin_hard_limit_pct
        if margin_pct > hard_limit:
            log.warning("compounder_margin_gate_blocked", margin_pct=round(margin_pct, 1),
                        limit=hard_limit, crash=crash_active)
            self._store_state("portfolio_blocked_reason",
                              f"margin {margin_pct:.0f}% > {hard_limit:.0f}% limit")
            return bought
        soft_scale = 1.0
        if margin_pct > cc.margin_soft_floor_pct:
            soft_scale = max(0.0, (hard_limit - margin_pct) / (hard_limit - cc.margin_soft_floor_pct))

        # Build ranked universe from live technicals + refreshed fundamental scores
        watch = self._get_watchlist()
        if not watch:
            log.warning("compounder_empty_watchlist"); return bought
        names: list[cmp.NameInput] = []
        analyses: dict[str, tuple] = {}
        skipped_exch: set[str] = set()
        for s in watch:
            if s.exchange in skipped_exch:
                continue
            # Size targets over the FULL universe, but only BUY names whose market is open now. A name
            # whose market is closed still gets a FRESH analysis only if open; otherwise it keeps its
            # last STORED watchlist metrics and stays in the ranking/sizing set. CRITICAL: ranking only
            # the currently-open names splits each tier budget among that handful, ballooning their
            # per-name targets up to the leader cap — an off-hours scan (US closed, only LSE/EU open)
            # then over-buys e.g. AZN as if it were the entire growth tier. The full-universe denominator
            # keeps per-name targets stable; `analyses` (open + freshly priced) gates what we can buy.
            a = None
            if self._is_market_open(s.currency):
                try:
                    a = self.analyzer.analyze_stock(s.symbol, s.exchange, s.currency, tier=s.tier)
                except Exception as e:
                    log.warning("compounder_analyze_error", symbol=s.symbol, error=str(e))
                    a = None
                if a and a.current_price and a.current_price > 0:
                    self._update_watchlist_metrics(s, a)
                    analyses[s.symbol] = (s, a)        # open + freshly priced → eligible to BUY
            # Ranking/sizing input: fresh price if we have it, else the last stored watchlist metrics.
            price = (a.current_price if (a and a.current_price) else s.current_price) or 0.0
            if price <= 0:
                continue                                # never priced → can't rank/size
            names.append(cmp.NameInput(
                symbol=s.symbol, tier=(s.tier or "growth"),
                growth=s.growth_score or 0.0, forward_growth=s.forward_growth_score or 0.0,
                quality=s.quality_score or 0.0, valuation=s.valuation_score or 0.0,
                dividend_total_return=s.dividend_total_return_score or 0.0,
                risk_penalty=s.risk_total_penalty or 0.0,
                price=price,
                sma200=(a.sma_200 if a else s.sma_200),
                high_52w=(a.high_52w if a else s.high_52w),
                momentum_12_1=(getattr(a, "momentum_12_1", None) if a
                               else getattr(s, "momentum_12_1", None)),
            ))
        if not names:
            log.info("compounder_no_priceable_names"); return bought

        ranked = cmp.rank_universe(names, cc.rank_fund_weight, cc.rank_mom_weight)
        rank_idx = {r.symbol: i + 1 for i, r in enumerate(ranked)}
        leaders = cmp.leader_symbols(ranked, cc.leader_top_frac)

        # Targets sized to base + currently-unlocked reserve (full base always live).
        # Compounder uses its own tier budgets (cc.tier_*) so the universe screener / classic
        # strategy aren't affected; leaders carry the higher cap.
        investable = nlv * (1 - cc.cash_buffer_pct)
        live_invest = investable * (cc.base_pct + (1 - cc.base_pct) * unlocked)
        tier_budgets = {
            "breakthrough": cc.tier_breakthrough,
            "dividend": cc.tier_dividend,
            "growth": cc.tier_growth,
        }
        targets = cmp.target_weights(ranked, tier_budgets, live_invest, cc.per_name_cap_pct,
                                     leader_syms=leaders, leader_cap_pct=cc.leader_cap_pct,
                                     conviction_power=cc.conviction_power,
                                     abs_ceiling=cc.per_name_abs_ceiling)
        # Sector cap: keep any single sector's TARGET ≤ sector_cap_pct of NLV. The tier/per-name caps are
        # sector-blind, so a momentum-led growth book could otherwise pile its top names into one sector
        # (AI/semis) — on a margin account that concentrates the drawdown that can trip a maintenance call.
        # New-buy sizing only; held winners are never trimmed.
        sectors = {s.symbol: (getattr(s, "sector", "") or "") for s in watch}
        targets = cmp.apply_sector_caps(targets, sectors, cc.sector_cap_pct * nlv)
        # Per-name order-size bounds scale with NLV (account grows ~$50k → $11M+); flat $5k/$100k
        # blocked deployment below ~$4M. See compounder.single_buy_bounds.
        min_buy, max_buy = cmp.single_buy_bounds(nlv, cc)

        held = self._get_holdings_map()           # symbol -> FILLED market value (excludes the park ETF)
        deployed = sum(held.values())
        parked = self._get_parked_value()         # cash reserve parked in the park ETF (XEON, sellable on demand)
        # Re-price every scan: cancel the prior scan's still-resting compounder BUY orders so each
        # green name is re-evaluated and re-placed at the CURRENT price. Prices move between the
        # 4-hourly scans — a stale DAY limit may no longer fill, or the name may have risen above
        # fair (we'd no longer buy it at all). Mirrors the options side's cancel-then-rescan. Filled
        # shares are holdings (not open orders) so they're untouched; only watchlist names are swept,
        # leaving the cash-park ETF and any manual/non-universe orders alone.
        self._cancel_stale_compounder_buys({getattr(s, "symbol", None) for s in watch})
        # Net resting DAY-limit BUY orders (from earlier scans/days) into both the budget gate and
        # the per-name underweight check, so we don't re-ladder a working name or deploy today's
        # budget twice intraday. Holdings reflect only fills; the orders below are still working.
        from src.portfolio.connection import refresh_portfolio_pending_orders_cache
        try:
            refresh_portfolio_pending_orders_cache()
        except Exception:
            pass
        open_buy = self._open_buy_map()           # symbol -> notional of resting BUY orders
        # Expire orphaned buy cards: a 'submitted' suggestion whose order is no longer working (it died
        # on its own — IBKR-rejected, went Inactive, or filled — NOT cancelled by us above, which the
        # _cancel_stale orphan-cleanup already handles) would otherwise linger 'submitted' (inflating the
        # pending count) until the EOD expires_at sweep. The pending cache was just refreshed, so any
        # working order is in open_buy; a 'submitted' portfolio buy with no working order is a ghost.
        # Portfolio cleans its OWN cards off its OWN account here (never the options-account reconciler).
        self._expire_orphan_buy_suggestions(working_syms=set(open_buy.keys()))
        # Net in compounder buy SUGGESTIONS queued but not yet placed at IBKR (suggestion mode): they
        # aren't resting orders yet, so without this a follow-up scan re-creates them and deploys the
        # day's budget twice. Folding them into open_buy makes deployed_eff, deployed_today, the accrual
        # window, AND the per-name underweight check all account for this in-flight intent.
        for _sym, _notional in self._pending_buy_suggestion_map().items():
            open_buy[_sym] = open_buy.get(_sym, 0.0) + _notional
        deployed_eff = deployed + sum(open_buy.values())
        target_total = sum(targets.values())
        open_put_syms = self._open_put_symbols()
        # Cash-first + bounded crash margin (see the leverage gate above). deployable_cash uses SETTLED
        # cash so once cash hits the buffer the book stops buying in normal regimes (no accidental
        # leverage); a fired tranche adds up to crash_margin_pct×NLV of NEW borrowing, net of any loan
        # already outstanding (negative settled cash). soft_scale de-rates as maint-margin rises.
        buffer_amt = nlv * cc.cash_buffer_pct
        deployable_cash = max(0.0, base_cash - buffer_amt)
        already_borrowed = max(0.0, -base_cash)
        # The cash reserve is PARKED in the park ETF (XEON) for yield; it's sellable on demand, so count it as
        # deployable (the executor un-parks it just-in-time before placing). Without this the cash-first
        # gate would read the parked reserve as "no cash" and starve deployment.
        margin_ok = capitulation if cc.margin_capitulation_only else crash_active
        crash_margin = max(0.0, cc.crash_margin_pct * nlv - already_borrowed) if margin_ok else 0.0
        free_cash = (deployable_cash + parked + crash_margin) * soft_scale
        # Per-day DCA throttle: base_pace is a per-DAY budget but the scan runs every 30 min, so cap
        # the day's TOTAL deployment at base_pace by subtracting what's already gone out today. Measure
        # that from ACTUALLY FILLED buys (PortfolioTransaction), NOT the scan's intended spend — orders
        # that never filled (a stuck HK PendingSubmit, a direct-route-cancelled US name) must not eat
        # the day's budget. The date filter resets it automatically each day.
        from sqlalchemy import func as _func
        from src.portfolio import fx as _pfx
        _fx_rates = _pfx.load_fx_rates()
        _today = datetime.utcnow().strftime("%Y-%m-%d")
        with get_db() as _db:
            # PortfolioTransaction.amount is stored in each fill's LOCAL currency (£ for an LSE fill),
            # so a raw SUM mixes currencies — group by currency and FX-normalise to base. Without this
            # a £4,693 AZN fill counts as 4,693 base instead of ~€5,443, mis-stating the day's pace.
            _fills_rows = _db.query(
                PortfolioTransaction.currency,
                _func.coalesce(_func.sum(PortfolioTransaction.amount), 0.0),
            ).filter(
                PortfolioTransaction.action == "buy",
                PortfolioTransaction.created_at >= _today + " 00:00:00",
                # Parking idle cash into the yield ETF is NOT compounder deployment — exclude it or a
                # single ~€400k park would swamp the day's pace and zero the stock-buy budget.
                PortfolioTransaction.symbol != (self.cfg.cash_yield_symbol or "__none__"),
            ).group_by(PortfolioTransaction.currency).all()
            _fills_today = _pfx.sum_base(_fills_rows, _fx_rates)
        # deployed_today = committed capital today = today's FILLS + still-working BUY orders. Counting
        # open_buy too means a placed order reduces the day's budget immediately (so the budget moves
        # with the buys, not only once they fill), while cancelled/failed orders — which leave open_buy
        # — don't. DAY orders don't survive overnight, so open_buy is today's working notional.
        deployed_today = _fills_today + sum(open_buy.values())
        # Froth throttle: slow the base DCA when SPY is extended above its 200-day trend (deploy slower
        # into euphoria, full speed once at/below trend). Linear ramp from deploy_throttle_start_pct
        # (full pace) to deploy_throttle_full_pct (floor). Never throttles the crash dump. The compounder
        # otherwise ignored the overbought guard entirely and would deploy at full pace into froth.
        pace_throttle = 1.0
        try:
            _, spy_ext = self.analyzer.check_market_overbought(cc.deploy_throttle_full_pct)
            if spy_ext is not None and spy_ext > cc.deploy_throttle_start_pct:
                _span = max(0.1, cc.deploy_throttle_full_pct - cc.deploy_throttle_start_pct)
                _frac = min(1.0, (spy_ext - cc.deploy_throttle_start_pct) / _span)
                pace_throttle = max(cc.deploy_throttle_floor, 1.0 - _frac * (1.0 - cc.deploy_throttle_floor))
        except Exception as e:
            log.warning("compounder_throttle_calc_failed", error=str(e))
        self._store_state("compounder_pace_throttle", str(round(pace_throttle, 2)))
        budget = cmp.daily_deploy_budget(
            investable, cc.base_pct, cc.dca_horizon_days, unlocked,
            deployed_eff, target_total, crash_active, free_cash,
            deployed_today=deployed_today,
            lump_horizon_days=cc.lump_horizon_days, pace_throttle=pace_throttle)

        # Middle road: when the per-DAY pace is below the minimum ORDER size (froth-throttled and/or
        # lump-stretched on a large account — e.g. ~$19k/day vs a $44k min_buy at $11M NLV), the daily
        # budget never reaches min_buy and deployment STALLS (every brick < min_buy → skipped). Bank the
        # allowance over a trailing window long enough to fund ONE min-size order, then deploy a single
        # fee-efficient ~min_buy chunk. Average pace stays ≈ base_pace; orders never go below min_buy, so
        # IBKR commissions stay trivial (~$1-2 on a $44k order). Crash dump is unaffected (full gap).
        remaining_gap = max(0.0, target_total - deployed_eff)
        if not crash_active and budget < min_buy and remaining_gap >= min_buy and free_cash >= min_buy:
            base_pace = cmp.base_daily_pace(investable, cc.base_pct, cc.dca_horizon_days,
                                            remaining_gap, deployed_today,
                                            cc.lump_horizon_days, pace_throttle)
            if base_pace > 0:
                import math
                accrual_days = min(cc.lump_horizon_days, max(1, math.ceil(min_buy / base_pace)))
                cal_window = accrual_days + accrual_days // 2 + 2   # ~×1.5 to span weekends (loose)
                _wstart = (datetime.utcnow().date() - timedelta(days=cal_window)).isoformat() + " 00:00:00"
                with get_db() as _db:
                    _window_rows = _db.query(
                        PortfolioTransaction.currency,
                        _func.coalesce(_func.sum(PortfolioTransaction.amount), 0.0),
                    ).filter(PortfolioTransaction.action == "buy",
                             PortfolioTransaction.created_at >= _wstart,
                             # Exclude park-ETF (XEON) fills like the daily _fills_today query — else
                             # recent multi-million parking buys poison deployed_window and suppress the
                             # accrual top-up that funds a single min-size order.
                             PortfolioTransaction.symbol != (self.cfg.cash_yield_symbol or "__none__"),
                             ).group_by(PortfolioTransaction.currency).all()
                    _fills_window = _pfx.sum_base(_window_rows, _fx_rates)   # FX-normalise (local→base)
                deployed_window = _fills_window + sum(open_buy.values())
                banked = base_pace * accrual_days - deployed_window
                if banked >= min_buy:
                    budget = max(budget, min(remaining_gap, free_cash, banked))
                    log.info("compounder_accrual_budget", base_pace=round(base_pace),
                             accrual_days=accrual_days, banked=round(banked), budget=round(budget))

        # Burn-in deployment cap — FINAL ceiling on this scan's budget. Caps TOTAL committed capital
        # (deployed_eff = filled holdings + working orders + pending suggestions) until the live FX /
        # unpark / order-placement paths are validated at small size. Self-arms off detected deposits and
        # ramps up over burn_in_ramp_days (or an explicit manual cap); 0 = no cap. Applies even to the
        # crash dump (don't lever into a half-trusted execution path). Parking still runs below, so a
        # bound scan parks idle reserve rather than leaving it as drag. See _compounder_burn_in_cap().
        burn_cap = self._compounder_burn_in_cap(cc, investable)
        if burn_cap > 0:
            room_to_cap = max(0.0, burn_cap - deployed_eff)
            if budget > room_to_cap:
                log.info("compounder_burn_in_cap_binding", cap=round(burn_cap),
                         deployed=round(deployed_eff), budget_before=round(budget),
                         budget_after=round(room_to_cap))
                budget = room_to_cap
                self._store_state(
                    "portfolio_blocked_reason",
                    f"burn-in cap ${burn_cap:,.0f} reached (committed ${deployed_eff:,.0f}) — "
                    f"ramping up automatically; raise compounder.burn_in_* to scale faster")

        # Persist state for the dashboard cards
        self._store_state("strategy", "compounder")
        self._store_state("compounder_reserve_peak", str(round(rstate.peak, 2)))
        self._store_state("compounder_drawdown_pct", str(round(dd * 100, 1)))
        self._store_state("compounder_tranches_fired", str(rstate.tranches_fired))
        self._store_state("compounder_reserve_unlocked_pct", str(round(unlocked * 100, 1)))
        self._store_state("compounder_backstop_pct", str(round(backstop * 100, 1)))
        self._store_state("compounder_investable", str(round(investable)))
        self._store_state("compounder_live_target", str(round(target_total)))
        self._store_state("compounder_deployed", str(round(deployed)))
        self._store_state("compounder_daily_budget", str(round(budget)))
        # Header badge: a real scan completed (priceable names existed). The compounder always
        # accumulates, so "normal" = deploying; flag active crash-reserve deployment separately.
        # This block is only reached with priceable names, so the "Awaiting first scan" placeholder
        # persists correctly until the first market-hours scan, then flips and stays (persisted).
        self._store_state("market_status", "crash" if crash_active else "normal")

        log.info("compounder_state", nlv=round(nlv), cash=round(cash), deployed=round(deployed),
                 target_total=round(target_total), budget=round(budget),
                 drawdown_pct=round(dd * 100, 1), tranches=rstate.tranches_fired,
                 crash_active=crash_active, ranked=len(ranked))

        # Always publish the ranking/signals to the dashboard — even with no deploy budget,
        # so /watchlist reflects the current universe ranking & intended actions.
        self._persist_compounder_signals(ranked, targets, held, open_put_syms,
                                         rank_idx, crash_active, cc, leaders)

        # No deploy budget today? Do NOT return here — the deploy loop below naturally no-ops (every
        # brick fails the min_buy floor when budget < min_buy), so no buys happen, but we still fall
        # through to PARK idle reserve cash at the end. Parking is treasury management and must run
        # regardless of whether there's deploy budget this scan; `spent` stays 0, so nothing the loop
        # intends to buy is wrongly withheld from parking.
        if budget < min_buy:
            log.info("compounder_no_budget_today", budget=round(budget), min_buy=round(min_buy))

        # Buy queue — accumulate toward the conviction targets, filling the biggest underweight $ gap
        # first. GREEN names (at/below fair price, attractiveness >= 0) are filled FIRST; YELLOW names
        # (above fair price) are NOT skipped — they're filled LAST, only if budget remains after the
        # greens. So we always make progress toward the targets (time-in-market), preferring better
        # entries first. Put-selling is retired: we fill the target rather than wait to be paid.
        # `cur` is FILLED holdings + resting BUY notional so a working name isn't re-laddered/double-counted.
        queue = []
        for r in ranked:
            if r.symbol not in analyses:
                continue                          # ranked for sizing, but market closed now → can't buy
            tgt = targets.get(r.symbol, 0.0)
            if tgt <= 0:
                continue
            cur = held.get(r.symbol, 0.0) + open_buy.get(r.symbol, 0.0)
            if cur >= tgt * 0.98:
                continue                          # already at/working toward target — hold
            if r.symbol in open_put_syms:
                continue                          # legacy: respect any still-open put on the name
            if _is_permission_blocked(r.symbol):
                continue                          # exchange not permissioned yet — keep it ranked/
                                                  # visible, but don't spend today's budget on a
                                                  # doomed order; auto-retries after the cooldown
            attractiveness = cmp.fair_price_attractiveness(r.price, r.sma200, r.high_52w)
            uw = (tgt - cur) / tgt if tgt > 0 else 0.0
            queue.append((attractiveness, uw, r, tgt, cur))
        # Green (attractiveness >= 0) before yellow (< 0); within each band, biggest underweight $ gap
        # first (gap-to-target convergence — backtested +~1pp CAGR vs quality-rank ordering at equal DD,
        # because it routes marginal cash to the laggards and reaches the conviction targets faster).
        # Quality already lives in the targets (rank_score**conviction_power); the buy order only needs
        # to close the gap, so re-ranking by quality here would double-concentrate the path. (tgt=x[3],
        # cur=x[4]; gap = tgt - cur.)
        queue.sort(key=lambda x: (0 if x[0] >= 0 else 1, -(x[3] - x[4])))

        spent = 0.0
        cash_room = free_cash          # bounds total resting notional placed this scan
        for attractiveness, uw, r, tgt, cur in queue:
            if spent >= budget:
                break
            stock, a = analyses[r.symbol]
            brick = min(max_buy, tgt - cur, budget - spent)
            if brick < min_buy:
                continue
            idx = rank_idx.get(r.symbol, 0)
            is_leader = r.symbol in leaders
            green = attractiveness >= 0
            a.signal_type = "compounder_direct"
            # Conviction: green-underweight names, leaders, and crash tranches bid near market so the
            # position actually fills (Rain is stagnation-averse); extended (yellow) names that aren't
            # urgent bid deeper for a better entry — they're filled only after the greens (queue order).
            urgency = max(uw if green else 0.0, 1.0 if is_leader else 0.0, 1.0 if crash_active else 0.0)
            rat = (
                f"Compounder #{idx} {stock.tier} (10x {r.s10x:.0f}, mom p{r.momentum_pct * 100:.0f}, "
                f"rank {r.rank_score:.0f}). Direct buy ${brick:,.0f} toward target ${tgt:,.0f} "
                f"(now ${cur:,.0f}, {uw * 100:.0f}% underweight). Price ${r.price:.2f} "
                f"{'cheap/fair' if green else 'extended — filled after greens'} vs trend"
                f"{'; LEADER — always direct + dip rungs' if is_leader else ''}"
                f"{'; CRASH tranche active' if crash_active else ''}."
            )
            core_placed, total_placed = self._execute_compounder_buy(
                stock, a, brick, urgency, is_leader, cash_room,
                rank=idx, rank_score=r.rank_score, rationale=rat, min_buy=min_buy)
            if total_placed > 0:
                bought.append(stock.symbol)
                spent += core_placed        # throttle base pace by the core rung only
                cash_room -= total_placed    # dips draw extra cash/reserve, not the base budget

        # Park any standing idle cash (the crash reserve + undeployed slack) into the park ETF (XEON) for yield;
        # the executor un-parks it just-in-time when it's time to deploy. Cash backing this scan's intended
        # buys (`spent`) and still-working orders is left liquid so fills don't open a margin loan.
        self._park_compounder_excess(nlv, spent, daily_budget=budget)

        self._store_state("compounder_last_spent", str(round(spent)))
        # deployed_today is computed live from actual fills each scan (see above) — just persist the
        # current value for the dashboard. Failed/unfilled orders never inflate it.
        self._store_state("compounder_deployed_today", str(round(deployed_today)))
        log.info("compounder_scan_completed", actions=len(bought), spent=round(spent),
                 deployed_today=round(deployed_today), bought=bought)
        return bought

    def _persist_compounder_signals(self, ranked, targets, held, open_put_syms,
                                    rank_idx, crash_active, cc, leaders=None):
        """Write the per-name ranking / targets / intended-action table to PortfolioState
        for the /watchlist dashboard. Called every scan (even with no deploy budget) so the
        dashboard always reflects the current universe ranking."""
        try:
            import json as _json
            from src.portfolio import compounder as cmp
            leaders = leaders or set()
            signals = []
            for r in ranked:
                tgt = targets.get(r.symbol, 0.0)
                cur = held.get(r.symbol, 0.0)
                attractiveness = cmp.fair_price_attractiveness(r.price, r.sma200, r.high_52w)
                uw = (tgt - cur) / tgt if tgt > 0 else 0.0
                if r.symbol in open_put_syms:
                    action = "put_open"
                elif tgt <= 0:
                    action = "—"
                elif cur >= tgt * 0.98:
                    action = "hold"
                elif attractiveness < 0:
                    action = "fill"      # yellow — above fair price; filled after greens, in rank order
                else:
                    action = "direct"    # green & underweight — buy first, in quality-rank order
                signals.append({
                    "symbol": r.symbol, "tier": r.tier, "rank": rank_idx.get(r.symbol, 0),
                    "rank_score": round(r.rank_score, 1), "s10x": round(r.s10x, 1),
                    "mom_pct": round(r.momentum_pct * 100, 0), "price": round(r.price, 2),
                    "target": round(tgt), "current": round(cur),
                    "underweight_pct": round(uw * 100, 0),
                    "attractiveness": round(attractiveness, 3), "action": action,
                })
            self._store_state("compounder_signals", _json.dumps(signals))
        except Exception as e:
            log.warning("compounder_signals_persist_failed", error=str(e))

    def _get_market_price(self, symbol: str = "SPY") -> float | None:
        """Latest price for a market gauge (SPY) via the shared IBKR data path."""
        try:
            # Use the PORTFOLIO connection — calling the options-side
            # get_stock_price here ran on the portfolio loop and raised
            # "This event loop is already running" (cross-connection).
            from src.portfolio.connection import get_portfolio_stock_price
            return get_portfolio_stock_price(symbol, exchange="SMART", currency="USD")
        except Exception as e:
            log.warning("compounder_market_price_failed", symbol=symbol, error=str(e))
            return None

    def _get_state_value(self, key: str) -> str | None:
        with get_db() as db:
            s = db.query(PortfolioState).filter(PortfolioState.key == key).first()
            return s.value if s else None

    def _open_put_symbols(self) -> set[str]:
        with get_db() as db:
            rows = db.query(PortfolioPutEntry).filter(
                PortfolioPutEntry.status == "open"
            ).all()
            return {r.symbol for r in rows}

    def _load_reserve_state(self):
        import json
        from src.portfolio.compounder import ReserveState
        with get_db() as db:
            st = db.query(PortfolioState).filter(
                PortfolioState.key == "compounder_reserve_state"
            ).first()
            if st and st.value:
                try:
                    d = json.loads(st.value)
                    return ReserveState(float(d.get("peak", 0.0)), int(d.get("tranches_fired", 0)))
                except Exception:
                    pass
        return ReserveState(0.0, 0)

    def _save_reserve_state(self, state):
        import json
        self._store_state(
            "compounder_reserve_state",
            json.dumps({"peak": state.peak, "tranches_fired": state.tranches_fired}),
        )

    def _detect_regime(self) -> MarketRegime:
        """Gather SPY + VIX data to determine market regime."""
        _ensure_event_loop()

        spy_data = {}
        vix = None

        try:
            # Get VIX
            from ib_insync import Index
            vix_contract = Index("VIX", "CBOE", "USD")
            with get_portfolio_lock():
                self.ib.qualifyContracts(vix_contract)
                vix_bars = self.ib.reqHistoricalData(
                    vix_contract, endDateTime="", durationStr="15 D",
                    barSizeSetting="1 day", whatToShow="TRADES",
                    useRTH=False, formatDate=1, timeout=8,
                )
            if vix_bars:
                vix = float(vix_bars[-1].close)
                # VIX 10 days ago for stabilization detection
                if len(vix_bars) >= 10:
                    spy_data["vix_10d_ago"] = float(vix_bars[-10].close)
        except Exception as e:
            log.warning("regime_vix_error", error=str(e))

        try:
            # Get SPY data
            spy_contract = Stock("SPY", "SMART", "USD")
            with get_portfolio_lock():
                self.ib.qualifyContracts(spy_contract)
                spy_bars = self.ib.reqHistoricalData(
                    spy_contract, endDateTime="", durationStr="260 D",
                    barSizeSetting="1 day", whatToShow="TRADES",
                    useRTH=False, formatDate=1, timeout=10,
                )
            if spy_bars and len(spy_bars) >= 200:
                closes = [b.close for b in spy_bars]
                highs = [b.high for b in spy_bars]

                spy_data["price"] = closes[-1]
                spy_data["sma_200"] = sum(closes[-200:]) / 200
                spy_data["sma_10"] = sum(closes[-10:]) / 10 if len(closes) >= 10 else closes[-1]
                spy_data["high_52w"] = max(highs[-252:]) if len(highs) >= 252 else max(highs)
        except Exception as e:
            log.warning("regime_spy_error", error=str(e))

        return detect_market_regime(vix, spy_data)

    def _get_holdings_map(self) -> dict[str, float]:
        """Get symbol → market value map for all holdings, in the account BASE currency. Excludes the
        cash-yield ETF (XEON) — that's a parked cash RESERVE, not a deployed compounder position, so it
        must not count toward targets. Market value is stored in the holding's LOCAL currency (£ for an
        LSE name); targets are base-ccy, so FX-normalise here or a foreign holding is mis-measured
        against its target by the FX rate (see src.portfolio.fx)."""
        from src.portfolio import fx as pfx
        park = getattr(self.cfg, "cash_yield_symbol", None)
        rates = pfx.load_fx_rates()
        with get_db() as db:
            holdings = db.query(PortfolioHolding).filter(
                PortfolioHolding.shares > 0
            ).all()
            return {
                h.symbol: pfx.to_base(h.market_value or h.total_invested or 0, h.currency, rates)
                for h in holdings if h.symbol != park
            }

    def _get_parked_value(self) -> float:
        """Market value of the cash reserve parked in the park ETF (XEON), in the account BASE currency
        — sellable on demand, so it counts as deployable cash (the executor un-parks it just-in-time
        before placing a buy). FX-normalise from the holding's stored currency to base (XEON is
        EUR-denominated = base, so this is a no-op today, but stays correct if the park ETF changes)."""
        from src.portfolio import fx as pfx
        park = getattr(self.cfg, "cash_yield_symbol", None)
        if not park or not getattr(self.cfg, "cash_yield_enabled", False):
            return 0.0
        with get_db() as db:
            h = db.query(PortfolioHolding).filter(
                PortfolioHolding.symbol == park, PortfolioHolding.shares > 0
            ).first()
            return pfx.to_base(float(h.market_value or h.total_invested or 0.0), h.currency) if h else 0.0

    def _per_currency_cash(self) -> dict:
        """Map {CCY: cash balance} for the portfolio account (skips the BASE roll-up row). Mirrors
        src.strategy.fx_treasury._per_currency_cash for the options account."""
        out: dict[str, float] = {}
        try:
            with get_portfolio_lock():
                vals = self.ib.accountValues()
            for v in vals:
                if v.tag == "CashBalance" and v.currency and v.currency != "BASE":
                    try:
                        out[v.currency.upper()] = float(v.value)
                    except (ValueError, TypeError):
                        continue
        except Exception as e:
            log.warning("portfolio_per_currency_cash_failed", error=str(e))
        return out

    def manage_fx_treasury(self) -> None:
        """Close any standing NON-BASE currency debit (foreign margin loan) by converting base→ccy, so
        a foreign buy/settlement never leaves us borrowing that currency at margin rates. One-directional
        (never ccy→base) and self-sizing; acts on the LARGEST debit per pass (the next daily run takes the
        next one). Double-gated: no-op unless fx_treasury_enabled; places NO orders while fx_treasury_dry_run
        (burn-in). Reuses the pre-buy funder `_ensure_currency_funding` (target = a small positive buffer,
        so with a negative `have` it converts |debit|+buffer). Mirrors src.strategy.fx_treasury."""
        cfg = self.cfg
        if not getattr(cfg, "fx_treasury_enabled", False) or getattr(cfg, "readonly", False):
            return
        dry = bool(getattr(cfg, "fx_treasury_dry_run", True))
        base = (getattr(cfg, "base_currency", "EUR") or "EUR").upper()
        try:
            nlv = self._get_net_liquidation() or 0.0
            if nlv <= 0:
                return

            from src.strategy.fx_treasury import plan_debit_close, crash_regime_active
            # SUSPENDED during a crash regime (Rain: only close debt when there's NO crash) — a foreign
            # loan carried into a drawdown is intentional leverage; don't de-lever against the crash-buy.
            if crash_regime_active():
                log.info("portfolio_fx_treasury_suspended_crash")
                return

            cash = self._per_currency_cash()
            thr = float(getattr(cfg, "fx_debit_close_threshold_pct", 0.005) or 0.0)
            buf_pct = float(getattr(cfg, "fx_settlement_buffer_pct", 0.005) or 0.0)

            debits = []
            for ccy, bal in cash.items():
                if ccy == base:
                    continue
                dc = plan_debit_close(bal, nlv, thr, buf_pct)
                if dc["act"]:
                    debits.append((ccy, bal, dc))
            if not debits:
                return
            debits.sort(key=lambda x: x[1])           # most-negative balance (largest debit) first
            ccy, ccy_cash, _dc = debits[0]

            # Target a small POSITIVE ccy cushion after the close (rate-free: a fraction of the debit),
            # so _ensure_currency_funding converts |debit| + cushion and lands slightly positive.
            target_ccy = abs(ccy_cash) * buf_pct
            # EUR to free from the XEON park before the SELL-EUR leg (est. from cached FX; 0 if unknown).
            import src.portfolio.fx as pfx
            base_per_ccy = pfx.to_base(1.0, ccy)
            need_base = (abs(ccy_cash) + target_ccy) * base_per_ccy if base_per_ccy > 0 else 0.0

            alerts = None
            try:
                from src.core.alerts import get_alert_manager
                alerts = get_alert_manager()
            except Exception:
                pass

            log.info("portfolio_fx_treasury_debit", dry_run=dry, ccy=ccy,
                     ccy_cash=round(ccy_cash), target_ccy=round(target_ccy),
                     est_eur=round(need_base), nlv=round(nlv),
                     other_debits=[c for c, _, _ in debits[1:]])

            if dry:
                if alerts:
                    alerts.treasury_alert(
                        f"{ccy} debit auto-close (dry-run)",
                        f"{ccy} balance {ccy_cash:,.0f} (debit) → would convert ≈€{need_base:,.0f} "
                        f"EUR→{ccy} to clear it (+ small buffer). No order placed (burn-in).",
                        dry_run=True,
                    )
                return

            # ARMED: free EUR from the XEON park first (best-effort), then convert base→ccy (fail-closed).
            if need_base > 0:
                _unpark_yield(self.ib, cfg, need_base, settle_ccy=base)
            ok = _ensure_currency_funding(self.ib, ccy, base, target_ccy, cfg=cfg)
            log.info("portfolio_fx_treasury_debit_close", ccy=ccy, ok=ok,
                     ccy_cash=round(ccy_cash), nlv=round(nlv))
            if alerts:
                msg = (f"{ccy} balance {ccy_cash:,.0f} (debit) → convert EUR→{ccy} to clear "
                       f"(target +{target_ccy:,.0f}).\n{'OK' if ok else 'FAILED — check manually'}")
                (alerts.treasury_alert(f"{ccy} debit auto-close", msg, dry_run=False) if ok
                 else alerts.critical(f"Portfolio FX: {ccy} debit close FAILED", msg))
        except Exception as e:
            log.error("portfolio_fx_treasury_error", error=str(e))

    def _park_compounder_excess(self, nlv: float, spent_this_scan: float, daily_budget: float = 0.0) -> None:
        """Hold the base-currency cash line at the deploy runway, parking or un-parking the ETF to suit.

        Idle cash above the runway is parked for yield; a runway that has drained below it is refilled
        out of the park. Cash committed to in-flight buys (resting orders + suggestion cards) is never
        parked out from under them, which would open a margin loan.

        Un-parking here — not only just-in-time in the executor — is what makes non-European sessions
        tradeable at all: the park's venue (Xetra) is shut while Tokyo/HK/Sydney trade, so a JIT sale
        during those sessions can never fill. Both legs are no-ops when the park venue is closed.

        `spent_this_scan` is retained for the log line only; it is already inside the in-flight total.
        Best-effort, gated on cash_yield_enabled + not-readonly."""
        cc = self.cfg.compounder
        if not getattr(self.cfg, "cash_yield_enabled", False) or getattr(self.cfg, "readonly", False):
            return
        try:
            # In-flight buys must be composed EXACTLY as the budget gate composes them (resting IBKR
            # orders + queued/approved/executing suggestion cards). Reading only _open_buy_map here was
            # blind to suggestion cards, so in suggestion_mode this parked the very cash an approved buy
            # was about to draw — the loan this function's docstring promises to avoid — and, now that
            # the same figure drives the un-park, it would under-restore the runway. Same lesson as the
            # park-symbol exclusion: every path feeding one gate has to agree.
            open_buy_now = sum(self._open_buy_map().values()) \
                + sum(self._pending_buy_suggestion_map().values())
            # Keep a cash RUNWAY un-parked so routine daily buys fund from cash (via _unpark_yield's
            # cash-first path) rather than selling the ETF each time. By the time the runway drains and
            # the ETF must be sold, its NAV has accrued past the buy-side spread, so the sale isn't at a
            # loss. Runway = park_reserve_days × the day's deploy budget, floored by the NLV buffer.
            _reserve_days = int(getattr(cc, "park_reserve_days", 10) or 0)
            _deploy_runway = _reserve_days * max(0.0, daily_budget)
            _reserve = max(nlv * cc.cash_buffer_pct, _deploy_runway)
            _park_min = getattr(cc, "cash_park_min", 5000.0)

            # Both legs key off the BASE-CURRENCY CASH LINE, never TotalCashValue. The park ETF is
            # bought and sold in EUR, and a foreign buy's IDEALPRO conversion sells EUR — so EUR is the
            # only balance that matters here. Reading the base-converted total instead let the parker
            # see "excess" that lived in the USD/GBP lines and buy XEON with EUR on margin.
            base_ccy = (getattr(self.cfg, "base_currency", "EUR") or "EUR").upper()
            base_cash = _ccy_cash(self.ib, base_ccy)
            if base_cash is None:
                return                     # cash line unreadable — never park or un-park on a guess
            # `spent_this_scan` is NOT added: every buy it counts was just written as a suggestion card
            # (suggestion_mode) or placed as a resting order (direct mode), so it is already inside
            # open_buy_now. Adding it again inflated `target` by up to a full day's deployment, which the
            # new un-park leg would have answered by selling park shares to raise cash nothing needed.
            target = _reserve + open_buy_now

            excess = base_cash - target
            if excess >= _park_min:
                self._park_cash(amount=excess)
                return

            # Runway is SHORT — top it back up out of the park. Without this the pair is one-way: cash
            # only ever flows INTO the ETF, and the runway is rebuilt solely by JIT sales in the
            # executor. A JIT sale can't work outside Xetra hours, so once the runway drained the EUR
            # line sat near zero (~€3.5k) all night and every Asian buy died at the FX leg with Error
            # 201 — Tokyo/HK/Sydney all close before Xetra opens, so those sessions can never fund
            # themselves JIT. Restoring the runway during EU hours is what makes them fundable at all.
            # The `_park_min` deadband on both sides stops a park→un-park round-trip on small drifts.
            if excess > -_park_min or not _market_open(base_ccy):
                return
            log.info("compounder_runway_restore", base_ccy=base_ccy, target=round(target),
                     cash=round(base_cash), short=round(-excess), spent=round(spent_this_scan))
            _unpark_yield(self.ib, self.cfg, target, settle_ccy=base_ccy)
        except Exception as e:
            log.warning("compounder_park_excess_error", error=str(e))

    def _cancel_stale_compounder_buys(self, watchlist_symbols: set) -> int:
        """Cancel still-resting compounder stock BUY orders so the scan re-prices them at the current
        market. Only watchlist symbols are touched (the cash-park ETF and any non-universe/manual
        orders are left alone); filled shares are holdings, not open orders, so they survive. Only
        working/unfilled BUY orders are cancelled — the scan then re-places the names that are still
        green & underweight at the current price (and simply doesn't re-place ones now above fair)."""
        cancelled = 0
        cancelled_syms: set[str] = set()
        try:
            _ensure_event_loop()
            park_sym = getattr(self.cfg, "cash_yield_symbol", None)
            # NOTE: read ib.trades() (ALL tracked orders), not ib.openTrades() — ib_insync treats
            # 'Inactive' as a done-state and HIDES it from openTrades(), so an order that IBKR parked
            # as Inactive (rejected/held at a foreign venue, e.g. a stuck BVME PendingSubmit) was
            # invisible to both this sweep AND the pending cache, and could never be cancelled (the
            # user can't cancel an API order from TWS either). And do NOT skip 'Inactive' — attempt to
            # cancel it (harmless if already dead). Truly-final states are still skipped.
            _TERMINAL = {"Filled", "Cancelled", "ApiCancelled", "PendingCancel"}
            with get_portfolio_lock():
                trades = list(self.ib.trades())
            for t in trades:
                c = getattr(t, "contract", None)
                o = getattr(t, "order", None)
                st = getattr(getattr(t, "orderStatus", None), "status", "") or ""
                if c is None or o is None:
                    continue
                sym = getattr(c, "symbol", None)
                if getattr(c, "secType", "") != "STK" \
                        or str(getattr(o, "action", "")).upper() != "BUY":
                    continue
                if sym not in watchlist_symbols or sym == park_sym or st in _TERMINAL:
                    continue
                try:
                    with get_portfolio_lock():
                        self.ib.cancelOrder(o)
                except Exception as ce:
                    # Cancelling an already-dead Inactive order can raise — log and keep sweeping
                    # the rest rather than aborting the whole pass on one stuck order.
                    log.warning("compounder_cancel_stale_buy_failed", symbol=sym,
                                order_id=getattr(o, "orderId", None), status=st, error=str(ce))
                    continue
                cancelled += 1
                cancelled_syms.add(sym)
                log.info("compounder_cancel_stale_buy", symbol=sym,
                         order_id=getattr(o, "orderId", None), status=st)
                # If the order we're re-pricing NEVER reached the exchange — still 'PendingSubmit'
                # (IBKR took it but never transmitted, e.g. missing venue rights/market-data) or
                # 'Inactive' (rejected/held) a full scan-cycle after placement — it can't fill at ANY
                # price. Mark the name venue-blocked so the deploy queue SKIPS it and routes the budget
                # to the next fillable buy, instead of churning ever-higher orders that never fill.
                # Auto-retries after the cooldown and on restart (in case the rights get approved).
                if st in ("PendingSubmit", "Inactive"):
                    _mark_permission_blocked(sym, hours=6.0)
                    log.warning("compounder_order_never_reached_exchange", symbol=sym, status=st,
                                note="venue rights/market-data likely missing — skipping, budget to next")
            if cancelled:
                with get_portfolio_lock():
                    self.ib.sleep(2)   # let the cancels settle before the scan reads open orders
        except Exception as e:
            log.warning("compounder_cancel_stale_buys_failed", error=str(e))
        # Cancelling the order orphans its suggestion: the card stays 'submitted' with no live order
        # (a portfolio-side ghost) until the EOD expires_at sweep. Expire it here so the dashboard's
        # pending count stays honest and the portfolio cleans its OWN ghosts (the options-account
        # reconciler must never touch portfolio suggestions — see jobs.py). The scan re-creates a fresh
        # suggestion below for any name still underweight, so this only clears the now-dead one.
        if cancelled_syms:
            try:
                from src.core.suggestions import TradeSuggestion
                with get_db() as db:
                    ghosts = db.query(TradeSuggestion).filter(
                        TradeSuggestion.source == "portfolio",
                        TradeSuggestion.action == "buy_stock",
                        TradeSuggestion.status == "submitted",
                        TradeSuggestion.symbol.in_(cancelled_syms),
                    ).all()
                    for g in ghosts:
                        g.status = "expired"
                        g.review_note = "Order cancelled by scan re-price"
                    if ghosts:
                        log.info("compounder_expired_orphan_suggestions", count=len(ghosts),
                                 symbols=sorted({g.symbol for g in ghosts}))
            except Exception as e:
                log.warning("compounder_expire_orphan_suggestions_failed", error=str(e))
        if cancelled:
            log.info("compounder_stale_buys_cancelled", count=cancelled)
        return cancelled

    def _expire_orphan_buy_suggestions(self, working_syms: set) -> int:
        """Expire 'submitted' portfolio buy_stock cards that no longer have a working IBKR order.

        Complements the _cancel_stale orphan-cleanup (which handles orders WE cancelled to re-price):
        this catches orders that died on their OWN — IBKR-rejected, went Inactive, or filled — which
        would otherwise leave the card 'submitted' (inflating the pending count) until the EOD
        expires_at sweep. `working_syms` = symbols with a live BUY order this scan (from the just-
        refreshed pending cache). A filled buy is normally already marked 'executed' by the fill sync,
        so it isn't 'submitted' here; expiring the card never cancels the IBKR order or moves budget
        (open_buy reflects live orders), so a rare timing mismatch is cosmetic and self-corrects."""
        from src.core.suggestions import TradeSuggestion
        n = 0
        try:
            with get_db() as db:
                rows = db.query(TradeSuggestion).filter(
                    TradeSuggestion.source == "portfolio",
                    TradeSuggestion.action == "buy_stock",
                    TradeSuggestion.status == "submitted",
                ).all()
                for s in rows:
                    if s.symbol not in working_syms:
                        s.status = "expired"
                        s.review_note = "No live order (filled/cancelled/rejected)"
                        n += 1
                if n:
                    log.info("compounder_expired_dead_order_suggestions", count=n)
        except Exception as e:
            log.warning("compounder_expire_orphan_suggestions_failed", error=str(e))
        return n

    def _open_buy_map(self) -> dict[str, float]:
        """symbol → notional of currently-RESTING stock BUY orders at IBKR, in the account BASE currency.

        Holdings reflect only FILLED shares (sync truth), so resting DAY-limit rungs from an
        earlier scan/day are invisible to _get_holdings_map. Netting this into both the per-name
        underweight check and the daily-budget gate prevents (a) re-laddering a name that already
        has working orders and (b) deploying today's budget twice intraday. Reads the pending-order
        cache; caller should refresh it first so the snapshot is current.
        """
        from src.portfolio.connection import get_cached_portfolio_pending_orders
        from src.portfolio import fx as pfx
        rates = pfx.load_fx_rates()
        park = getattr(self.cfg, "cash_yield_symbol", None)
        out: dict[str, float] = {}
        for o in get_cached_portfolio_pending_orders() or []:
            try:
                if o.get("sec_type") != "STK" or o.get("action") != "BUY":
                    continue
                # A resting BUY of the cash-yield PARK ETF (XEON) is idle-cash parking, NOT compounder
                # deployment — mirror the fills-side exclusion (see the _fills_today query). Counting it
                # would inflate deployed_today / deployed_eff and zero the stock-buy budget: a single
                # ~€1.9M resting park order (or the leftover XEON target-bug order) starves real names.
                if park and o.get("symbol") == park:
                    continue
                rem = float(o.get("remaining") or 0)
                px = float(o.get("limit_price") or 0)
                if rem <= 0 or px <= 0:
                    continue
                ccy = (o.get("currency") or "").upper()
                # LSE/GBP orders quote in PENCE — convert to pounds first so the notional is in the
                # order's major LOCAL unit, then FX-normalise local→base so it matches holdings/targets
                # (all base-ccy). Without the pence step an AZN order counts ~100× its value (14222 × 33
                # ≈ 469k) and swamps the daily budget; without the FX step a foreign order is mis-scaled.
                if ccy == "GBP":
                    px = px / 100.0
                out[o["symbol"]] = out.get(o["symbol"], 0.0) + pfx.to_base(rem * px, ccy, rates)
            except Exception:
                continue
        return out

    def _pending_buy_suggestion_map(self) -> dict[str, float]:
        """symbol → notional ($) of portfolio buy_stock suggestions that are queued/approved/executing
        but NOT yet resting at IBKR. In suggestion_mode the compounder writes a buy card each scan and a
        separate 30s executor places it later; between those two moments no IBKR order exists, so the
        order is invisible to _open_buy_map — and a follow-up scan would re-create it and deploy the day's
        budget twice. Netting these in (alongside resting orders) closes that window so the budget gate,
        deployed_today, and the per-name underweight check all see in-flight intent. Empty in live-direct
        mode (no pending cards). Conservative: an order momentarily both 'executing' here AND already
        resting at IBKR is double-counted for ~2s, which only slightly slows deployment (the safe way)."""
        from src.core.suggestions import TradeSuggestion
        from src.portfolio import fx as pfx
        rates = pfx.load_fx_rates()
        park = getattr(self.cfg, "cash_yield_symbol", None)
        out: dict[str, float] = {}
        try:
            with get_db() as db:
                # symbol → currency for the universe, so a foreign suggestion's notional (stored in its
                # LOCAL currency: est_cost = shares × local price) is FX-normalised to base like holdings.
                ccy_map = {w.symbol: (w.currency or "USD")
                           for w in db.query(PortfolioWatchlist).all()}
                rows = db.query(TradeSuggestion).filter(
                    TradeSuggestion.source == "portfolio",
                    TradeSuggestion.action == "buy_stock",
                    TradeSuggestion.status.in_(("pending", "approved", "queued", "executing")),
                    # Parking the cash-yield ETF (XEON) is not compounder deployment — exclude it from the
                    # budget gate exactly like resting park orders and filled park buys (line ~789).
                    TradeSuggestion.symbol != (park or "__none__"),
                ).all()
                for s in rows:
                    notional = float(s.est_cost or 0.0)
                    if notional <= 0 and s.quantity and s.limit_price:
                        notional = float(s.quantity) * float(s.limit_price)
                    if notional > 0:
                        out[s.symbol] = out.get(s.symbol, 0.0) + pfx.to_base(
                            notional, ccy_map.get(s.symbol, "USD"), rates)
        except Exception as e:
            log.warning("compounder_pending_suggestion_map_failed", error=str(e))
        return out

    def _compounder_burn_in_cap(self, cc, investable: float) -> float:
        """Active burn-in ceiling on TOTAL committed capital (0 = disabled / no cap).

        A manual `burn_in_max_deployed` > 0 always wins (explicit operator cap). Otherwise, when
        `burn_in_auto_arm` is set, the cap SELF-ARMS off authoritative deposit data: when cumulative
        deposits (PortfolioCapitalInjection, NOT NLV — which moves with the market) jump by at least
        `burn_in_trigger_deposit`, a large lump is landing → hold deployment to `burn_in_floor` and ramp
        the ceiling linearly to full (`investable`) over `burn_in_ramp_days`, then auto-disarm. So an
        $11M arriving ~$1M/day deploys at a controlled, self-scaling pace through the freshly-live
        funding paths, while the small pre-deposit account (no big deposit) is never throttled.

        State (PortfolioState): compounder_burn_in_deposits_seen (last cumulative total — baselined on
        the first scan so PRE-EXISTING deposits never arm) and compounder_burn_in_armed_date.
        """
        manual = getattr(cc, "burn_in_max_deployed", 0.0) or 0.0
        if manual > 0:
            return manual
        if not getattr(cc, "burn_in_auto_arm", False):
            return 0.0

        import datetime as _dt
        from sqlalchemy import func as _func
        from src.portfolio.models import PortfolioCapitalInjection
        try:
            with get_db() as _db:
                total_dep = float(_db.query(
                    _func.coalesce(_func.sum(PortfolioCapitalInjection.amount_usd), 0.0)
                ).scalar() or 0.0)
        except Exception as e:
            log.warning("compounder_burn_in_deposit_read_failed", error=str(e))
            return 0.0

        seen_str = self._get_state_value("compounder_burn_in_deposits_seen")
        armed_str = self._get_state_value("compounder_burn_in_armed_date")
        # First ever scan: baseline the seen-total so deposits already in the account don't arm a burn-in.
        if seen_str is None:
            self._store_state("compounder_burn_in_deposits_seen", str(round(total_dep, 2)))
            return 0.0
        seen = float(seen_str or 0.0)
        trigger = getattr(cc, "burn_in_trigger_deposit", 500000.0)
        if total_dep - seen >= trigger:
            # A large new deposit landed — re-baseline, and arm the burn-in if not already armed (an
            # already-running ramp keeps its original clock so successive daily deposits don't reset it).
            self._store_state("compounder_burn_in_deposits_seen", str(round(total_dep, 2)))
            if not armed_str:
                armed_str = _dt.date.today().isoformat()
                self._store_state("compounder_burn_in_armed_date", armed_str)
                log.info("compounder_burn_in_armed", new_deposit=round(total_dep - seen),
                         total_deposits=round(total_dep), floor=getattr(cc, "burn_in_floor", 250000.0),
                         ramp_days=getattr(cc, "burn_in_ramp_days", 21))

        if not armed_str:
            return 0.0
        floor = getattr(cc, "burn_in_floor", 250000.0)
        ramp_days = max(1, int(getattr(cc, "burn_in_ramp_days", 21)))
        try:
            days = (_dt.date.today() - _dt.date.fromisoformat(armed_str)).days
        except Exception:
            days = 0
        if days >= ramp_days:
            # Window elapsed — the live paths have run for the full burn-in; disarm so the cap lifts.
            self._store_state("compounder_burn_in_armed_date", "")
            log.info("compounder_burn_in_complete", days=days, ramp_days=ramp_days)
            return 0.0
        from src.portfolio import compounder as _cmp
        cap = _cmp.burn_in_ceiling(days, ramp_days, floor, investable)
        self._store_state("compounder_burn_in_cap", str(round(cap)))
        self._store_state("compounder_burn_in_day", f"{days}/{ramp_days}")
        return cap

    def _get_tier_values(self) -> dict[str, float]:
        """Get total value per tier."""
        with get_db() as db:
            holdings = db.query(PortfolioHolding).filter(
                PortfolioHolding.shares > 0
            ).all()
            tier_vals: dict[str, float] = {"dividend": 0, "breakthrough": 0, "growth": 0}
            for h in holdings:
                val = h.market_value or h.total_invested or 0
                tier = getattr(h, 'tier', 'growth') or 'growth'
                tier_vals[tier] = tier_vals.get(tier, 0) + val
            return tier_vals

    def _get_sector_counts(self) -> dict[str, int]:
        """Get count of holdings per sector."""
        with get_db() as db:
            holdings = db.query(PortfolioHolding).filter(
                PortfolioHolding.shares > 0
            ).all()
            counts: dict[str, int] = {}
            for h in holdings:
                sector = getattr(h, 'sector', '') or ''
                if sector:
                    counts[sector] = counts.get(sector, 0) + 1
            return counts

    def _get_tier_criteria(self, tier: str) -> tuple[float, float]:
        """Return (min_discount_pct, rsi_oversold) for the given tier."""
        if tier == "dividend":
            return self.cfg.dividend_min_discount_pct, self.cfg.dividend_rsi_oversold
        elif tier == "breakthrough":
            return self.cfg.breakthrough_min_discount_pct, self.cfg.breakthrough_rsi_oversold
        else:  # growth
            return self.cfg.min_discount_pct, self.cfg.rsi_oversold

    # ── Entry method decision ────────────────────────────────
    def _choose_entry_method(self, stock: PortfolioWatchlist, analysis: StockAnalysis) -> str:
        """
        Decide: buy directly or sell a put at target price?

        Rules:
        - If put-entry is disabled → always direct buy
        - Composite score > 75 (very strong signal) → direct buy
        - Deep discount (>15% below SMA) → direct buy (rare opportunity)
        - RSI < 20 → direct buy (extreme oversold, grab it)
        - Volume surge + trend healthy → direct buy (capitulation in uptrend)
        - Otherwise → sell put at target strike (get paid to wait)
        """
        if not self.cfg.put_entry.enabled:
            return "direct_buy"

        # Very strong composite signal → buy now
        # Threshold 75 chosen post-rebalance: under the new 30/70 raw/quality
        # blend, composite=75 requires raw_score>=15, ensuring direct-buy
        # candidates always have at least some price-side entry signal,
        # not just high quality at exact-SMA pricing. Below 75 → CSP path.
        if analysis.composite_score > 75:
            log.info("portfolio_direct_buy_strong_signal", symbol=stock.symbol,
                     score=round(analysis.composite_score, 1))
            return "direct_buy"

        # Deep discount or extreme oversold → buy now, don't wait
        if analysis.discount_pct and analysis.discount_pct > 15:
            log.info("portfolio_direct_buy_deep_discount", symbol=stock.symbol,
                     discount=round(analysis.discount_pct, 1))
            return "direct_buy"

        if analysis.rsi_14 and analysis.rsi_14 < 20:
            log.info("portfolio_direct_buy_extreme_oversold", symbol=stock.symbol,
                     rsi=round(analysis.rsi_14, 1))
            return "direct_buy"

        # Capitulation in an uptrend → high conviction entry
        if analysis.volume_surge and analysis.trend_healthy:
            log.info("portfolio_direct_buy_capitulation", symbol=stock.symbol)
            return "direct_buy"

        # Otherwise → sell put at target price (get paid to wait)
        return "put_entry"

    # ── Put-entry execution ──────────────────────────────────
    def _execute_put_entry(self, stock: PortfolioWatchlist, analysis: StockAnalysis,
                           rank: int = 0, rank_score: float = 0.0,
                           funding_source: str = "cash",
                           rationale: str | None = None,
                           target_discount_override: float | None = None) -> bool:
        """
        Sell a cash-secured put at the target buy price.
        Strike = current price * (1 - target_discount_pct/100)
        """
        _ensure_event_loop()

        if not analysis.current_price or analysis.current_price <= 0:
            return False

        try:
            # Calculate target strike (compounder can pass a deeper discount for
            # extended names via target_discount_override)
            target_discount = (
                target_discount_override if target_discount_override is not None
                else self.cfg.put_entry.target_discount_pct
            ) / 100
            target_strike = analysis.current_price * (1 - target_discount)

            # Find available option chain
            contract = Stock(stock.symbol, stock.exchange, stock.currency)
            with get_portfolio_lock():
                self.ib.qualifyContracts(contract)

                chains = self.ib.reqSecDefOptParams(
                    stock.symbol, '', 'STK', contract.conId
                )
            if not chains:
                log.debug("portfolio_no_option_chains", symbol=stock.symbol)
                return False

            # Find SMART exchange chain
            smart_chains = [c for c in chains if c.exchange == 'SMART']
            chain = smart_chains[0] if smart_chains else chains[0]

            # Find best expiration in our DTE range
            today = datetime.now().date()
            best_exp = None
            for exp_str in sorted(chain.expirations):
                from datetime import datetime as dt
                exp_date = dt.strptime(exp_str, "%Y%m%d").date()
                dte = (exp_date - today).days
                if self.cfg.put_entry.min_dte <= dte <= self.cfg.put_entry.max_dte:
                    best_exp = exp_str
                    break  # take earliest in range

            if not best_exp:
                log.debug("portfolio_no_expiry_in_range", symbol=stock.symbol)
                return False

            # Find closest strike to our target
            available_strikes = sorted(chain.strikes)
            best_strike = min(available_strikes, key=lambda s: abs(s - target_strike))

            # Only use strikes below current price (OTM puts)
            if best_strike >= analysis.current_price:
                otm_strikes = [s for s in available_strikes if s < analysis.current_price]
                if not otm_strikes:
                    return False
                best_strike = min(otm_strikes, key=lambda s: abs(s - target_strike))

            # Create and qualify the option contract
            opt = Option(stock.symbol, best_exp, best_strike, 'P',
                         chain.exchange, currency=stock.currency)
            with get_portfolio_lock():
                qualified = self.ib.qualifyContracts(opt)
            if not qualified or opt.conId <= 0:
                return False

            # Live bid only — no BS. Snapshot quote for the option we are about to sell.
            from src.broker.market_data import _ensure_market_data_type
            _ensure_market_data_type()
            with get_portfolio_lock():
                ticker = self.ib.reqMktData(opt, "", True, False)
                self.ib.sleep(2)
                self.ib.cancelMktData(opt)
            real_bid = ticker.bid
            if not real_bid or real_bid <= 0 or real_bid == float("inf") or real_bid == -1.0:
                log.info("portfolio_no_live_bid_skipping", symbol=stock.symbol,
                         strike=best_strike, expiry=best_exp)
                return False
            sell_price = round(real_bid, 2)
            if sell_price < 0.05:
                return False

            # Suggestion mode — create approval request
            if self.cfg.suggestion_mode:
                from src.core.suggestions import create_suggestion
                effective_cost = best_strike - sell_price
                create_suggestion(
                    symbol=stock.symbol,
                    action="sell_put",
                    quantity=self.cfg.put_entry.max_contracts,
                    limit_price=round(sell_price, 2),
                    order_type="LMT",
                    strike=best_strike,
                    expiry=best_exp,
                    right="P",
                    source="portfolio",
                    tier=stock.tier,
                    signal=f"put_entry_{analysis.signal_type}",
                    rationale=rationale or (
                        f"Rank #{rank} (score {rank_score:.0f}). "
                        f"Sell CSP to enter {stock.symbol} at effective ${effective_cost:.2f}. "
                        f"Strike ${best_strike}, premium ${sell_price:.2f}, "
                        f"expiry {best_exp}. "
                        f"Stock {analysis.discount_pct:.1f}% below SMA, RSI {analysis.rsi_14:.0f}"
                    ),
                    current_price=analysis.current_price,
                    sma_200=analysis.sma_200,
                    rsi_14=analysis.rsi_14,
                    est_cost=round(best_strike * self.cfg.put_entry.max_contracts * 100, 2),
                    rank=rank,
                    rank_score=rank_score,
                    funding_source=funding_source,
                )
                log.info("portfolio_put_suggestion_created",
                         symbol=stock.symbol,
                         strike=best_strike,
                         expiry=best_exp,
                         premium=round(sell_price, 2))
                return True

            # Live mode — place the order
            contracts = self.cfg.put_entry.max_contracts
            order = LimitOrder('SELL', contracts, round(sell_price, 2))
            order.tif = 'GTC'  # Good till cancelled — we want this to fill
            order.outsideRth = True  # allow extended hours fills
            with get_portfolio_lock():
                trade = self.ib.placeOrder(opt, order)
                self.ib.sleep(2)

            # Refresh dashboard cache so new order appears immediately
            try:
                from src.portfolio.connection import refresh_portfolio_pending_orders_cache
                refresh_portfolio_pending_orders_cache()
            except Exception:
                pass

            # Record in database
            effective_cost = best_strike - sell_price
            with get_db() as db:
                entry = PortfolioPutEntry(
                    symbol=stock.symbol,
                    tier=stock.tier,
                    exchange=stock.exchange,
                    currency=stock.currency,
                    strike=best_strike,
                    expiry=best_exp,
                    contracts=contracts,
                    premium=round(sell_price, 2),
                    total_premium=round(sell_price * contracts * 100, 2),
                    status="open",
                    effective_cost=round(effective_cost, 2),
                )
                db.add(entry)

                # Update watchlist
                wl = db.query(PortfolioWatchlist).filter(
                    PortfolioWatchlist.symbol == stock.symbol
                ).first()
                if wl:
                    wl.has_open_put = True
                    wl.put_strike = best_strike
                    wl.put_expiry = best_exp
                    wl.target_buy_price = effective_cost

            self._record_transaction(
                symbol=stock.symbol,
                action="sell_put",
                shares=0,
                price=sell_price,
                amount=round(sell_price * contracts * 100, 2),
                currency=stock.currency,
                signal=f"put_entry_{stock.tier}",
                strike=best_strike,
                expiry=best_exp,
                premium_collected=round(sell_price * contracts * 100, 2),
                tier=stock.tier,
                notes=f"Target entry at ${effective_cost:.2f} (strike ${best_strike} - premium ${sell_price:.2f})",
            )

            log.info("portfolio_put_entry_placed",
                     symbol=stock.symbol,
                     tier=stock.tier,
                     strike=best_strike,
                     expiry=best_exp,
                     premium=round(sell_price, 2),
                     effective_cost=round(effective_cost, 2))

            return True

        except Exception as e:
            log.error("portfolio_put_entry_error", symbol=stock.symbol, error=str(e))
            return False

    # ── Check put-entry status ───────────────────────────────
    def _check_put_entries(self):
        """Check open put-entries for assignment or expiry."""
        _ensure_event_loop()

        with get_db() as db:
            open_puts = db.query(PortfolioPutEntry).filter(
                PortfolioPutEntry.status == "open"
            ).all()

        if not open_puts:
            return

        today = datetime.now().date()

        for entry in open_puts:
            try:
                from datetime import datetime as dt
                exp_date = dt.strptime(entry.expiry, "%Y%m%d").date()

                if today > exp_date:
                    # Expired — check if assigned by looking at portfolio positions
                    with get_portfolio_lock():
                        positions = self.ib.positions()
                    assigned = any(
                        p.contract.symbol == entry.symbol and
                        p.position > 0 and
                        isinstance(p.contract, Stock)
                        for p in positions
                    )

                    if assigned:
                        self._handle_put_assignment(entry)
                    else:
                        self._handle_put_expiry(entry)

            except Exception as e:
                log.warning("portfolio_put_check_error",
                            symbol=entry.symbol, error=str(e))

    def _handle_put_assignment(self, entry: PortfolioPutEntry):
        """Put was assigned — record the stock acquisition."""
        shares = entry.contracts * 100
        effective_cost = entry.strike - entry.premium

        with get_db() as db:
            pe = db.query(PortfolioPutEntry).filter(
                PortfolioPutEntry.id == entry.id
            ).first()
            # Idempotency guard (mirrors wheel d9550e6): if this put-entry was
            # already processed as an assignment — a concurrent _check_put_entries
            # run or a retry — skip. Re-running double-adds holding shares (self-
            # heals via the IBKR holdings sync) AND appends a duplicate
            # put_assigned transaction (append-only, does NOT self-heal). This is
            # a reporting guard only; no order logic lives in this method.
            if pe and pe.status == "assigned":
                log.info("portfolio_put_assignment_already_processed",
                         symbol=entry.symbol, entry_id=entry.id,
                         note="skipping duplicate holding + transaction")
                return
            if pe:
                pe.status = "assigned"
                pe.assigned_shares = shares
                pe.effective_cost = effective_cost
                pe.closed_at = datetime.utcnow()

            # Create/update holding
            h = db.query(PortfolioHolding).filter(
                PortfolioHolding.symbol == entry.symbol
            ).first()

            if h:
                total_cost = h.avg_cost * h.shares + effective_cost * shares
                h.shares += shares
                h.avg_cost = total_cost / h.shares if h.shares > 0 else 0
                h.total_invested += effective_cost * shares
                h.last_bought = datetime.utcnow()
            else:
                h = PortfolioHolding(
                    symbol=entry.symbol,
                    tier=entry.tier,
                    exchange=entry.exchange,
                    currency=entry.currency,
                    shares=shares,
                    avg_cost=effective_cost,
                    total_invested=effective_cost * shares,
                    entry_method="put_entry",
                    target_price=entry.strike,
                    first_bought=datetime.utcnow(),
                    last_bought=datetime.utcnow(),
                )
                db.add(h)

            # Clear watchlist put flag
            wl = db.query(PortfolioWatchlist).filter(
                PortfolioWatchlist.symbol == entry.symbol
            ).first()
            if wl:
                wl.has_open_put = False

        # Dedup the assignment transaction on its natural key — an assignment for
        # a given contract is a one-time event. Together with the status guard
        # above this covers re-entry, retries, and sequential double-calls (the
        # IBM/IONQ duplicate-row class). Reporting guard only.
        with get_db() as db:
            dup_tx = db.query(PortfolioTransaction).filter(
                PortfolioTransaction.symbol == entry.symbol,
                PortfolioTransaction.action == "put_assigned",
                PortfolioTransaction.strike == entry.strike,
                PortfolioTransaction.expiry == entry.expiry,
            ).first()
        if dup_tx:
            log.info("portfolio_put_assigned_tx_deduped",
                     symbol=entry.symbol, strike=entry.strike, expiry=entry.expiry)
        else:
            self._record_transaction(
                symbol=entry.symbol,
                action="put_assigned",
                shares=shares,
                price=effective_cost,
                amount=round(effective_cost * shares, 2),
                currency=entry.currency,
                strike=entry.strike,
                expiry=entry.expiry,
                premium_collected=entry.total_premium,
                tier=entry.tier,
                notes=f"Put assigned at ${entry.strike}, effective cost ${effective_cost:.2f}",
            )

        log.info("portfolio_put_assigned",
                 symbol=entry.symbol,
                 shares=shares,
                 strike=entry.strike,
                 effective_cost=round(effective_cost, 2))

    def _handle_put_expiry(self, entry: PortfolioPutEntry):
        """Put expired worthless — collect premium, optionally re-sell."""
        with get_db() as db:
            pe = db.query(PortfolioPutEntry).filter(
                PortfolioPutEntry.id == entry.id
            ).first()
            if pe:
                pe.status = "expired"
                pe.closed_at = datetime.utcnow()

            wl = db.query(PortfolioWatchlist).filter(
                PortfolioWatchlist.symbol == entry.symbol
            ).first()
            if wl:
                wl.has_open_put = False

        self._record_transaction(
            symbol=entry.symbol,
            action="put_expired",
            shares=0,
            price=0,
            amount=0,
            currency=entry.currency,
            strike=entry.strike,
            expiry=entry.expiry,
            premium_collected=entry.total_premium,
            tier=entry.tier,
            notes=f"Put expired worthless, kept ${entry.total_premium:.2f} premium",
        )

        log.info("portfolio_put_expired",
                 symbol=entry.symbol,
                 strike=entry.strike,
                 premium_kept=entry.total_premium)

        # Auto re-sell if enabled (will happen on next scan cycle)
        if self.cfg.put_entry.auto_resell:
            log.info("portfolio_put_will_resell", symbol=entry.symbol)

    # ── Buy amount calculation ───────────────────────────────
    def _check_total_exposure(self, net_liq: float) -> bool:
        """
        Check if total portfolio exposure is within cap.
        Cap = min(NLV × total_exposure_pct, total_exposure_max_usd).
        Returns True if more deployment is allowed, False if cap reached.
        Skipped on small accounts where cap would be below min_single_buy_eur.
        """
        cap = min(net_liq * self.cfg.total_exposure_pct, self.cfg.total_exposure_max_usd)
        if cap < self.cfg.min_single_buy_eur:
            return True  # small account — skip check
        try:
            from src.core.database import get_db
            from src.portfolio.models import PortfolioHolding, PortfolioPutEntry
            with get_db() as db:
                holdings = db.query(PortfolioHolding).filter(
                    PortfolioHolding.shares > 0
                ).all()
                total = sum(h.market_value or 0 for h in holdings)
                open_puts = db.query(PortfolioPutEntry).filter(
                    PortfolioPutEntry.status == "open"
                ).all()
                total += sum((p.strike * p.contracts * 100) for p in open_puts)
            if total >= cap:
                log.info("portfolio_total_exposure_cap",
                         total=round(total, 0), cap=round(cap, 0),
                         nlv=round(net_liq, 0))
                return False
        except Exception as e:
            log.warning("portfolio_exposure_check_failed", error=str(e))
        return True

    def _get_daily_deployed(self) -> float:
        """
        Sum of capital deployed today — stock buys + put collateral.
        Used for daily deployment cap enforcement.
        """
        try:
            from src.core.database import get_db
            from src.portfolio.models import PortfolioTransaction, PortfolioPutEntry
            from datetime import date, datetime
            today_start = datetime.combine(date.today(), datetime.min.time())
            with get_db() as db:
                txns = db.query(PortfolioTransaction).filter(
                    PortfolioTransaction.action == "buy",
                    PortfolioTransaction.created_at >= today_start,
                    # Exclude the cash-yield ETF — a cash-reserve park isn't deployment (see _fills_today).
                    PortfolioTransaction.symbol != (self.cfg.cash_yield_symbol or "__none__"),
                ).all()
                stock_deployed = sum(t.amount for t in txns)
                puts = db.query(PortfolioPutEntry).filter(
                    PortfolioPutEntry.status == "open",
                    PortfolioPutEntry.opened_at >= today_start,
                ).all()
                put_collateral = sum((p.strike * p.contracts * 100) for p in puts)
            return stock_deployed + put_collateral
        except Exception as e:
            log.warning("portfolio_daily_deployed_check_failed", error=str(e))
            return 0.0

    def _calculate_buy_amount(
        self,
        analysis: StockAnalysis,
        net_liq: float | None,
        deployable: float,
    ) -> float:
        """Calculate how much to buy, respecting tier allocation and limits."""
        max_buy = min(self.cfg.max_single_buy_eur, deployable)

        # Check portfolio concentration limit
        if net_liq and net_liq > 0:
            current_value = self._get_holding_value(analysis.symbol)
            max_for_stock = (net_liq * self.cfg.max_portfolio_pct) - current_value
            max_buy = min(max_buy, max(0, max_for_stock))

        # Scale by signal strength (30-100% of max)
        strength_factor = 0.3 + (analysis.signal_strength / 100) * 0.7
        buy_amount = max_buy * strength_factor

        # Round to whole shares
        if analysis.current_price and analysis.current_price > 0:
            shares = int(buy_amount / analysis.current_price)
            buy_amount = shares * analysis.current_price

        # Deep discount or extreme oversold → full allocation
        if analysis.discount_pct and analysis.discount_pct > 15:
            buy_amount = min(self.cfg.max_single_buy_eur, deployable)
            if analysis.current_price and analysis.current_price > 0:
                shares = int(buy_amount / analysis.current_price)
                buy_amount = shares * analysis.current_price

        if analysis.rsi_14 and analysis.rsi_14 < 20:
            buy_amount = min(self.cfg.max_single_buy_eur, deployable)
            if analysis.current_price and analysis.current_price > 0:
                shares = int(buy_amount / analysis.current_price)
                buy_amount = shares * analysis.current_price

        return round(buy_amount, 2)

    # ── Direct buy execution ─────────────────────────────────
    def _execute_buy(
        self,
        stock: PortfolioWatchlist,
        analysis: StockAnalysis,
        buy_amount: float,
        funding_source: str = "cash",
        rank: int = 0,
        rank_score: float = 0.0,
        rationale: str | None = None,
    ) -> bool:
        """Place a limit buy order or create a suggestion if in suggestion_mode."""
        _ensure_event_loop()

        if not analysis.current_price or analysis.current_price <= 0:
            return False

        try:
            shares = int(buy_amount / analysis.current_price)
            if shares <= 0:
                return False

            limit_price = round(analysis.current_price * 0.998, 2)

            # Build rationale with ranking info
            margin_note = ""
            if "margin" in funding_source:
                margin_note = f" [MARGIN: {funding_source}]"

            # Suggestion mode — create approval request instead of placing order
            if self.cfg.suggestion_mode:
                from src.core.suggestions import create_suggestion
                create_suggestion(
                    symbol=stock.symbol,
                    action="buy_stock",
                    quantity=shares,
                    limit_price=limit_price,
                    order_type="LMT",
                    source="portfolio",
                    tier=stock.tier,
                    signal=analysis.signal_type,
                    rationale=rationale or (
                        f"Rank #{rank} (score {rank_score:.0f}). "
                        f"{analysis.signal_type}: price ${analysis.current_price:.2f} "
                        f"vs SMA ${analysis.sma_200:.2f} "
                        f"({analysis.discount_pct:.1f}% discount), "
                        f"RSI {analysis.rsi_14:.0f}"
                        f"{margin_note}"
                    ),
                    current_price=analysis.current_price,
                    sma_200=analysis.sma_200,
                    rsi_14=analysis.rsi_14,
                    est_cost=round(shares * limit_price, 2),
                    rank=rank,
                    rank_score=rank_score,
                    funding_source=funding_source,
                )
                log.info("portfolio_suggestion_created",
                         symbol=stock.symbol,
                         action="buy_stock",
                         shares=shares,
                         price=limit_price,
                         signal=analysis.signal_type)
                return True

            # Live mode — place actual order
            contract = Stock(stock.symbol, stock.exchange, stock.currency)
            with get_portfolio_lock():
                self.ib.qualifyContracts(contract)

            order = LimitOrder("BUY", shares, limit_price)
            order.tif = "DAY"
            order.outsideRth = _outside_rth_ok(stock.currency)

            with get_portfolio_lock():
                trade = self.ib.placeOrder(contract, order)
                self.ib.sleep(2)

            # Refresh dashboard cache so new order appears immediately
            try:
                from src.portfolio.connection import refresh_portfolio_pending_orders_cache
                refresh_portfolio_pending_orders_cache()
            except Exception:
                pass

            log.info("portfolio_buy_placed",
                     symbol=stock.symbol,
                     tier=stock.tier,
                     shares=shares,
                     price=limit_price,
                     amount=round(shares * limit_price, 2),
                     signal=analysis.signal_type)

            self._record_transaction(
                symbol=stock.symbol,
                action="buy",
                shares=shares,
                price=limit_price,
                amount=round(shares * limit_price, 2),
                currency=stock.currency,
                signal=analysis.signal_type,
                sma_200=analysis.sma_200,
                rsi=analysis.rsi_14,
                discount_pct=analysis.discount_pct,
                tier=stock.tier,
            )

            self._update_holding(stock, shares, limit_price)
            return True

        except Exception as e:
            log.error("portfolio_buy_error", symbol=stock.symbol, error=str(e))
            return False

    # ── Compounder direct buy: conviction-scaled DAY limit ladder ──────────
    def _execute_compounder_buy(
        self,
        stock: PortfolioWatchlist,
        analysis: StockAnalysis,
        core_amount: float,
        urgency: float,
        is_leader: bool,
        cash_room: float,
        rank: int = 0,
        rank_score: float = 0.0,
        rationale: str | None = None,
        min_buy: float | None = None,
    ) -> tuple[float, float]:
        """Place a conviction-scaled DAY limit ladder (live) or one core-rung suggestion card
        (suggestion mode). Returns (core_placed_notional, total_placed_notional).

        Unlike the legacy _execute_buy, this does NOT optimistically touch holdings or record a
        buy transaction — resting DAY limits may not fill, so sync_ibkr_holdings() is the single
        source of truth for fills (run_compounder_scan nets resting orders via _open_buy_map so a
        working name isn't re-laddered). `cash_room` caps total resting notional placed this call
        to keep the account out of unintended margin; the core rung has priority over dip rungs.
        """
        _ensure_event_loop()
        px = analysis.current_price
        if not px or px <= 0 or core_amount <= 0:
            return (0.0, 0.0)

        from src.portfolio import compounder as cmp
        from src.portfolio import fx as pfx
        cc = self.cfg.compounder
        # core_amount (the caller's brick) and the floors/cash_room are all in the account BASE currency,
        # but `px` and the ladder rung prices are in the instrument's LOCAL currency (£ for an LSE name).
        # Convert each rung price to base for share-sizing — a base brick ÷ a LOCAL price over-buys a
        # foreign name by its FX rate (the AZN bug). `rate` is LOCAL→BASE (1.0 for base/USD-on-USD).
        ccy = stock.currency or "USD"
        rate = pfx.rate_to_base(ccy)
        # NLV-scaled core-rung floor (the caller's brick already cleared it); fall back to the
        # configured cap if not threaded through, so the method is safe to call standalone.
        core_floor = min_buy if min_buy is not None else cc.min_single_buy
        plan = cmp.ladder_plan(px, urgency, is_leader, cc,
                               sma200=analysis.sma_200, high_52w=analysis.high_52w)
        if not plan:
            return (0.0, 0.0)

        # Resolve rungs to (local price, shares); core sized from core_amount, dips as frac of it.
        # Share count uses the BASE per-share cost (price × rate); the order itself prices in LOCAL.
        rungs: list[tuple[float, int]] = []
        for i, (price, frac) in enumerate(plan):
            if price <= 0:
                continue
            shares = int((core_amount * frac) / (price * rate))
            notional_base = shares * price * rate
            # Core rung must clear the NLV-scaled min order; dip rungs just need to be non-trivial.
            floor = core_floor if i == 0 else 1000.0
            if shares <= 0 or notional_base < floor:
                continue
            rungs.append((price, shares))
        if not rungs or rungs[0][1] <= 0:
            return (0.0, 0.0)

        core_price, core_shares = rungs[0]

        # Suggestion mode — one card at the core-rung price (no dip cards; keep the queue clean).
        if self.cfg.suggestion_mode:
            from src.core.suggestions import create_suggestion
            create_suggestion(
                symbol=stock.symbol, action="buy_stock", quantity=core_shares,
                limit_price=core_price, order_type="LMT", source="portfolio",
                tier=stock.tier, signal=analysis.signal_type,
                rationale=rationale, current_price=px,
                sma_200=analysis.sma_200, rsi_14=analysis.rsi_14,
                est_cost=round(core_shares * core_price, 2),
                rank=rank, rank_score=rank_score, funding_source="cash",
            )
            log.info("compounder_suggestion_created", symbol=stock.symbol,
                     shares=core_shares, price=core_price, urgency=round(urgency, 2),
                     leader=is_leader, signal=analysis.signal_type)
            # Card est_cost is the actual LOCAL order cost; the RETURN must be base-ccy so the caller's
            # `spent`/budget/cash_room accounting (all base) nets a foreign order at its true base value.
            est_base = round(core_shares * core_price * rate, 2)
            return (est_base, est_base)

        # Live mode — place each rung as a DAY limit, core first, bounded by cash_room.
        # A broker error on one symbol must not abort the whole scan — return whatever filled.
        core_placed = 0.0
        total_placed = 0.0
        try:
            contract = Stock(stock.symbol, stock.exchange, stock.currency)
            with get_portfolio_lock():
                self.ib.qualifyContracts(contract)

            # Un-park the park-ETF (XEON) reserve, then fund the FX leg, then GATE on the authoritative
            # funding path for this currency: the FX conversion for a foreign buy, or the XEON unpark for a
            # same-currency buy. If that path could not be funded, SKIP this name rather than silently
            # open a margin loan (fail-closed). The 4-hourly scan re-creates the buy when cash is ready.
            # Funding is settled in the instrument's LOCAL currency (the FX leg converts base→local to
            # buy the foreign stock; the XEON unpark frees the park ETF's EUR), so size the ask in LOCAL.
            total_notional = sum(price * shares for price, shares in rungs)
            base_ccy = getattr(self.cfg, "base_currency", "EUR") or "EUR"
            unpark_ok = _unpark_yield(self.ib, self.cfg, total_notional, settle_ccy=ccy)
            fx_ok = _ensure_currency_funding(self.ib, ccy, base_ccy, total_notional, cfg=self.cfg)
            funded = fx_ok if ccy.upper() != base_ccy.upper() else unpark_ok
            if not funded:
                log.warning("compounder_buy_unfunded_skip", symbol=stock.symbol,
                            ccy=ccy, notional=round(total_notional))
                return (0.0, 0.0)

            # `cash_room` and the returned notionals are base-ccy (the caller's budget/spent are base),
            # while the order prices in LOCAL — convert each rung's spend to base for the gate/return.
            room = cash_room
            for i, (price, shares) in enumerate(rungs):
                notional_base = shares * price * rate
                if notional_base > room:
                    continue                      # can't afford this rung — skip (core has priority)
                order = LimitOrder("BUY", shares, price)
                order.tif = "DAY"
                order.outsideRth = _outside_rth_ok(stock.currency)
                with get_portfolio_lock():
                    self.ib.placeOrder(contract, order)
                    self.ib.sleep(1)
                room -= notional_base
                total_placed += notional_base
                if i == 0:
                    core_placed = notional_base
                log.info("compounder_rung_placed", symbol=stock.symbol, rung=i,
                         shares=shares, price=price, notional_base=round(notional_base),
                         ccy=ccy, leader=is_leader)
        except Exception as e:
            log.error("compounder_buy_error", symbol=stock.symbol, error=str(e),
                      placed=round(total_placed))
            return (core_placed, total_placed)

        if total_placed <= 0:
            return (0.0, 0.0)

        # Refresh dashboard cache so the new working orders appear immediately.
        try:
            from src.portfolio.connection import refresh_portfolio_pending_orders_cache
            refresh_portfolio_pending_orders_cache()
        except Exception:
            pass

        log.info("compounder_ladder_placed", symbol=stock.symbol, tier=stock.tier,
                 rungs=len(rungs), core=round(core_placed), total=round(total_placed),
                 urgency=round(urgency, 2), leader=is_leader, signal=analysis.signal_type)
        return (core_placed, total_placed)

    # ── Cash management ──────────────────────────────────────
    def _park_cash(self, amount: float | None = None):
        """Park excess cash in treasury ETF for yield. With `amount` set (compounder path), park exactly
        that much; otherwise (classic path) park all available cash above the 5% reserve."""
        if not self.cfg.cash_yield_enabled:
            return
        # Stop re-firing a park order the venue keeps rejecting (e.g. a US ETF an EU account can't trade
        # — Error 201/PRIIPs/KID). Set on the first rejection below; auto-clears after the cooldown / on
        # restart so a config change to a tradeable ETF gets a fresh attempt.
        if _is_permission_blocked(self.cfg.cash_yield_symbol):
            return
        # Same venue rule as the un-park: the ETF trades on one exchange. A park BUY placed while it's
        # shut rests overnight instead of filling — and a resting BUY counts against deployed_today,
        # which is how a €1.9M overnight parking order once zeroed the 00:00-04:00 deploy budget.
        if not _market_open(self.cfg.cash_yield_currency):
            return
        _ensure_event_loop()
        try:
            if amount is not None:
                parkable = amount
                if parkable < 1000:
                    return
            else:
                available = self._get_available_cash()
                if not available or available < 1000:
                    return
                net_liq = self._get_net_liquidation() or available
                reserve = net_liq * self.cfg.cash_reserve_pct
                parkable = available - reserve
                if parkable < 1000:
                    return

            contract = Stock(
                self.cfg.cash_yield_symbol,
                self.cfg.cash_yield_exchange,
                self.cfg.cash_yield_currency,
            )
            with get_portfolio_lock():
                self.ib.qualifyContracts(contract)
                contract.exchange = "SMART"
                bars = self.ib.reqHistoricalData(
                    contract, endDateTime="",
                    durationStr="2 D", barSizeSetting="1 day",
                    whatToShow="TRADES", useRTH=False,
                    formatDate=1, timeout=8,
                )

            price = float(bars[-1].close) if bars else None
            if not price or price <= 0:
                return

            shares = int(parkable / price)
            if shares <= 0:
                return

            order = LimitOrder("BUY", shares, round(price * 1.001, 2))
            order.tif = "DAY"
            order.outsideRth = _outside_rth_ok(self.cfg.cash_yield_currency)
            with get_portfolio_lock():
                trade = self.ib.placeOrder(contract, order)
                self.ib.sleep(1)

            # Refresh dashboard cache so new order appears immediately
            try:
                from src.portfolio.connection import refresh_portfolio_pending_orders_cache
                refresh_portfolio_pending_orders_cache()
            except Exception:
                pass

            # Only report parked on a real outcome — a rejection (e.g. EU PRIIPs/KID 201) flips the order
            # PendingSubmit→Inactive→Cancelled within ~200ms, well inside the sleep above. On rejection,
            # venue-block the symbol so the scan stops re-placing the doomed order every cycle and the
            # cash simply stays liquid; don't log it as "parked" (that was the misleading-success bug).
            status = ""
            try:
                status = getattr(getattr(trade, "orderStatus", None), "status", "") or ""
            except Exception:
                pass
            if status in ("Cancelled", "Inactive", "ApiCancelled"):
                if _order_blocked_by_permission(trade):
                    # Hard venue/permission rejection (e.g. a US ETF on an EU account — Error 201 /
                    # PRIIPs / no-KID): venue-block so the scan stops re-placing the doomed order.
                    _mark_permission_blocked(self.cfg.cash_yield_symbol, hours=24.0)
                    log.warning("portfolio_cash_park_rejected", symbol=self.cfg.cash_yield_symbol,
                                status=status, note="venue-blocked; cash stays liquid (ETF not tradeable here)")
                else:
                    # Transient cancel (e.g. the ETF's market is closed right now, no route) — do NOT
                    # block; just don't claim it parked. It retries on the next scan when the venue opens.
                    log.warning("portfolio_cash_park_unfilled", symbol=self.cfg.cash_yield_symbol,
                                status=status, note="not filled (market closed / transient) — retry next scan")
                return
            log.info("portfolio_cash_parked", symbol=self.cfg.cash_yield_symbol,
                     shares=shares, amount=round(shares * price, 2), status=status)
        except Exception as e:
            log.warning("portfolio_park_cash_error", error=str(e))

    def _reinvest_dividends(self, deployable: float):
        """Dividends increase available balance, deployed in next buy cycle."""
        pass

    # ── Account queries ──────────────────────────────────────
    def _get_available_cash(self) -> float | None:
        try:
            _ensure_event_loop()
            with get_portfolio_lock():
                values = self.ib.accountValues()
            for item in values:
                if item.tag == "AvailableFunds" and item.currency in ("EUR", "BASE"):
                    return float(item.value)
            for item in values:
                if item.tag == "TotalCashValue":
                    return float(item.value)
            return None
        except Exception as e:
            log.warning("portfolio_cash_query_error", error=str(e))
            return None

    def _get_settled_cash(self) -> float | None:
        """Genuine settled cash in base currency (TotalCashValue) — can be NEGATIVE on a margin loan.
        The cash-first deployable base uses this, NOT AvailableFunds (which already includes margin
        buying power and silently levered the compounder in calm markets)."""
        try:
            _ensure_event_loop()
            with get_portfolio_lock():
                values = self.ib.accountValues()
            for item in values:
                if item.tag == "TotalCashValue" and item.currency in ("EUR", "BASE"):
                    return float(item.value)
            for item in values:
                if item.tag == "TotalCashValue":
                    return float(item.value)
            return None
        except Exception as e:
            log.warning("portfolio_settled_cash_query_error", error=str(e))
            return None

    def _get_net_liquidation(self) -> float | None:
        try:
            _ensure_event_loop()
            with get_portfolio_lock():
                values = self.ib.accountValues()
            for item in values:
                if item.tag == "NetLiquidation":
                    return float(item.value)
            return None
        except Exception:
            return None

    def _get_maintenance_margin(self) -> float | None:
        """Get current maintenance margin requirement from IBKR."""
        try:
            _ensure_event_loop()
            with get_portfolio_lock():
                values = self.ib.accountValues()
            for item in values:
                if item.tag == "MaintMarginReq" and item.currency in ("EUR", "BASE"):
                    return float(item.value)
            for item in values:
                if item.tag == "MaintMarginReq":
                    return float(item.value)
            return None
        except Exception:
            return None

    def _get_holding_value(self, symbol: str) -> float:
        """Holding market value in the account BASE currency (compared against base NLV caps)."""
        from src.portfolio import fx as pfx
        with get_db() as db:
            h = db.query(PortfolioHolding).filter(
                PortfolioHolding.symbol == symbol
            ).first()
            return pfx.to_base(h.market_value or h.total_invested or 0, h.currency) if h else 0

    # ── Database helpers ─────────────────────────────────────
    def _get_watchlist(self) -> list[PortfolioWatchlist]:
        with get_db() as db:
            all_stocks = db.query(PortfolioWatchlist).all()
            # CRITICAL: never let the cash-yield PARK ETF (XEON) into the ranked/buyable universe. It
            # can land in portfolio_watchlist because the holdings-sync auto-adds every held position
            # (the parked reserve IS a holding) as an 'existing_holding' growth name. If ranked, its
            # smooth money-market uptrend reads as a permanently-"green" underweight target (its park
            # holdings are excluded from _get_holdings_map), so the deploy loop buys it as a STOCK and
            # monopolises the daily budget — starving the real names. Park logic uses cfg.cash_yield_symbol
            # directly and does NOT need a watchlist row, so filtering here is always safe.
            park = getattr(self.cfg, "cash_yield_symbol", None)
            all_stocks = [s for s in all_stocks if s.symbol != park]
            # Sort: non-SMART exchanges first (European/Asian available now),
            # then SMART (US) — so we get prices for available markets quickly
            non_us = [s for s in all_stocks if s.exchange != "SMART"]
            us = [s for s in all_stocks if s.exchange == "SMART"]
            return non_us + us

    def _update_watchlist_metrics(self, stock: PortfolioWatchlist, analysis: StockAnalysis):
        with get_db() as db:
            entry = db.query(PortfolioWatchlist).filter(
                PortfolioWatchlist.symbol == stock.symbol
            ).first()
            if entry:
                entry.current_price = analysis.current_price
                entry.sma_200 = analysis.sma_200
                entry.discount_pct = analysis.discount_pct
                entry.rsi_14 = analysis.rsi_14
                entry.high_52w = analysis.high_52w
                entry.momentum_12_1 = getattr(analysis, "momentum_12_1", None)
                entry.buy_signal = analysis.buy_signal
                entry.signal_type = analysis.signal_type if analysis.buy_signal else None
                entry.raw_score = round(analysis.composite_score, 1)
                entry.updated_at = datetime.utcnow()
                entry.last_metrics_success = datetime.utcnow()
                entry.metrics_stale = False

                if analysis.composite_score > 0:
                    log.info("portfolio_score_saved",
                             symbol=stock.symbol,
                             score=round(analysis.composite_score, 1),
                             signal=analysis.buy_signal,
                             discount=round(analysis.discount_pct, 1) if analysis.discount_pct else None)

    def update_watchlist_metrics(self):
        """
        Update SMA, RSI, discount for ALL watchlist stocks.
        Runs independently from run_scan() so metrics are always fresh,
        even when margin gate blocks buying.
        """
        _ensure_event_loop()
        with get_db() as db:
            watchlist = db.query(PortfolioWatchlist).all()

        if not watchlist:
            return

        updated = 0
        failed = 0
        failed_exchanges: dict[str, int] = {}
        skipped_exchanges: set[str] = set()

        for stock in watchlist:
            if stock.exchange in skipped_exchanges:
                failed += 1
                continue

            try:
                analysis = self.analyzer.analyze_stock(
                    stock.symbol, stock.exchange, stock.currency,
                    tier=stock.tier,
                )
                if analysis:
                    self._update_watchlist_metrics(stock, analysis)
                    updated += 1
                    failed_exchanges[stock.exchange] = 0
                else:
                    failed += 1
                    failed_exchanges[stock.exchange] = failed_exchanges.get(stock.exchange, 0) + 1
            except Exception as e:
                failed += 1
                failed_exchanges[stock.exchange] = failed_exchanges.get(stock.exchange, 0) + 1
                log.warning("portfolio_metrics_error", symbol=stock.symbol, error=str(e))

            if failed_exchanges.get(stock.exchange, 0) >= 3:
                skipped_exchanges.add(stock.exchange)
                log.warning("portfolio_metrics_exchange_skipped",
                            exchange=stock.exchange, consecutive_failures=3)

        # If many failed due to timeouts, recalc scores from existing DB data
        if failed > updated and updated == 0:
            self.recalc_scores_from_db()

        # Mark stocks stale when last_metrics_success is older than 24h.
        # Stocks whose analyze just succeeded already had metrics_stale=False set
        # by _update_watchlist_metrics; this pass catches stocks that failed or
        # were skipped and are past the staleness threshold.
        from datetime import timedelta
        stale_cutoff = datetime.utcnow() - timedelta(hours=24)
        stale_count = 0
        with get_db() as db:
            entries = db.query(PortfolioWatchlist).all()
            for e in entries:
                if e.last_metrics_success is None or e.last_metrics_success < stale_cutoff:
                    if not e.metrics_stale:
                        e.metrics_stale = True
                        stale_count += 1

        log.info("portfolio_metrics_updated",
                 updated=updated, failed=failed, total=len(watchlist),
                 newly_stale=stale_count)

    def _compute_compound_quality(self) -> None:
        """
        Compute compound quality score for all watchlist stocks.
        Each tier uses a tier-appropriate raw quality metric, then normalizes
        within-tier to 1-100 based on actual score distance (best=100, worst=1).
        Stored as compound_quality_pct in portfolio_watchlist.

        Tier formulas:
          growth / breakthrough: raw = 0.40*growth + 0.25*valuation + 0.35*quality
            (matches screener's portfolio_score formula exactly, so compound
            ranks stocks on the same basis the screener selected them on)
          dividend: raw = dividend_total_return_score
            (the screener's dividend-specific ranking metric)

        Called by recalc_scores_from_db() before the 80/20 composite blend.
        """
        with get_db() as db:
            all_stocks = db.query(PortfolioWatchlist).all()

            # Group by tier
            by_tier: dict[str, list] = {}
            for s in all_stocks:
                by_tier.setdefault(s.tier, []).append(s)

            for tier, stocks in by_tier.items():
                # Compute tier-specific raw score per stock
                raw: dict[str, float] = {}
                for s in stocks:
                    if tier == "dividend":
                        # Dividend tier ranks on dividend_total_return_score only
                        raw[s.symbol] = s.dividend_total_return_score or 0.0
                    else:
                        # Growth and breakthrough use portfolio_score formula
                        g = s.growth_score or 0.0
                        v = s.valuation_score or 0.0
                        q = s.quality_score or 0.0
                        raw[s.symbol] = g * 0.40 + v * 0.25 + q * 0.35

                if not raw:
                    continue

                min_raw = min(raw.values())
                max_raw = max(raw.values())
                spread = max_raw - min_raw

                for s in stocks:
                    if spread > 0:
                        # Normalize: best=100, worst=1, proportional to actual distance
                        pct = ((raw[s.symbol] - min_raw) / spread) * 99 + 1
                    else:
                        # All stocks identical score — give everyone 50
                        pct = 50.0

                    entry = db.query(PortfolioWatchlist).filter(
                        PortfolioWatchlist.symbol == s.symbol
                    ).first()
                    if entry:
                        entry.compound_quality_pct = round(pct, 1)

            db.commit()
        log.info("compound_quality_computed")

    def recalc_scores_from_db(self):
        """
        Recalculate composite scores using metrics already in the database.
        No IBKR connection needed — uses stored SMA, RSI, discount values.
        Useful when metrics were fetched but scores weren't saved yet.
        """
        from src.portfolio.analyzer import TIER_PARAMS

        # Compute compound quality scores first — needed for 70/30 blending
        self._compute_compound_quality()

        with get_db() as db:
            watchlist = db.query(PortfolioWatchlist).all()

        if not watchlist:
            return

        recalced = 0
        for stock in watchlist:
            if stock.discount_pct is None or stock.rsi_14 is None:
                continue

            params = TIER_PARAMS.get(stock.tier, TIER_PARAMS["growth"])
            score = 0.0
            triggered = False

            # SMA discount signal (0-40 points)
            min_disc = params["min_discount_pct"]
            if stock.discount_pct >= min_disc:
                triggered = True
                score += min(40, (stock.discount_pct / min_disc) * 20)

            # RSI oversold signal (0-30 points)
            rsi_threshold = params["rsi_oversold"]
            if stock.rsi_14 <= rsi_threshold:
                triggered = True
                score += min(30, ((rsi_threshold - stock.rsi_14) / rsi_threshold) * 30 + 15)

            if not triggered:
                score = 0.0

            # Apply structural risk penalty
            # Use raw SQL to avoid ORM column caching issues
            risk_penalty = 0.0
            try:
                from sqlalchemy import text
                from src.core.database import get_engine
                with get_engine().connect() as conn:
                    row = conn.execute(text(
                        "SELECT risk_total_penalty FROM portfolio_watchlist WHERE symbol = :sym"
                    ), {"sym": stock.symbol}).fetchone()
                    if row and row[0]:
                        risk_penalty = float(row[0])
            except Exception:
                pass

            raw_score = score  # save before penalty
            if risk_penalty > 0 and score > 0:
                original = score
                score = max(0, score - risk_penalty)
                if score == 0:
                    log.info("portfolio_risk_penalty_blocked_recalc",
                             symbol=stock.symbol,
                             original_score=round(original, 1),
                             penalty=risk_penalty)

            with get_db() as db:
                entry = db.query(PortfolioWatchlist).filter(
                    PortfolioWatchlist.symbol == stock.symbol
                ).first()
                if entry:
                    entry.raw_score = round(raw_score, 1)
                    # 30/70 blend: raw signal (30%) + compound quality (70%).
                    # Buffett-style: quality screened upstream, fair price
                    # is the dominant factor, panic adds urgency.
                    quality_pct = entry.compound_quality_pct or 50.0
                    blended = (score * 0.30) + (quality_pct * 0.70)
                    entry.composite_score = round(blended, 1)
                    # Composite floor — see analyzer.py MIN_COMPOSITE_FOR_ACTION
                    if score > 0 and blended >= 40.0 and not entry.buy_signal:
                        entry.buy_signal = True
                        if entry.discount_pct and entry.discount_pct >= min_disc:
                            entry.signal_type = "below_sma"
                        elif entry.rsi_14 and entry.rsi_14 <= rsi_threshold:
                            entry.signal_type = "rsi_oversold"

            if score > 0:
                recalced += 1
                log.info("portfolio_score_recalced", symbol=stock.symbol, score=round(score, 1))

        log.info("portfolio_scores_recalced", count=recalced, total=len(watchlist))

    def _record_transaction(self, **kwargs):
        with get_db() as db:
            db.add(PortfolioTransaction(**kwargs))

    def _update_holding(self, stock: PortfolioWatchlist, shares: int, price: float):
        with get_db() as db:
            h = db.query(PortfolioHolding).filter(
                PortfolioHolding.symbol == stock.symbol
            ).first()
            if h:
                total_cost = h.avg_cost * h.shares + price * shares
                h.shares += shares
                h.avg_cost = total_cost / h.shares if h.shares > 0 else 0
                h.total_invested += price * shares
                h.last_bought = datetime.utcnow()
            else:
                h = PortfolioHolding(
                    symbol=stock.symbol,
                    name=stock.name,
                    exchange=stock.exchange,
                    currency=stock.currency,
                    sector=stock.sector,
                    tier=stock.tier,
                    shares=shares,
                    avg_cost=price,
                    total_invested=price * shares,
                    entry_method="direct_buy",
                    first_bought=datetime.utcnow(),
                    last_bought=datetime.utcnow(),
                )
                db.add(h)

    def _store_state(self, key: str, value: str):
        with get_db() as db:
            state = db.query(PortfolioState).filter(PortfolioState.key == key).first()
            if state:
                state.value = value
                state.updated_at = datetime.utcnow()
            else:
                db.add(PortfolioState(key=key, value=value))

    def _is_market_open(self, currency: str) -> bool:
        """Check if the market for a given currency is currently open (see module-level _market_open,
        which the funding path shares — the table must not fork)."""
        return _market_open(currency)

    def update_holdings_prices(self):
        """Update current prices and P&L for all holdings."""
        _ensure_event_loop()
        with get_db() as db:
            holdings = db.query(PortfolioHolding).filter(
                PortfolioHolding.shares > 0
            ).all()
            for h in holdings:
                try:
                    contract = Stock(h.symbol, h.exchange, h.currency)
                    with get_portfolio_lock():
                        self.ib.qualifyContracts(contract)
                    # Only override to SMART for exchanges that support it
                    # Non-US/EU/developed exchanges must keep their original exchange
                    _non_smart_exchanges = {"SEHK", "JSE", "SGX", "TASE", "NSE", "ASX",
                                            "BSE", "KSE", "TWSE", "BKK", "IDX"}
                    if h.exchange not in _non_smart_exchanges:
                        contract.exchange = "SMART"
                    price = None
                    for what in ("TRADES", "MIDPOINT"):
                        try:
                            with get_portfolio_lock():
                                bars = self.ib.reqHistoricalData(
                                    contract, endDateTime="",
                                    durationStr="2 D", barSizeSetting="1 day",
                                    whatToShow=what, useRTH=False,
                                    formatDate=1, timeout=8,
                                )
                            if bars:
                                price = float(bars[-1].close)
                                break
                        except Exception:
                            pass
                        self.ib.sleep(0.5)
                    if price and price > 0:
                        price = float(bars[-1].close)
                        # IBKR returns LSE/GBP prices in PENCE — normalise to pounds, identical to the
                        # analyzer (analyzer.py:156) and the seven other GBP-handling sites. This was
                        # the ONE price path missing it, so it intermittently stored pence and the
                        # holding showed ~100× its value + a bogus gain (the AZN +9907% case).
                        if (h.currency or "").upper() == "GBP":
                            price = price / 100.0
                        h.current_price = price
                        h.market_value = price * h.shares
                        h.unrealized_pnl = h.market_value - h.total_invested
                        h.unrealized_pnl_pct = (
                            (h.unrealized_pnl / h.total_invested * 100)
                            if h.total_invested > 0 else 0
                        )
                        h.updated_at = datetime.utcnow()
                except Exception as e:
                    log.debug("portfolio_price_update_error", symbol=h.symbol, error=str(e))


def _fx_conversion_plan(base: str, ccy: str, shortfall_ccy: float, rate_ccy_per_base: float,
                        pair_symbol: str, idealpro_min_base: float,
                        min_convert: float = 1000.0) -> dict:
    """Pure decision for the pre-buy FX conversion — no IBKR access, so it's unit-testable.

    Given the shortfall in the stock's currency, the live rate (units of `ccy` per 1 unit of `base`),
    and which currency is the SYMBOL of the canonical IBKR cash pair (IBKR defines only one direction
    per pair — e.g. EUR.HKD, never HKD.EUR), decide whether to place an IDEALPRO conversion and with
    what action/qty. Quantity is always denominated in the pair's symbol currency (IBKR convention),
    with a small buffer so rate drift can't leave the buy a hair short.

    Returns {place, action, qty, base_value, reason}:
      • place=False reason='funded'    — nothing to convert (shortfall below min_convert).
      • place=False reason='no_rate'   — couldn't price the leg; caller proceeds (auto-FX) non-blocking.
      • place=False reason='below_min' — leg value under the IDEALPRO minimum; let IBKR auto-FX it.
      • place=True                     — fire a real IDEALPRO conversion (action/qty set)."""
    base = (base or "").upper()
    ccy = (ccy or "").upper()
    if shortfall_ccy <= 0 or shortfall_ccy < min_convert:
        return {"place": False, "action": None, "qty": 0, "base_value": 0.0, "reason": "funded"}
    if not rate_ccy_per_base or rate_ccy_per_base <= 0:
        return {"place": False, "action": None, "qty": 0, "base_value": 0.0, "reason": "no_rate"}
    base_value = shortfall_ccy / rate_ccy_per_base
    if base_value < idealpro_min_base:
        return {"place": False, "action": None, "qty": 0, "base_value": base_value, "reason": "below_min"}
    buf = 1.01
    if (pair_symbol or "").upper() == ccy:
        # Canonical pair is ccy.base (symbol == ccy) → BUY ccy directly; qty denominated in ccy.
        return {"place": True, "action": "BUY", "qty": int(round(shortfall_ccy * buf)),
                "base_value": base_value, "reason": "convert"}
    # Canonical pair is base.ccy (symbol == base) → SELL base to receive ccy; qty denominated in base.
    return {"place": True, "action": "SELL", "qty": int(round(base_value * buf)),
            "base_value": base_value, "reason": "convert"}


def _fx_rate_ccy_per_base(ib, pair, base: str, ccy: str) -> float:
    """Snapshot the FX mid for `pair` and return it as units of `ccy` per 1 unit of `base`.
    The quoted price is symbol-per-currency, so invert when the pair's symbol is the stock ccy.
    Returns 0.0 on any failure (caller treats that as 'unpriced' and proceeds via auto-FX)."""
    try:
        with get_portfolio_lock():
            t = ib.reqMktData(pair, "", True, False)
            ib.sleep(2)
        px = None
        for cand in (t.midpoint(), t.marketPrice(), t.last, t.close):
            if cand and cand == cand and cand > 0:   # truthy, not-NaN, positive
                px = float(cand)
                break
        try:
            with get_portfolio_lock():
                ib.cancelMktData(pair)
        except Exception:
            pass
        if not px:
            return 0.0
        # Pair price is HKD-per-EUR when symbol==base (EUR.HKD); invert when symbol==ccy.
        if (pair.symbol or "").upper() == (base or "").upper():
            return px
        return 1.0 / px
    except Exception:
        return 0.0


def _ensure_currency_funding(ib, stock_ccy: str, base_ccy: str, notional_ccy: float,
                             cfg=None, min_convert: float = 1000.0) -> bool:
    """Auto-convert base currency into a foreign stock's settlement currency BEFORE the buy, so the
    trade is cash-funded and no overnight margin loan accrues. IBKR otherwise borrows the foreign ccy
    automatically even when base-currency cash is positive — recurring margin interest with no return
    benefit, and invisible to the (base-currency) leverage gate.

    IBKR exposes only ONE direction per FX pair (e.g. EUR.HKD, never HKD.EUR), so we qualify the
    canonical pair and derive BUY/SELL + qty from which side it's quoted (see `_fx_conversion_plan`).

    Returns True if the buy MAY proceed: same-currency, already-funded, a sub-min remainder, a leg
    BELOW the IDEALPRO minimum (left to IBKR auto-FX — converting it is impossible, and the residual
    margin is a few hundred currency units that self-cures), or a conversion that filled. Returns
    False only when a REAL, above-minimum shortfall could NOT be converted (order errored / didn't
    fill) — the caller treats that as fatal and SKIPS rather than silently open a foreign margin loan
    (fail-closed)."""
    ccy = (stock_ccy or "").upper()
    base = (base_ccy or "EUR").upper()
    if not ccy or ccy == base or notional_ccy <= 0:
        return True
    idealpro_min_base = float(getattr(cfg, "fx_idealpro_min_base", 22000.0) or 0.0)
    wait_secs = float(getattr(cfg, "fx_fill_wait_secs", 12.0) or 12.0)
    try:
        have = 0.0
        with get_portfolio_lock():
            vals = ib.accountValues()
        for v in vals:
            if v.tag == "CashBalance" and v.currency == ccy:
                have = float(v.value)
                break
        shortfall = notional_ccy - have
        if shortfall < min_convert:
            return True                   # already funded, or a sub-min remainder left to the loan

        # Qualify the canonical pair. Try base-first (for an EUR base this is the IBKR-canonical
        # direction, so no spurious Error-200), then ccy-first. If neither resolves (e.g. no FX
        # permission for this pair), let IBKR auto-FX rather than block — but log it so a persistent
        # large-leg failure is visible.
        pair = None
        with get_portfolio_lock():
            for sym, cur in ((base, ccy), (ccy, base)):
                cand = Forex(sym + cur)
                try:
                    ib.qualifyContracts(cand)
                except Exception:
                    cand = None
                if cand is not None and getattr(cand, "conId", 0):
                    pair = cand
                    break
        if pair is None:
            log.warning("portfolio_fx_no_pair", ccy=ccy, base=base)
            return True

        rate = _fx_rate_ccy_per_base(ib, pair, base, ccy)
        plan = _fx_conversion_plan(base, ccy, shortfall, rate, pair.symbol,
                                   idealpro_min_base, min_convert)
        if not plan["place"]:
            if plan["reason"] == "below_min":
                log.info("portfolio_fx_below_idealpro_min", ccy=ccy, base=base,
                         base_value=round(plan["base_value"]), min_base=round(idealpro_min_base))
            elif plan["reason"] == "no_rate":
                log.warning("portfolio_fx_no_rate", ccy=ccy, base=base)
            return True                   # proceed; IBKR auto-FX funds the sub-min / unpriced leg

        # Above the IDEALPRO minimum → place the real conversion and verify the fill (fail-closed).
        with get_portfolio_lock():
            trade = ib.placeOrder(pair, MarketOrder(plan["action"], plan["qty"]))
        waited = 0.0
        while waited < wait_secs:
            with get_portfolio_lock():
                ib.sleep(1.0)
            waited += 1.0
            st = (trade.orderStatus.status or "")
            if st in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                break
        status = trade.orderStatus.status or ""
        filled = float(trade.orderStatus.filled or 0.0)
        if status == "Filled" or filled >= plan["qty"] * 0.9:
            log.info("portfolio_fx_funded", ccy=ccy, base=base,
                     pair=pair.symbol + pair.currency, action=plan["action"],
                     qty=plan["qty"], status=status)
            return True
        # Didn't fill — cancel the leftover so it doesn't linger as a working order, then fail closed.
        try:
            with get_portfolio_lock():
                ib.cancelOrder(trade.order)
        except Exception:
            pass
        log.warning("portfolio_fx_fund_unfilled", ccy=ccy, pair=pair.symbol + pair.currency,
                    action=plan["action"], qty=plan["qty"], status=status, filled=filled)
        return False
    except Exception as e:
        log.warning("portfolio_fx_fund_failed", ccy=ccy, error=str(e))
        return False


def _ccy_cash(ib, ccy: str) -> float | None:
    """Settled CashBalance in ONE currency (can be negative on a margin debit). None if unreadable.

    Distinct from TotalCashValue, which is the base-converted sum across every currency: the account
    can hold a healthy base-currency total while the EUR line is ~zero, and it is the EUR line that an
    IDEALPRO EUR→foreign conversion draws on. Selling EUR you don't hold is what IBKR rejects with
    Error 201 'FX trade would expose account to currency leverage'. Treating TotalCashValue as the EUR
    line is also how the park drained: it read 'excess cash' off other currencies' balances and bought
    XEON with EUR it did not have, on margin, until the EUR line sat near zero.

    Returns None rather than 0.0 on a read failure so callers fail closed — a transient
    accountValues() error must never be mistaken for 'no cash' and trigger a park liquidation."""
    try:
        with get_portfolio_lock():
            vals = ib.accountValues()
        for v in vals:
            if v.tag == "CashBalance" and (v.currency or "").upper() == (ccy or "").upper():
                return float(v.value)
        return 0.0                        # connected, no line for this ccy → genuinely zero
    except Exception:
        return None


def _unpark_yield(ib, cfg, needed: float, settle_ccy: str | None = None) -> bool:
    """Sell enough of the cash-yield ETF (XEON) to raise ~`needed` of cash BEFORE a stock buy, so the
    buy is funded from the parked reserve instead of opening a margin loan. XEON is a EUR overnight-rate
    (€STR) money-market ETF (tight spreads, ~flat accruing NAV) so a sell is effectively a cash withdrawal.

    Returns True if the buy MAY proceed (disabled/readonly, nothing parked, ETF-currency cash already
    covers the need, or the sell filled). Returns False ONLY when this unpark is the AUTHORITATIVE
    funding path — i.e. the settlement currency equals the ETF currency, so its proceeds directly fund
    the buy (no FX leg) — liquid cash in that currency is short, AND the sell failed to fill. In that
    one case the caller SKIPS rather than draw margin. For a foreign buy the FX leg is the real funding
    gate, so an unpark hiccup there stays best-effort and never blocks (returns True)."""
    if not getattr(cfg, "cash_yield_enabled", False) or getattr(cfg, "readonly", False) or needed <= 0:
        return True
    sym = getattr(cfg, "cash_yield_symbol", None)
    if not sym:
        return True
    park_ccy = (getattr(cfg, "cash_yield_currency", "USD") or "USD").upper()
    # Authoritative only when the ETF's proceeds settle the buy directly (same currency, no FX leg).
    fatal_on_fail = (settle_ccy or "").upper() == park_ccy
    try:
        # Fund from LIQUID CASH in the relevant currency FIRST — only sell the ETF for the genuine
        # shortfall, so routine buys draw the un-parked cash runway (left for exactly this) instead of
        # churning the reserve on every buy. Same-ccy buy → the ETF/settlement currency; a foreign buy's
        # FX leg draws BASE-ccy cash, so check that. (Was gated on fatal_on_fail, so foreign USD buys
        # always sold XEON even with ample EUR cash — needless spread churn.)
        _cash_ccy = park_ccy if fatal_on_fail else (getattr(cfg, "base_currency", park_ccy) or park_ccy).upper()
        # `needed` arrives in the stock's SETTLEMENT (local) currency (£ for AZN, ¥ for 4385, HK$ for
        # SEHK …), but `have` below and the XEON price used to size the sale are in the park/base currency
        # (EUR). Convert to base FIRST — otherwise a high-value ccy is sized as if it were EUR and the
        # ENTIRE park gets dumped: the 4385 case sent ¥11.8M through int(needed/€149) → 71,098 shares
        # (the whole park), which never fills, so no EUR is freed and the downstream FX leg goes Inactive.
        # to_base passes an unknown/missing rate through unscaled (same-ccy EUR buys are a no-op).
        from src.portfolio import fx as _pfx
        needed_base = _pfx.to_base(needed, settle_ccy, _pfx.load_fx_rates()) if settle_ccy else needed
        have = _ccy_cash(ib, _cash_ccy)
        if have is None:
            # Couldn't read the cash line — don't guess. Guessing 0.0 here would sell park shares we
            # may not need to sell; guessing "plenty" would open a margin loan. Fail closed.
            log.warning("portfolio_unpark_cash_unreadable", symbol=sym, ccy=_cash_ccy)
            return not fatal_on_fail
        if have >= needed_base:
            return True                   # liquid cash already covers it — don't sell the ETF
        shortfall = max(0.0, needed_base - have)
        with get_portfolio_lock():
            positions = ib.positions()
        pos = next((p for p in positions
                    if getattr(getattr(p, "contract", None), "symbol", None) == sym), None)
        held_shares = int(getattr(pos, "position", 0) or 0) if pos else 0
        if held_shares <= 0:
            # Nothing parked. The upstream free_cash gate already bounds deployment to settled cash +
            # parked, so 'nothing parked' means settled cash is the funding source — proceed.
            return True
        # A sale is genuinely required. The park ETF trades on ONE venue (XEON/Xetra) and a sell placed
        # while it is shut cannot fill, so it frees no cash for THIS buy — and because every ~30s
        # executor retry placed a fresh MarketOrder that nothing ever cancelled, twenty queued overnight
        # and all filled at the 07:00 UTC open: 7,394 shares (~€1.1M) liquidated for buys that never
        # happened, then re-parked an hour later. Never place into a closed venue. This check sits AFTER
        # the cash-first and nothing-parked returns above so an already-funded buy is never blocked
        # merely because the park is shut — it doesn't need the park at all.
        if not _market_open(park_ccy):
            log.info("portfolio_unpark_skipped_market_closed", symbol=sym, park_ccy=park_ccy,
                     settle_ccy=settle_ccy, needed=round(needed), shortfall=round(shortfall))
            return not fatal_on_fail
        contract = Stock(sym, "SMART", cfg.cash_yield_currency)
        with get_portfolio_lock():
            ib.qualifyContracts(contract)
            bars = ib.reqHistoricalData(contract, endDateTime="", durationStr="2 D",
                                        barSizeSetting="1 day", whatToShow="TRADES",
                                        useRTH=False, formatDate=1, timeout=8)
        price = float(bars[-1].close) if bars else None
        if not price or price <= 0:
            return not fatal_on_fail       # can't price the ETF; only fatal if its proceeds were required
        shares = min(held_shares, int(shortfall / price) + 1)
        if shares <= 0:
            return True
        order = MarketOrder("SELL", shares)
        order.tif = "DAY"
        with get_portfolio_lock():
            trade = ib.placeOrder(contract, order)
            ib.sleep(2)
        status, filled = "", 0.0
        try:
            status = trade.orderStatus.status or ""
            filled = float(trade.orderStatus.filled or 0.0)
        except Exception:
            pass
        if status == "Filled" or filled >= shares * 0.9:
            log.info("portfolio_unparked", symbol=sym, shares=shares,
                     raised=round(shares * price), status=status)
            return True
        # Didn't fill — cancel the leftover so it can't rest and fill later against a buy that has
        # long since expired (see the market-closed note above). Mirrors _ensure_currency_funding.
        try:
            with get_portfolio_lock():
                ib.cancelOrder(trade.order)
        except Exception:
            pass
        log.warning("portfolio_unpark_unfilled", symbol=sym, shares=shares, status=status, filled=filled)
        return not fatal_on_fail
    except Exception as e:
        log.warning("portfolio_unpark_error", error=str(e))
        return not fatal_on_fail


def execute_portfolio_buy_suggestion(suggestion_id: int) -> str:
    """Place the real LimitOrder for an approved portfolio buy_stock suggestion.

    Mirrors the options side's approve -> queued -> executor flow (_execute_approved_order_inner),
    but places the order on the PORTFOLIO IBKR connection (U26413485) — the options executor's
    buy_stock branch was a no-op stub and never routed here, which is why approved portfolio buys
    never reached IBKR. Returns the resulting suggestion status string.

    Idempotent: marks the suggestion 'executing' before placing so a second pass can't double-fire,
    and skips if an open order for the same symbol already exists on the portfolio account.
    """
    from src.core.suggestions import TradeSuggestion
    from src.core.config import get_settings
    from src.portfolio.connection import (
        get_portfolio_ib, is_portfolio_connected,
        refresh_portfolio_pending_orders_cache,
    )

    # ── Load + validate, then claim the suggestion (status='executing') ──
    with get_db() as db:
        s = db.query(TradeSuggestion).filter(TradeSuggestion.id == suggestion_id).first()
        if not s or s.source != "portfolio" or s.action != "buy_stock":
            return "skip"
        if s.status not in ("approved", "queued", "executing"):
            return s.status
        if not s.quantity or s.quantity <= 0 or not s.limit_price or s.limit_price <= 0:
            s.status = "approved"
            s.review_note = "Invalid quantity/price — not placed"
            return "approved"

        if getattr(get_settings().portfolio, "readonly", False):
            s.review_note = "Portfolio account read-only — order not placed"
            return s.status
        if not is_portfolio_connected():
            s.review_note = "Portfolio IBKR disconnected — will retry next cycle"
            return s.status

        # Exchange/currency for the stock contract come from the watchlist row.
        wl = db.query(PortfolioWatchlist).filter(
            PortfolioWatchlist.symbol == s.symbol).first()
        exch = (wl.exchange if wl and wl.exchange else "SMART")
        ccy = (wl.currency if wl and wl.currency else "USD")
        symbol = s.symbol
        shares = int(s.quantity)
        limit_price = round(float(s.limit_price), 2)

        s.status = "executing"
        s.review_note = "Placing order on portfolio account"
        db.commit()

    # ── Place the order on the live portfolio connection ──
    try:
        _ensure_event_loop()
        ib = get_portfolio_ib()

        # Dedup: if an order for this symbol is already working, don't place another.
        try:
            with get_portfolio_lock():
                open_trades = ib.openTrades()
            for t in open_trades:
                c = getattr(t, "contract", None)
                if c is not None and getattr(c, "symbol", None) == symbol \
                        and getattr(c, "secType", "STK") == "STK":
                    with get_db() as db:
                        s = db.query(TradeSuggestion).filter(
                            TradeSuggestion.id == suggestion_id).first()
                        if s:
                            s.status = "submitted"
                            s.review_note = "Order already open on IBKR"
                    log.info("portfolio_exec_dedup_open_order", id=suggestion_id, symbol=symbol)
                    return "submitted"
        except Exception as e:
            log.debug("portfolio_exec_dedup_check_failed", error=str(e))

        # Qualify on the NATIVE listing exchange, then route where IBKR SAYS this contract can route
        # (its validExchanges) — don't hardcode a rule. Two independent failure modes forced this:
        #   1. Qualifying directly on SMART returns Error 200 "No security definition" for symbols SMART
        #      can't resolve by ticker (numeric HK/Japan tickers 3690/2318/4385, NSE, ...) — order never
        #      places. Native qualification resolves the conId AND pins the correct listing for dual-listed
        #      names (ASML on AEB vs NASDAQ, AZN on LSE vs its US ADR).
        #   2. Routing is venue-specific and CONTRADICTORY: SMART fills LSE/AEB but HANGS on TSEJ (Tokyo)
        #      and Error-200s on TASE (Tel Aviv) at placement; native/direct fills those but is rejected on
        #      ASX with Error 10311. No blanket "always SMART" / "always native" rule works (each breaks the
        #      other set). So ask IBKR: route SMART iff it lists SMART in validExchanges for THIS contract,
        #      else route on the native exchange. reqContractDetails returns both the qualified contract
        #      (conId) and validExchanges in one call. US names (exch == SMART) are unchanged.
        if exch and exch != "SMART":
            with get_portfolio_lock():
                details = ib.reqContractDetails(Stock(symbol, exch, ccy))
            if not details:
                log.warning("portfolio_order_qualify_failed", id=suggestion_id, symbol=symbol,
                            exchange=exch, currency=ccy)
                with get_db() as db:
                    s = db.query(TradeSuggestion).filter(
                        TradeSuggestion.id == suggestion_id).first()
                    if s:
                        s.status = "approved"   # transient — leave for the next cycle to retry
                        s.review_note = f"Contract not resolved on {exch} yet — will retry"
                return "approved"
            contract = details[0].contract                          # qualified: conId + correct listing
            valid = [e.strip() for e in (details[0].validExchanges or "").split(",") if e.strip()]
            contract.exchange = "SMART" if "SMART" in valid else exch
            log.info("portfolio_order_routing", id=suggestion_id, symbol=symbol,
                     route=contract.exchange, native=exch, smart_valid=("SMART" in valid),
                     valid_exchanges=details[0].validExchanges)
        else:
            contract = Stock(symbol, "SMART", ccy)
            with get_portfolio_lock():
                ib.qualifyContracts(contract)

        # LSE/GBP stocks quote in PENCE (GBX). The analyzer normalises IBKR's pence quotes to pounds
        # (÷100, analyzer.py), so the suggestion's limit_price is in pounds — but an LSE order must be
        # priced in pence. Multiply back by 100 (this exactly reverses the analyzer's ÷100, so it
        # equals IBKR's original quote). Share count is unit-independent, so only the price changes.
        order_price = round(limit_price * 100.0, 2) if (ccy or "").upper() == "GBP" else limit_price
        # Fund the foreign-currency leg up front (settlement amount: GBP order_price is in pence → ÷100
        # for pounds) so the buy doesn't silently open a margin loan in the foreign ccy.
        notional_settle = shares * (order_price / 100.0 if (ccy or "").upper() == "GBP" else order_price)
        # Un-park the park-ETF (XEON) reserve, then convert the FX leg, then GATE on the authoritative funding
        # path for this currency. If it could not be funded, leave the suggestion 'approved' (retry next
        # cycle) rather than place an order that silently opens a margin loan (fail-closed).
        pcfg = get_settings().portfolio
        unpark_ok = _unpark_yield(ib, pcfg, notional_settle, settle_ccy=ccy)
        fx_ok = _ensure_currency_funding(ib, ccy, pcfg.base_currency, notional_settle, cfg=pcfg)
        funded = fx_ok if (ccy or "").upper() != (pcfg.base_currency or "EUR").upper() else unpark_ok
        if not funded:
            # The suggestion-execution job runs every ~30s, so a permanently-unfundable foreign leg
            # would re-fire (and re-spam IBKR) forever while sitting 'approved'. Count the attempts and
            # EXPIRE + alert once the cap is hit, so a broken FX pair / missing permission is visible
            # instead of a silent perpetual retry. (A genuinely transient shortfall recovers well
            # within the cap.)
            max_tries = int(getattr(pcfg, "fx_funding_max_attempts", 6) or 6)
            log.warning("portfolio_suggestion_unfunded_skip", id=suggestion_id, symbol=symbol,
                        ccy=ccy, notional=round(notional_settle))
            with get_db() as db:
                s = db.query(TradeSuggestion).filter(TradeSuggestion.id == suggestion_id).first()
                if s:
                    s.funding_attempts = (s.funding_attempts or 0) + 1
                    if s.funding_attempts >= max_tries:
                        s.status = "expired"
                        s.review_note = (f"Could not fund {ccy} leg after {s.funding_attempts} "
                                         f"attempts (FX conversion failed) — needs review")
                        try:
                            from src.core.alerts import get_alert_manager
                            get_alert_manager().critical(
                                "Portfolio buy unfunded",
                                f"{symbol} ({ccy}) expired after {s.funding_attempts} failed "
                                f"FX-funding attempts. Check FX permission / pair for {ccy}.")
                        except Exception:
                            pass
                        return "expired"
                    s.status = "approved"   # retry next cycle; do not place an unfunded (margin) order
                    s.review_note = (f"Skipped — could not fund {ccy} leg (try {s.funding_attempts}/"
                                     f"{max_tries}); will retry")
            return "approved"
        order = LimitOrder("BUY", shares, order_price)
        order.tif = "DAY"
        order.outsideRth = _outside_rth_ok(ccy)

        with get_portfolio_lock():
            trade = ib.placeOrder(contract, order)
            ib.sleep(2)

        try:
            refresh_portfolio_pending_orders_cache()
        except Exception:
            pass

        fill_status = "Submitted"
        try:
            fill_status = trade.orderStatus.status or "Submitted"
        except Exception:
            pass

        with get_db() as db:
            s = db.query(TradeSuggestion).filter(
                TradeSuggestion.id == suggestion_id).first()
            if s:
                if fill_status == "Filled":
                    s.status = "executed"
                    s.review_note = "Filled"
                elif fill_status in ("Cancelled", "ApiCancelled", "Inactive", "Rejected"):
                    # Terminal non-fill (no trading permission, direct-route block, etc.). REJECT —
                    # do NOT bounce back to 'approved', which made the 30s executor hammer the same
                    # doomed order forever (the AZN/XRO loop). The 2-hourly scan re-creates a fresh
                    # suggestion for still-green names, so genuinely-tradeable names retry at that
                    # cadence; permanently-blocked ones just show their reason and stop churning.
                    s.status = "rejected"
                    s.review_note = f"Order {fill_status} — not placed"
                    # If the rejection is a permission / no-security-definition error (the account
                    # can't trade this exchange yet — e.g. HK/3690), pause the symbol so the scan
                    # stops re-creating doomed suggestions and the daily budget flows to fillable
                    # names. Auto-expires; resumes once perms are granted.
                    if _order_blocked_by_permission(trade):
                        _mark_permission_blocked(symbol)
                        s.review_note = (f"Order {fill_status} — {ccy} not permissioned yet; "
                                         f"paused (auto-retries later, doesn't block budget)")
                else:
                    s.status = "submitted"
                    s.review_note = f"Order {fill_status} — awaiting fill"
        log.info("portfolio_suggestion_order_placed", id=suggestion_id, symbol=symbol,
                 shares=shares, price=limit_price, exchange=exch, currency=ccy,
                 order_status=fill_status)
        return fill_status

    except Exception as e:
        err = str(e) or type(e).__name__
        log.error("portfolio_suggestion_execution_failed", id=suggestion_id,
                  symbol=symbol, error=err)
        with get_db() as db:
            s = db.query(TradeSuggestion).filter(
                TradeSuggestion.id == suggestion_id).first()
            if s:
                s.status = "approved"  # back to approved so it can retry / be visible
                s.review_note = f"Execution failed: {err}"
        return "approved"
