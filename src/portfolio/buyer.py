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
            if not self._is_market_open(s.currency):
                continue
            try:
                a = self.analyzer.analyze_stock(s.symbol, s.exchange, s.currency, tier=s.tier)
            except Exception as e:
                log.warning("compounder_analyze_error", symbol=s.symbol, error=str(e))
                a = None
            if not a or not a.current_price or a.current_price <= 0:
                continue
            self._update_watchlist_metrics(s, a)
            analyses[s.symbol] = (s, a)
            names.append(cmp.NameInput(
                symbol=s.symbol, tier=(s.tier or "growth"),
                growth=s.growth_score or 0.0, forward_growth=s.forward_growth_score or 0.0,
                quality=s.quality_score or 0.0, valuation=s.valuation_score or 0.0,
                dividend_total_return=s.dividend_total_return_score or 0.0,
                risk_penalty=s.risk_total_penalty or 0.0,
                price=a.current_price, sma200=a.sma_200, high_52w=a.high_52w,
                momentum_12_1=getattr(a, "momentum_12_1", None),
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

        held = self._get_holdings_map()           # symbol -> FILLED market value (excludes the bill-ETF)
        deployed = sum(held.values())
        parked = self._get_parked_value()         # cash reserve parked in the bill ETF (sellable T+1)
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
        # The cash reserve is PARKED in the bill ETF for yield; it's sellable on demand, so count it as
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
        _today = datetime.utcnow().strftime("%Y-%m-%d")
        with get_db() as _db:
            _fills_today = float(_db.query(
                _func.coalesce(_func.sum(PortfolioTransaction.amount), 0.0)
            ).filter(
                PortfolioTransaction.action == "buy",
                PortfolioTransaction.created_at >= _today + " 00:00:00",
            ).scalar() or 0.0)
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
                    _fills_window = float(_db.query(
                        _func.coalesce(_func.sum(PortfolioTransaction.amount), 0.0)
                    ).filter(PortfolioTransaction.action == "buy",
                             PortfolioTransaction.created_at >= _wstart).scalar() or 0.0)
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

        # Park any standing idle cash (the crash reserve + undeployed slack) into the bill ETF for yield;
        # the executor un-parks it just-in-time when it's time to deploy. Cash backing this scan's intended
        # buys (`spent`) and still-working orders is left liquid so fills don't open a margin loan.
        self._park_compounder_excess(nlv, spent)

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
        """Get symbol → market value map for all holdings. Excludes the cash-yield ETF (SGOV) — that's
        a parked cash RESERVE, not a deployed compounder position, so it must not count toward targets."""
        park = getattr(self.cfg, "cash_yield_symbol", None)
        with get_db() as db:
            holdings = db.query(PortfolioHolding).filter(
                PortfolioHolding.shares > 0
            ).all()
            return {
                h.symbol: (h.market_value or h.total_invested or 0)
                for h in holdings if h.symbol != park
            }

    def _get_parked_value(self) -> float:
        """Market value of the cash reserve parked in the bill ETF (SGOV) — sellable on demand, so it
        counts as deployable cash (the executor un-parks it just-in-time before placing a buy)."""
        park = getattr(self.cfg, "cash_yield_symbol", None)
        if not park or not getattr(self.cfg, "cash_yield_enabled", False):
            return 0.0
        with get_db() as db:
            h = db.query(PortfolioHolding).filter(
                PortfolioHolding.symbol == park, PortfolioHolding.shares > 0
            ).first()
            return float(h.market_value or h.total_invested or 0.0) if h else 0.0

    def _park_compounder_excess(self, nlv: float, spent_this_scan: float) -> None:
        """Park standing idle cash (the crash reserve + undeployed slack) into the bill ETF for yield.
        Parks only cash that is genuinely idle: settled cash above the operational buffer, minus cash
        committed to still-working BUY orders and minus this scan's intended deployment (`spent`), so a
        resting/approved buy never has its funding parked out from under it (which would open a loan).
        Best-effort and gated on cash_yield_enabled + not-readonly; un-parking is JIT in the executor."""
        cc = self.cfg.compounder
        if not getattr(self.cfg, "cash_yield_enabled", False) or getattr(self.cfg, "readonly", False):
            return
        try:
            settled = self._get_settled_cash() or 0.0
            open_buy_now = sum(self._open_buy_map().values())
            excess = settled - nlv * cc.cash_buffer_pct - open_buy_now - max(0.0, spent_this_scan)
            if excess < getattr(cc, "cash_park_min", 5000.0):
                return
            self._park_cash(amount=excess)
        except Exception as e:
            log.warning("compounder_park_excess_error", error=str(e))

    def _cancel_stale_compounder_buys(self, watchlist_symbols: set) -> int:
        """Cancel still-resting compounder stock BUY orders so the scan re-prices them at the current
        market. Only watchlist symbols are touched (the cash-park ETF and any non-universe/manual
        orders are left alone); filled shares are holdings, not open orders, so they survive. Only
        working/unfilled BUY orders are cancelled — the scan then re-places the names that are still
        green & underweight at the current price (and simply doesn't re-place ones now above fair)."""
        cancelled = 0
        try:
            _ensure_event_loop()
            park_sym = getattr(self.cfg, "cash_yield_symbol", None)
            _TERMINAL = {"Filled", "Cancelled", "ApiCancelled", "Inactive", "PendingCancel"}
            with get_portfolio_lock():
                trades = list(self.ib.openTrades())
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
                with get_portfolio_lock():
                    self.ib.cancelOrder(o)
                cancelled += 1
                log.info("compounder_cancel_stale_buy", symbol=sym,
                         order_id=getattr(o, "orderId", None), status=st)
            if cancelled:
                with get_portfolio_lock():
                    self.ib.sleep(2)   # let the cancels settle before the scan reads open orders
        except Exception as e:
            log.warning("compounder_cancel_stale_buys_failed", error=str(e))
        if cancelled:
            log.info("compounder_stale_buys_cancelled", count=cancelled)
        return cancelled

    def _open_buy_map(self) -> dict[str, float]:
        """symbol → notional ($) of currently-RESTING stock BUY orders at IBKR.

        Holdings reflect only FILLED shares (sync truth), so resting DAY-limit rungs from an
        earlier scan/day are invisible to _get_holdings_map. Netting this into both the per-name
        underweight check and the daily-budget gate prevents (a) re-laddering a name that already
        has working orders and (b) deploying today's budget twice intraday. Reads the pending-order
        cache; caller should refresh it first so the snapshot is current.
        """
        from src.portfolio.connection import get_cached_portfolio_pending_orders
        out: dict[str, float] = {}
        for o in get_cached_portfolio_pending_orders() or []:
            try:
                if o.get("sec_type") != "STK" or o.get("action") != "BUY":
                    continue
                rem = float(o.get("remaining") or 0)
                px = float(o.get("limit_price") or 0)
                if rem <= 0 or px <= 0:
                    continue
                # LSE/GBP orders quote in PENCE — convert to pounds so the notional matches
                # holdings/targets (all base-ccy). Without this an AZN order counts ~100× its
                # value (14222 × 33 ≈ 469k) and swamps the daily budget → budget stuck at 0.
                if (o.get("currency") or "").upper() == "GBP":
                    px = px / 100.0
                out[o["symbol"]] = out.get(o["symbol"], 0.0) + rem * px
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
        out: dict[str, float] = {}
        try:
            with get_db() as db:
                rows = db.query(TradeSuggestion).filter(
                    TradeSuggestion.source == "portfolio",
                    TradeSuggestion.action == "buy_stock",
                    TradeSuggestion.status.in_(("pending", "approved", "queued", "executing")),
                ).all()
                for s in rows:
                    notional = float(s.est_cost or 0.0)
                    if notional <= 0 and s.quantity and s.limit_price:
                        notional = float(s.quantity) * float(s.limit_price)
                    if notional > 0:
                        out[s.symbol] = out.get(s.symbol, 0.0) + notional
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
        cc = self.cfg.compounder
        # NLV-scaled core-rung floor (the caller's brick already cleared it); fall back to the
        # configured cap if not threaded through, so the method is safe to call standalone.
        core_floor = min_buy if min_buy is not None else cc.min_single_buy
        plan = cmp.ladder_plan(px, urgency, is_leader, cc)
        if not plan:
            return (0.0, 0.0)

        # Resolve rungs to (price, shares); core sized from core_amount, dips as frac of it.
        rungs: list[tuple[float, int]] = []
        for i, (price, frac) in enumerate(plan):
            if price <= 0:
                continue
            shares = int((core_amount * frac) / price)
            notional = shares * price
            # Core rung must clear the NLV-scaled min order; dip rungs just need to be non-trivial.
            floor = core_floor if i == 0 else 1000.0
            if shares <= 0 or notional < floor:
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
            est = round(core_shares * core_price, 2)
            return (est, est)

        # Live mode — place each rung as a DAY limit, core first, bounded by cash_room.
        # A broker error on one symbol must not abort the whole scan — return whatever filled.
        core_placed = 0.0
        total_placed = 0.0
        try:
            contract = Stock(stock.symbol, stock.exchange, stock.currency)
            with get_portfolio_lock():
                self.ib.qualifyContracts(contract)

            # Un-park the bill-ETF reserve, then fund the FX leg, then GATE on the authoritative funding
            # path for this currency: the FX conversion for a foreign buy, or the SGOV unpark for a
            # same-currency buy. If that path could not be funded, SKIP this name rather than silently
            # open a margin loan (fail-closed). The 4-hourly scan re-creates the buy when cash is ready.
            total_notional = sum(price * shares for price, shares in rungs)
            base_ccy = getattr(self.cfg, "base_currency", "EUR") or "EUR"
            ccy = stock.currency or "USD"
            unpark_ok = _unpark_yield(self.ib, self.cfg, total_notional, settle_ccy=ccy)
            fx_ok = _ensure_currency_funding(self.ib, ccy, base_ccy, total_notional, cfg=self.cfg)
            funded = fx_ok if ccy.upper() != base_ccy.upper() else unpark_ok
            if not funded:
                log.warning("compounder_buy_unfunded_skip", symbol=stock.symbol,
                            ccy=ccy, notional=round(total_notional))
                return (0.0, 0.0)

            room = cash_room
            for i, (price, shares) in enumerate(rungs):
                notional = shares * price
                if notional > room:
                    continue                      # can't afford this rung — skip (core has priority)
                order = LimitOrder("BUY", shares, price)
                order.tif = "DAY"
                order.outsideRth = _outside_rth_ok(stock.currency)
                with get_portfolio_lock():
                    self.ib.placeOrder(contract, order)
                    self.ib.sleep(1)
                room -= notional
                total_placed += notional
                if i == 0:
                    core_placed = notional
                log.info("compounder_rung_placed", symbol=stock.symbol, rung=i,
                         shares=shares, price=price, notional=round(notional),
                         leader=is_leader)
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
                self.ib.placeOrder(contract, order)
                self.ib.sleep(1)

            # Refresh dashboard cache so new order appears immediately
            try:
                from src.portfolio.connection import refresh_portfolio_pending_orders_cache
                refresh_portfolio_pending_orders_cache()
            except Exception:
                pass

            log.info("portfolio_cash_parked", symbol=self.cfg.cash_yield_symbol,
                     shares=shares, amount=round(shares * price, 2))
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
        with get_db() as db:
            h = db.query(PortfolioHolding).filter(
                PortfolioHolding.symbol == symbol
            ).first()
            return h.market_value or h.total_invested if h else 0

    # ── Database helpers ─────────────────────────────────────
    def _get_watchlist(self) -> list[PortfolioWatchlist]:
        with get_db() as db:
            all_stocks = db.query(PortfolioWatchlist).all()
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

    # Market hours by currency for portfolio scan filtering
    _MARKET_HOURS = {
        # (timezone, open_hour, close_hour)
        "USD": ("US/Eastern", 9, 16),
        "CAD": ("US/Eastern", 9, 16),
        "EUR": ("Europe/Berlin", 9, 17),
        "CHF": ("Europe/Berlin", 9, 17),
        "GBP": ("Europe/London", 8, 16),
        "NOK": ("Europe/Berlin", 9, 17),
        "SEK": ("Europe/Berlin", 9, 17),
        "DKK": ("Europe/Berlin", 9, 17),
        "JPY": ("Asia/Tokyo", 9, 15),
        "AUD": ("Australia/Sydney", 10, 16),
        "HKD": ("Asia/Hong_Kong", 9, 16),
    }

    def _is_market_open(self, currency: str) -> bool:
        """Check if the market for a given currency is currently open."""
        import pytz
        hours = self._MARKET_HOURS.get(currency)
        if not hours:
            return True  # unknown currency — scan anyway
        tz_name, open_h, close_h = hours
        tz = pytz.timezone(tz_name)
        now = datetime.now(tz)
        return now.weekday() < 5 and open_h <= now.hour < close_h

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


def _unpark_yield(ib, cfg, needed: float, settle_ccy: str | None = None) -> bool:
    """Sell enough of the cash-yield ETF (SGOV) to raise ~`needed` of cash BEFORE a stock buy, so the
    buy is funded from the parked reserve instead of opening a margin loan. SGOV is a 0–3mo T-bill ETF
    (penny spreads, ~flat NAV) so a market sell is effectively a cash withdrawal.

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
        shortfall = needed
        if fatal_on_fail:
            have = 0.0
            with get_portfolio_lock():
                vals = ib.accountValues()
            for v in vals:
                if v.tag == "CashBalance" and (v.currency or "").upper() == park_ccy:
                    have = float(v.value)
                    break
            if have >= needed:
                return True               # settlement-currency cash already covers it — no sell needed
            shortfall = needed - have
        with get_portfolio_lock():
            positions = ib.positions()
        pos = next((p for p in positions
                    if getattr(getattr(p, "contract", None), "symbol", None) == sym), None)
        held_shares = int(getattr(pos, "position", 0) or 0) if pos else 0
        if held_shares <= 0:
            # Nothing parked. The upstream free_cash gate already bounds deployment to settled cash +
            # parked, so 'nothing parked' means settled cash is the funding source — proceed.
            return True
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

        # Route via SMART (with the listing exchange as a hint) for SMART-eligible markets. A raw
        # direct-route to e.g. NASDAQ is rejected by IBKR's Precautionary Settings (error 10311 —
        # "directly routed orders may result in higher fees"), which silently cancels the order.
        # Only the genuinely non-SMART venues keep their native exchange.
        # Route EVERY order via SMART with the listing exchange as a hint. IBKR's Precautionary
        # Settings reject direct-routed orders (error 10311 — GOOG/NASDAQ, XRO/ASX), so SMART is the
        # only path that fills, and SMART supports the venues in this universe (ASX, SEHK, LSE, AEB,
        # BVME, ...). Don't special-case "non-SMART" exchanges to direct routing — that was the XRO bug.
        if exch and exch != "SMART":
            contract = Stock(symbol, "SMART", ccy, primaryExchange=exch)
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
        # Un-park the bill-ETF reserve, then convert the FX leg, then GATE on the authoritative funding
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
