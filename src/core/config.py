"""
Configuration loader — merges settings.yaml + .env + env vars.
"""
from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, Field


_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


# ── Sub-models ──────────────────────────────────────────────
class IBKRConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 4001
    client_id: int = 12
    timeout: int = 30
    readonly: bool = False
    account: str = ""
    # Flex Web Service creds for the options account — stored for a future
    # options-side deposit/dividend feature; no consumer wired yet.
    flex_token: str = ""
    flex_query_id: str = ""


class DteTierLow(BaseModel):
    vix_max: float = 20
    dte_min_usd: int = 0
    dte_max_usd: int = 3
    dte_min_other: int = 0
    dte_max_other: int = 7

class DteTierMid(BaseModel):
    vix_max: float = 30
    dte_min_usd: int = 7
    dte_max_usd: int = 14
    dte_min_other: int = 7
    dte_max_other: int = 14

class DteTiers(BaseModel):
    low_vix: DteTierLow = DteTierLow()
    mid_vix: DteTierMid = DteTierMid()
    high_vix: str = "halt"

class StrategyConfig(BaseModel):
    dte_tiers: DteTiers = DteTiers()
    delta_min: float = 0.20
    delta_max: float = 0.30
    contracts_per_stock: int = 2     # growth-mode 2026-05-26: double base size
    min_premium: float = 0.20          # call minimum ($0.20 for covered calls)
    min_premium_put: float = 0.50       # put minimum ($0.50 for puts)
    min_open_interest: int = 10
    min_bid: float = 0.05
    min_net_premium_multiplier: float = 5.0
    min_stock_price: dict = {
        "USD": 5.0, "AUD": 2.0, "GBP": 2.0, "EUR": 2.0,
        "CHF": 5.0, "JPY": 500.0, "NOK": 20.0, "DKK": 15.0, "CAD": 2.0,
    }
    # IV Rank
    iv_rank_enabled: bool = True
    # 2026-05-27: reverted from 0 → 20 after honest review. The growth-mode
    # premise ("write everything, capital must deploy") was contradicted by
    # the same session's bull-regime IV-rank floor of 50 (skip dead-IV names).
    # Low-IV-rank writes have structurally bad premium-to-risk ratio: collect
    # pennies, take full assignment loss in a drawdown. Bull-regime floor of 50
    # remains the layered defense for confirmed bulls.
    iv_rank_min: int = 20
    iv_lookback_days: int = 252
    # Dynamic delta
    dynamic_delta_enabled: bool = True
    delta_vix_low: float = 0.25
    delta_vix_low_max: float = 0.30
    delta_vix_mid: float = 0.18
    delta_vix_mid_max: float = 0.22
    delta_vix_high: float = 0.12
    delta_vix_high_max: float = 0.18
    # Profit taking / rolling
    profit_take_enabled: bool = True
    profit_take_pct: float = 0.55
    roll_enabled: bool = True
    # Earnings avoidance
    earnings_avoid_enabled: bool = True
    earnings_avoid_days: int = 3
    # Wheel
    wheel_enabled: bool = True
    cc_dte_min: int = 1
    cc_dte_max: int = 7
    cc_delta_min: float = 0.35
    cc_delta_max: float = 0.45
    cc_above_cost_basis: bool = True
    cc_progressive_strikes: bool = True
    # Hedge
    hedge_enabled: bool = True
    hedge_otm_pct: float = 0.05
    hedge_dte_target: int = 30
    hedge_roll_dte: int = 7
    hedge_budget_pct: float = 0.04
    hedge_contracts: int = 1

    # ── Cash-machine upgrades (changes #1–#4) ──────────────────
    # #1 Exit-velocity: on assigned stock, prefer a deep-ITM covered call that is
    # near-certain to be called away next expiry → fastest return to cash. Falls
    # back to the normal exit-mode delta band when no deep-ITM strike exists at or
    # above breakeven. Revert: wheel_exit_velocity_enabled = false.
    wheel_exit_velocity_enabled: bool = True
    wheel_exit_velocity_delta_min: float = 0.80
    wheel_exit_velocity_delta_max: float = 0.95
    # #3 IV-rank-scaled sizing: sell more contracts when premium is rich.
    # multiplier = 1x (<mid), 2x (>=mid), 3x (>=high), hard-capped by max_multiplier.
    # Existing per-position $ cap + whatif-margin (live) and human review
    # (suggestion mode) remain the backstops. Revert: iv_rank_sizing_enabled = false.
    iv_rank_sizing_enabled: bool = True
    iv_rank_size_mid: int = 50
    iv_rank_size_high: int = 70
    # Growth-mode 2026-05-26: cap raised 5 → 10 to let the IV-rank ladder
    # (1/2/4/7/10 bands in risk.iv_rank_size_multiplier) scale through.
    iv_rank_size_max_multiplier: int = 10
    # Stagnation booster (2026-05-27, ported from MarsWalk longgrind_sweep).
    # When rolling NLV return is flat across stagnation_lookback_days, multiply
    # the IV-rank ladder result by stagnation_multiplier (capped by
    # iv_rank_size_max_multiplier). Lifts ai_crash +3.11pp, oil_crash +1.67pp,
    # bull_2021 +1.81pp in MarsWalk; no regime hurt below -2pp.
    stagnation_boost_enabled: bool = True
    stagnation_lookback_days: int = 60
    stagnation_threshold_pct: float = 1.0
    stagnation_multiplier: float = 2.0
    # Deep-bear safeguard for stagnation booster (2026-05-27): suppress booster
    # when SPY is more than this fraction below MA200. Doubling positions into
    # a sustained collapse stacks losses (gfc_2008 backtest: -1.35pp penalty
    # when unguarded). gfc-class regimes (SPY 30-40% below MA200) → suppressed.
    # debt_2011 / bear_2022 (~15% below) → boost still fires normally.
    stagnation_deep_bear_threshold: float = 0.15
    # #4 Weekend/holiday theta capture: small additive score bonus for contracts
    # that span non-trading (weekend) days — rewards capturing decay without
    # market exposure. weight 0 disables. Revert: weekend_theta_enabled = false.
    weekend_theta_enabled: bool = True
    weekend_theta_weight: float = 0.10
    # #2 Roll tested ~0DTE puts down-and-out instead of taking assignment.
    # DEFAULT OFF — this conflicts with the current explicit design where
    # profit_taker lets DTE<=3 puts expire/assign (profit_taker.py:114). Enable
    # only deliberately. Requires net credit; capped rolls/symbol/day.
    roll_tested_puts_enabled: bool = False
    roll_tested_dte_max: int = 1           # only roll when DTE <= this
    roll_tested_itm_buffer: float = 0.0    # roll when underlying < strike - buffer
    roll_tested_max_per_day: int = 1       # max rolls per symbol per day
    roll_tested_min_credit: float = 0.0    # require net credit >= this (per share)


