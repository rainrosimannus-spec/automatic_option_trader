"""
Core put selling logic — the main trade engine.
Scans universe, checks risk, sells puts on qualifying stocks.
"""
from __future__ import annotations

from datetime import datetime

from src.broker.orders import sell_put, _is_permission_blocked
from src.broker.market_data import get_stock_price
from src.core.config import get_settings
from src.core.database import get_db
from src.core.models import Trade, Position, TradeType, PositionStatus, OrderStatus
from src.core.logger import get_logger
from src.strategy.universe import UniverseManager
from src.strategy.screener import screen_puts
from src.strategy.risk import RiskManager, affordable_base_contracts

log = get_logger(__name__)


class PutSeller:
    """Scans the stock universe and sells puts where risk allows."""

    def __init__(self, universe: UniverseManager, risk: RiskManager):
        self.universe = universe
        self.risk = risk
        self.cfg = get_settings().strategy

    def _net_liq(self) -> float | None:
        """Current account NLV for account-size-aware sizing, or None if unavailable
        (cached by the broker layer; risk gates fetch it the same way each scan)."""
        try:
            from src.broker.account import get_account_summary
            acct = get_account_summary()
            if acct and acct.net_liquidation > 0:
                return acct.net_liquidation
        except Exception as e:
            log.warning("net_liq_fetch_failed", error=str(e))
        return None

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
                        # A clean None (no_put_contracts / no_qualifying_puts) is a
                        # NORMAL outcome, not a connection failure — even when slow.
                        # Only count it against the abort budget if the connection
                        # is actually dead. Timing alone was misfiring: a slow-but-
                        # healthy qualifyContracts on monthly-only names aborted the
                        # scan after 3 names, before it ever reached the affordable
                        # short-DTE names (random.shuffle + 43/49 monthly-only).
                        from src.broker.connection import is_connected
                        if _time.time() - sym_start > 25 and not is_connected():
                            consecutive_failures += 1
                        else:
                            consecutive_failures = 0
                except Exception as e:
                    log.error("put_scan_error", symbol=symbol, error=str(e))
                    # Only a genuinely dead connection should abort the whole scan.
                    # Transient per-symbol errors (e.g. "This event loop is already
                    # running" from loop reentry, a one-off qualifyContracts timeout)
                    # must NOT abort — they'd otherwise kill the scan after 3 names and
                    # starve it of put-sells. Probe the real connection state instead.
                    from src.broker.connection import is_connected
                    if not is_connected():
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 0

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

        # Cash-and-carry orchestration — react to detector state once per scan.
        # No-op when cfg.risk.cash_carry_enabled is False. Idempotent within a
        # state (if SGOV already held while detector ON, no second buy).
        try:
            from src.strategy.cash_carry import maybe_rotate
            grind_active, _ = self.risk.evaluate_grind_detector()
            maybe_rotate(grind_active)
        except Exception as e:
            log.warning("cash_carry_rotate_error", error=str(e))

        log.info("put_scan_completed", market=market_label, trades=len(traded), symbols=traded)
        return traded

    def _resolve_dte(self, currency: str) -> tuple[int, int] | None:
        """Return (dte_min, dte_max) based on VIX tier (spike-aware) and currency. None = halt."""
        tiers = self.cfg.dte_tiers
        regime = self.risk.get_regime()
        if regime.vix is None:
            return (7, 14)  # fail-open: no VIX data, use mid tier
        # Bull-regime DTE override (USD only): in confirmed low-VIX bulls,
        # extend DTE from 0-3 to 0-7. The 4-7 DTE band captures meaningful
        # theta in low-IV bulls where 0-3 DTE quotes pennies. Marswalk Phase 2
        # sweep showed +3 to +24 pp/yr across the three bull regimes with zero
        # impact on bears/crashes/wars (because the bull detector doesn't fire
        # there). See RiskConfig.bull_regime_dte_min/max.
        rcfg = self.risk.cfg
        if (currency == "USD" and rcfg.bull_regime_enabled
                and self.risk.in_bull_regime()):
            return (rcfg.bull_regime_dte_min, rcfg.bull_regime_dte_max)
        # Use effective tier from risk manager (accounts for VIX rate-of-change spike)
        tier = self.risk.effective_vix_tier(regime)
        if tier == "halt":
            return None
        if tier == "low":
            if currency == "USD":
                return (tiers.low_vix.dte_min_usd, tiers.low_vix.dte_max_usd)
            else:
                return (tiers.low_vix.dte_min_other, tiers.low_vix.dte_max_other)
        # mid tier (tier == "mid" or escalated from low)
        if currency == "USD":
            return (tiers.mid_vix.dte_min_usd, tiers.mid_vix.dte_max_usd)
        else:
            return (tiers.mid_vix.dte_min_other, tiers.mid_vix.dte_max_other)

    def _evaluate_symbol(self, symbol: str, current_vix: float | None, market: str | None = None) -> dict | None:
        """Evaluate a symbol for put-selling. Returns candidate dict or None."""
        log.info("scanning_symbol", symbol=symbol)

        risk_check = self.risk.can_open_put(symbol, market=market)
        if not risk_check.allowed:
            log.info("risk_blocked", symbol=symbol, reason=risk_check.reason)
            return None

        if _is_permission_blocked(symbol):
            log.info("scan_skip_permission_blocked", symbol=symbol)
            return None

        exchange = self.universe.get_exchange(symbol)
        opt_exchange = self.universe.get_options_exchange(symbol)
        currency = self.universe.get_currency(symbol)
        contract_size = self.universe.get_contract_size(symbol)
        delta_range = self.risk.get_dynamic_delta_range()

        # Resolve DTE range based on VIX and currency
        dte_range = self._resolve_dte(currency)
        if dte_range is None:
            log.info("vix_halt_dte", symbol=symbol, vix=current_vix)
            return None
        dte_min, dte_max = dte_range

        candidate = screen_puts(symbol, exchange=opt_exchange, currency=currency, delta_override=delta_range, stock_exchange=exchange, dte_min=dte_min, dte_max=dte_max)
        if not candidate:
            return None

        from datetime import datetime as _dt
        premium = round(candidate.bid, 2)

        # Sizing. At/above $500K NLV: flat contracts_per_stock base scaled UP by
        # IV-rank (#3 rich-premium sizing). Below $500K: account-size-aware — the
        # contract count is DERIVED from what fits the per-name notional cap
        # (affordable_base_contracts), so a small account is right-sized onto a
        # name instead of over-committed by the flat base. IV-rank up-scaling is
        # intentionally skipped below $500K so affordability stays the governor.
        iv_rank = self.risk.get_iv_rank_value(symbol)
        size_mult = self.risk.iv_rank_size_multiplier(iv_rank)
        net_liq = self._net_liq()
        if net_liq is not None and net_liq < 500_000:
            quantity = affordable_base_contracts(net_liq, candidate.strike)
        else:
            quantity = self.cfg.contracts_per_stock * size_mult
        # Breadth-gated MA200 halve: when risk has flagged halve regime + name
        # below own MA200, scale contracts down. Order: IV-rank first, then halve.
        if risk_check.size_multiplier < 1.0:
            quantity = max(1, int(round(quantity * risk_check.size_multiplier)))
            log.info("ma200_breadth_halve_applied", symbol=symbol,
                     mult=risk_check.size_multiplier, final_quantity=quantity)
        collateral = candidate.strike * contract_size * quantity

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
            "quantity": quantity,
            "size_mult": size_mult,
            "iv_rank": iv_rank,
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
            quantity=cand["quantity"],
            limit_price=premium,
            strike=candidate.strike,
            expiry=candidate.expiry,
            source="options",
            signal=f"delta={round(candidate.delta, 3)} DTE={dte} ivr={round(cand['iv_rank']) if cand.get('iv_rank') is not None else 'na'} x{cand['quantity']}",
            rationale=(
                f"Rank #{rank} (score {cand['score']:.1f}). "
                f"Sell {candidate.expiry} ${candidate.strike}P @ ${premium} x{cand['quantity']} "
                f"(delta {round(candidate.delta, 3)}, IV {round(candidate.iv * 100, 1)}%, "
                f"IVrank {round(cand['iv_rank']) if cand.get('iv_rank') is not None else 'na'})"
            ),
            current_price=cand["price"],
            est_cost=cand["collateral"],
            order_type="sell_put",
            # Decision-time quote → trade_sync stamps fill-vs-mid onto the Trade (exec-quality measure).
            bid_at_entry=getattr(candidate, "bid", None),
            ask_at_entry=getattr(candidate, "ask", None),
            mid_at_entry=getattr(candidate, "mid", None),
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

        if _is_permission_blocked(symbol):
            log.info("scan_skip_permission_blocked", symbol=symbol)
            return False

        # Get exchange/currency for this stock
        exchange = self.universe.get_exchange(symbol)
        opt_exchange = self.universe.get_options_exchange(symbol)
        currency = self.universe.get_currency(symbol)
        contract_size = self.universe.get_contract_size(symbol)

        # Get dynamic delta range based on current VIX
        delta_range = self.risk.get_dynamic_delta_range()

        # Resolve DTE range based on VIX and currency
        dte_range = self._resolve_dte(currency)
        if dte_range is None:
            log.info("vix_halt_dte", symbol=symbol, vix=current_vix)
            return False
        dte_min, dte_max = dte_range

        # Screen for best put contract with dynamic delta and VIX-adaptive DTE
        candidate = screen_puts(symbol, exchange=opt_exchange, currency=currency, delta_override=delta_range, stock_exchange=exchange, dte_min=dte_min, dte_max=dte_max)
        if not candidate:
            return False

        # Sizing — see _evaluate_symbol. At/above $500K: flat base × IV-rank (the
        # whatif-margin check below runs on this scaled quantity, so an oversized
        # multiple is blocked by the per-position cap rather than placed). Below
        # $500K: account-size-aware affordability-derived count.
        iv_rank = self.risk.get_iv_rank_value(symbol)
        size_mult = self.risk.iv_rank_size_multiplier(iv_rank)
        net_liq = self._net_liq()
        if net_liq is not None and net_liq < 500_000:
            quantity = affordable_base_contracts(net_liq, candidate.strike)
        else:
            quantity = self.cfg.contracts_per_stock * size_mult
        # Breadth-gated MA200 halve (see can_open_put). Apply AFTER IV-rank
        # so the halve scales down what the rich-premium sizing already chose.
        if risk_check.size_multiplier < 1.0:
            quantity = max(1, int(round(quantity * risk_check.size_multiplier)))
            log.info("ma200_breadth_halve_applied", symbol=symbol,
                     mult=risk_check.size_multiplier, final_quantity=quantity)

        # Whatif margin check: ask IBKR exactly how much buying power this contract consumes
        try:
            from src.broker.orders import get_whatif_margin
            from src.broker.account import get_account_summary
            from src.core.config import get_settings
            acct = get_account_summary()
            if acct and acct.net_liquidation > 0:
                nlv = acct.net_liquidation
                buying_power = acct.buying_power
                maintenance_margin = acct.maintenance_margin
                risk_cfg = get_settings().risk

                # Layer 1 — percentage-based cap (existing logic)
                if nlv < 100_000:
                    total_capacity = nlv * 6
                    max_pct = 0.25
                else:
                    total_capacity = buying_power + maintenance_margin
                    max_pct = 0.15
                max_per_position = total_capacity * max_pct

                # Layer 2 — hard dollar cap (scaling safeguard, only bites at large NLV)
                dollar_cap = min(
                    nlv * risk_cfg.position_dollar_pct,
                    risk_cfg.max_position_dollars,
                )
                if dollar_cap >= risk_cfg.min_position_dollars:
                    max_per_position = min(max_per_position, dollar_cap)

                real_margin = get_whatif_margin(
                    symbol=symbol,
                    expiry=candidate.expiry,
                    strike=candidate.strike,
                    right="P",
                    quantity=quantity,
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
                             max_pct=f"{max_pct:.0%}",
                             dollar_cap=f"${dollar_cap:,.0f}")
                    return False
                log.info("whatif_margin_passed",
                         symbol=symbol,
                         real_margin=f"${real_margin:,.0f}" if real_margin else "n/a",
                         max_per_position=f"${max_per_position:,.0f}")
        except Exception as e:
            log.warning("whatif_margin_check_failed", symbol=symbol, error=str(e))
            # fail open — don't block if whatif unavailable

        # 52-week high filter: reject if price is more than 40% below year high
        try:
            from src.broker.market_data import get_52week_high
            year_high = get_52week_high(symbol, exchange=exchange, currency=currency)
            if year_high and year_high > 0:
                current_price = get_stock_price(symbol, exchange=exchange, currency=currency)
                if current_price and current_price < year_high * 0.60:
                    log.info("year_high_filter_blocked",
                             symbol=symbol,
                             price=round(current_price, 2),
                             year_high=round(year_high, 2),
                             pct_below=round((1 - current_price / year_high) * 100, 1))
                    return False
        except Exception as e:
            log.warning("year_high_filter_failed", symbol=symbol, error=str(e))
            # fail open — don't block if data unavailable

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
                quantity=quantity,
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
                bid_at_entry=getattr(candidate, "bid", None),
                ask_at_entry=getattr(candidate, "ask", None),
                mid_at_entry=getattr(candidate, "mid", None),
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
            quantity=quantity,
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
            quantity=quantity,
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

        # ── Strangle leg (mirror of src/marswalk/engine.py:1213-1276) ─────
        # Fires when EITHER strangle_when_grind+hvg_active OR
        # crash_strangle_when_active+crash_active. Sells a symmetric-delta
        # call at the same expiry. Naked short — broker must accept under
        # portfolio margin. Daily ITM-avoidance check closes ITM calls before
        # assignment (src/strategy/cash_carry.py).
        cfg_risk = get_settings().risk
        try:
            should_strangle = False
            if cfg_risk.strangle_when_grind:
                hvg_active, _ = self.risk.evaluate_grind_detector()
                if hvg_active:
                    should_strangle = True
            if not should_strangle and cfg_risk.crash_strangle_when_active:
                crash_active, _ = self.risk.evaluate_crash_detector()
                if crash_active:
                    should_strangle = True
            if should_strangle:
                self._sell_strangle_call_leg(
                    symbol, candidate.expiry, quantity,
                    opt_exchange, currency,
                )
        except Exception as e:
            log.warning("strangle_leg_failed", symbol=symbol, error=str(e))

        return True

    def _sell_strangle_call_leg(self, symbol: str, expiry: str, quantity: int,
                                  opt_exchange: str, currency: str) -> None:
        """Sell a symmetric-delta call to pair with the just-sold put.
        Screens the call chain, picks best-score candidate, places order, and
        records as Position(position_type='short_call_naked'). Idempotent
        protection: skip if a naked-call position already exists for this
        symbol+expiry."""
        from src.broker.orders import sell_call_to_open_naked
        from src.strategy.screener import screen_calls
        from src.core.models import Position, PositionStatus
        from src.core.database import get_db
        from datetime import datetime

        cfg_risk = get_settings().risk
        # Skip if a naked-call position already exists for this name+expiry
        # (avoid stacking on re-scans within the same day).
        with get_db() as db:
            existing = db.query(Position).filter(
                Position.symbol == symbol,
                Position.position_type == "short_call_naked",
                Position.expiry == expiry,
                Position.status == PositionStatus.OPEN,
            ).first()
            if existing:
                log.info("strangle_call_already_held",
                         symbol=symbol, expiry=expiry, qty=existing.quantity)
                return

        # Screen for a symmetric-delta call. screen_calls uses cc_delta_min/max
        # by default — we override with strangle-specific band, and target_expiry
        # filters to ONLY the just-sold put's expiry.
        call_cand = screen_calls(
            symbol, exchange=opt_exchange, currency=currency,
            delta_min_override=cfg_risk.strangle_call_delta_min,
            delta_max_override=cfg_risk.strangle_call_delta_max,
            target_expiry=expiry,
        )
        if call_cand is None:
            log.info("strangle_call_no_candidate", symbol=symbol, expiry=expiry)
            return

        trade = sell_call_to_open_naked(
            symbol=symbol,
            expiry=call_cand.expiry,
            strike=call_cand.strike,
            quantity=quantity,
            limit_price=round(call_cand.bid, 2),
            exchange=opt_exchange,
            currency=currency,
        )
        if trade is None:
            log.warning("strangle_call_order_failed",
                        symbol=symbol, strike=call_cand.strike, expiry=expiry)
            return

        with get_db() as db:
            pos = Position(
                symbol=symbol,
                status=PositionStatus.OPEN,
                position_type="short_call_naked",
                strike=call_cand.strike,
                expiry=call_cand.expiry,
                entry_premium=call_cand.bid,
                quantity=quantity,
                total_premium_collected=call_cand.bid * 100 * quantity,
                opened_at=datetime.utcnow(),
                is_wheel=False,
            )
            db.add(pos)
            db.commit()

        log.info(
            "strangle_call_sold",
            symbol=symbol, strike=call_cand.strike, expiry=call_cand.expiry,
            delta=round(call_cand.delta, 3), premium=round(call_cand.bid, 2),
            quantity=quantity,
        )

    def _record_trade(self, symbol, candidate, order_id, current_vix, contract_size=100, currency="USD", quantity=None):
        """Save the trade and position to the database."""
        # UK options prices are in pence — convert to pounds for storage
        premium = candidate.bid / 100.0 if currency == "GBP" else candidate.bid
        # Decision-time quote in the same unit as premium/fill_price (Consigliere
        # execution-quality). Guarded: never raises if a field is missing.
        _unit = 100.0 if currency == "GBP" else 1.0
        def _norm(v):
            return None if v is None else v / _unit
        qty = quantity if quantity is not None else self.cfg.contracts_per_stock
        with get_db() as db:
            position = Position(
                symbol=symbol,
                status=PositionStatus.OPEN,
                position_type="short_put",
                strike=candidate.strike,
                expiry=candidate.expiry,
                entry_premium=premium,
                quantity=qty,
                total_premium_collected=premium * contract_size * qty,
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
                quantity=qty,
                fill_price=premium,
                order_id=order_id,
                order_status=OrderStatus.SUBMITTED,
                delta_at_entry=candidate.delta,
                iv_at_entry=candidate.iv,
                vix_at_entry=current_vix,
                bid_at_entry=_norm(getattr(candidate, "bid", None)),
                ask_at_entry=_norm(getattr(candidate, "ask", None)),
                mid_at_entry=_norm(getattr(candidate, "mid", None)),
            )
            db.add(trade_record)
