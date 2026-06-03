"""
Risk management — VIX gate, SPY MA gate, position limits, exposure checks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date

from src.broker.market_data import get_vix, get_stock_price, get_spy_moving_averages, get_iv_rank, has_upcoming_earnings, get_stock_ma200
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
    reduce_pct: float = 1.0       # universe-filter pre-scan (1.0 = scan all, 0.5 = scan 50%)
    size_multiplier: float = 1.0  # per-trade contract multiplier (1.0 = full size, 0.5 = halve)


@dataclass
class MarketRegime:
    """Snapshot of current market conditions for risk decisions."""
    vix: float | None = None
    vix_prev_day: float | None = None   # yesterday's VIX close (from SystemState)
    vix_spike: float | None = None      # today - yesterday (None if no prev)
    spy_bullish: bool | None = None
    spy_fast_ma: float | None = None
    spy_slow_ma: float | None = None
    spy_ma50: float | None = None                  # 50-day SMA (slower trend filter)
    spy_distance_below_ma50: float | None = None   # + when below MA50, - when above
    spy_ma200: float | None = None                  # 200-day SMA (bear-market gate)
    spy_distance_below_ma200: float | None = None   # + when below MA200, - when above
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
        self._iv_rank_cache: dict[str, tuple[datetime, float | None]] = {}
        # Per-name MA200 cache: (date_iso, is_below). One IBKR call/sym/day.
        self._ma200_cache: dict[str, tuple[str, bool | None]] = {}
        # Cash-and-carry grind detector state (cached per scan). Persists across
        # process restarts via SystemState keys (grind_active / grind_raw_true_streak
        # / grind_raw_false_streak / grind_last_eval_date).
        self._grind_active_cached: bool | None = None
        self._grind_reason_cached: str = ""

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

        # Pull yesterday's VIX close for rate-of-change computation
        regime.vix_prev_day = self._get_last_known("vix_prev_day")
        if regime.vix is not None and regime.vix_prev_day is not None:
            regime.vix_spike = regime.vix - regime.vix_prev_day
            if regime.vix_spike > self.cfg.vix_spike_bump_1_tier:
                log.info("vix_spike_detected",
                         vix=round(regime.vix, 2),
                         vix_prev=round(regime.vix_prev_day, 2),
                         spike=round(regime.vix_spike, 2))

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
                    regime.spy_ma50 = spy_data.get("ma50")
                    regime.spy_distance_below_ma50 = spy_data.get("distance_below_ma50")
                    regime.spy_ma200 = spy_data.get("ma200")
                    regime.spy_distance_below_ma200 = spy_data.get("distance_below_ma200")
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
        from datetime import date as _date
        with get_db() as db:
            # Promote current_vix -> vix_prev_day if stored date is not today
            if regime.vix is not None:
                today_str = _date.today().isoformat()
                stored_date = db.query(SystemState).filter(
                    SystemState.key == "current_vix_date"
                ).first()
                stored_vix = db.query(SystemState).filter(
                    SystemState.key == "current_vix"
                ).first()
                if (stored_date and stored_vix
                        and stored_date.value != today_str
                        and stored_vix.value):
                    # Yesterday's VIX becomes vix_prev_day
                    prev = db.query(SystemState).filter(
                        SystemState.key == "vix_prev_day"
                    ).first()
                    if prev:
                        prev.value = stored_vix.value
                    else:
                        db.add(SystemState(key="vix_prev_day", value=stored_vix.value))
                    log.info("vix_prev_day_promoted",
                             prev_date=stored_date.value,
                             prev_vix=stored_vix.value)
                # Update or create current_vix_date
                if stored_date:
                    stored_date.value = today_str
                else:
                    db.add(SystemState(key="current_vix_date", value=today_str))
            if regime.vix is not None and regime.vix > self.cfg.vix_pause_threshold:
                regime_label = "halt"
            elif regime.spy_bullish is False:
                regime_label = "bear"
            else:
                regime_label = "normal"

            # Compute derived fields for dashboard display
            try:
                eff_tier = self.effective_vix_tier(regime)
            except Exception:
                eff_tier = ""
            try:
                drawdown_5d = self._get_recent_nlv_drawdown()
            except Exception:
                drawdown_5d = 0.0

            pairs = {
                "market_regime": regime_label,
                "current_vix": str(regime.vix) if regime.vix else "",
                "vix_spike": str(round(regime.vix_spike, 2)) if regime.vix_spike is not None else "",
                "effective_vix_tier": eff_tier,
                "spy_bullish": str(regime.spy_bullish).lower() if regime.spy_bullish is not None else "",
                "spy_fast_ma": str(regime.spy_fast_ma) if regime.spy_fast_ma else "",
                "spy_slow_ma": str(regime.spy_slow_ma) if regime.spy_slow_ma else "",
                "spy_ma50": str(regime.spy_ma50) if regime.spy_ma50 else "",
                "spy_distance_below_ma50": str(round(regime.spy_distance_below_ma50, 4)) if regime.spy_distance_below_ma50 is not None else "",
                "spy_ma200": str(regime.spy_ma200) if regime.spy_ma200 else "",
                "spy_distance_below_ma200": str(round(regime.spy_distance_below_ma200, 4)) if regime.spy_distance_below_ma200 is not None else "",
                "spy_price": str(regime.spy_price) if regime.spy_price else "",
                "eu_bullish": str(regime.eu_bullish).lower() if regime.eu_bullish is not None else "",
                "eu_price": str(regime.eu_price) if regime.eu_price else "",
                "asia_bullish": str(regime.asia_bullish).lower() if regime.asia_bullish is not None else "",
                "asia_price": str(regime.asia_price) if regime.asia_price else "",
                "drawdown_5d": str(round(drawdown_5d, 4)),
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
        """Block trading if VIX is above threshold AND rising. Direction-aware
        2026-05-27: VIX 30↑ (fear building, prices falling) and VIX 30↓ (vol
        crush, theta + vega paying you) are opposite states. If VIX is above
        threshold but clearly falling vs yesterday, keep writing into the crush.
        Fail-open if VIX unavailable; fail-closed if no prev-day data (treats
        unknown direction as rising for safety)."""
        regime = self.get_regime()
        if regime.vix is None:
            log.warning("vix_data_unavailable_allowing_trades")
            return RiskCheck(True)  # fail open — allow trading if VIX data unavailable
        if regime.vix <= self.cfg.vix_pause_threshold:
            return RiskCheck(True)

        prev = regime.vix_prev_day
        if prev is None:
            log.warning("vix_gate_triggered_no_prev",
                        vix=regime.vix, threshold=self.cfg.vix_pause_threshold)
            return RiskCheck(
                False,
                f"VIX at {regime.vix:.1f} > {self.cfg.vix_pause_threshold} threshold (no prev-day — halt for safety)",
            )

        falling_delta = prev - regime.vix
        if falling_delta > 2.0:
            log.info("vix_above_threshold_but_falling",
                     vix=regime.vix, prev=prev, drop=round(falling_delta, 2),
                     threshold=self.cfg.vix_pause_threshold)
            return RiskCheck(
                True,
                reason=(
                    f"VIX {regime.vix:.1f} > {self.cfg.vix_pause_threshold} but falling "
                    f"(prev {prev:.1f}, Δ −{falling_delta:.1f}) — keep writing into vol crush"
                ),
            )

        log.warning("vix_gate_triggered_rising",
                    vix=regime.vix, prev=prev,
                    threshold=self.cfg.vix_pause_threshold)
        return RiskCheck(
            False,
            f"VIX at {regime.vix:.1f} > {self.cfg.vix_pause_threshold} (prev {prev:.1f}, rising/flat-high)",
        )

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

    # ── Cash-and-carry: grind-detector state persistence ────────────────────
    # SystemState keys used (string-valued):
    #   grind_active                — "true" / "false"
    #   grind_raw_true_streak       — int (consecutive raw-true detector days)
    #   grind_raw_false_streak      — int (consecutive raw-false detector days)
    #   grind_last_eval_date        — YYYY-MM-DD (detector evaluates once per
    #                                 business day; idempotent on re-scan)
    # Same pattern as the existing VIX-history persistence in _store_regime.

    def _load_grind_state(self) -> tuple[bool, int, int, str | None]:
        """Return (active, raw_true_streak, raw_false_streak, last_eval_date)."""
        try:
            with get_db() as db:
                rows = {
                    s.key: s.value for s in db.query(SystemState).filter(
                        SystemState.key.in_([
                            "grind_active", "grind_raw_true_streak",
                            "grind_raw_false_streak", "grind_last_eval_date",
                        ])
                    ).all()
                }
            return (
                rows.get("grind_active", "false") == "true",
                int(rows.get("grind_raw_true_streak") or "0"),
                int(rows.get("grind_raw_false_streak") or "0"),
                rows.get("grind_last_eval_date") or None,
            )
        except Exception as e:
            log.debug("grind_state_load_failed", error=str(e))
            return (False, 0, 0, None)

    def _save_grind_state(self, active: bool, raw_true_streak: int,
                          raw_false_streak: int, last_eval_date: str) -> None:
        """Idempotent upsert of the four grind-state keys."""
        try:
            with get_db() as db:
                pairs = {
                    "grind_active": "true" if active else "false",
                    "grind_raw_true_streak": str(raw_true_streak),
                    "grind_raw_false_streak": str(raw_false_streak),
                    "grind_last_eval_date": last_eval_date,
                }
                for key, value in pairs.items():
                    row = db.query(SystemState).filter(SystemState.key == key).first()
                    if row:
                        row.value = value
                    else:
                        db.add(SystemState(key=key, value=value))
                db.commit()
        except Exception as e:
            log.warning("grind_state_save_failed", error=str(e))

    def evaluate_grind_detector(self, force: bool = False) -> tuple[bool, str]:
        """Evaluate the cash-and-carry grind detector once per business day.

        Returns (active, reason). Caches the result in self._grind_active_cached
        for the current scan; subsequent calls within the same scan reuse the
        cached value. Persists debounce state across process restarts via
        SystemState (grind_active / grind_raw_*_streak / grind_last_eval_date).

        Day-level idempotency: signals are recomputed only if the last
        evaluation date isn't today (IBKR fetch is ~10 seconds — too heavy
        for every 30-minute scan). Subsequent scans the same day read the
        persisted state without re-fetching. Pass force=True to override.
        """
        if not self.cfg.cash_carry_detector_enabled:
            self._grind_active_cached = False
            self._grind_reason_cached = "detector disabled"
            return (False, self._grind_reason_cached)

        if self._grind_active_cached is not None and not force:
            return (self._grind_active_cached, self._grind_reason_cached)

        active, true_streak, false_streak, last_eval = self._load_grind_state()
        today = date.today().isoformat()

        # Day-level idempotency: don't recompute signals if already done today.
        if last_eval == today and not force:
            self._grind_active_cached = active
            self._grind_reason_cached = (
                f"cached (last_eval={last_eval}, "
                f"streaks {true_streak}/{false_streak})"
            )
            return (active, self._grind_reason_cached)

        # Compute today's raw detector signals.
        from src.broker.market_data import compute_grind_signals
        try:
            # US-listed names only — SGOV is US, and the wheel's universe is
            # tech-heavy US. EU/ASIA exchanges have different timing/holidays.
            symbols = self.universe.symbols_for_market("SMART") if self.universe else []
        except Exception:
            symbols = []
        if not symbols:
            log.info("grind_skip_no_universe")
            self._grind_active_cached = active
            self._grind_reason_cached = "no universe"
            return (active, self._grind_reason_cached)

        signals = compute_grind_signals(
            symbols,
            rv_window_days=self.cfg.cash_carry_detect_window_days,
            trend_window_days=self.cfg.cash_carry_trend_window_days,
        )
        if signals is None:
            log.warning("grind_signals_unavailable_keeping_state")
            self._grind_active_cached = active
            self._grind_reason_cached = "signals unavailable; keeping prior state"
            return (active, self._grind_reason_cached)

        rv = signals.get("realized_vol_median")
        trend = signals.get("spy_trend_return_pct")
        if rv is None or trend is None:
            log.info(
                "grind_signals_partial",
                rv=rv, trend=trend, n_syms=signals.get("n_symbols_used"),
            )
            self._grind_active_cached = active
            self._grind_reason_cached = (
                f"signals partial (rv={rv}, trend={trend}); keeping prior state"
            )
            return (active, self._grind_reason_cached)

        raw = (rv > self.cfg.cash_carry_realized_vol_threshold
               and abs(trend) < self.cfg.cash_carry_trend_max_abs_pct)

        if raw:
            true_streak += 1
            false_streak = 0
            if not active and true_streak >= self.cfg.cash_carry_on_days_required:
                active = True
                log.info("grind_detector_activated",
                         rv=round(rv, 4), trend_pct=round(trend, 2))
        else:
            false_streak += 1
            true_streak = 0
            if active and false_streak >= self.cfg.cash_carry_off_days_required:
                active = False
                log.info("grind_detector_deactivated",
                         rv=round(rv, 4), trend_pct=round(trend, 2))

        self._save_grind_state(active, true_streak, false_streak, today)
        self._grind_active_cached = active
        self._grind_reason_cached = (
            f"rv={round(rv, 4)} (>{self.cfg.cash_carry_realized_vol_threshold}?) "
            f"trend={round(trend, 2)}% (|.|<{self.cfg.cash_carry_trend_max_abs_pct}?) "
            f"streaks true/false={true_streak}/{false_streak}"
        )
        return (active, self._grind_reason_cached)

    # ── Crash detector (mirror of MarsWalk crash detector) ──────────────────
    # Opposite shape from hvg: high vol AND SHARP trend (|60d|>15%). Designed
    # for Lehman-class regimes. State keys: crash_active, crash_raw_*_streak,
    # crash_last_eval_date.

    def _load_crash_state(self) -> tuple[bool, int, int, str | None]:
        try:
            with get_db() as db:
                rows = {
                    s.key: s.value for s in db.query(SystemState).filter(
                        SystemState.key.in_([
                            "crash_active", "crash_raw_true_streak",
                            "crash_raw_false_streak", "crash_last_eval_date",
                        ])
                    ).all()
                }
            return (
                rows.get("crash_active", "false") == "true",
                int(rows.get("crash_raw_true_streak") or "0"),
                int(rows.get("crash_raw_false_streak") or "0"),
                rows.get("crash_last_eval_date") or None,
            )
        except Exception as e:
            log.debug("crash_state_load_failed", error=str(e))
            return (False, 0, 0, None)

    def _save_crash_state(self, active: bool, raw_true_streak: int,
                          raw_false_streak: int, last_eval_date: str) -> None:
        try:
            with get_db() as db:
                pairs = {
                    "crash_active": "true" if active else "false",
                    "crash_raw_true_streak": str(raw_true_streak),
                    "crash_raw_false_streak": str(raw_false_streak),
                    "crash_last_eval_date": last_eval_date,
                }
                for key, value in pairs.items():
                    row = db.query(SystemState).filter(SystemState.key == key).first()
                    if row:
                        row.value = value
                    else:
                        db.add(SystemState(key=key, value=value))
                db.commit()
        except Exception as e:
            log.warning("crash_state_save_failed", error=str(e))

    def evaluate_crash_detector(self, force: bool = False) -> tuple[bool, str]:
        """Day-level idempotent eval. Reuses compute_grind_signals output —
        same upstream signals (universe rv + SPY trend) but different
        thresholds. Crash = rv > crash_realized_vol_threshold AND
        |trend60d| > crash_trend_abs_pct."""
        if not self.cfg.crash_when_active_enabled:
            self._crash_active_cached = False
            self._crash_reason_cached = "detector disabled"
            return (False, self._crash_reason_cached)
        if getattr(self, "_crash_active_cached", None) is not None and not force:
            return (self._crash_active_cached, self._crash_reason_cached)

        active, true_streak, false_streak, last_eval = self._load_crash_state()
        today = date.today().isoformat()
        if last_eval == today and not force:
            self._crash_active_cached = active
            self._crash_reason_cached = (
                f"cached (last_eval={last_eval}, "
                f"streaks {true_streak}/{false_streak})"
            )
            return (active, self._crash_reason_cached)

        from src.broker.market_data import compute_grind_signals
        try:
            symbols = self.universe.symbols_for_market("SMART") if self.universe else []
        except Exception:
            symbols = []
        if not symbols:
            self._crash_active_cached = active
            self._crash_reason_cached = "no universe"
            return (active, self._crash_reason_cached)
        # The 60d return signal is approximated via SPY trend with a 60d window
        # (overriding the default 180d for the trend window). compute_grind_signals
        # returns SPY trend at trend_window_days — we call with trend_window=60.
        signals = compute_grind_signals(
            symbols,
            rv_window_days=self.cfg.crash_detect_window_days,
            trend_window_days=self.cfg.crash_detect_window_days,  # 60d for crash
        )
        if signals is None:
            self._crash_active_cached = active
            self._crash_reason_cached = "signals unavailable; keeping prior state"
            return (active, self._crash_reason_cached)
        rv = signals.get("realized_vol_median")
        trend = signals.get("spy_trend_return_pct")
        if rv is None or trend is None:
            self._crash_active_cached = active
            self._crash_reason_cached = (
                f"signals partial (rv={rv}, trend={trend}); keeping prior state"
            )
            return (active, self._crash_reason_cached)
        raw = (rv > self.cfg.crash_realized_vol_threshold
               and abs(trend) > self.cfg.crash_trend_abs_pct)
        if raw:
            true_streak += 1
            false_streak = 0
            if not active and true_streak >= self.cfg.crash_on_days_required:
                active = True
                log.warning("crash_detector_activated",
                            rv=round(rv, 4), trend_pct=round(trend, 2))
        else:
            false_streak += 1
            true_streak = 0
            if active and false_streak >= self.cfg.crash_off_days_required:
                active = False
                log.info("crash_detector_deactivated",
                         rv=round(rv, 4), trend_pct=round(trend, 2))
        self._save_crash_state(active, true_streak, false_streak, today)
        self._crash_active_cached = active
        self._crash_reason_cached = (
            f"rv={round(rv, 4)} (>{self.cfg.crash_realized_vol_threshold}?) "
            f"|trend|={round(abs(trend), 2)}% (>{self.cfg.crash_trend_abs_pct}?) "
            f"streaks true/false={true_streak}/{false_streak}"
        )
        return (active, self._crash_reason_cached)

    def _rolling_nlv_return_pct(self, lookback_days: int) -> float | None:
        """Percent change in NLV vs lookback_days ago. Positive = grew, negative
        = lost. Returns None if insufficient history (caller decides fail-open).
        Used by the stagnation-boost detector to multiply the IV-rank ladder
        when NLV has gone nowhere — mirrors MarsWalk +stag (longgrind_sweep)."""
        try:
            from src.core.models import AccountSnapshot
            with get_db() as db:
                rows = (
                    db.query(AccountSnapshot)
                    .order_by(AccountSnapshot.date.desc())
                    .limit(lookback_days + 1)
                    .all()
                )
            if not rows or len(rows) < 2:
                return None
            current = rows[0].net_liquidation
            # rows[-1] is the OLDEST in the window (limit returns desc).
            past = rows[-1].net_liquidation
            if current <= 0 or past <= 0:
                return None
            return (current - past) / past * 100
        except Exception as e:
            log.debug("rolling_nlv_calc_failed", error=str(e))
            return None

    def _get_recent_nlv_drawdown(self, lookback_override: int | None = None) -> float:
        """
        Compute NLV drawdown over the lookback window.
        drawdown = (peak_of_prior_days - current) / peak_of_prior_days
        Positive value means we are below recent peak (= in drawdown).
        Returns 0.0 if insufficient data or no drawdown.

        Optional lookback_override lets callers compute the 20d parallel
        drawdown alongside the default 5d window (ported from MarsWalk
        Params.drawdown_long_lookback_days, 2026-05-28).
        """
        try:
            from src.core.models import AccountSnapshot
            lookback = lookback_override if lookback_override is not None else self.cfg.drawdown_lookback_days
            with get_db() as db:
                rows = (
                    db.query(AccountSnapshot)
                    .order_by(AccountSnapshot.date.desc())
                    .limit(lookback + 1)
                    .all()
                )
            if not rows or len(rows) < 2:
                return 0.0
            # rows[0] is most recent (today or last snapshot); rest are prior days
            current = rows[0].net_liquidation
            prior = [r.net_liquidation for r in rows[1:] if r.net_liquidation > 0]
            if not prior or current <= 0:
                return 0.0
            peak = max(prior)
            if peak <= 0:
                return 0.0
            drawdown = (peak - current) / peak
            return max(drawdown, 0.0)
        except Exception as e:
            log.debug("drawdown_calc_failed", error=str(e))
            return 0.0

    def _drawdown_cap_multiplier_long(self, drawdown: float) -> float:
        """20d-window variant of _drawdown_cap_multiplier — same shape, looser
        thresholds (3% / 6% / 12% by default) to catch slow-grind bears.
        Mirrors MarsWalk Params.drawdown_long_threshold_* defaults."""
        if drawdown > self.cfg.drawdown_long_threshold_severe:
            return 0.25
        if drawdown > self.cfg.drawdown_long_threshold_mid:
            return 0.50
        if drawdown > self.cfg.drawdown_long_threshold_light:
            return 0.75
        return 1.0

    def _drawdown_cap_multiplier(self, drawdown: float) -> float:
        """Return multiplier on daily position cap based on drawdown size."""
        if drawdown > self.cfg.drawdown_threshold_severe:
            return 0.25
        if drawdown > self.cfg.drawdown_threshold_mid:
            return 0.50
        if drawdown > self.cfg.drawdown_threshold_light:
            return 0.75
        return 1.0

    def _get_dynamic_daily_limit(self) -> int:
        """
        Calculate daily position limit based on portfolio size, scaled down
        if the account is in recent NLV drawdown.
        Base: 10 trades for first 100K. Then +1 per additional 100K.
        Capped at max_daily_positions_cap. Floored at drawdown_min_cap.
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
            size_limit = base
        else:
            extra = int((net_liq - step) / step)
            size_limit = min(base + extra, cap)

        # Drawdown scaling — 5d window AND optional parallel 20d window.
        # Final multiplier = min(5d, 20d) so whichever sees the deeper trouble
        # wins. 20d catches slow-grind bears (bear_2022-class) that 5d misses.
        # Set drawdown_long_lookback_days=0 to disable the long window.
        drawdown = self._get_recent_nlv_drawdown()
        multiplier = self._drawdown_cap_multiplier(drawdown)
        if self.cfg.drawdown_long_lookback_days > 0:
            dd_long = self._get_recent_nlv_drawdown(
                lookback_override=self.cfg.drawdown_long_lookback_days
            )
            mult_long = self._drawdown_cap_multiplier_long(dd_long)
            multiplier = min(multiplier, mult_long)
        if multiplier < 1.0:
            scaled = max(int(size_limit * multiplier), self.cfg.drawdown_min_cap)
            log.info("drawdown_scaling_applied",
                     drawdown=round(drawdown, 4),
                     multiplier=multiplier,
                     base_limit=size_limit,
                     scaled_limit=scaled)
            return scaled
        return size_limit

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
        Small accounts: concentrated, high conviction.
        Large accounts: up to 40 positions (diversified).

        Tiers:
          NLV < $25K   → 4 positions
          NLV < $50K   → 6 positions
          NLV < $100K  → 10 positions   # 2026-06-03: was 8 — count cap was pinning
                                         # well-funded $50-100K accounts below the
                                         # delta/margin gates; hand throttle to those.
          NLV < $200K  → 10 positions
          NLV < $500K  → 15 positions
          NLV < $2M    → 30 positions   # was 20 — lift so big accounts can use the
          NLV < $5M    → 50 positions   # margin allowance the collateral cap admits
          NLV >= $5M   → 75 positions   # without leaving 2/3 of room idle.
        Lower tiers unchanged — small accounts already saturate their cap.
        """
        try:
            summary = get_account_summary()
            net_liq = summary.net_liquidation if summary and summary.net_liquidation > 0 else 0
        except Exception as e:
            log.warning("position_limit_check_skipped", error=str(e))
            return RiskCheck(True)  # fail open

        if net_liq <= 0:
            return RiskCheck(True)

        if net_liq < 25_000:
            max_pos = 4
        elif net_liq < 50_000:
            max_pos = 6
        elif net_liq < 100_000:
            max_pos = 10
        elif net_liq < 200_000:
            max_pos = 10
        elif net_liq < 500_000:
            max_pos = 15
        elif net_liq < 2_000_000:
            max_pos = 30
        elif net_liq < 5_000_000:
            max_pos = 50
        else:
            max_pos = 75

        # Count only "slot-consuming" positions: short_put (cash committed) and
        # stock (capital deployed). Covered calls don't consume an additional slot
        # — they're bound to an existing stock position. Counting them double-blocks
        # the wheel: a stock + its covered call would consume 2/4 slots when really
        # it's one wheel cycle.
        with get_db() as db:
            open_count = (
                db.query(Position)
                .filter(
                    Position.status == PositionStatus.OPEN,
                    Position.position_type.in_(["short_put", "stock"]),
                )
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
        Adaptive per-position size limit based on NLV, with hard dollar cap for large accounts.

        Two-layer check:
          Layer 1 — percentage-based (existing): NLV × 6 capacity × adaptive %
            NLV < $50K   → 25%
            NLV < $200K  → 15%
            NLV >= $200K → 5%

          Layer 2 — hard dollar cap (scaling safeguard):
            max = min(NLV × position_dollar_pct, max_position_dollars)
            floor at min_position_dollars so small accounts are unaffected
            At $5M NLV → min($50K, $150K) = $50K per position
            At $15M NLV → min($150K, $150K) = $150K per position (ceiling)

        Real margin enforcement is handled by get_whatif_margin in put_seller.py.
        This check adds secondary guardrails on position concentration.
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

        estimated_margin = price * 100  # 1 contract = 100 shares

        # Layer 1 — percentage-based cap (existing logic)
        capacity = net_liq * 6
        max_position_value = capacity * effective_limit
        if estimated_margin > max_position_value:
            return RiskCheck(
                False,
                f"{symbol} estimated margin ${estimated_margin:,.0f} exceeds "
                f"{effective_limit:.0%} position limit (${max_position_value:,.0f}) at NLV ${net_liq:,.0f}",
            )

        # Layer 2 — hard dollar cap (scaling safeguard, only bites at large NLV)
        dollar_cap = min(
            net_liq * self.cfg.position_dollar_pct,
            self.cfg.max_position_dollars,
        )
        if dollar_cap >= self.cfg.min_position_dollars and estimated_margin > dollar_cap:
            return RiskCheck(
                False,
                f"{symbol} estimated margin ${estimated_margin:,.0f} exceeds "
                f"hard dollar cap ${dollar_cap:,.0f} (NLV ${net_liq:,.0f} × {self.cfg.position_dollar_pct:.0%}, "
                f"ceiling ${self.cfg.max_position_dollars:,.0f})",
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

        iv_rank = self._iv_rank_cached(symbol)

        if iv_rank is None:
            # Can't determine IV rank — allow trading (fail open)
            return RiskCheck(True)

        # Bull-regime override: in confirmed bulls, only write on names with
        # IV rank >= bull_regime_iv_rank_min (default 50). Filters out the
        # dead-IV consumer-staples that dilute bull-regime yield.
        if self.in_bull_regime():
            effective_min = self.cfg.bull_regime_iv_rank_min
        else:
            # VIX-adaptive threshold (baseline outside bull regime)
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
            in_bull = self.in_bull_regime()
            ctx = "bull-regime override" if in_bull else f"VIX-adaptive, base={strat_cfg.iv_rank_min}%"
            return RiskCheck(
                False,
                f"{symbol} IV rank {iv_rank:.0f}% < {effective_min}% minimum ({ctx}, premium too cheap)",
            )

        log.debug("iv_rank_passed", symbol=symbol, iv_rank=round(iv_rank, 1),
                   threshold=effective_min, bull=self.in_bull_regime())
        return RiskCheck(True)

    def _iv_rank_cached(self, symbol: str, ttl_seconds: int = 900) -> float | None:
        """Fetch IV-rank once and cache per symbol (TTL ~15 min) so the risk
        gauntlet (check_iv_rank) and IV-rank sizing share a single fetch per
        scan instead of calling get_iv_rank twice for the same symbol."""
        now = datetime.utcnow()
        cached = self._iv_rank_cache.get(symbol)
        if cached and (now - cached[0]).total_seconds() < ttl_seconds:
            return cached[1]
        strat_cfg = get_settings().strategy
        stock = self.universe.get_stock(symbol)
        exchange = stock.exchange if stock else "SMART"
        currency = stock.currency if stock else "USD"
        try:
            value = get_iv_rank(symbol, exchange=exchange, currency=currency,
                                lookback_days=strat_cfg.iv_lookback_days)
        except Exception as e:
            log.debug("iv_rank_fetch_failed", symbol=symbol, error=str(e))
            value = None
        self._iv_rank_cache[symbol] = (now, value)
        return value

    def get_iv_rank_value(self, symbol: str) -> float | None:
        """Raw IV-rank (0-100) for sizing decisions, or None if unavailable.
        Shares the per-symbol cache with check_iv_rank (one fetch per scan)."""
        if not get_settings().strategy.iv_rank_enabled:
            return None
        return self._iv_rank_cached(symbol)

    def iv_rank_size_multiplier(self, iv_rank: float | None) -> int:
        """#3 Scale contracts up when premium is rich: 1x / 2x / 3x by IV-rank
        band, hard-capped by iv_rank_size_max_multiplier. 1x when disabled or
        IV-rank unknown. The per-position $ cap + whatif margin (live) and human
        review (suggestion mode) remain the backstops on the resulting size.

        MA200 gate: when SPY trades below its 200d SMA the final multiplier is
        halved (configurable). This is the slow-grind-bear brake; it has near-
        zero effect in bull markets where SPY stays above MA200.
        """
        # Growth-mode 2026-05-26: extended ladder — 1/2/4/7/10 bands so the
        # raised iv_rank_size_max_multiplier (10) is reachable. Old 1/2/3 ladder
        # capped at 3 regardless of how rich IV got.
        strat_cfg = get_settings().strategy
        if not strat_cfg.iv_rank_sizing_enabled or iv_rank is None:
            base = 1
        elif iv_rank >= 95:
            base = 10
        elif iv_rank >= 85:
            base = 7
        elif iv_rank >= 75:
            base = 4
        elif iv_rank >= strat_cfg.iv_rank_size_mid:   # 50 default
            base = 2
        else:
            base = 1
        # Stagnation booster (2026-05-27, ported from MarsWalk longgrind_sweep).
        # When rolling NLV is flat (< threshold over lookback days), boost the
        # ladder result. Capped by iv_rank_size_max_multiplier — never exceeds
        # the existing hard ceiling.
        # Deep-bear safeguard: suppress when SPY > stagnation_deep_bear_threshold
        # below MA200 — doubling positions into a sustained collapse stacks
        # losses (gfc_2008 backtest evidence).
        if strat_cfg.stagnation_boost_enabled:
            rolling = self._rolling_nlv_return_pct(strat_cfg.stagnation_lookback_days)
            if rolling is not None and rolling < strat_cfg.stagnation_threshold_pct:
                deep_bear = False
                try:
                    regime = self.get_regime()
                    dist = regime.spy_distance_below_ma200
                    if dist is not None and dist > strat_cfg.stagnation_deep_bear_threshold:
                        deep_bear = True
                except Exception:
                    deep_bear = False  # fail-open: data unavailable → allow boost
                if deep_bear:
                    log.info("stagnation_boost_suppressed_deep_bear",
                             rolling_pct=round(rolling, 2),
                             pct_below_ma200=round(dist, 4) if dist else None,
                             threshold=strat_cfg.stagnation_deep_bear_threshold)
                else:
                    base = int(round(base * strat_cfg.stagnation_multiplier))
                    log.info("stagnation_boost_active", rolling_pct=round(rolling, 2),
                             threshold=strat_cfg.stagnation_threshold_pct, base=base)
        capped = max(1, min(base, strat_cfg.iv_rank_size_max_multiplier))
        gated = int(round(capped * self._ma200_size_multiplier()))
        return max(1, gated)

    def _ma200_size_multiplier(self) -> float:
        """Return 0.5 (or configured value) when SPY < MA200, else 1.0.
        Fail-open if MA200 unavailable (e.g. outside US hours, no history yet)."""
        if not self.cfg.bear_market_ma200_enabled:
            return 1.0
        try:
            regime = self.get_regime()
        except Exception:
            return 1.0
        dist = regime.spy_distance_below_ma200
        if dist is None or dist <= 0:
            return 1.0
        log.info("ma200_size_gate_active",
                 distance_below_ma200=round(dist, 4),
                 multiplier=self.cfg.bear_market_size_multiplier)
        return self.cfg.bear_market_size_multiplier

    def is_below_ma200(self, symbol: str) -> bool | None:
        """True iff the symbol is trading below its 200d SMA today.
        Returns None on fail-open (data unavailable / not enough history).

        Daily-cached: first call/sym/day pays the IBKR roundtrip, subsequent
        calls reuse. Cache keyed by date string. Mirrors the MarsWalk per-name
        MA200 gate (engine mode B), which backtests show beats SPY-MA200 across
        all 11 regimes — bear_2022 -49% → -31%, bulls slightly improve.
        """
        if not self.cfg.per_name_ma200_enabled:
            return None
        today_iso = date.today().isoformat()
        cached = self._ma200_cache.get(symbol)
        if cached and cached[0] == today_iso:
            return cached[1]
        stock = self.universe.get_stock(symbol)
        exchange = stock.exchange if stock else "SMART"
        currency = stock.currency if stock else "USD"
        try:
            ma_data = get_stock_ma200(symbol, exchange=exchange, currency=currency)
        except Exception as e:
            log.warning("per_name_ma200_error", symbol=symbol, error=str(e))
            self._ma200_cache[symbol] = (today_iso, None)
            return None
        if not ma_data:
            self._ma200_cache[symbol] = (today_iso, None)
            return None
        is_below = bool(ma_data.get("is_below"))
        self._ma200_cache[symbol] = (today_iso, is_below)
        if is_below:
            log.info("per_name_ma200_below",
                     symbol=symbol,
                     price=ma_data.get("price"),
                     ma200=ma_data.get("ma200"),
                     distance_below=ma_data.get("distance_below_ma200"))
        return is_below

    def _universe_ma200_breadth(self) -> float | None:
        """Fraction of universe currently below MA200. Cached by ISO date so we
        compute once per trading day across all callers. Returns None if not
        enough names report a determinate is_below (fail open at the gate)."""
        today_iso = date.today().isoformat()
        cached = getattr(self, "_ma200_breadth_cache", None)
        if cached and cached[0] == today_iso:
            return cached[1]
        below = 0
        total = 0
        for sym in self.universe.all_symbols:
            v = self.is_below_ma200(sym)
            if v is None:
                continue
            total += 1
            if v:
                below += 1
        if total == 0:
            self._ma200_breadth_cache = (today_iso, None)
            return None
        breadth = below / total
        self._ma200_breadth_cache = (today_iso, breadth)
        log.info("ma200_breadth_computed", below=below, total=total, breadth=round(breadth, 3))
        return breadth

    def ma200_breadth_state(self) -> str:
        """Return 'off' | 'halve' | 'skip' based on universe-wide MA200 breadth.
        See RiskConfig.ma200_breadth_off_threshold / _full_threshold."""
        if not self.cfg.ma200_breadth_gate_enabled:
            return "skip"  # fall back to strict per-name skip behavior
        breadth = self._universe_ma200_breadth()
        if breadth is None:
            return "off"  # fail open
        if breadth >= self.cfg.ma200_breadth_full_threshold:
            return "skip"
        if breadth >= self.cfg.ma200_breadth_off_threshold:
            return "halve"
        return "off"

    def check_per_name_ma200(self, symbol: str) -> RiskCheck:
        """Per-name MA200 gate. Two modes:

        - `ma200_breadth_gate_enabled=True` (default): three-state regime gate
          keyed off universe breadth (% of names below their own MA200).
            * OFF (<30% breadth): write everywhere, ignore individual MA200.
            * HALVE (30-50%): halve contracts on names below their own MA200.
            * SKIP (>=50%): skip entry on names below their own MA200.
        - `ma200_breadth_gate_enabled=False`: legacy strict per-name skip.

        Fail-open on data unavailability."""
        if not self.cfg.per_name_ma200_enabled and not self.cfg.ma200_breadth_gate_enabled:
            return RiskCheck(True)
        state = self.ma200_breadth_state()
        if state == "off":
            return RiskCheck(True)
        below = self.is_below_ma200(symbol)
        if below is not True:
            return RiskCheck(True)
        if state == "halve":
            return RiskCheck(True,
                             f"{symbol} < own MA200 in halve regime — contracts × {self.cfg.ma200_breadth_halve_multiplier}",
                             size_multiplier=self.cfg.ma200_breadth_halve_multiplier)
        # state == "skip"
        return RiskCheck(False, f"{symbol} below its 200d SMA — bear-name gate (skip regime)")

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

    def in_bull_regime(self) -> bool:
        """Confirmed bull: VIX < bull_regime_vix_max AND SPY > MA200.
        Fail-closed on missing data (treat unknown as NOT bull — strategy
        falls back to the broader VIX-tier baseline).

        When True, only one override fires:
          - iv_rank_min -> bull_regime_iv_rank_min (default 50, skip dead-IV)

        Higher-delta and smaller-per-name-cap overrides were tested and
        removed: delta had zero effect (low-VIX chains don't quote 0.30+
        delta at 0-3 DTE), smaller per-name cap actively hurt by diluting
        concentration on the few high-IV movers in narrow-leadership bulls.
        """
        if not self.cfg.bull_regime_enabled:
            return False
        regime = self.get_regime()
        if regime.vix is None or regime.spy_distance_below_ma200 is None:
            return False
        if regime.vix >= self.cfg.bull_regime_vix_max:
            return False
        # spy_distance_below_ma200 > 0 means SPY is BELOW MA200 (per the
        # field's docstring at line 62 of this file).
        if regime.spy_distance_below_ma200 > 0:
            return False
        return True

    def get_dynamic_delta_range(self) -> tuple[float, float]:
        """
        Get the current delta range based on VIX level and SPY trend regime.

        VIX tiers (baseline):
          VIX < 20  → 0.20-0.30 (normal, standard wheel)
          VIX 20-25 → 0.15-0.25 (elevated, step back)
          VIX 25-30 → 0.10-0.20 (high, conservative)

        TREND_BEARISH override (SPY MA10 < MA20):
          Forces minimum high-VIX range regardless of actual VIX.
          If also VIX > 25: forces tightest range (0.08-0.15).
        """
        strat_cfg = get_settings().strategy
        if not strat_cfg.dynamic_delta_enabled:
            return (strat_cfg.delta_min, strat_cfg.delta_max)

        regime = self.get_regime()
        vix = regime.vix
        spy_bearish = regime.spy_bullish is False

        if vix is None:
            return (strat_cfg.delta_min, strat_cfg.delta_max)

        # Spike-aware tier (can escalate beyond raw VIX level)
        tier = self.effective_vix_tier(regime)

        # TREND_BEARISH + high tier: tightest range
        if spy_bearish and tier in ("high", "halt"):
            delta_range = (0.08, 0.15)
            log.debug("dynamic_delta", vix=vix, spike=regime.vix_spike,
                      regime="bearish+high_tier", delta_range=delta_range)

        # TREND_BEARISH: force at least high-VIX range
        elif spy_bearish:
            delta_range = (strat_cfg.delta_vix_high, strat_cfg.delta_vix_high_max)
            log.debug("dynamic_delta", vix=vix, spike=regime.vix_spike,
                      regime="bearish", delta_range=delta_range)

        # Tier-based
        elif tier == "low":
            delta_range = (strat_cfg.delta_vix_low, strat_cfg.delta_vix_low_max)
            log.debug("dynamic_delta", vix=vix, spike=regime.vix_spike,
                      regime="low", delta_range=delta_range)
        elif tier == "mid":
            delta_range = (strat_cfg.delta_vix_mid, strat_cfg.delta_vix_mid_max)
            log.debug("dynamic_delta", vix=vix, spike=regime.vix_spike,
                      regime="mid", delta_range=delta_range)
        else:  # high or halt
            delta_range = (strat_cfg.delta_vix_high, strat_cfg.delta_vix_high_max)
            log.debug("dynamic_delta", vix=vix, spike=regime.vix_spike,
                      regime=tier, delta_range=delta_range)

        # Note: DTE-specific delta widening is handled in the screener per-contract.
        # This method provides the base delta range from VIX level and trend regime.

        return delta_range

    # ── VIX tier helper ─────────────────────────────────────
    def effective_vix_tier(self, regime: MarketRegime) -> str:
        """
        Return current VIX tier accounting for rate-of-change spike.
        Returns one of: "low", "mid", "high", "halt".

        Base tiers from raw VIX:
            < 20 -> low
            20-25 -> mid
            25-30 -> high
            > 30 -> halt

        Spike escalation (never de-escalates):
            spike > vix_spike_bump_2_tiers -> two tiers higher (forces high)
            spike > vix_spike_bump_1_tier  -> one tier higher
        """
        vix = regime.vix
        if vix is None:
            return "low"  # permissive default when no data
        if vix > self.cfg.vix_pause_threshold:
            return "halt"

        # Base tier from raw VIX
        if vix < 20:
            base_tier_idx = 0  # low
        elif vix < 25:
            base_tier_idx = 1  # mid
        else:
            base_tier_idx = 2  # high

        # Spike escalation
        bump = 0
        spike = regime.vix_spike
        if spike is not None and spike > 0:
            if spike > self.cfg.vix_spike_bump_2_tiers:
                bump = 2
            elif spike > self.cfg.vix_spike_bump_1_tier:
                bump = 1

        effective_idx = min(base_tier_idx + bump, 2)  # cap at high

        # MA50 clamp: if SPY is below MA50, don't de-escalate below mid/high
        dist = regime.spy_distance_below_ma50
        if dist is not None and dist > 0:
            if dist > self.cfg.spy_ma50_clamp_high_pct:
                ma50_min_idx = 2  # high
            elif dist > self.cfg.spy_ma50_clamp_mid_pct:
                ma50_min_idx = 1  # mid
            else:
                ma50_min_idx = 0
            if ma50_min_idx > effective_idx:
                log.info("ma50_clamp_applied",
                         raw_tier=["low","mid","high"][effective_idx],
                         clamped_tier=["low","mid","high"][ma50_min_idx],
                         distance_below_ma50=round(dist, 4))
                effective_idx = ma50_min_idx

        return ["low", "mid", "high"][effective_idx]

    # ── Scaling safeguards ─────────────────────────────────
    def check_total_exposure(self) -> RiskCheck:
        """
        Collateral cap is disabled (son-mode). The margin-usage gate
        (max_margin_usage) is now the binding aggregate constraint; this
        method remains as a no-op so legacy callers keep working.
        """
        return RiskCheck(True)

    def _check_total_exposure_legacy(self) -> RiskCheck:
        """Original % NLV cap — retained for reference only (not called)."""
        try:
            summary = get_account_summary()
            net_liq = summary.net_liquidation if summary and summary.net_liquidation > 0 else 0
        except Exception:
            return RiskCheck(True)

        base_pct = self.cfg.total_exposure_pct
        if net_liq >= 4_000_000:
            eff_pct = max(0.40, base_pct)
        elif net_liq >= 2_000_000:
            eff_pct = max(0.30, base_pct)
        else:
            eff_pct = base_pct
        exposure_cap = min(net_liq * eff_pct, self.cfg.max_total_exposure)
        if exposure_cap < 100_000:
            return RiskCheck(True)

        with get_db() as db:
            open_puts = (
                db.query(Position)
                .filter(
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "short_put",
                )
                .all()
            )

        total_exposure = 0.0
        for pos in open_puts:
            try:
                stock = self.universe.get_stock(pos.symbol)
                currency = stock.currency if stock else "USD"
                exchange = stock.exchange if stock else "SMART"
                price = get_stock_price(pos.symbol, exchange=exchange, currency=currency)
                if price and price > 0:
                    total_exposure += price * 100 * pos.quantity
            except Exception:
                continue

        if total_exposure >= exposure_cap:
            return RiskCheck(
                False,
                f"Total open exposure ${total_exposure:,.0f} >= cap ${exposure_cap:,.0f} "
                f"(NLV ${net_liq:,.0f} × {eff_pct:.0%}, "
                f"ceiling ${self.cfg.max_total_exposure:,.0f})",
            )
        return RiskCheck(True)

    def check_daily_deployment(self) -> RiskCheck:
        """
        Block if new collateral deployed today exceeds daily limit.
        Limit = min(NLV × daily_deployment_pct, max_daily_deployment).
        Skipped on small accounts where limit would be below $50K.
        Tracks sum of estimated margin for puts opened today.
        """
        try:
            summary = get_account_summary()
            net_liq = summary.net_liquidation if summary and summary.net_liquidation > 0 else 0
        except Exception:
            return RiskCheck(True)

        daily_limit = min(
            net_liq * self.cfg.daily_deployment_pct,
            self.cfg.max_daily_deployment,
        )
        if daily_limit < 50_000:
            return RiskCheck(True)  # small account — skip

        today_start = datetime.combine(date.today(), datetime.min.time())
        with get_db() as db:
            todays_puts = (
                db.query(Position)
                .filter(
                    Position.opened_at >= today_start,
                    Position.position_type == "short_put",
                )
                .all()
            )

        deployed_today = 0.0
        for pos in todays_puts:
            try:
                stock = self.universe.get_stock(pos.symbol)
                currency = stock.currency if stock else "USD"
                exchange = stock.exchange if stock else "SMART"
                price = get_stock_price(pos.symbol, exchange=exchange, currency=currency)
                if price and price > 0:
                    deployed_today += price * 100 * pos.quantity
            except Exception:
                continue

        if deployed_today >= daily_limit:
            return RiskCheck(
                False,
                f"Daily deployment ${deployed_today:,.0f} >= limit ${daily_limit:,.0f} "
                f"(NLV ${net_liq:,.0f} × {self.cfg.daily_deployment_pct:.0%}, "
                f"ceiling ${self.cfg.max_daily_deployment:,.0f})",
            )
        return RiskCheck(True)

    def check_intraday_loss(self) -> RiskCheck:
        """
        Halt new trades if unrealized loss exceeds the binding threshold.
        Reads total unrealized P&L from all open positions in DB.

        Threshold = max(intraday_loss_halt_pct * NLV, intraday_loss_halt_floor).
        Floor binds at small NLV (effectively dormant); pct binds above the
        crossover NLV (= floor / pct, ~$2M with defaults).
        """
        try:
            summary = get_account_summary()
            net_liq = summary.net_liquidation if summary and summary.net_liquidation > 0 else 0
        except Exception:
            return RiskCheck(True)

        pct_threshold = net_liq * self.cfg.intraday_loss_halt_pct
        halt_threshold = max(pct_threshold, self.cfg.intraday_loss_halt_floor)
        binding = "floor" if halt_threshold == self.cfg.intraday_loss_halt_floor else "pct"

        with get_db() as db:
            open_puts = (
                db.query(Position)
                .filter(
                    Position.status == PositionStatus.OPEN,
                    Position.position_type == "short_put",
                )
                .all()
            )

        total_unrealized = sum(
            (pos.unrealized_pnl or 0) for pos in open_puts
        )

        if total_unrealized <= -halt_threshold:
            return RiskCheck(
                False,
                f"Intraday loss ${abs(total_unrealized):,.0f} exceeds halt threshold "
                f"${halt_threshold:,.0f} (binding={binding}: "
                f"max of {self.cfg.intraday_loss_halt_pct:.1%} of NLV ${net_liq:,.0f} "
                f"or floor ${self.cfg.intraday_loss_halt_floor:,.0f})",
            )
        return RiskCheck(True)

    # ── Daily circuit breaker (ported 2026-05-28 from MarsWalk engine.py:762-768) ──
    # When yesterday's NLV dropped > daily_cb_pct vs day-before, halt new put
    # writes for daily_cb_halt_days trading days. Catches gap-down scenarios
    # that intraday-loss-halt and VIX gates miss. State persists across process
    # restarts via SystemState (daily_cb_halt_remaining + last_check_date).
    # See memory: live-marswalk-parity-rule.

    def _load_daily_cb_state(self) -> tuple[int, str | None]:
        """Return (halt_days_remaining, last_check_date_iso)."""
        try:
            with get_db() as db:
                rows = {
                    s.key: s.value for s in db.query(SystemState).filter(
                        SystemState.key.in_([
                            "daily_cb_halt_remaining", "daily_cb_last_check_date",
                        ])
                    ).all()
                }
            return (
                int(rows.get("daily_cb_halt_remaining") or "0"),
                rows.get("daily_cb_last_check_date") or None,
            )
        except Exception as e:
            log.debug("daily_cb_state_load_failed", error=str(e))
            return (0, None)

    def _save_daily_cb_state(self, halt_remaining: int, last_check_date: str) -> None:
        """Upsert the two daily-CB state keys."""
        try:
            with get_db() as db:
                pairs = {
                    "daily_cb_halt_remaining": str(halt_remaining),
                    "daily_cb_last_check_date": last_check_date,
                }
                for key, value in pairs.items():
                    row = db.query(SystemState).filter(SystemState.key == key).first()
                    if row:
                        row.value = value
                    else:
                        db.add(SystemState(key=key, value=value))
                db.commit()
        except Exception as e:
            log.warning("daily_cb_state_save_failed", error=str(e))

    def check_daily_circuit_breaker(self) -> RiskCheck:
        """Halt new put writes when yesterday's NLV dropped > pct vs day-before.

        Day-level idempotent: evaluates the trigger only once per business day
        (first scan after midnight UTC). Subsequent scans the same day read
        cached halt counter. Each new day decrements the counter by 1.
        Fail-open on insufficient AccountSnapshot history (need ≥2 rows)."""
        if self.cfg.daily_cb_pct <= 0:
            return RiskCheck(True)
        halt_remaining, last_check = self._load_daily_cb_state()
        today = date.today().isoformat()

        # Once-per-day evaluation: only update on a new business day.
        if last_check != today:
            try:
                from src.core.models import AccountSnapshot
                with get_db() as db:
                    rows = (
                        db.query(AccountSnapshot)
                        .order_by(AccountSnapshot.date.desc())
                        .limit(2)
                        .all()
                    )
                if len(rows) >= 2 and rows[0].net_liquidation > 0 and rows[1].net_liquidation > 0:
                    yesterday_nlv = rows[0].net_liquidation
                    day_before_nlv = rows[1].net_liquidation
                    day_change = (yesterday_nlv - day_before_nlv) / day_before_nlv
                    if day_change < -self.cfg.daily_cb_pct and halt_remaining == 0:
                        halt_remaining = self.cfg.daily_cb_halt_days
                        log.warning(
                            "daily_cb_triggered",
                            day_change=round(day_change, 4),
                            threshold=-self.cfg.daily_cb_pct,
                            halt_days=halt_remaining,
                        )
                    elif halt_remaining > 0:
                        halt_remaining -= 1  # decrement on new day
            except Exception as e:
                log.debug("daily_cb_check_failed", error=str(e))
            self._save_daily_cb_state(halt_remaining, today)

        if halt_remaining > 0:
            return RiskCheck(
                False,
                f"Daily circuit breaker active — {halt_remaining} day(s) remaining "
                f"(triggered by >{self.cfg.daily_cb_pct:.0%} NLV drop)",
            )
        return RiskCheck(True)

    def check_cash_carry_gate(self) -> RiskCheck:
        """Block new put entries while the high-vol-grind detector is active
        AND cash-and-carry is enabled. Existing positions settle naturally;
        idle cash gets rotated to the cash-carry ticker by the wheel
        orchestrator (see strategy.wheel.maybe_rotate_cash_carry).

        Precedence: strangle_when_grind takes priority over cash_carry_enabled.
        If BOTH are enabled, this gate is suppressed — the wheel writes puts
        + strangle adds the call leg (sweep showed strangle beats cash-carry).
        """
        if not (self.cfg.cash_carry_enabled and self.cfg.cash_carry_detector_enabled):
            return RiskCheck(True)
        # Precedence: strangle wins. Don't halt the wheel — let strangle fire.
        if self.cfg.strangle_when_grind:
            return RiskCheck(True)
        active, reason = self.evaluate_grind_detector()
        if active:
            return RiskCheck(
                False,
                f"Cash-and-carry mode active — new puts paused. {reason}",
            )
        return RiskCheck(True)

    def check_crash_carry_gate(self) -> RiskCheck:
        """Block new put entries while the crash detector is active AND
        crash_carry_when_active is enabled. Mirror of check_cash_carry_gate
        but with the crash detector (opposite shape: high vol + sharp trend).
        MarsWalk sweep recommended strangle over halt for crashes — this gate
        is opt-in via YAML for users who prefer lower-DD over the strangle
        alpha. Default OFF.

        Precedence: crash_strangle_when_active wins. If BOTH are enabled,
        this gate is suppressed — the wheel writes puts + strangle adds
        the call leg (sweep showed crash-strangle beats crash-carry).
        """
        if not self.cfg.crash_carry_when_active:
            return RiskCheck(True)
        if self.cfg.crash_strangle_when_active:
            return RiskCheck(True)
        active, reason = self.evaluate_crash_detector()
        if active:
            return RiskCheck(
                False,
                f"Crash-carry mode active — new puts paused. {reason}",
            )
        return RiskCheck(True)

    # ── Master check ────────────────────────────────────────
    def can_open_put(self, symbol: str, market: str | None = None) -> RiskCheck:
        """Run all pre-trade checks for selling a put."""
        # Order matters: cheap, definitive blockers first. If position_limit
        # or duplicate_position fires, no IBKR/FMP calls were made → the put_seller
        # scanner's "elapsed > 10s" heuristic correctly classifies risk-blocks
        # as fast (not connection failures). Slow checks (IBKR account state,
        # FMP earnings/IV) run only after the cheap ones pass.
        ma200_check = self.check_per_name_ma200(symbol)
        checks = [
            self.check_position_limit(),       # DB read — instant
            self.check_duplicate_position(symbol),  # DB read — instant
            self.check_daily_limit(),          # DB read — instant
            self.check_vix_gate(),             # cached — instant
            self.check_daily_circuit_breaker(),  # state cached after first call/day
            self.check_cash_carry_gate(),      # cached after first scan/day
            self.check_crash_carry_gate(),     # cached after first scan/day
            self.check_margin_usage(),
            self.check_daily_deployment(),
            self.check_position_size(symbol),
            self.check_total_exposure(),
            self.check_intraday_loss(),
            self.check_sector_exposure(symbol),
            self.check_correlation(symbol),
            self.check_delta_exposure(symbol),
            self.check_buying_power(),
            self.check_earnings(symbol),
            self.check_iv_rank(symbol),
            ma200_check,
        ]
        for check in checks:
            if not check.allowed:
                log.info("risk_blocked", symbol=symbol, reason=check.reason)
                return check

        # SPY MA gate — doesn't block, but signals universe-filter reduction.
        # Compose with the per-name MA200 halve (per-trade contract multiplier
        # from the halve-regime breadth state) — these are independent levers
        # carried on separate fields and applied by the put_seller separately.
        spy_check = self.check_spy_ma_gate(market=market)
        if spy_check.reduce_pct < 1.0 or ma200_check.size_multiplier < 1.0:
            reasons = []
            if spy_check.reduce_pct < 1.0:
                reasons.append(spy_check.reason)
            if ma200_check.size_multiplier < 1.0:
                reasons.append(ma200_check.reason)
            return RiskCheck(True,
                             reason=" + ".join(reasons),
                             reduce_pct=spy_check.reduce_pct,
                             size_multiplier=ma200_check.size_multiplier)

        return RiskCheck(True)

    def increment_daily_count(self) -> None:
        """Call after a successful trade to update the daily counter."""
        if self._daily_count is not None:
            self._daily_count += 1