class RiskConfig(BaseModel):
    vix_pause_threshold: float = 35.0    # growth-mode 2026-05-26: keep capacity through fat-IV days, halt only on panic spikes
    max_portfolio_positions: int = 50
    max_daily_positions: int = 10          # base daily limit (for first 100K)
    max_daily_positions_cap: int = 25      # hard cap regardless of portfolio size
    daily_position_step: float = 100000.0  # +1 trade per this amount above 100K
    max_sector_pct: float = 0.30
    max_single_stock_pct: float = 0.05
    max_buying_power_usage: float = 0.80     # growth-mode 2026-05-26
    max_margin_usage: float = 0.80           # growth-mode 2026-05-26: top 33% of margin capacity reclaimed (was 60%)
    min_cash_reserve: float = 10000.0
    # Scaling safeguards ($5M+)
    position_dollar_pct: float = 0.05        # per-position cap as % of NLV (son-mode)
    max_position_dollars: float = 500000.0   # hard ceiling per position
    min_position_dollars: float = 25000.0    # floor — small accounts unaffected below this
    total_exposure_pct: float = 0.20         # total open collateral cap as % of NLV
    max_total_exposure: float = 20000000.0   # hard ceiling — margin-cap is now the binding constraint
    # Aggregate commitment cap (re-enabled 2026-06-08). Bounds total equity commitment
    # — open short-put assignment liability PLUS already-assigned stock — as a multiple
    # of NLV, scaled by adaptive_commitment_multiple(). Prevents the correlated-assignment
    # cash-lockout (maintenance-margin gate alone admits ~3-5x notional). Mirrored in
    # MarsWalk _commitment_multiple(). cash_reserve_pct gates check_buying_power on
    # excess liquidity, not raw cash. max_single_name_notional_pct caps any single
    # underlying's assignment notional as % of NLV (steers small accounts off lumpy
    # high-priced names; non-binding at large NLV).
    cash_reserve_pct: float = 0.15           # reserve floor = max(min_cash_reserve, this * NLV), tested vs excess liquidity
    max_single_name_notional_pct: float = 0.25  # per-name assignment notional cap as % of NLV
    daily_deployment_pct: float = 0.03       # max new collateral per day as % of NLV
    max_daily_deployment: float = 500000.0   # hard ceiling new collateral per day
    intraday_loss_halt_pct: float = 0.025    # halt if unrealized loss > 2.5% of NLV (Option C: max of pct or floor)
    intraday_loss_halt_floor: float = 50000.0  # absolute $ floor; halt = max(pct * NLV, floor)
    # Correlation gate (skip if NLV < 50K or fewer than 3 open positions)
    max_correlation: float = 0.85        # block if avg pairwise correlation > this
    correlation_nlv_threshold: float = 50000.0
    correlation_lookback_days: int = 60
    # Delta exposure gate (skip if NLV < 50K)
    max_portfolio_delta: float = 500.0   # total abs delta units across all open puts
    delta_nlv_threshold: float = 50000.0
    # SPY MA gate
    spy_ma_enabled: bool = True
    spy_ma_fast: int = 10
    spy_ma_slow: int = 20
    spy_bearish_reduction: float = 0.50
    # VIX rate-of-change (spike) escalation
    vix_spike_bump_1_tier: float = 4.0   # spike > this -> treat VIX as one tier higher
    vix_spike_bump_2_tiers: float = 6.0  # spike > this -> treat as two tiers higher
    # SPY MA50 regime clamp (prevents de-escalation while trend is broken)
    spy_ma50_clamp_mid_pct: float = 0.0     # SPY below MA50 by this -> clamp tier >= mid
    spy_ma50_clamp_high_pct: float = 0.03   # SPY below MA50 by 3%+ -> clamp tier >= high
    # SPY MA200 bear-market size gate — halves per-trade contracts when SPY trades
    # below its 200d SMA. Targets slow-grind bears (e.g. 2022) that the MA10/MA20
    # candidate-halver doesn't dampen enough on its own. Triggers very rarely in
    # bull markets (SPY > MA200 ~99% of bull days) so calm regimes are untouched.
    bear_market_ma200_enabled: bool = True
    bear_market_size_multiplier: float = 0.5
    # Per-name MA200 gate — skip writing puts on any symbol trading below its OWN
    # 200d SMA, even if SPY is fine. MarsWalk backtests across 11 regimes show this
    # beats SPY-MA200: bear_2022 -49% → -31%, bulls slightly improve too (skips
    # individually-broken names that would have assigned). Daily-cached per
    # symbol (1 IBKR call/sym/day). Fails open if data unavailable.
    per_name_ma200_enabled: bool = False   # growth-mode 2026-05-26: rely on cost-basis averaging + margin cap, not skip-on-MA200
    # Breadth-gradual MA200 gate — count universe symbols below their own MA200,
    # then switch the per-name gate by regime breadth: <off% → OFF (write
    # everywhere, ignore individual MA200), [off%, full%) → HALVE contracts on
    # names below their own MA200, ≥full% → SKIP entirely. Lets fat premium in
    # corrections through (cost-basis averaging works) while still stepping
    # aside in real bear regimes. Set ma200_breadth_gate_enabled=False to fall
    # back to the strict per_name_ma200 skip-everywhere behavior.
    ma200_breadth_gate_enabled: bool = False    # growth-mode 2026-05-26: off — accept full deployment in bears too
    ma200_breadth_off_threshold: float = 0.30
    ma200_breadth_full_threshold: float = 0.50
    ma200_breadth_halve_multiplier: float = 0.5
    # Bull-regime adaptive overrides (2026-05-26). When VIX < bull_regime_vix_max
    # AND SPY > MA200 (confirmed bull), three settings flip to fight the
    # bull-regime yield ceiling (premium per trade is structurally tiny in
    # low-IV uptrends). Outside the bull window, baseline values apply.
    # Empirical 2026-05-26 marswalk sweep tested three proposed bull
    # adaptations; only the IV-rank floor improves bull returns. Higher delta
    # had zero effect (low-VIX chains don't quote 0.30+ delta within 0-3 DTE).
    # Smaller per-name cap actively hurt (narrow-leadership bulls want
    # concentration on the few movers, not dilution across 47 names). Both
    # rejected; only the IV-rank floor remains.
    bull_regime_enabled: bool = True
    # Lowered 2026-05-26 from 18 -> 16 after empirical sweep showed vix_max=18
    # caused the detector to fire on some iran_war days (VIX briefly dipped
    # below 18 mid-regime), regressing iran_war from +64 -> +57 %/yr. At
    # vix_max=16 the detector cleanly distinguishes bulls from war/bear regimes.
    bull_regime_vix_max: float = 16.0
    bull_regime_iv_rank_min: float = 50.0     # write only on names with IV rank >= 50 -> skip dead-IV consumer staples
    # DTE extension in bulls — 0-7 instead of the cash-machine 0-3. Phase 2
    # sweep (2026-05-26): adds +3 pp/yr on bull_2021, +24 pp/yr on grind_2024h1,
    # +3.8 pp/yr on ai_2023, zero impact on non-bull regimes. The 4-7 DTE band
    # captures meaningful theta in low-IV bulls where 0-3 DTE quotes pennies.
    bull_regime_dte_min: int = 0
    bull_regime_dte_max: int = 7
    # Drawdown-based daily position sizing (scales max_daily_positions)
    drawdown_lookback_days: int = 5
    drawdown_threshold_light: float = 0.02   # drawdown > this -> 75% of base cap
    drawdown_threshold_mid: float = 0.05     # drawdown > this -> 50% of base cap
    drawdown_threshold_severe: float = 0.10  # drawdown > this -> 25% of base cap
    drawdown_min_cap: int = 2                # floor - never scale below this many trades/day
    # Parallel 20-day drawdown window — catches slow-grind bears (like bear_2022)
    # that the 5d window misses. Final multiplier = min(5d_mult, 20d_mult).
    # Set to 0 to disable. Ported 2026-05-28 from MarsWalk Params.drawdown_long_*.
    # See memory: live-marswalk-parity-rule.
    drawdown_long_lookback_days: int = 20
    drawdown_long_threshold_light: float = 0.03    # 20d dd > 3% -> 75% cap
    drawdown_long_threshold_mid: float = 0.06      # 20d dd > 6% -> 50% cap
    drawdown_long_threshold_severe: float = 0.12   # 20d dd > 12% -> 25% cap
    # Daily circuit breaker — when yesterday's NLV dropped > pct vs day-before,
    # halt all new put writes for halt_days trading days. Catches gap-down
    # scenarios that intraday-loss-halt and VIX gates miss. Ported 2026-05-28
    # from MarsWalk Params.daily_cb_pct / daily_cb_halt_days.
    daily_cb_pct: float = 0.05               # NLV drop > 5% day-over-day -> halt
    daily_cb_halt_days: int = 5              # halt persists for N trading days
    # Wheel exit-ASAP mode (for stocks received via put assignment — prioritize exit over premium)
    wheel_exit_mode_enabled: bool = True          # master switch — when True, new wheel assignments flag as exit mode
    wheel_exit_delta_min: float = 0.35            # closer-to-money than normal 0.30
    wheel_exit_delta_max: float = 0.55            # accepts deeper ITM than normal 0.45
    wheel_exit_margin_rate_annual: float = 0.07   # margin interest rate for surcharge in min_strike
    wheel_cc_profit_threshold: float = 0.80       # close CC when (entry - ask)/entry >= this (80% profit)
    wheel_sell_fee_per_share: float = 0.04        # per-share sell commission used in pre-market exit threshold
    # ── Regime-specific covered calls (2026-06-23) ──────────────────────────
    # DEFAULT = VELOCITY-ALWAYS in every regime: attempt the deep-ITM exit-velocity
    # call (wheel_exit_velocity_delta_*) on EVERY assigned lot (not just below-MA200
    # names) so it is called away in days, and relax the breakeven floor by
    # cc_exit_loss_tolerance_pct so a lot a hair underwater still clears (accept a
    # SMALL loss to recycle capital into the put engine). Replaces the old
    # below-MA200 distressed-exit + interest-surcharge branch.
    #
    # CRASH-BOLSTER (cc_crash_bolster_enabled, default OFF): when the crash detector
    # fires, skip exit-velocity, re-impose the strict net-cost-basis floor, and write
    # defensive patient OTM CCs (cc_crash_delta_*) out to cc_crash_dte_max.
    # REJECTED by the MarsWalk A/B (data/cc_regime_sweep_ab_2026*): every bolster
    # variant — defensive AND aggressive-dump — LOST to velocity-everywhere across
    # all 7 crash regimes (negative CRASH-sum, max-DD unchanged). The CC branch is
    # not a crash lever (crash P&L is dominated by held-stock notional; holding
    # longer forgoes the recycling velocity captures). Crash defense lives on the
    # put-entry side (crash detector → strangle/halt) + hedge module. Flag retained
    # OFF for future experimentation. Mirrored in MarsWalk engine + dashboard chip.
    cc_velocity_always: bool = True               # deep-ITM exit-velocity on ALL assigned lots, every regime
    cc_exit_loss_tolerance_pct: float = 0.02      # allow strike down to net_basis*(1-tol) for a fast small-loss exit
    cc_rescue_threshold: float = 0.97             # spot < cost_basis*thr → rescue (deep-OTM cushion) vs velocity dump; 0.97 swept-optimal (was 0.95)
    cc_crash_bolster_enabled: bool = False        # OFF — sweep-rejected; True = use bolster branch when crash detector fires
    cc_crash_dte_max: int = 21                    # bolster: longer CC DTE to ride out the move
    cc_crash_delta_min: float = 0.15              # bolster: defensive patient OTM band floor
    cc_crash_delta_max: float = 0.30              # bolster: defensive patient OTM band ceiling
    # ── Cash-and-carry mode (high-vol-grind detector + SGOV rotation) ──
    # Ported 2026-05-28 from MarsWalk after the high-vol-grind detector + parameter-
    # override experiment (memory: stagflation-strategy-attempted-2026-05-28) showed
    # the WHEEL itself can't beat T-bills in stagflation-class regimes. Cash-and-carry
    # halts new put writes when the detector fires and rotates idle cash into a
    # short-Treasury ETF (default SGOV) so it earns the prevailing yield. When the
    # detector clears, sells the ETF and resumes the wheel.
    # All flags default OFF so existing live behavior is unchanged until opt-in.
    cash_carry_enabled: bool = False              # master switch for the whole feature
    cash_carry_detector_enabled: bool = False     # compute the detector signal each scan
    cash_carry_ticker: str = "SGOV"               # treasury-ETF symbol used for cash rotation
    cash_carry_realized_vol_threshold: float = 0.25   # universe-median 60d rv (annualized) > this
    cash_carry_trend_window_days: int = 180           # SPY trailing-return window
    cash_carry_trend_max_abs_pct: float = 20.0        # |SPY trailing return| < this
    cash_carry_detect_window_days: int = 60           # realized-vol lookback per symbol
    cash_carry_on_days_required: int = 15             # consecutive raw-True days to flip ON
    cash_carry_off_days_required: int = 5             # consecutive raw-False days to flip OFF
    cash_carry_min_cash_buffer: float = 25_000.0      # don't tie up cash below this in SGOV
    # ── Strangle mode (mirror of MarsWalk Params.strangle_when_grind) ──
    # When True AND the high-vol-grind detector is active, sell a symmetric-
    # delta call alongside each put (naked short call). Mutually exclusive
    # with cash_carry action — they share the hvg detector. Head-to-head on
    # stagflation_70s: strangle (+88.28%/2.85%DD) beats cash-carry
    # (+85.13%/1.68%DD) by +3.15pp triggered, +9.61pp always-on. Default OFF.
    # Naked short calls require IBKR portfolio margin AND a daily check that
    # closes ITM calls before expiry (see scheduler/jobs.py).
    strangle_when_grind: bool = False
    strangle_call_delta_min: float = 0.15         # symmetric to put delta band
    strangle_call_delta_max: float = 0.30
    strangle_itm_close_dte: int = 1               # buy-to-close naked calls when ITM and DTE <= this
    # ── Crash detector (mirror of MarsWalk Params.crash_*) ──
    # Opposite shape from hvg detector: high vol AND SHARP trend (|60d| > 15%
    # either direction). Designed for Lehman-class regimes (gfc_2008, etc).
    # MW sweep showed strangle is the right action (NOT cash-carry — surprising
    # finding, see live-marswalk-parity-rule memo). Both actions kept available
    # via separate flags; user picks via YAML.
    crash_when_active_enabled: bool = False
    crash_realized_vol_threshold: float = 0.40
    crash_trend_abs_pct: float = 15.0
    crash_detect_window_days: int = 60
    crash_on_days_required: int = 5
    crash_off_days_required: int = 10
    crash_carry_when_active: bool = False         # halt + hold cash (opt-in, lower-DD)
    crash_strangle_when_active: bool = False      # sell strangles (recommended per sweep)


