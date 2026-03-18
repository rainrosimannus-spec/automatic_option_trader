"""
Core put selling logic — the main trade engine.
Scans universe, checks risk, sells puts on qualifying stocks.
"""
from __future__ import annotations

from datetime import datetime

from src.broker.orders import sell_put
from src.broker.market_data import get_stock_price
from src.core.config import get_settings
from src.core.database import get_db
from src.core.models import Trade, Position, TradeType, PositionStatus, OrderStatus
from src.core.logger import get_logger
from src.strategy.universe import UniverseManager
from src.strategy.screener import screen_puts
from src.strategy.risk import RiskManager

log = get_logger(__name__)


class PutSeller:
    """Scans the stock universe and sells puts where risk allows."""

    def __init__(self, universe: UniverseManager, risk: RiskManager):
        self.universe = universe
        self.risk = risk
        self.cfg = get_settings().strategy

    def run_scan(self, market: str | None = None) -> list[str]:
        """
        Main scan loop — iterate universe, screen, and trade.
        In suggestion mode: collects all candidates, ranks by score, creates
        sequentially numbered suggestions (#1 = best, #2 = second best, etc.)
        Returns list of symbols where puts were sold/suggested.
        """
        market_label = market or "ALL"
        log.info("put_scan_started", market=market_label)
        traded: list[str] = []

        # Expire pending OPTIONS suggestions from THIS market's previous scan
        # (don't touch suggestions from other markets or portfolio suggestions)
        from src.core.database import get_db
        from src.core.suggestions import TradeSuggestion
        from datetime import datetime as dt
        with get_db() as db:
            if market:
                market_symbols = [s.upper() for s in self.universe.symbols_for_market(market)]
                old_pending = db.query(TradeSuggestion).filter(
                    TradeSuggestion.status.in_(["pending", "queued"]),
                    TradeSuggestion.source == "options",
                    TradeSuggestion.symbol.in_(market_symbols) if market_symbols else False,
                ).all()
            else:
                old_pending = db.query(TradeSuggestion).filter(
                    TradeSuggestion.status.in_(["pending", "queued"]),
                    TradeSuggestion.source == "options",
                ).all()
            for s in old_pending:
                s.status = "expired"
                s.reviewed_at = dt.utcnow()
                s.review_note = f"Expired by new {market_label} scan"
            if old_pending:
                expired_syms = [s.symbol for s in old_pending]
                log.info("expired_old_suggestions", count=len(old_pending),
                         market=market_label, symbols=expired_syms)

        # Reset margin circuit breaker for this new scan cycle
        from src.core.suggestions import reset_margin_circuit_breaker
        reset_margin_circuit_breaker()

        # Refresh market regime once per scan (caches VIX + SPY MA)
        regime = self.risk.get_regime(force_refresh=True)
        current_vix = regime.vix

        # Determine if SPY MA gate is reducing entries
        spy_check = self.risk.check_spy_ma_gate()
        reduce_pct = spy_check.reduce_pct
        if reduce_pct < 1.0:
            log.info("bearish_reduction_active", reduce_pct=f"{reduce_pct:.0%}")

        # Get symbols — filter by market if specified
        import random
        if market:
            symbols = self.universe.symbols_for_market(market)
        else:
            symbols = list(self.universe.all_symbols)

        random.shuffle(symbols)
        if reduce_pct < 1.0:
            max_candidates = max(1, int(len(symbols) * reduce_pct))
            symbols = symbols[:max_candidates]
            log.info("universe_reduced", market=market_label, original=len(self.universe.all_symbols), scanning=len(symbols))

        # Suggestion mode: collect all candidates first, then rank
        cfg = get_settings()
        if cfg.app.suggestion_mode:
            candidates = []
            consecutive_failures = 0
            import time as _time
            for symbol in symbols:
                sym_start = _time.time()
                try:
                    result = self._evaluate_symbol(symbol, current_vix, market=market)
                    if result:
                        candidates.append(result)
                        consecutive_failures = 0
                    else:
                        # If a symbol took >10 seconds and returned None,
                        # it's likely a timeout/connection issue
                        if _time.time() - sym_start > 10:
                            consecutive_failures += 1
                        else:
                            consecutive_failures = 0
                except Exception as e:
                    log.error("put_scan_error", symbol=symbol, error=str(e))
                    consecutive_failures += 1

                if consecutive_failures >= 3:
                    log.warning("scan_aborted_connection_dead",
                                market=market_label,
                                consecutive_failures=consecutive_failures,
                                last_symbol=symbol)
                    break

            # Rank by screener score (highest = best trade)
            candidates.sort(key=lambda c: c["score"], reverse=True)

            for seq, cand in enumerate(candidates, start=1):
                self._create_ranked_suggestion(cand, rank=seq)
                traded.append(cand["symbol"])

            log.info("put_scan_completed", market=market_label,
                     candidates=len(candidates), suggestions=len(traded))
            return traded

        # Live mode: trade one-by-one
        for symbol in symbols:
            try:
                result = self._process_symbol(symbol, current_vix, market=market)
                if result:
                    traded.append(symbol)
                    self.risk.increment_daily_count()
            except Exception as e:
                log.error("put_scan_error", symbol=symbol, error=str(e))

        log.info("put_scan_completed", market=market_label, trades=len(traded), symbols=traded)
        return traded

    def _evaluate_symbol(self, symbol: str, current_vix: float | None, market: str | None = None) -> dict | None:
        """Evaluate a symbol for put-selling. Returns candidate dict or None."""
        log.info("scanning_symbol", symbol=symbol)

        risk_check = self.risk.can_open_put(symbol, market=market)
        if not risk_check.allowed:
            log.info("risk_blocked", symbol=symbol, reason=risk_check.reason)
            return None

        exchange = self.universe.get_exchange(symbol)
        opt_exchange = self.universe.get_options_exchange(symbol)
        currency = self.universe.get_currency(symbol)
        contract_size = self.universe.get_contract_size(symbol)
        delta_range = self.risk.get_dynamic_delta_range()

        candidate = screen_puts(symbol, exchange=opt_exchange, currency=currency, delta_override=delta_range, stock_exchange=exchange)
        if not candidate:
            return None

        from datetime import datetime as _dt
        premium = round(candidate.bid, 2)
        collateral = candidate.strike * contract_size

        try:
            exp_date = _dt.strptime(candidate.expiry, "%Y%m%d").date()
            dte = (exp_date - _dt.now().date()).days
        except Exception:
            dte = 0

        price = get_stock_price(symbol, exchange=exchange, currency=currency)

        return {
            "symbol": symbol,
            "exchange": exchange,
            "opt_exchange": opt_exchange,
            "currency": currency,
            "contract_size": contract_size,
            "candidate": candidate,
            "premium": premium,
            "collateral": collateral,
            "dte": dte,
            "price": price,
            "score": candidate.score,
        }

    def _create_ranked_suggestion(self, cand: dict, rank: int):
        """Create a ranked suggestion from an evaluated candidate."""
        from src.core.suggestions import create_suggestion
        candidate = cand["candidate"]
        premium = cand["premium"]
        dte = cand["dte"]
        symbol = cand["symbol"]

        create_suggestion(
            symbol=symbol,
            action="sell_put",
            quantity=self.cfg.contracts_per_stock,
            limit_price=premium,
            strike=candidate.strike,
            expiry=candidate.expiry,
            source="options",
            signal=f"delta={round(candidate.delta, 3)} DTE={dte}",
            rationale=(
                f"Rank #{rank} (score {cand['score']:.1f}). "
                f"Sell {candidate.expiry} ${candidate.strike}P @ ${premium} "
                f"(delta {round(candidate.delta, 3)}, IV {round(candidate.iv * 100, 1)}%)"
            ),
            current_price=cand["price"],
            est_cost=cand["collateral"],
            order_type="sell_put",
            rank=rank,
            rank_score=cand["score"],
            funding_source="cash",
            opt_exchange=cand.get("opt_exchange"),
            opt_currency=cand.get("currency"),
        )
        log.info("options_ranked_suggestion",
                 rank=rank, symbol=symbol,
                 strike=candidate.strike, expiry=candidate.expiry,
                 premium=premium, score=round(cand["score"], 1))

    def _process_symbol(self, symbol: str, current_vix: float | None, market: str | None = None) -> bool:
        """Evaluate and potentially trade a single symbol. Returns True if traded."""
        log.info("scanning_symbol", symbol=symbol)
        # Risk check
        risk_check = self.risk.can_open_put(symbol, market=market)
        if not risk_check.allowed:
            log.info("risk_blocked", symbol=symbol, reason=risk_check.reason)
            return False

        # Get exchange/currency for this stock
        exchange = self.universe.get_exchange(symbol)
        opt_exchange = self.universe.get_options_exchange(symbol)
        currency = self.universe.get_currency(symbol)
        contract_size = self.universe.get_contract_size(symbol)

        # Get dynamic delta range based on current VIX
        delta_range = self.risk.get_dynamic_delta_range()

        # Screen for best put contract with dynamic delta
        candidate = screen_puts(symbol, exchange=opt_exchange, currency=currency, delta_override=delta_range, stock_exchange=exchange)
        if not candidate:
            return False

        # Whatif margin check: ask IBKR exactly how much buying power this contract consumes
        try:
            from src.broker.orders import get_whatif_margin
            from src.broker.account import get_account_summary
            acct = get_account_summary()
            if acct and acct.net_liquidation > 0:
                nlv = acct.net_liquidation
                buying_power = acct.buying_power
                maintenance_margin = acct.maintenance_margin
                if nlv < 100_000:
                    total_capacity = nlv * 6
                    max_pct = 0.25
                else:
                    total_capacity = buying_power + maintenance_margin
                    max_pct = 0.15
                max_per_position = total_capacity * max_pct
                real_margin = get_whatif_margin(
                    symbol=symbol,
                    expiry=candidate.expiry,
                    strike=candidate.strike,
                    right="P",
                    quantity=self.cfg.contracts_per_stock,
                    limit_price=round(candidate.bid, 2),
                    exchange=opt_exchange,
                    currency=currency,
                )
                if real_margin and real_margin > max_per_position:
                    log.info("whatif_margin_blocked",
                             symbol=symbol,
                             real_margin=f"${real_margin:,.0f}",
                             max_per_position=f"${max_per_position:,.0f}",
                             total_capacity=f"${total_capacity:,.0f}",
                             max_pct=f"{max_pct:.0%}")
                    return False
                log.info("whatif_margin_passed",
                         symbol=symbol,
                         real_margin=f"${real_margin:,.0f}" if real_margin else "n/a",
                         max_per_position=f"${max_per_position:,.0f}")
        except Exception as e:
            log.warning("whatif_margin_check_failed", symbol=symbol, error=str(e))
            # fail open — don't block if whatif unavailable

        # Suggestion mode: create suggestion instead of placing order
        cfg = get_settings()
        if cfg.app.suggestion_mode:
            from src.core.suggestions import create_suggestion
            from datetime import datetime as _dt
            premium = round(candidate.bid, 2)
            collateral = candidate.strike * contract_size
            # Calculate DTE
            try:
                exp_date = _dt.strptime(candidate.expiry, "%Y%m%d").date()
                dte = (exp_date - _dt.now().date()).days
            except Exception:
                dte = 0
            suggestion = create_suggestion(
                symbol=symbol,
                action="sell_put",
                quantity=self.cfg.contracts_per_stock,
                limit_price=premium,
                strike=candidate.strike,
                expiry=candidate.expiry,
                source="options",
                signal=f"delta={round(candidate.delta, 3)} DTE={dte}",
                rationale=(
                    f"Sell {candidate.expiry} ${candidate.strike}P @ ${premium} "
                    f"(delta {round(candidate.delta, 3)}, IV {round(candidate.iv * 100, 1)}%)"
                ),
                current_price=get_stock_price(symbol, exchange=exchange, currency=currency),
                est_cost=collateral,
                order_type="sell_put",
            )
            if suggestion:
                log.info("options_put_suggestion_created",
                         symbol=symbol, strike=candidate.strike,
                         expiry=candidate.expiry, premium=premium)
                return True
            return False

        # Place the order (use bid as limit to get filled near market)
        trade = sell_put(
            symbol=symbol,
            expiry=candidate.expiry,
            strike=candidate.strike,
            quantity=self.cfg.contracts_per_stock,
            limit_price=round(candidate.bid, 2),
            exchange=opt_exchange,
            currency=currency,
        )

        if trade is None:
            log.warning("order_failed", symbol=symbol)
            return False

        # Record in database
        self._record_trade(
            symbol=symbol,
            candidate=candidate,
            order_id=trade.order.orderId,
            current_vix=current_vix,
            contract_size=contract_size,
            currency=currency,
        )

        log.info(
            "put_sold",
            symbol=symbol,
            strike=candidate.strike,
            expiry=candidate.expiry,
            delta=round(candidate.delta, 3),
            premium=round(candidate.bid, 2),
            order_id=trade.order.orderId,
            exchange=exchange,
            currency=currency,
        )
        return True

    def _record_trade(self, symbol, candidate, order_id, current_vix, contract_size=100, currency="USD"):
        """Save the trade and position to the database."""
        # UK options prices are in pence — convert to pounds for storage
        premium = candidate.bid / 100.0 if currency == "GBP" else candidate.bid
        with get_db() as db:
            position = Position(
                symbol=symbol,
                status=PositionStatus.OPEN,
                position_type="short_put",
                strike=candidate.strike,
                expiry=candidate.expiry,
                entry_premium=premium,
                quantity=self.cfg.contracts_per_stock,
                total_premium_collected=premium * contract_size * self.cfg.contracts_per_stock,
            )
            db.add(position)
            db.flush()  # get position.id

            trade_record = Trade(
                position_id=position.id,
                symbol=symbol,
                trade_type=TradeType.SELL_PUT,
                strike=candidate.strike,
                expiry=candidate.expiry,
                premium=premium,
                quantity=self.cfg.contracts_per_stock,
                fill_price=premium,
                order_id=order_id,
                order_status=OrderStatus.SUBMITTED,
                delta_at_entry=candidate.delta,
                iv_at_entry=candidate.iv,
                vix_at_entry=current_vix,
            )
            db.add(trade_record)
