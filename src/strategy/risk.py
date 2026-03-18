"""
Risk management — VIX gate, SPY MA gate, position limits, exposure checks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date

from src.broker.market_data import get_vix, get_stock_price, get_spy_moving_averages, get_iv_rank, has_upcoming_earnings
from src.broker.account import get_account_summary, get_portfolio_positions
from src.core.config import get_settings
from src.core.database import get_db
from src.core.models import Position, PositionStatus, SystemState
from src.core.logger import get_logger
from src.strategy.universe import UniverseManager

log = get_logger(__name__)


# Approximate FX rates to USD for position sizing
# GBP prices from London come in pence (1/100 of a pound)
_FX_TO_USD = {
    "USD": 1.0,
    "GBP": 1.27 / 100,  # pence to USD (price in pence, divide by 100 then multiply by GBP/USD)
    "EUR": 1.08,
    "CHF": 1.12,
    "JPY": 1.0 / 150,   # yen to USD
    "AUD": 0.65,
    "NOK": 1.0 / 10.5,
    "SEK": 1.0 / 10.3,
    "HKD": 1.0 / 7.8,
    "CAD": 0.74,
}


def _convert_to_usd(price: float, currency: str) -> float:
    """Convert a price in local currency to approximate USD for risk comparison."""
    rate = _FX_TO_USD.get(currency, 1.0)
    return price * rate


@dataclass
class RiskCheck:
    allowed: bool
    reason: str = ""
    reduce_pct: float = 1.0  # 1.0 = no reduction, 0.5 = skip 50% of candidates


@dataclass
class MarketRegime:
    """Snapshot of current market conditions for risk decisions."""
    vix: float | None = None
    spy_bullish: bool | None = None
    spy_fast_ma: float | None = None
    spy_slow_ma: float | None = None
    spy_price: float | None = None
    eu_bullish: bool | None = None   # FEZ (Euro Stoxx 50 ETF)
    eu_price: float | None = None
    asia_bullish: bool | None = None  # EWJ (MSCI Japan ETF)
    asia_price: float | None = None


class RiskManager:
    """Enforces all risk rules before allowing new trades."""

    def __init__(self, universe: UniverseManager):
        self.universe = universe
        self.cfg = get_settings().risk
        self._regime: MarketRegime | None = None
        self._daily_count: int | None = None
        self._daily_count_date: date | None = None

    # ── Market regime ───────────────────────────────────────
    def get_regime(self, force_refresh: bool = False) -> MarketRegime:
        """
        Fetch and cache market regime data for this scan cycle.
        If US market data is unavailable (outside US hours), uses last known
        values from the database, or defaults to permissive settings.
        """
        if self._regime and not force_refresh:
            return self._regime

        regime = MarketRegime()

        # Try to get VIX — may fail outside US hours
        regime.vix = get_vix()

        # If VIX unavailable, try last known value from DB
        if regime.vix is None:
            regime.vix = self._get_last_known("current_vix")
            if regime.vix is not None:
                log.info("vix_using_cached", vix=regime.vix)

        # Try SPY MA — may also fail outside US hours
        if self.cfg.spy_ma_enabled:
            try:
                spy_data = get_spy_moving_averages(
                    fast_period=self.cfg.spy_ma_fast,
                    slow_period=self.cfg.spy_ma_slow,
                )
                if spy_data:
                    regime.spy_bullish = spy_data["is_bullish"]
                    regime.spy_fast_ma = spy_data["fast_ma"]
                    regime.spy_slow_ma = spy_data["slow_ma"]
                    regime.spy_price = spy_data["spy_price"]
            except Exception as e:
                log.warning("spy_ma_fetch_error", error=str(e))
                # Use last known values
                cached_bullish = self._get_last_known("spy_bullish")
                if cached_bullish is not None:
                    regime.spy_bullish = cached_bullish == "true"
                    log.info("spy_ma_using_cached", bullish=regime.spy_bullish)

        # Fetch regional proxies (use cached if unavailable)
        if self.cfg.spy_ma_enabled:
            try:
                from src.broker.market_data import get_regional_moving_averages
                eu_data = get_regional_moving_averages("FEZ", "SMART", "USD",
                    fast_period=self.cfg.spy_ma_fast, slow_period=self.cfg.spy_ma_slow)
                if eu_data:
                    regime.eu_bullish = eu_data["is_bullish"]
                    regime.eu_price = eu_data["price"]
                else:
                    cached = self._get_last_known("eu_bullish")
                    if cached is not None:
                        regime.eu_bullish = cached == "true"
                        log.info("eu_ma_using_cached", bullish=regime.eu_bullish)
            except Exception as e:
                log.warning("eu_ma_fetch_error", error=str(e))

            try:
                from src.broker.market_data import get_regional_moving_averages
                asia_data = get_regional_moving_averages("EWJ", "SMART", "USD",
                    fast_period=self.cfg.spy_ma_fast, slow_period=self.cfg.spy_ma_slow)
                if asia_data:
                    regime.asia_bullish = asia_data["is_bullish"]
                    regime.asia_price = asia_data["price"]
                else:
                    cached = self._get_last_known("asia_bullish")
                    if cached is not None:
                        regime.asia_bullish = cached == "true"
                        log.info("asia_ma_using_cached", bullish=regime.asia_bullish)
            except Exception as e:
                log.warning("asia_ma_fetch_error", error=str(e))

        self._regime = regime
        self._store_regime(regime)
        return regime

    def _get_last_known(self, key: str) -> float | str | None:
        """Get last known value from SystemState DB."""
        try:
            with get_db() as db:
                state = db.query(SystemState).filter(SystemState.key == key).first()
                if state and state.value:
                    try:
                        return float(state.value)
                    except ValueError:
                        return state.value
        except Exception:
            pass
        return None

    def _store_regime(self, regime: MarketRegime) -> None:
        """Persist regime data to SystemState for dashboard display."""
        with get_db() as db:
            if regime.vix is not None and regime.vix > self.cfg.vix_pause_threshold:
                regime_label = "halt"
            elif regime.spy_bullish is False:
                regime_label = "bear"
            else:
                regime_label = "normal"

            pairs = {
                "market_regime": regime_label,
                "current_vix": str(regime.vix) if regime.vix else "",
                "spy_bullish": str(regime.spy_bullish).lower() if regime.spy_bullish is not None else "",
                "spy_fast_ma": str(regime.spy_fast_ma) if regime.spy_fast_ma else "",
                "spy_slow_ma": str(regime.spy_slow_ma) if regime.spy_slow_ma else "",
                "spy_price": str(regime.spy_price) if regime.spy_price else "",
                "eu_bullish": str(regime.eu_bullish).lower() if regime.eu_bullish is not None else "",
                "eu_price": str(regime.eu_price) if regime.eu_price else "",
                "asia_bullish": str(regime.asia_bullish).lower() if regime.asia_bullish is not None else "",
                "asia_price": str(regime.asia_price) if regime.asia_price else "",
            }
            for key, value in pairs.items():
                if not value:
                    continue
                state = db.query(SystemState).filter(SystemState.key == key).first()
                if state:
                    state.value = value
                    state.updated_at = datetime.utcnow()
                else:
                    db.add(SystemState(key=key, value=value))

    # ── Individual checks ───────────────────────────────────
    def check_vix_gate(self) -> RiskCheck:
        """Block trading if VIX is above threshold. Fail-open if VIX unavailable."""
        regime = self.get_regime()
        if regime.vix is None:
            log.warning("vix_data_unavailable_allowing_trades")
            return RiskCheck(True)  # fail open — allow trading if VIX data unavailable
        if regime.vix > self.cfg.vix_pause_threshold:
            log.warning("vix_gate_triggered", vix=regime.vix, threshold=self.cfg.vix_pause_threshold)
            return RiskCheck(False, f"VIX at {regime.vix:.1f} > {self.cfg.vix_pause_threshold} threshold")
        return RiskCheck(True)

    def check_spy_ma_gate(self, market: str | None = None) -> RiskCheck:
        """
        MA crossover gate — uses regional proxy when market is EU or ASIA.
        When bearish (fast < slow), reduce new entries by configured percentage.
        Does NOT fully block — returns a reduction signal.
        """
        if not self.cfg.spy_ma_enabled:
            return RiskCheck(True)

        regime = self.get_regime()

        # Pick the right bullish flag for this market
        if market == "SMART_EU":
            is_bullish = regime.eu_bullish
            label = "FEZ (EU)"
        elif market == "SMART_ASIA":
            is_bullish = regime.asia_bullish
            label = "EWJ (Asia)"
        else:
            is_bullish = regime.spy_bullish
            label = "SPY (US)"

        if is_bullish is None:
            log.warning("ma_data_unavailable", market=market or "US")
            return RiskCheck(True)  # fail open — don't block if data unavailable

        if not is_bullish:
            reduction = self.cfg.spy_bearish_reduction
            log.info(
                "regional_bearish_gate",
                proxy=label,
                market=market or "SMART",
                reduction=f"{reduction:.0%}",
            )
            return RiskCheck(
                True,  # allowed but reduced
                reason=f"{label} bearish (MA{self.cfg.spy_ma_fast} < MA{self.cfg.spy_ma_slow}) — reducing entries to {reduction:.0%}",
                reduce_pct=reduction,
            )
        return RiskCheck(True)

    def _get_dynamic_daily_limit(self) -> int:
        """
        Calculate daily position limit based on portfolio size.
        Base: 10 trades for first 100K. Then +1 per additional 100K.
        Capped at max_daily_positions_cap.
        """
        try:
            summary = get_account_summary()
            net_liq = summary.net_liquidation
        except Exception:
            return self.cfg.max_daily_positions  # fallback to base

        base = self.cfg.max_daily_positions
        step = self.cfg.daily_position_step
        cap = self.cfg.max_daily_positions_cap

        if net_liq <= step:
            return base

        extra = int((net_liq - step) / step)
        limit = min(base + extra, cap)
        return limit

    def check_daily_limit(self) -> RiskCheck:
        """Enforce max N new positions per day (scales with portfolio size)."""
        today = date.today()
        daily_limit = self._get_dynamic_daily_limit()

        # Cache the count for this day
        if self._daily_count_date != today or self._daily_count is None:
            today_start = datetime.combine(today, datetime.min.time())
            with get_db() as db:
                self._daily_count = (
                    db.query(Position)
                    .filter(
                        Position.opened_at >= today_start,
                        Position.position_type == "short_put",
                    )
                    .count()
                )
            self._daily_count_date = today

        if self._daily_count >= daily_limit:
            return RiskCheck(
                False,
                f"Daily limit reached: {self._daily_count}/{daily_limit} positions today",
            )
        return RiskCheck(True)

    def check_position_limit(self) -> RiskCheck:
        """
        Adaptive max open positions based on NLV.
        Small accounts: 4 positions (concentrated, high conviction).
        Large accounts: up to 20 positions (diversified).

        Tiers:
          NLV < $50K   → 4 positions
          NLV < $200K  → 8 positions
          NLV < $500K  → 15 positions
          NLV >= $500K → 20 positions
        """
        try:
            summary = get_account_summary()
            net_liq = summary.net_liquidation if summary and summary.net_liquidation > 0 else 0
        except Exception as e:
            log.warning("position_limit_check_skipped", error=str(e))
            return RiskCheck(True)  # fail open

        if net_liq <= 0:
            return RiskCheck(True)

        if net_liq < 50_000:
            max_pos = 4
        elif net_liq < 200_000:
            max_pos = 8
        elif net_liq < 500_000:
            max_pos = 15
        else:
            max_pos = 20

        with get_db() as db:
            open_count = (
                db.query(Position)
                .filter(Position.status == PositionStatus.OPEN)
                .count()
            )

        if open_count >= max_pos:
            return RiskCheck(
                False,
                f"Position limit reached: {open_count}/{max_pos} open positions (NLV ${net_liq:,.0f})",
            )
        return RiskCheck(True)

    def check_position_size(self, symbol: str) -> RiskCheck:
        """
        Adaptive per-position size limit based on NLV.
        Uses same anchor pattern as sector check — starts permissive,
        tightens automatically as account grows.

        Tiers (% of NLV × 6 capacity per position):
          NLV < $50K   → 25% (4 positions max by capacity)
          NLV < $200K  → 15%
          NLV >= $200K → floors at 5% (max_single_stock_pct target)

        Real margin enforcement is handled by get_whatif_margin in put_seller.py.
        This check adds a secondary guardrail on position concentration.
        """
        try:
            summary = get_account_summary()
            net_liq = summary.net_liquidation if summary and summary.net_liquidation > 0 else 0
        except Exception as e:
            log.warning("position_size_check_skipped", error=str(e))
            return RiskCheck(True)  # fail open

        if net_liq <= 0:
            return RiskCheck(True)

        effective_limit = self._get_adaptive_position_limit(net_liq)

        # Get current margin used by this symbol if already open
        # For new positions: estimate using current stock price × 100 multiplier
        try:
            stock = self.universe.get_stock(symbol)
            currency = stock.currency if stock else "USD"
            exchange = stock.exchange if stock else "SMART"
            price = get_stock_price(symbol, exchange=exchange, currency=currency)
            if not price or price <= 0:
                return RiskCheck(True)  # can't check, allow
        except Exception:
            return RiskCheck(True)

        capacity = net_liq * 6  # same formula as put_seller.py
        max_position_value = capacity * effective_limit
        estimated_margin = price * 100  # 1 contract = 100 shares

        if estimated_margin > max_position_value:
            return RiskCheck(
                False,
                f"{symbol} estimated margin ${estimated_margin:,.0f} exceeds "
                f"{effective_limit:.0%} position limit (${max_position_value:,.0f}) at NLV ${net_liq:,.0f}",
            )
        return RiskCheck(True)

    def _get_adaptive_position_limit(self, net_liq: float) -> float:
        """
        Adaptive per-position size limit as fraction of total capacity.
        Starts permissive for small accounts, tightens as NLV grows.

        Tiers:
          NLV < $50K   → 25% per position
          NLV < $200K  → 15% per position
          NLV >= $200K → 5% per position (max_single_stock_pct floor)
        """
        target_pct = max(self.cfg.max_single_stock_pct, 0.05)  # floor at 5%

        if net_liq < 50_000:
            return 0.25
        elif net_liq < 200_000:
            return 0.15
        else:
            return target_pct

    def check_correlation(self, symbol: str) -> RiskCheck:
        """
        Block if new symbol is too highly correlated with existing open positions.
        Uses 60-day price history from FMP.
        Skipped entirely if NLV < threshold or fewer than 3 open positions.
        """
        try:
            summary = get_account_summary()
            net_liq = summary.net_liquidation if summary and summary.net_liquidation > 0 else 0
        except Exception:
            return RiskCheck(True)

        # Skip on small accounts — not enough positions to matter
        if net_liq < self.cfg.correlation_nlv_threshold:
            return RiskCheck(True)

        with get_db() as db:
            open_positions = (
                db.query(Position)
                .filter(Position.status == PositionStatus.OPEN)
                .all()
            )

        open_symbols = [p.symbol for p in open_positions if p.symbol != symbol]

        # Need at least 3 existing positions to run correlation check
        if len(open_symbols) < 3:
            return RiskCheck(True)

        try:
            import datetime as dt
            from src.data.fmp import FMPClient
            fmp = FMPClient()
            lookback = self.cfg.correlation_lookback_days

            end = dt.date.today()
            start = end - dt.timedelta(days=lookback + 10)

            # Fetch price history for new symbol
            new_prices = fmp.get_price_history(symbol, start.isoformat(), end.isoformat())
            if not new_prices or len(new_prices) < 20:
                return RiskCheck(True)  # not enough data, allow

            import statistics

            def pct_returns(prices: list) -> list:
                return [(prices[i] - prices[i-1]) / prices[i-1]
                        for i in range(1, len(prices)) if prices[i-1] != 0]

            def pearson(x: list, y: list) -> float:
                n = min(len(x), len(y))
                if n < 10:
                    return 0.0
                x, y = x[-n:], y[-n:]
                mx, my = sum(x)/n, sum(y)/n
                num = sum((x[i]-mx)*(y[i]-my) for i in range(n))
                den = (sum((v-mx)**2 for v in x) * sum((v-my)**2 for v in y)) ** 0.5
                return num / den if den != 0 else 0.0

            new_returns = pct_returns(new_prices)
            correlations = []

            for sym in open_symbols[:10]:  # cap at 10 to limit API calls
                try:
                    prices = fmp.get_price_history(sym, start.isoformat(), end.isoformat())
                    if prices and len(prices) >= 20:
                        r = pearson(new_returns, pct_returns(prices))
                        correlations.append(abs(r))
                except Exception:
                    continue

            if not correlations:
                return RiskCheck(True)

            avg_corr = sum(correlations) / len(correlations)

            if avg_corr > self.cfg.max_correlation:
                return RiskCheck(
                    False,
                    f"{symbol} avg correlation {avg_corr:.2f} with open positions "
                    f"exceeds limit {self.cfg.max_correlation:.2f}",
                )
        except Exception as e:
            log.warning("correlation_check_failed", symbol=symbol, error=str(e))
            return RiskCheck(True)  # fail open — don't block on data errors

        return RiskCheck(True)

    def check_delta_exposure(self, symbol: str) -> RiskCheck:
        """
        Block if total portfolio delta would exceed max_portfolio_delta.
        Delta units = abs(delta) × contracts × 100 multiplier per position.
        Skipped entirely if NLV < threshold.
        """
        try:
            summary = get_account_summary()
            net_liq = summary.net_liquidation if summary and summary.net_liquidation > 0 else 0
        except Exception:
            return RiskCheck(True)

        # Skip on small accounts
        if net_liq < self.cfg.delta_nlv_threshold:
            return RiskCheck(True)

        try:
            from src.broker.account import get_option_positions
            positions = get_option_positions()

            total_delta = 0.0
            for pos in positions:
                if pos.contract.secType == "OPT":
                    # modelGreeks gives delta if available, else use marketPrice delta
                    greeks = getattr(pos, "unrealizedPNL", None)
                    # Use position delta from portfolio item directly
                    delta = None
                    if hasattr(pos, "modelGreeks") and pos.modelGreeks:
                        delta = pos.modelGreeks.delta
                    if delta is None and hasattr(pos, "delta"):
                        delta = pos.delta
                    if delta is not None:
                        # pos.position is negative for short puts (e.g. -1 contract)
                        # abs delta × abs position × 100
                        total_delta += abs(delta) * abs(pos.position) * 100

            # Scale max delta with NLV — larger accounts can hold more delta
            if net_liq < 200_000:
                max_delta = self.cfg.max_portfolio_delta
            elif net_liq < 500_000:
                max_delta = self.cfg.max_portfolio_delta * 2
            else:
                max_delta = self.cfg.max_portfolio_delta * 4

            if total_delta >= max_delta:
                return RiskCheck(
                    False,
                    f"Portfolio delta {total_delta:.0f} at or above limit {max_delta:.0f} "
                    f"(NLV ${net_liq:,.0f})",
                )
        except Exception as e:
            log.warning("delta_exposure_check_failed", error=str(e))
            return RiskCheck(True)  # fail open

        return RiskCheck(True)

    def check_sector_exposure(self, symbol: str) -> RiskCheck:
        """Ensure no single sector exceeds adaptive allocation."""
        sector = self.universe.get_sector(symbol)
        if not sector:
            return RiskCheck(True)

        # Get adaptive limit — permissive when small, tightens as NLV grows
        try:
            summary = get_account_summary()
            net_liq = summary.net_liquidation if summary and summary.net_liquidation > 0 else 0
        except Exception:
            net_liq = 0
        if net_liq <= 0:
            return RiskCheck(True)  # can't calculate, allow

        effective_limit = self._get_adaptive_sector_limit(net_liq)

        # Skip check entirely if effective limit is >= 100% (too small to diversify)
        if effective_limit >= 1.0:
            return RiskCheck(True)

        sector_symbols = self.universe.symbols_in_sector(sector)

        with get_db() as db:
            sector_positions = (
                db.query(Position)
                .filter(
                    Position.status == PositionStatus.OPEN,
                    Position.symbol.in_(sector_symbols),
                )
                .count()
            )
            total_positions = (
                db.query(Position)
                .filter(Position.status == PositionStatus.OPEN)
                .count()
            )

        if total_positions == 0:
            return RiskCheck(True)

        sector_pct = (sector_positions + 1) / max(total_positions + 1, 1)
        if sector_pct > effective_limit:
            return RiskCheck(
                False,
                f"Sector {sector} at {sector_pct:.0%} > {effective_limit:.0%} adaptive limit",
            )
        return RiskCheck(True)

    def _get_adaptive_sector_limit(self, net_liq: float) -> float:
        """
        Adaptive sector concentration limit — same pattern as position limit.

        Anchored at first trade, tightens as NLV grows toward 30% target.

        Example with $13,767 NLV:
          anchor = $13,767 * 100% = $13,767 (100% = fully permissive)
          At $13,767 NLV → $13,767 / $13,767 = 100% (no sector limit)
          At $25,000 NLV → $13,767 / $25,000 = 55%
          At $50,000 NLV → $13,767 / $50,000 = 28% → target 30% takes over
          At $100,000+ NLV → 30% (fully diversified)
        """
        target_pct = self.cfg.max_sector_pct  # 0.30 = 30% final target
        if target_pct <= 0:
            return 1.0  # disabled

        start_pct = 1.0  # 100% = no sector limit when small

        with get_db() as db:
            anchor_row = db.query(SystemState).filter(
                SystemState.key == "sector_limit_anchor_dollars"
            ).first()

            if anchor_row:
                anchor_dollars = float(anchor_row.value)
            else:
                anchor_dollars = net_liq * start_pct
                db.add(SystemState(key="sector_limit_anchor_dollars",
                                   value=str(round(anchor_dollars, 2))))
                log.info("sector_limit_anchor_set",
                         nlv=net_liq, anchor_dollars=round(anchor_dollars, 2))

        effective = anchor_dollars / max(net_liq, 1)
        effective = max(effective, target_pct)  # floor at target (30%)

        return effective

    def check_buying_power(self, required_margin: float = 0) -> RiskCheck:
        """Ensure sufficient buying power remains.

        If account data is unavailable, blocks trading (fail-closed)
        to prevent suggestions being created when limits are exceeded.
        """
        try:
            summary = get_account_summary()
        except Exception as e:
            log.warning("buying_power_check_failed", error=str(e))
            return RiskCheck(False, "Account data unavailable — blocking for safety")

        if summary.net_liquidation <= 0:
            log.warning("buying_power_no_net_liq")
            return RiskCheck(False, "Net liquidation is zero — blocking for safety")

        used_pct = 1 - (summary.buying_power / max(summary.net_liquidation, 1))
        if used_pct >= self.cfg.max_buying_power_usage:
            return RiskCheck(
                False,
                f"Buying power usage {used_pct:.0%} >= {self.cfg.max_buying_power_usage:.0%} limit",
            )

        effective_reserve = max(self.cfg.min_cash_reserve, summary.net_liquidation * 0.15)
        if summary.cash_balance < effective_reserve:
            return RiskCheck(
                False,
                f"Cash ${summary.cash_balance:,.0f} < ${effective_reserve:,.0f} reserve "
                f"(max of ${self.cfg.min_cash_reserve:,.0f} floor or 15% of NLV)",
            )
        return RiskCheck(True)

    def dynamic_margin_ceiling(self, open_position_count: int = None) -> float:
        """
        Dynamic margin ceiling: 0.90 - (positions * 0.03), floor 0.60.
        VIX: GREEN=no change, YELLOW=*0.95, RED=*0.90
        """
        if open_position_count is None:
            try:
                from src.core.database import get_db
                from src.core.models import Position, PositionStatus
                with get_db() as db:
                    open_position_count = db.query(Position).filter(
                        Position.status == PositionStatus.OPEN
                    ).count()
            except Exception:
                open_position_count = 3

        base_ceiling = max(0.60, 0.90 - (open_position_count * 0.03))

        try:
            vix = self._get_vix()
            if vix is None:
                vix_factor = 1.0
            elif vix >= 30:
                vix_factor = 0.90
            elif vix >= 20:
                vix_factor = 0.95
            else:
                vix_factor = 1.0
        except Exception:
            vix_factor = 1.0

        ceiling = base_ceiling * vix_factor
        log.debug("dynamic_margin_ceiling",
                  positions=open_position_count,
                  base=f"{base_ceiling:.0%}",
                  vix_factor=vix_factor,
                  ceiling=f"{ceiling:.0%}")
        return ceiling

    def check_margin_usage(self) -> RiskCheck:
        """Block trading when maintenance margin exceeds threshold of NLV."""
        try:
            summary = get_account_summary()
        except Exception as e:
            log.warning("margin_check_failed", error=str(e))
            return RiskCheck(False, "Account data unavailable — blocking for safety")

        if summary.net_liquidation <= 0:
            return RiskCheck(False, "Net liquidation is zero — blocking for safety")

        ceiling = self.dynamic_margin_ceiling()
        margin_pct = summary.maintenance_margin / summary.net_liquidation
        log.info("margin_check",
                 margin=round(summary.maintenance_margin, 0),
                 nlv=round(summary.net_liquidation, 0),
                 margin_pct=f"{margin_pct:.1%}",
                 dynamic_ceiling=f"{ceiling:.0%}")

        if margin_pct >= ceiling:
            return RiskCheck(
                False,
                f"Margin usage {margin_pct:.0%} >= dynamic ceiling {ceiling:.0%}",
            )
        return RiskCheck(True)

    def check_duplicate_position(self, symbol: str) -> RiskCheck:
        """Don't open a second put on the same stock."""
        with get_db() as db:
            existing = (
                db.query(Position)
                .filter(
                    Position.symbol == symbol,
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "short_put",
                )
                .first()
            )
        if existing:
            return RiskCheck(False, f"Already have open put on {symbol}")
        return RiskCheck(True)

    def check_iv_rank(self, symbol: str) -> RiskCheck:
        """
        Only sell puts when IV rank is elevated (premium is rich).

        VIX-adaptive threshold:
          VIX < 15  → iv_rank_min = 15% (low-vol regime, most stocks have low IV rank)
          VIX 15-20 → iv_rank_min = 20% (normal-low, slightly relaxed)
          VIX 20-25 → iv_rank_min = 30% (normal, standard threshold)
          VIX > 25  → iv_rank_min = 30% (high-vol, keep standard)

        Rationale: In low-VIX environments, IV rank across the board compresses.
        A stock at IV rank 20% in VIX=15 is still selling relatively rich premium
        compared to its own history — it's just that the whole market is calm.
        """
        strat_cfg = get_settings().strategy
        if not strat_cfg.iv_rank_enabled:
            return RiskCheck(True)

        stock = self.universe.get_stock(symbol)
        exchange = stock.exchange if stock else "SMART"
        currency = stock.currency if stock else "USD"

        iv_rank = get_iv_rank(
            symbol,
            exchange=exchange,
            currency=currency,
            lookback_days=strat_cfg.iv_lookback_days,
        )

        if iv_rank is None:
            # Can't determine IV rank — allow trading (fail open)
            return RiskCheck(True)

        # VIX-adaptive threshold
        regime = self.get_regime()
        vix = regime.vix
        base_min = strat_cfg.iv_rank_min  # default 30

        if vix is not None and vix < 15:
            effective_min = max(15, base_min - 15)  # 30 → 15
        elif vix is not None and vix < 20:
            effective_min = max(15, base_min - 10)  # 30 → 20
        else:
            effective_min = base_min  # 30

        if iv_rank < effective_min:
            return RiskCheck(
                False,
                f"{symbol} IV rank {iv_rank:.0f}% < {effective_min}% minimum "
                f"(VIX={vix:.0f}, base={base_min}%, premium too cheap)",
            )

        log.debug("iv_rank_passed", symbol=symbol, iv_rank=round(iv_rank, 1),
                   threshold=effective_min, vix=vix)
        return RiskCheck(True)

    def check_earnings(self, symbol: str) -> RiskCheck:
        """Block puts on stocks with imminent earnings."""
        strat_cfg = get_settings().strategy
        if not strat_cfg.earnings_avoid_enabled:
            return RiskCheck(True)

        stock = self.universe.get_stock(symbol)
        exchange = stock.exchange if stock else "SMART"
        currency = stock.currency if stock else "USD"

        if has_upcoming_earnings(symbol, exchange, currency, within_days=strat_cfg.earnings_avoid_days):
            return RiskCheck(False, f"{symbol} has earnings within {strat_cfg.earnings_avoid_days} days — skipping")
        return RiskCheck(True)

    def get_dynamic_delta_range(self) -> tuple[float, float]:
        """
        Get the current delta range based on VIX level.
        Low VIX (<15): wider delta (more aggressive, but VIX is low so risk is low)
        Mid VIX (15-25): moderate delta
        High VIX (25-30): tight delta (further OTM, fatter premium anyway)

        0-2 DTE override: widen delta range because:
        - Theta decay is extreme, gamma risk is the main factor
        - Narrow delta windows eliminate too many candidates
        - At 0-2 DTE, a 0.25 delta put is very far OTM in absolute terms
        """
        strat_cfg = get_settings().strategy
        if not strat_cfg.dynamic_delta_enabled:
            return (strat_cfg.delta_min, strat_cfg.delta_max)

        regime = self.get_regime()
        vix = regime.vix

        if vix is None:
            return (strat_cfg.delta_min, strat_cfg.delta_max)

        if vix < 15:
            delta_range = (strat_cfg.delta_vix_low, strat_cfg.delta_vix_low_max)
            log.debug("dynamic_delta", vix=vix, regime="low", delta_range=delta_range)
        elif vix < 25:
            delta_range = (strat_cfg.delta_vix_mid, strat_cfg.delta_vix_mid_max)
            log.debug("dynamic_delta", vix=vix, regime="mid", delta_range=delta_range)
        else:
            delta_range = (strat_cfg.delta_vix_high, strat_cfg.delta_vix_high_max)
            log.debug("dynamic_delta", vix=vix, regime="high", delta_range=delta_range)

        # Note: DTE-specific delta widening is handled in the screener per-contract.
        # This method provides the base delta range from VIX level.

        return delta_range

    # ── Master check ────────────────────────────────────────
    def can_open_put(self, symbol: str, market: str | None = None) -> RiskCheck:
        """Run all pre-trade checks for selling a put."""
        checks = [
            self.check_vix_gate(),
            self.check_margin_usage(),
            self.check_daily_limit(),
            self.check_position_limit(),
            self.check_position_size(symbol),
            self.check_sector_exposure(symbol),
            self.check_correlation(symbol),
            self.check_delta_exposure(symbol),
            self.check_buying_power(),
            self.check_duplicate_position(symbol),
            self.check_earnings(symbol),
            self.check_iv_rank(symbol),
        ]
        for check in checks:
            if not check.allowed:
                log.info("risk_blocked", symbol=symbol, reason=check.reason)
                return check

        # SPY MA gate — doesn't block, but signals reduction
        spy_check = self.check_spy_ma_gate(market=market)
        if spy_check.reduce_pct < 1.0:
            return RiskCheck(True, reason=spy_check.reason, reduce_pct=spy_check.reduce_pct)

        return RiskCheck(True)

    def increment_daily_count(self) -> None:
        """Call after a successful trade to update the daily counter."""
        if self._daily_count is not None:
            self._daily_count += 1