class ScheduleConfig(BaseModel):
    market_open: str = "09:30"
    market_close: str = "16:00"
    timezone: str = "US/Eastern"
    scan_interval_minutes: int = 30
    position_check_minutes: int = 5
    enabled_markets: list[str] = []  # empty = all markets; set ["SMART"] to limit to US only


class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False


class AppConfig(BaseModel):
    name: str = "Options Trader"
    mode: Literal["paper", "live"] = "paper"
    suggestion_mode: bool = False    # true = suggest trades with Approve/Reject
    log_level: str = "INFO"
    db_path: str = "data/trades.db"
    # Option trader's own ledger DB (positions + trades). Routed there via SQLAlchemy
    # binds so the option trader's books are physically separate from the portfolio's,
    # even though both run in one process sharing the overview dashboard. Everything
    # else (portfolio tables, system_state, account_snapshots, suggestions, earnings,
    # ipo_watchlist) stays in db_path.
    options_db_path: str = "data/options.db"
    bruno_run_integrations: bool = False  # gated: True on MesiCap clone, False on Rain dev


# ── Portfolio config ─────────────────────────────────────────
from src.portfolio.config import PortfolioConfig


# ── Root config ─────────────────────────────────────────────
class Settings(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    app: AppConfig = AppConfig()
    ibkr: IBKRConfig = IBKRConfig()
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig = RiskConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    web: WebConfig = WebConfig()
    portfolio: PortfolioConfig = PortfolioConfig()
    raw: dict = {}  # full YAML dict for sections not modeled (alerts, bridge)


def _load_yaml(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _apply_env_overrides(settings: Settings) -> Settings:
    """Override selected settings from environment variables."""
    env = os.environ
    if v := env.get("IBKR_HOST"):
        settings.ibkr.host = v
    if v := env.get("IBKR_PORT"):
        settings.ibkr.port = int(v)
    if v := env.get("IBKR_CLIENT_ID"):
        settings.ibkr.client_id = int(v)
    if v := env.get("IBKR_ACCOUNT"):
        settings.ibkr.account = v
    if v := env.get("IBKR_FLEX_TOKEN"):
        settings.ibkr.flex_token = v
    if v := env.get("IBKR_FLEX_QUERY_ID"):
        settings.ibkr.flex_query_id = v
    if v := env.get("TRADING_MODE"):
        settings.app.mode = v  # type: ignore[assignment]
    if v := env.get("WEB_HOST"):
        settings.web.host = v
    if v := env.get("WEB_PORT"):
        settings.web.port = int(v)
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and cache application settings."""
    # Load .env if present
    env_file = _CONFIG_DIR / ".env"
    if env_file.exists():
        from dotenv import load_dotenv
        load_dotenv(env_file)

    raw = _load_yaml(_CONFIG_DIR / "settings.yaml")
    settings = Settings(**raw, raw=raw)
    settings = _apply_env_overrides(settings)
    return settings


# ── Watchlist loader ────────────────────────────────────────
class StockEntry(BaseModel):
    symbol: str
    name: str
    sector: str
    exchange: str = "SMART"
    currency: str = "USD"
    primary_exchange: str | None = None
    options_exchange: str | None = None    # derivatives exchange (DTB, ICEEU, OSE, etc.)
    contract_size: int = 100          # options multiplier (100 for US/EU, varies for Japan)
    div_yield: float | None = None
    category: Literal["growth", "dividend"] = "growth"

    @property
    def opt_exchange(self) -> str:
        """Return the options exchange — falls back to stock exchange if not set."""
        return self.options_exchange or self.exchange


@lru_cache(maxsize=1)
def get_watchlist() -> list[StockEntry]:
    """Load the portfolio watchlist (watchlist.yaml)."""
    raw = _load_yaml(_CONFIG_DIR / "watchlist.yaml")
    stocks: list[StockEntry] = []
    for item in raw.get("growth", []):
        item["symbol"] = str(item["symbol"])
        stocks.append(StockEntry(**item, category="growth"))
    for item in raw.get("dividend", []):
        item["symbol"] = str(item["symbol"])
        stocks.append(StockEntry(**item, category="dividend"))
    return stocks


@lru_cache(maxsize=1)
def get_options_universe() -> list[StockEntry]:
    """
    Load the options trading universe (options_universe.yaml).
    This is the top 50 stocks ranked by options_score (fundamentals + liquidity).
    Generated monthly by the screener job running on portfolio port 7496.
    Falls back to watchlist.yaml if options_universe.yaml does not exist yet.
    """
    options_path = _CONFIG_DIR / "options_universe.yaml"
    if not options_path.exists():
        import logging
        logging.getLogger(__name__).warning(
            "options_universe.yaml not found — falling back to watchlist.yaml. "
            "Run the monthly screener to generate it."
        )
        return get_watchlist()

    raw = _load_yaml(options_path)
    stocks: list[StockEntry] = []
    for item in raw.get("stocks", []):
        item["symbol"] = str(item["symbol"])
        # tier field in options_universe maps to category for StockEntry
        tier = item.pop("tier", "growth")
        category = "dividend" if tier == "dividend" else "growth"
        # Remove options-specific fields not in StockEntry
        for key in ["options_score", "options_liquidity", "rank"]:
            item.pop(key, None)
        try:
            stocks.append(StockEntry(**item, category=category))
        except Exception:
            pass
    return stocks
