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

import asyncio
import math
from datetime import datetime
from typing import Optional

from ib_insync import IB, Stock, Option, LimitOrder, MarketOrder

from src.core.logger import get_logger
from src.core.database import get_db
from src.portfolio.models import (
    PortfolioHolding, PortfolioTransaction, PortfolioWatchlist,
    PortfolioPutEntry, PortfolioState,
)
from src.portfolio.analyzer import PortfolioAnalyzer, StockAnalysis
from src.portfolio.config import PortfolioConfig
from src.portfolio.ranker import (
    MarketRegime, CashPolicy, RankedSignal,
    rank_signals, detect_market_regime,
)

log = get_logger(__name__)


def _ensure_event_loop():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


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
    def _detect_regime(self) -> MarketRegime:
        """Gather SPY + VIX data to determine market regime."""
        _ensure_event_loop()

        spy_data = {}
        vix = None

        try:
            # Get VIX
            from ib_insync import Index
            vix_contract = Index("VIX", "CBOE", "USD")
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
        """Get symbol → market value map for all holdings."""
        with get_db() as db:
            holdings = db.query(PortfolioHolding).filter(
                PortfolioHolding.shares > 0
            ).all()
            return {
                h.symbol: (h.market_value or h.total_invested or 0)
                for h in holdings
            }

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
        - Composite score > 70 (very strong signal) → direct buy
        - Deep discount (>15% below SMA) → direct buy (rare opportunity)
        - RSI < 20 → direct buy (extreme oversold, grab it)
        - Volume surge + trend healthy → direct buy (capitulation in uptrend)
        - Otherwise → sell put at target strike (get paid to wait)
        """
        if not self.cfg.put_entry.enabled:
            return "direct_buy"

        # Very strong composite signal → buy now
        if analysis.composite_score > 70:
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
                           funding_source: str = "cash") -> bool:
        """
        Sell a cash-secured put at the target buy price.
        Strike = current price * (1 - target_discount_pct/100)
        """
        _ensure_event_loop()

        if not analysis.current_price or analysis.current_price <= 0:
            return False

        try:
            # Calculate target strike
            target_discount = self.cfg.put_entry.target_discount_pct / 100
            target_strike = analysis.current_price * (1 - target_discount)

            # Find available option chain
            contract = Stock(stock.symbol, stock.exchange, stock.currency)
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
            qualified = self.ib.qualifyContracts(opt)
            if not qualified or opt.conId <= 0:
                return False

            # Get option price using Black-Scholes from historical IV
            from src.broker.greeks import compute_put_greeks, get_current_iv
            iv = get_current_iv(self.ib, stock.symbol, exchange=stock.exchange, currency=stock.currency)
            if iv and iv > 0:
                from datetime import datetime as dt
                exp_date = dt.strptime(best_exp, "%Y%m%d").date()
                T = max((exp_date - today).days, 1) / 365.0
                greeks = compute_put_greeks(analysis.current_price, best_strike, T, iv)
                if greeks:
                    sell_price = greeks.bid  # use bid for selling
                else:
                    log.debug("portfolio_no_option_price", symbol=stock.symbol)
                    return False
            else:
                log.debug("portfolio_no_iv_for_option", symbol=stock.symbol)
                return False

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
                    rationale=(
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
            trade = self.ib.placeOrder(opt, order)
            self.ib.sleep(2)

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

    # ── Black-Scholes helper ─────────────────────────────────
    @staticmethod
    def _bs_put_price(S: float, K: float, T: float, r: float, sigma: float) -> float:
        """Black-Scholes put price."""
        if T <= 0 or sigma <= 0 or S <= 0:
            return 0.0
        d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)

        def N(x):
            return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

        return K * math.exp(-r * T) * N(-d2) - S * N(-d1)

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
                    rationale=(
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
            self.ib.qualifyContracts(contract)

            order = LimitOrder("BUY", shares, limit_price)
            order.tif = "DAY"
            order.outsideRth = True

            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(2)

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

    # ── Cash management ──────────────────────────────────────
    def _park_cash(self):
        """Park excess cash in treasury ETF for yield."""
        if not self.cfg.cash_yield_enabled:
            return
        _ensure_event_loop()
        try:
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
            order.outsideRth = True
            self.ib.placeOrder(contract, order)
            self.ib.sleep(1)

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

    def _get_net_liquidation(self) -> float | None:
        try:
            _ensure_event_loop()
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
                entry.buy_signal = analysis.buy_signal
                entry.signal_type = analysis.signal_type if analysis.buy_signal else None
                entry.composite_score = analysis.composite_score
                entry.updated_at = datetime.utcnow()

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

        log.info("portfolio_metrics_updated", updated=updated, failed=failed, total=len(watchlist))

    def recalc_scores_from_db(self):
        """
        Recalculate composite scores using metrics already in the database.
        No IBKR connection needed — uses stored SMA, RSI, discount values.
        Useful when metrics were fetched but scores weren't saved yet.
        """
        from src.portfolio.analyzer import TIER_PARAMS

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
                    entry.composite_score = round(score, 1)
                    if score > 0 and not entry.buy_signal:
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
                    self.ib.qualifyContracts(contract)
                    contract.exchange = "SMART"
                    price = None
                    for what in ("TRADES", "MIDPOINT"):
                        try:
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
