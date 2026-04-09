"""
Portfolio Analyzer — multi-timeframe, tier-aware buy signal detection.

Signal system (composite score, higher = stronger opportunity):
  1. SMA discount — tier-specific period (50d growth, 150d breakthrough, 200d dividend)
  2. RSI oversold — tier-specific thresholds
  3. Price vs 52-week range — how close to yearly low
  4. Trend confirmation — shorter SMA above longer SMA = healthy uptrend pullback
  5. Volume surge — above-average volume on down days = capitulation

Composite score determines both IF and HOW MUCH to buy.

Market guard:
  - If SPY is >15% above its 200-day SMA → reduce buys (accumulate cash)
  - Per-stock guard: skip if stock is >20% above its own SMA (chasing)
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from ib_insync import IB, Stock

from src.core.logger import get_logger

log = get_logger(__name__)


def _ensure_event_loop():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


# ── Tier-specific parameters ─────────────────────────────────
TIER_PARAMS = {
    "dividend": {
        "sma_period": 200,       # slow-moving, mean-reverts well
        "trend_sma_fast": 50,    # for trend confirmation
        "trend_sma_slow": 200,
        "min_discount_pct": 7.0, # wait for real value
        "rsi_oversold": 25.0,    # conservative — only deep oversold
        "rsi_period": 14,
        "near_low_pct": 5.0,     # within 5% of 52w low
    },
    "growth": {
        "sma_period": 100,       # faster — growth stocks trend strongly
        "trend_sma_fast": 21,    # ~1 month
        "trend_sma_slow": 100,
        "min_discount_pct": 5.0, # moderate discount
        "rsi_oversold": 30.0,
        "rsi_period": 14,
        "near_low_pct": 8.0,     # wider band — growth stocks are more volatile
    },
    "breakthrough": {
        "sma_period": 50,        # fastest — high-vol, trend can shift quickly
        "trend_sma_fast": 10,    # ~2 weeks
        "trend_sma_slow": 50,
        "min_discount_pct": 3.0, # don't need big discount, these move fast
        "rsi_oversold": 35.0,    # more generous — these overshoot often
        "rsi_period": 14,
        "near_low_pct": 10.0,    # even wider — 50%+ drawdowns are normal
    },
}


class StockAnalysis:
    """Analysis result for a single stock."""
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.exchange: str = "SMART"
        self.currency: str = "USD"
        self.current_price: float | None = None
        self.sma_primary: float | None = None   # tier-specific SMA
        self.sma_fast: float | None = None       # short-term trend SMA
        self.sma_slow: float | None = None       # long-term trend SMA
        self.discount_pct: float | None = None   # % below primary SMA (positive = below)
        self.rsi_14: float | None = None
        self.high_52w: float | None = None
        self.low_52w: float | None = None
        self.near_52w_low: bool = False
        self.pct_from_52w_low: float | None = None
        self.trend_healthy: bool = False          # fast SMA > slow SMA (uptrend pullback)
        self.volume_surge: bool = False           # recent volume > 1.5x average
        self.buy_signal: bool = False
        self.signal_type: str = ""
        self.signal_strength: float = 0           # 0-100 composite score
        self.composite_score: float = 0           # raw composite before normalization

        # Keep sma_200 for backward compatibility (dashboard, put entry)
        self.sma_200: float | None = None


class PortfolioAnalyzer:
    """Analyze stocks for buy opportunities with tier-aware multi-timeframe signals."""

    def __init__(self, ib: IB, sma_period: int = 200, rsi_period: int = 14,
                 min_discount_pct: float = 5.0, rsi_oversold: float = 30.0):
        self.ib = ib
        # These are defaults — overridden per-tier in analyze_stock
        self.sma_period = sma_period
        self.rsi_period = rsi_period
        self.min_discount_pct = min_discount_pct
        self.rsi_oversold = rsi_oversold

    def analyze_stock(self, symbol: str, exchange: str = "SMART",
                      currency: str = "USD",
                      tier: str = "growth") -> Optional[StockAnalysis]:
        """
        Full analysis of a single stock for buy signal.
        Uses tier-specific parameters for optimal signal detection.
        """
        _ensure_event_loop()

        # Get tier-specific parameters
        params = TIER_PARAMS.get(tier, TIER_PARAMS["growth"])

        analysis = StockAnalysis(symbol)
        analysis.exchange = exchange
        analysis.currency = currency

        try:
            contract = Stock(symbol, exchange, currency)
            qualified = self.ib.qualifyContracts(contract)
            if not qualified:
                log.warning("portfolio_qualify_failed", symbol=symbol)
                return None
            log.info("portfolio_qualify_ok", symbol=symbol, primaryExch=contract.primaryExch)

            contract.exchange = "SMART"

            # Need enough bars for the longest SMA + RSI + some buffer
            max_period = max(params["sma_period"], params["trend_sma_slow"], 252) + 30
            bars = self._fetch_bars(contract, max_period)

            if not bars or len(bars) < params["sma_period"]:
                log.warning("portfolio_insufficient_data", symbol=symbol,
                          bars=len(bars) if bars else 0)
                return None

            closes = [b.close for b in bars]
            highs = [b.high for b in bars]
            lows = [b.low for b in bars]
            volumes = [b.volume for b in bars]

            # Current price
            analysis.current_price = closes[-1]

            # ── Primary SMA (tier-specific) ──
            sma_period = params["sma_period"]
            if len(closes) >= sma_period:
                analysis.sma_primary = sum(closes[-sma_period:]) / sma_period

            # ── 200d SMA for backward compatibility ──
            if len(closes) >= 200:
                analysis.sma_200 = sum(closes[-200:]) / 200

            # ── Trend SMAs ──
            fast_p = params["trend_sma_fast"]
            slow_p = params["trend_sma_slow"]
            if len(closes) >= fast_p:
                analysis.sma_fast = sum(closes[-fast_p:]) / fast_p
            if len(closes) >= slow_p:
                analysis.sma_slow = sum(closes[-slow_p:]) / slow_p

            # Trend health: fast SMA > slow SMA means the stock is in an uptrend
            # A pullback in an uptrend is a much better buy than a pullback in a downtrend
            if analysis.sma_fast and analysis.sma_slow:
                analysis.trend_healthy = analysis.sma_fast > analysis.sma_slow

            # ── Discount from primary SMA ──
            if analysis.sma_primary and analysis.sma_primary > 0:
                analysis.discount_pct = (
                    (analysis.sma_primary - analysis.current_price) / analysis.sma_primary * 100
                )

            # ── RSI ──
            analysis.rsi_14 = self._calculate_rsi(closes, params["rsi_period"])

            # ── 52-week high/low ──
            recent_252 = min(252, len(bars))
            analysis.high_52w = max(highs[-recent_252:])
            analysis.low_52w = min(lows[-recent_252:])

            if analysis.low_52w and analysis.low_52w > 0:
                analysis.pct_from_52w_low = (
                    (analysis.current_price - analysis.low_52w) / analysis.low_52w * 100
                )
                analysis.near_52w_low = analysis.pct_from_52w_low <= params["near_low_pct"]

            # ── Volume surge detection ──
            # High volume on recent down days = capitulation selling = opportunity
            if len(volumes) >= 50 and len(closes) >= 5:
                avg_vol = sum(volumes[-50:]) / 50
                recent_vol = sum(volumes[-3:]) / 3
                recent_return = (closes[-1] - closes[-5]) / closes[-5]
                # Volume > 1.5x average AND price down in last week
                analysis.volume_surge = (
                    avg_vol > 0 and recent_vol > avg_vol * 1.5 and recent_return < -0.02
                )

            # ── Anti-chase guard ──
            # Don't buy stocks that are too far above their SMA (chasing momentum)
            if analysis.discount_pct is not None and analysis.discount_pct < -20:
                log.debug("portfolio_anti_chase", symbol=symbol,
                          discount_pct=round(analysis.discount_pct, 1))
                return analysis  # return analysis but no buy signal

            # ── Composite signal scoring ──
            score = self._compute_composite_score(analysis, params)

            # ── Apply structural risk penalty ──
            # Stocks with higher structural risks need a bigger discount to trigger
            risk_penalty = 0.0
            try:
                from sqlalchemy import text as sa_text
                from src.core.database import get_engine
                with get_engine().connect() as conn:
                    row = conn.execute(sa_text(
                        "SELECT risk_total_penalty FROM portfolio_watchlist WHERE symbol = :sym"
                    ), {"sym": symbol}).fetchone()
                    if row and row[0]:
                        risk_penalty = float(row[0])
            except Exception:
                pass

            if risk_penalty > 0 and score > 0:
                original_score = score
                score = max(0, score - risk_penalty)
                if score == 0:
                    log.info("portfolio_risk_penalty_blocked",
                             symbol=symbol,
                             original_score=round(original_score, 1),
                             penalty=risk_penalty)

            if score > 0:
                analysis.buy_signal = True
                analysis.composite_score = score
                analysis.signal_strength = min(100, score)

                # Determine primary signal type for logging
                analysis.signal_type = self._primary_signal_type(analysis, params)

                log.info(
                    "portfolio_buy_signal",
                    symbol=symbol,
                    tier=tier,
                    signal=analysis.signal_type,
                    strength=round(analysis.signal_strength, 1),
                    composite=round(score, 1),
                    price=round(analysis.current_price, 2),
                    sma=round(analysis.sma_primary, 2) if analysis.sma_primary else None,
                    discount_pct=round(analysis.discount_pct, 1) if analysis.discount_pct else None,
                    rsi=round(analysis.rsi_14, 1) if analysis.rsi_14 else None,
                    trend="up" if analysis.trend_healthy else "down",
                    volume_surge=analysis.volume_surge,
                )

            return analysis

        except Exception as e:
            log.warning("portfolio_analysis_error", symbol=symbol,
                        error=str(e), error_type=type(e).__name__)
            return None

    def _fetch_bars(self, contract, max_period: int):
        """Fetch historical bars with TRADES→MIDPOINT fallback."""
        bars = None
        for what in ("TRADES", "MIDPOINT"):
            try:
                bars = self.ib.reqHistoricalData(
                    contract,
                    endDateTime="",
                    durationStr=f"{max_period} D",
                    barSizeSetting="1 day",
                    whatToShow=what,
                    useRTH=False,
                    formatDate=1,
                    timeout=10,
                )
                if bars and len(bars) > 50:
                    break
            except Exception:
                pass
            self.ib.sleep(0.5)
        return bars

    @staticmethod
    def _compute_composite_score(analysis: StockAnalysis, params: dict) -> float:
        """
        Compute a composite buy score from multiple signals.
        Returns >0 if any signal triggers. Higher = stronger opportunity.

        Scoring weights:
          - SMA discount:      0-40 points (primary value signal)
          - RSI oversold:      0-30 points (momentum signal)
          - 52-week low:       0-15 points (historical context)
          - Trend confirmation: 0-10 points (bonus for uptrend pullback)
          - Volume surge:       0-5 points  (bonus for capitulation)
        """
        score = 0.0
        triggered = False

        # ── SMA discount signal (0-40 points) ──
        if analysis.discount_pct is not None:
            min_disc = params["min_discount_pct"]
            if analysis.discount_pct >= min_disc:
                triggered = True
                # Scale: min_discount → 20 pts, 2x min_discount → 40 pts
                disc_score = min(40, (analysis.discount_pct / min_disc) * 20)
                score += disc_score

        # ── RSI oversold signal (0-30 points) ──
        if analysis.rsi_14 is not None:
            rsi_threshold = params["rsi_oversold"]
            if analysis.rsi_14 <= rsi_threshold:
                triggered = True
                # Scale: threshold → 15 pts, RSI 10 → 30 pts
                rsi_score = min(30, ((rsi_threshold - analysis.rsi_14) / rsi_threshold) * 30 + 15)
                score += rsi_score

        # ── 52-week low proximity (0-15 points) ──
        if analysis.near_52w_low and analysis.pct_from_52w_low is not None:
            triggered = True
            # Closer to low = more points: at low → 15, at threshold → 8
            near_low_pct = params["near_low_pct"]
            low_score = max(8, 15 - (analysis.pct_from_52w_low / near_low_pct) * 7)
            score += low_score

        # Only apply bonuses if at least one primary signal triggered
        if not triggered:
            return 0.0

        # ── Trend confirmation bonus (0-10 points) ──
        # Pullback in an uptrend is healthier than in a downtrend
        if analysis.trend_healthy:
            score += 10

        # ── Volume surge bonus (0-5 points) ──
        # High volume selling = capitulation = better entry
        if analysis.volume_surge:
            score += 5

        return score

    @staticmethod
    def _primary_signal_type(analysis: StockAnalysis, params: dict) -> str:
        """Determine the primary signal type for logging."""
        signals = []

        if analysis.discount_pct is not None and analysis.discount_pct >= params["min_discount_pct"]:
            signals.append(("below_sma", analysis.discount_pct))
        if analysis.rsi_14 is not None and analysis.rsi_14 <= params["rsi_oversold"]:
            signals.append(("rsi_oversold", params["rsi_oversold"] - analysis.rsi_14))
        if analysis.near_52w_low:
            signals.append(("52w_low", 10.0))

        if not signals:
            return "composite"

        signals.sort(key=lambda s: s[1], reverse=True)
        return signals[0][0]

    def check_market_overbought(self, threshold_pct: float = 15.0) -> tuple[bool, float | None]:
        """
        Check if the broad market (SPY) is overbought.
        Returns (is_overbought, pct_above_sma).
        """
        _ensure_event_loop()

        try:
            contract = Stock("SPY", "SMART", "USD")
            self.ib.qualifyContracts(contract)

            bars = self.ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="220 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                timeout=10,
            )

            if not bars or len(bars) < 200:
                return False, None

            closes = [b.close for b in bars]
            sma_200 = sum(closes[-200:]) / 200
            current = closes[-1]
            pct_above = ((current - sma_200) / sma_200) * 100

            is_overbought = pct_above > threshold_pct

            if is_overbought:
                log.info("market_overbought", spy_price=round(current, 2),
                         sma_200=round(sma_200, 2), pct_above=round(pct_above, 1))

            return is_overbought, round(pct_above, 1)

        except Exception as e:
            log.warning("market_check_error", error=str(e))
            return False, None

    @staticmethod
    def _calculate_rsi(closes: list[float], period: int = 14) -> float | None:
        """Calculate RSI from closing prices using Wilder smoothing."""
        if len(closes) < period + 1:
            return None

        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        recent = deltas[-(period + 50):]  # extra data for Wilder smoothing

        gains = [d if d > 0 else 0 for d in recent]
        losses = [-d if d < 0 else 0 for d in recent]

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 1)
