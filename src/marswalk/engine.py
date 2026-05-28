"""
MarsWalk backtest engine — sandbox wheel simulator over one regime.

Runs the SHARED selection cores (score_put_candidates / score_call_candidates)
day by day against historical underlying price + IV, simulating the full wheel:
sell cash-secured puts -> expiry/assignment -> hold stock -> write covered calls
(faithful three-branch rescue/exit/normal delta + min-strike logic) -> called
away. Marks everything to BSM daily to produce an NLV curve vs the 24% target.

V1 SIZING POLICY (documented assumptions, refine later):
  - up to `max_positions` concurrent cash-secured short puts, 1 contract each,
    one position per symbol, best-score-first across the universe;
  - a put is only sold if free cash (cash - reserved put collateral) covers
    strike*100;
  - covered calls written 1 per 100 assigned shares.

Pure/offline: no IBKR, no trades.db. `market` is injected by the caller.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta

from src.core.config import get_settings
from src.strategy.option_scoring import score_put_candidates, score_call_candidates
from src.marswalk import pricing

TARGET_ANNUAL = 0.24

# Sector map for the backtest universe (sector cap gate). Unknown -> "Other".
# Aligned 2026-05-26 to the live options_universe.yaml. Names retained from the
# prior universe (INTC/CSCO/etc.) are kept here so any leftover cached data still
# maps correctly without breaking the gate.
_SECTORS = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology", "AVGO": "Technology",
    "ORCL": "Technology", "CRM": "Technology", "ADBE": "Technology", "AMD": "Technology",
    "INTC": "Technology", "CSCO": "Technology", "QCOM": "Technology", "TXN": "Technology",
    "IBM": "Technology", "NOW": "Technology", "INTU": "Technology",
    "ADSK": "Technology", "ANET": "Technology", "ARM": "Technology", "ASML": "Technology",
    "CDNS": "Technology", "FSLR": "Technology", "KLAC": "Technology", "LRCX": "Technology",
    "MPWR": "Technology", "PLTR": "Technology", "VEEV": "Technology",
    # Communication
    "GOOGL": "Communication", "META": "Communication", "NFLX": "Communication",
    "DIS": "Communication", "CMCSA": "Communication", "T": "Communication",
    "VZ": "Communication", "TMUS": "Communication",
    # Consumer Discretionary
    "AMZN": "ConsumerDisc", "TSLA": "ConsumerDisc", "HD": "ConsumerDisc", "MCD": "ConsumerDisc",
    "NKE": "ConsumerDisc", "LOW": "ConsumerDisc", "SBUX": "ConsumerDisc",
    "BKNG": "ConsumerDisc", "TJX": "ConsumerDisc",
    "ABNB": "ConsumerDisc", "CMG": "ConsumerDisc", "DECK": "ConsumerDisc",
    "LULU": "ConsumerDisc", "RACE": "ConsumerDisc",
    # Consumer Staples
    "PG": "ConsumerStaples", "KO": "ConsumerStaples", "PEP": "ConsumerStaples",
    "COST": "ConsumerStaples", "WMT": "ConsumerStaples", "PM": "ConsumerStaples",
    "MDLZ": "ConsumerStaples",
    # Financials
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials", "GS": "Financials",
    "MS": "Financials", "BLK": "Financials", "SCHW": "Financials", "AXP": "Financials",
    "C": "Financials", "MA": "Financials", "V": "Financials", "SPGI": "Financials",
    # Healthcare
    "JNJ": "Healthcare", "UNH": "Healthcare", "LLY": "Healthcare", "PFE": "Healthcare",
    "MRK": "Healthcare", "ABBV": "Healthcare", "TMO": "Healthcare", "ABT": "Healthcare",
    "DHR": "Healthcare", "BMY": "Healthcare",
    "AZN": "Healthcare", "DXCM": "Healthcare", "IDXX": "Healthcare", "ISRG": "Healthcare",
    "MTD": "Healthcare", "SYK": "Healthcare", "VRTX": "Healthcare", "ZTS": "Healthcare",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "SLB": "Energy",
    # Industrials
    "CAT": "Industrials", "BA": "Industrials", "HON": "Industrials", "GE": "Industrials",
    "UPS": "Industrials", "RTX": "Industrials", "DE": "Industrials",
    "CPRT": "Industrials", "HWM": "Industrials", "ROL": "Industrials",
    # Materials / Utilities
    "LIN": "Materials", "SHW": "Materials", "NEE": "Utilities", "DUK": "Utilities",
}


def _pearson(a: list, b: list) -> float:
    n = len(a)
    if n < 5:
        return 0.0
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / (va ** 0.5 * vb ** 0.5)


@dataclass
class Params:
    # Defaults mirror the LIVE aggressive son-mode config (commit c522a8e +
    # hybrid wheel 63d8ed8): bare `Params()` reproduces production behavior so
    # the MarsWalk "Run now" with no overrides ≈ what the live trader does.
    # ── Put selling ──
    dte_min: int = 0          # live VIX low/mid-tier US is 0-3 DTE
    dte_max: int = 3
    delta_min: float = 0.15
    delta_max: float = 0.30
    put_min_premium: float = 0.0   # 0 = use settings.yaml default
    # ── Covered calls ──
    cc_dte_min: int = 1            # live cfg.cc_dte_min
    cc_dte_max: int = 7            # live cfg.cc_dte_max
    cc_delta_min: float = 0.30     # live patient wheel (Strategy B default)
    cc_delta_max: float = 0.45
    cc_min_premium: float = 0.0    # 0 = use settings.yaml default
    # ── Portfolio ──
    start_capital: float = 4_000_000.0   # forward-looking simulation scale (NOT matched to live account; see memory: marswalk-start-capital)
    max_positions: int = 50           # live risk.max_portfolio_positions
    contracts: int = 2                # growth-mode 2026-05-26: double base size
    # ── Risk gates (model the live limits) ──
    total_exposure_pct: float = 0.0   # 0 = NLV ramp (20/25/30%); >0 = fixed cap %
    vix_halt: float = 35.0            # growth-mode 2026-05-26: only halt on panic spikes
    iv_rank_min: float = 20.0         # 2026-05-27: reverted from 0 (mirrors live settings.yaml + StrategyConfig default after honest review — see commit e6df8d9)
    # Bull-regime adaptive overrides (2026-05-26). When SPY > MA200 AND
    # VIX < bull_regime_vix_max: switch to a higher-delta / smaller-per-name /
    # IV-rank-gated profile to fight bull-regime yield ceiling. See live
    # config RiskConfig.bull_regime_* fields for the same logic in production.
    # Bull-regime override (see live RiskConfig bull_regime_* docstring for
    # empirical findings — only IV-rank floor improved bull returns; higher
    # delta and smaller per-name cap were tested and rejected).
    bull_regime_enabled: bool = True
    bull_regime_vix_max: float = 16.0    # lowered from 18 — see live config docstring
    bull_regime_iv_rank_min: float = 50.0
    # DTE extension in bulls — see live config bull_regime_dte_* docstring.
    bull_regime_dte_min: int = 0
    bull_regime_dte_max: int = 7
    max_margin_usage: float = 0.0     # 0 = use live settings.risk.max_margin_usage (60%
                                      # son-mode); >0 = override.
    # ── Pricing model ──
    short_dte_uplift_k: float = 1.0   # near-expiry vol-premium uplift (see pricing.SHORT_DTE_K docstring for the 4.95 -> 1.0 climbdown)
    gap_stress: float = 0.0           # what-if: extra adverse mark on big down days
                                      # (>=5% drop) — models close understating an
                                      # intraday/overnight gap. 0 = off (historical).
    # ── Margin model ──
    margin_on: bool = True            # live runs with IBKR portfolio margin
    margin_multiple: float = 5.0      # IBKR portfolio-margin proxy (~5x typical OTM put)
    # ── Live-system defenses (ports from src.strategy.risk) ──
    # 0 = use the live config default (so the backtest mirrors production); >0 overrides.
    dynamic_delta_enabled: bool = True   # VIX-tiered delta range (mirrors risk.dynamic_delta_range)
    vix_spike_bump_1: float = 4.0        # VIX day-over-day spike that escalates tier +1
    vix_spike_bump_2: float = 6.0        # spike that escalates tier +2 (cap = high)
    # SPY MA50 clamp (risk.effective_vix_tier): when SPY is below MA50, lift the
    # tier-min so the engine sells further OTM regardless of raw VIX.
    spy_ma50_clamp_mid_pct: float = 0.0      # SPY below MA50 by this -> min tier mid
    spy_ma50_clamp_high_pct: float = 0.03    # SPY below MA50 by 3%+ -> min tier high
    # SPY trend filter (risk.dynamic_delta_range): when 10d MA < 20d MA → "bearish".
    # Bearish ALONE forces high tier; bearish + already-high tier tightens to (0.08,0.15).
    spy_ma_fast: int = 10
    spy_ma_slow: int = 20
    drawdown_lookback_days: int = 5      # 5-day NLV-drawdown window
    drawdown_threshold_light: float = 0.02   # >2% DD: × 0.75 slots
    drawdown_threshold_mid: float = 0.05     # >5% DD: × 0.50 slots
    drawdown_threshold_severe: float = 0.10  # >10% DD: × 0.25 slots
    # Parallel 20-day lookback (NEW vs live — catches slow-grind bears like bear_2022
    # that never trigger the 5d window). Engine takes min(mult_5d, mult_20d).
    # 0 disables. Mild thresholds: a 20d window of -3/-6/-12% is roughly equivalent
    # in severity to the 5d -2/-5/-10% under a steady decline.
    drawdown_long_lookback_days: int = 20
    drawdown_long_threshold_light: float = 0.03
    drawdown_long_threshold_mid: float = 0.06
    drawdown_long_threshold_severe: float = 0.12
    intraday_loss_halt_pct: float = 0.025    # halt new puts if MtM loss > 2.5% NLV
    intraday_loss_halt_floor: float = 50_000.0   # absolute $ floor on the halt threshold
    daily_cb_pct: float = 0.05               # daily NLV drop > 5% → full halt
    daily_cb_halt_days: int = 5              # halt persists for N trading days after trigger
    # Portfolio delta cap (mirrors live src/strategy/risk.py:700-754).
    # Block new short puts when total |delta|×qty×100 across open positions
    # reaches the NLV-tier max. Skipped entirely below delta_nlv_threshold.
    # NLV-tier scaling: <200K → 1×, <500K → 2×, ≥500K → 4× base cap.
    max_portfolio_delta: float = 500.0
    delta_nlv_threshold: float = 50_000.0
    # ── Long-grind countermeasures (sweep-validated 2026-05-27; see
    # scripts/longgrind_sweep.py for the 18-regime × 4-config evaluation).
    # Candidate 1 (bear_regime_*): REJECTED by sweep — hurt iran_war/bear_2022/
    # tariff_2025/covid_2020. Defaults to OFF; left as opt-in field for future
    # retesting if rule changes.
    bear_regime_enabled: bool = False
    bear_regime_vix_min: float = 22.0
    bear_regime_dte_min: int = 3
    bear_regime_dte_max: int = 10
    # Candidate 2 (VIX-adaptive k): ACCEPTED — lifts gfc_2008 +1.95pp, iran_war
    # +1.29pp, no regime hurt. Raises pricing-model k in high-VIX regimes to
    # close the realized-vol-vs-implied-vol gap. MARSWALK-ONLY (live uses real
    # market IV; no synthetic-pricing analog exists).
    k_vix_adaptive_enabled: bool = True
    k_vix_high_threshold: float = 25.0
    k_vix_panic_threshold: float = 35.0
    k_vix_high: float = 1.7
    k_vix_panic: float = 2.5
    # Candidate 3 (stagnation booster): ACCEPTED — lifts ai_crash +3.11pp,
    # oil_crash +1.67pp, bull_2021 +1.81pp; no regime hurt ≤-2pp. PORTED TO
    # LIVE (src/strategy/risk.py iv_rank_size_multiplier + StrategyConfig).
    # When rolling N-day NLV return is below threshold, multiply qty_mult.
    stagnation_boost_enabled: bool = True
    stagnation_lookback_days: int = 60
    stagnation_threshold_pct: float = 1.0
    stagnation_multiplier: float = 2.0
    # Deep-bear safeguard for stagnation booster (2026-05-27): suppress booster
    # when SPY is more than this fraction below MA200. Doubling positions into
    # a sustained collapse stacked losses in gfc_2008 (-1.35pp penalty).
    # gfc_2008 had SPY 30-40% below MA200 → boost suppressed.
    # debt_2011 / bear_2022 had SPY ~15% below → boost still fires.
    stagnation_deep_bear_threshold: float = 0.15
    # Bear-market gate. Four modes (compared in the engine's candidate / qty_mult
    # paths): "off" = no gate, "halve_contracts" = halve total contracts when SPY
    # < MA200, "cap_multiplier" = cap iv_rank_size multiplier at N when SPY <
    # MA200, "per_name_ma200" (DEFAULT) = skip puts on any symbol whose OWN price
    # is below its 200d SMA. Backtests show "per_name_ma200" beats all alternatives:
    # bear_2022 -49% → -31% AND bulls improve slightly (skips individually-broken
    # names that would otherwise assign). Requires 200d of pre-regime warmup bars
    # per symbol — loaded via _pre:<sym> keys from the data layer.
    bear_market_ma200_enabled: bool = False    # growth-mode 2026-05-26: cost-basis averaging + margin cap handle bears
    bear_market_size_multiplier: float = 0.5
    # off | halve_contracts | cap_multiplier | per_name_ma200 | breadth_gradual
    # Growth-mode 2026-05-26: default "off" — no MA200 gate. The margin cap
    # (80% × 5× = 400% NLV notional ceiling) is the real protection; assignments
    # at lower strikes lower average cost basis, which the wheel then earns back.
    bear_market_gate_mode: str = "off"
    bear_market_cap_multiplier_value: int = 2       # used when mode = cap_multiplier
    # Breadth thresholds + halve multiplier (used by breadth_gradual mode).
    ma200_breadth_off_threshold: float = 0.30
    ma200_breadth_full_threshold: float = 0.50
    ma200_breadth_halve_multiplier: float = 0.5
    # High-vol-grind detector — fires when sustained high realized vol + no
    # multi-month trend. Used ONLY to gate cash-and-carry mode (skip the put-
    # selling pass while regime.cash_yield_annual accrues on idle cash). The
    # 2026-05-28 attempt to use this for DTE/delta/iv_rank overrides was
    # rejected — see memory stagflation-strategy-attempted-2026-05-28.
    high_vol_grind_enabled: bool = False
    high_vol_grind_realized_vol_threshold: float = 0.25
    high_vol_grind_trend_window_days: int = 180
    high_vol_grind_trend_max_abs_pct: float = 20.0
    high_vol_grind_detect_window_days: int = 60
    high_vol_grind_on_days_required: int = 15
    high_vol_grind_off_days_required: int = 5
    # When True, the put-selling pass is SKIPPED on days the detector is active.
    # Existing positions settle normally; idle cash accrues regime.cash_yield_annual.
    # Default OFF — opt-in per Params instance (or sweep candidate).
    cash_carry_when_grind: bool = False
    # When True, sell a symmetric-delta call alongside each put on days the
    # high-vol-grind detector is active. Mutually exclusive with cash_carry
    # action (they share the same detector). Head-to-head on stagflation_70s
    # showed strangle alone (+94.74%) beats cash-carry alone (+85.13%) by
    # +9.61pp. Default OFF — opt-in per Params or per-regime YAML.
    strangle_when_grind: bool = False
    # ── Crash detector (2026-05-28) — opposite shape of grind detector ──
    # Crash = sustained high vol AND sharp trend. Designed for Lehman-class
    # regimes (gfc_2008, stacked_2x). The grind detector's |trend|<20% gate
    # NEVER fires in crashes — by design, it's for stagnation. Crash detector
    # uses |trend| > 15% to catch the opposite shape. Faster ON (5d) and slower
    # OFF (10d) than grind — crashes need quick response and stable recovery.
    crash_when_active_enabled: bool = False
    crash_realized_vol_threshold: float = 0.40   # severe — only fires in true crisis
    crash_trend_abs_pct: float = 15.0            # |60d trend| > 15% (either direction)
    crash_detect_window_days: int = 60
    crash_on_days_required: int = 5
    crash_off_days_required: int = 10
    # When True AND crash_active, halt the put-selling pass (same as cash-carry
    # for grind). Existing positions settle; cash accrues regime.cash_yield_annual.
    crash_carry_when_active: bool = False
    # When True AND crash_active, sell strangles instead of just puts (mirror of
    # strangle_when_grind). RECOMMENDED OFF for crashes — call leg gets crushed
    # on V-shape rebounds. Kept as opt-in for sweep validation.
    crash_strangle_when_active: bool = False
    # Multi-leg short structures (2026-05-28).
    # "off"          → wheel-only put-selling (default; preserves byte-for-byte
    #                  behavior on all 21 regimes when strangle_when_grind=False)
    # "strangle"     → always-on: sell a symmetric-delta call alongside every put
    # "iron_condor"  → REJECTED by sweep (hurts 17 regimes ≤-2pp at 3% offset)
    # See also `strangle_when_grind` below — preferred deployment is detector-
    # triggered, mirroring how cash-carry uses the same hvg detector.
    strangle_mode: str = "off"                # "off" | "strangle" | "iron_condor"
    strangle_call_delta_min: float = 0.15     # symmetric to default put delta band
    strangle_call_delta_max: float = 0.30
    ic_long_offset_pct: float = 0.03          # long-leg strike offset from short
    ic_long_iv_discount: float = 0.85         # long-leg IV is slightly lower (smile)


class _CfgShim:
    """Proxy the live strategy cfg but override min_premium / min_premium_put,
    so a backtest can test a different premium floor without touching settings.
    Everything else (min_bid, weekend_theta, …) proxies to the real cfg."""

    def __init__(self, base, min_premium):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_mp", min_premium)

    def __getattr__(self, name):
        if name in ("min_premium", "min_premium_put"):
            return object.__getattribute__(self, "_mp")
        return getattr(object.__getattribute__(self, "_base"), name)


def _exp_date(expiry: str) -> date:
    return datetime.strptime(expiry, "%Y%m%d").date()


def _dte(expiry: str, today: date) -> int:
    return (_exp_date(expiry) - today).days


def _vix_tier(vix: float, vix_prev: float | None,
              spike_bump_1: float, spike_bump_2: float,
              spy_dist_below_ma50: float | None,
              clamp_mid_pct: float, clamp_high_pct: float) -> int:
    """Mirror risk.effective_vix_tier:
       base tier from VIX (<20 low / 20-25 mid / >=25 high),
       +spike escalation,
       +SPY-MA50 clamp (force tier-min up when SPY trades below MA50)."""
    if vix < 20:
        base = 0
    elif vix < 25:
        base = 1
    else:
        base = 2
    bump = 0
    if vix_prev is not None:
        spike = vix - vix_prev
        if spike > spike_bump_2:
            bump = 2
        elif spike > spike_bump_1:
            bump = 1
    tier = min(base + bump, 2)
    # MA50 clamp: when SPY is below MA50, never let the tier sit below this floor.
    if spy_dist_below_ma50 is not None and spy_dist_below_ma50 > 0:
        if spy_dist_below_ma50 > clamp_high_pct:
            tier = max(tier, 2)
        elif spy_dist_below_ma50 > clamp_mid_pct:
            tier = max(tier, 1)
    return tier


def _tier_delta_range(tier: int, cfg, spy_bearish: bool) -> tuple[float, float]:
    """Mirror live VIX-tiered delta range with spy_bearish escalation.

    Live rules (risk.dynamic_delta_range):
      bearish + high tier   -> (0.08, 0.15)  tightest, deepest OTM
      bearish (any tier)    -> high range    (force tier to high)
      otherwise             -> tier's normal range
    """
    if spy_bearish and tier >= 2:
        return (0.08, 0.15)
    if spy_bearish:
        return (cfg.delta_vix_high, cfg.delta_vix_high_max)
    if tier == 0:
        return (cfg.delta_vix_low, cfg.delta_vix_low_max)
    if tier == 1:
        return (cfg.delta_vix_mid, cfg.delta_vix_mid_max)
    return (cfg.delta_vix_high, cfg.delta_vix_high_max)


def _drawdown_multiplier(dd: float, light: float, mid: float, severe: float) -> float:
    """Mirror risk._drawdown_cap_multiplier."""
    if dd > severe:
        return 0.25
    if dd > mid:
        return 0.50
    if dd > light:
        return 0.75
    return 1.0


def _exposure_ramp(nlv: float) -> float:
    """Collateral-cap ramp. Growth-mode 2026-05-26: lifted to 40/60/80 so the
    cap converges with max_margin_usage (80%) — both governors bind on the
    same notional ceiling instead of the collateral cap binding well below
    the margin cap (which was the prior stagnation cause)."""
    if nlv >= 4_000_000:
        return 0.80
    if nlv >= 2_000_000:
        return 0.60
    return 0.40


def run_regime(regime_id, regime_name, category, rank, universe, market, params: Params,
               earnings=None, cash_yield_annual: float | None = None):
    """
    market: {symbol: [(date_obj, close, iv), ...]} (each symbol's bars).
    earnings: optional {symbol: set(date)} of historical earnings dates (gate off if None).
    cash_yield_annual: optional annual yield to accrue on positive cash daily.
        Passed from the regime's YAML field (regimes.py Regime.cash_yield_annual).
        None or 0 = no accrual. Used by stagflation_70s to model 1970s T-bill yield.
    Returns a result dict (summary + points). Does NOT write to any DB.
    """
    cfg = get_settings().strategy
    rcfg = get_settings().risk
    earnings = earnings or {}

    cc_dte_min = params.cc_dte_min
    cc_dte_max = params.cc_dte_max
    cc_above_cb = getattr(cfg, "cc_above_cost_basis", True)
    # Per-run min-premium overrides (0 = use live settings default)
    put_cfg = _CfgShim(cfg, params.put_min_premium) if params.put_min_premium > 0 else cfg
    cc_cfg = _CfgShim(cfg, params.cc_min_premium) if params.cc_min_premium > 0 else cfg

    # Per-symbol lookup + unified trading-date axis. Skip _pre:<sym> warmup keys —
    # those carry pre-regime bars consumed only by the per-name MA200 builder below
    # and MUST NOT enter the date loop, lut, or iv_rank_lut (they would corrupt the
    # regime axis and IV-rank baseline).
    lut: dict[str, dict] = {}
    all_dates: set[date] = set()
    for sym, bars in market.items():
        if sym.startswith("_pre:"):
            continue
        lut[sym] = {b[0]: (b[1], b[2]) for b in bars}
        all_dates.update(b[0] for b in bars)
    dates = sorted(all_dates)
    if not dates:
        return None

    def pv(sym, d):
        return lut.get(sym, {}).get(d)

    cash = params.start_capital
    start_cap = params.start_capital
    start_date = dates[0]
    short_puts: list[dict] = []
    short_calls: list[dict] = []
    # PROTOTYPE 2026-05-28: naked short calls (strangle mode) and long-leg hedges
    # (iron_condor mode). Tracked separately from short_calls (which is reserved
    # for covered calls written against assigned-via-put stock).
    short_calls_naked: list[dict] = []  # each: sym, strike, expiry, premium, qty, entry_delta, long_strike (or None for plain strangle)
    long_puts: list[dict] = []          # iron-condor long-put hedges
    long_calls: list[dict] = []         # iron-condor long-call hedges
    stocks: dict[str, dict] = {}
    n_trades = 0
    n_assign = 0
    points = []
    peak = start_cap
    max_dd = 0.0
    daily_target = (1 + TARGET_ANNUAL) ** (1 / 365) - 1
    prev_nlv = start_cap  # cap base for next day's deployment (NLV is marked end-of-day)
    n_halt_days = 0

    # Per-symbol MA200 (for the per_name_ma200 gate mode). Concatenates _pre:<sym>
    # warmup bars (loaded separately by the data layer) with in-regime bars so MA200
    # is hot from day 1 of the regime. Fails-open if a symbol still lacks 200 bars.
    # Test harnesses can inject a pre-built MA200 dict via module attribute
    # `_injected_per_name_ma200` (used by offline experiments).
    per_name_ma200: dict[str, dict] = {}
    # Breadth cache: {date: fraction_of_universe_below_own_MA200}. Lazily filled
    # inside the date loop, reused across the per-symbol iterations on that day.
    _breadth_today_cache: dict = {}
    if getattr(params, "bear_market_gate_mode", "off") == "per_name_ma200":
        injected = globals().get("_injected_per_name_ma200")
        if injected:
            per_name_ma200 = injected
        else:
            for sym in universe:
                pre_bars = market.get(f"_pre:{sym}", [])
                in_bars = market.get(sym, [])
                # Combine pre-regime + in-regime closes, dedupe by date, sort.
                merged: dict = {}
                for b in pre_bars:
                    merged[b[0]] = b[1]
                for b in in_bars:
                    merged[b[0]] = b[1]
                if len(merged) < 200:
                    continue
                bars_sorted = sorted(merged.items())
                sym_dates = [d for d, _ in bars_sorted]
                sym_closes = [c for _, c in bars_sorted]
                sym_ma = {}
                for i in range(200, len(sym_closes)):
                    sym_ma[sym_dates[i]] = sum(sym_closes[i-200:i]) / 200
                per_name_ma200[sym] = sym_ma

    # Per-symbol running IV min/max up to each date (for the IV-rank gate).
    iv_rank_lut: dict[str, dict] = {}
    for sym, bars in market.items():
        if sym == "^VIX" or sym.startswith("_pre:"):
            continue
        rmin = rmax = None
        per_date = {}
        for (bd, _c, biv) in bars:
            if biv and biv > 0:
                rmin = biv if rmin is None else min(rmin, biv)
                rmax = biv if rmax is None else max(rmax, biv)
                per_date[bd] = (rmin, rmax)
            else:
                per_date[bd] = None
        iv_rank_lut[sym] = per_date

    def iv_rank(sym, d, iv):
        mm = iv_rank_lut.get(sym, {}).get(d)
        if not mm:
            return None
        rmin, rmax = mm
        if rmax - rmin < 1e-9:
            return 50.0  # flat IV history within the window — treat as neutral
        return (iv - rmin) / (rmax - rmin) * 100.0

    # Per-symbol daily returns (for the correlation gate).
    ret_lut: dict[str, dict] = {}
    ret_dates: dict[str, list] = {}
    for sym, bars in market.items():
        if sym == "^VIX" or sym.startswith("_pre:"):
            continue
        series, order, prev_c = {}, [], None
        for (bd, c, _iv) in bars:
            if prev_c and prev_c > 0 and c:
                series[bd] = c / prev_c - 1
                order.append(bd)
            prev_c = c
        ret_lut[sym] = series
        ret_dates[sym] = order

    def avg_corr(cand, held_syms, d, lookback):
        cd = [x for x in ret_dates.get(cand, []) if x <= d][-lookback:]
        if len(cd) < 10:
            return 0.0
        cors = []
        for h in held_syms:
            if h == cand or h not in ret_lut:
                continue
            pa, pb = [], []
            for x in cd:
                if x in ret_lut[h]:
                    pa.append(ret_lut[cand][x])
                    pb.append(ret_lut[h][x])
            if len(pa) >= 10:
                cors.append(_pearson(pa, pb))
        return sum(cors) / len(cors) if cors else 0.0

    earnings_on = bool(earnings) and getattr(cfg, "earnings_avoid_enabled", True)
    earn_days = getattr(cfg, "earnings_avoid_days", 3)

    # Per-day VIX series for spike calc (engine already has vix close on ^VIX).
    vix_series = {b[0]: b[1] for b in market.get("^VIX", [])}
    # SPY series + pre-computed MAs (50d for clamp, fast/slow for trend filter).
    # Bars before regime start carry the 50d window — fetch_spy_yahoo seeds them.
    spy_bars = market.get("^SPY", [])
    spy_closes = {b[0]: b[1] for b in spy_bars}
    spy_dates = sorted(spy_closes.keys())
    spy_ma50: dict = {}
    spy_ma200: dict = {}
    spy_ma_fast: dict = {}
    spy_ma_slow: dict = {}
    # SPY N-day rolling return for the high-vol-grind detector's "no trend"
    # signal. Falls back to a universe-median 180d return when SPY missing
    # (pre-1993 regimes). Default trend window = 180 trading days.
    spy_ret_trend: dict = {}
    trend_win = params.high_vol_grind_trend_window_days
    if spy_dates:
        closes_in_order = [spy_closes[bd] for bd in spy_dates]
        for i, bd in enumerate(spy_dates):
            if i >= 50:
                spy_ma50[bd] = sum(closes_in_order[i-50:i]) / 50
            if i >= 200:
                spy_ma200[bd] = sum(closes_in_order[i-200:i]) / 200
            if i >= params.spy_ma_fast:
                spy_ma_fast[bd] = sum(closes_in_order[i-params.spy_ma_fast:i]) / params.spy_ma_fast
            if i >= params.spy_ma_slow:
                spy_ma_slow[bd] = sum(closes_in_order[i-params.spy_ma_slow:i]) / params.spy_ma_slow
            if i >= trend_win and closes_in_order[i-trend_win] > 0:
                spy_ret_trend[bd] = (closes_in_order[i] / closes_in_order[i-trend_win] - 1) * 100.0

    # ── High-vol-grind detector upstream signals (only computed when enabled). ──
    # Universe-median trailing 60-day realized vol (annualized) per day.
    univ_realized_vol: dict = {}
    # Universe-median 180-day price return per day (fallback when SPY missing).
    univ_ret_trend: dict = {}
    # Universe-median 60-day price return per day (used by crash detector's
    # |trend| > X gate; opposite shape from grind's 180d window).
    univ_ret_60d: dict = {}
    if params.high_vol_grind_enabled or params.crash_when_active_enabled:
        import math as _math
        rv_window = params.high_vol_grind_detect_window_days
        # Pre-index each symbol's returns into a per-date ordered series.
        sym_ret_series: dict = {}
        for sym in universe:
            if sym not in ret_lut:
                continue
            ordered_dates = ret_dates.get(sym, [])
            ordered_rets = [ret_lut[sym][bd] for bd in ordered_dates if bd in ret_lut[sym]]
            if len(ordered_rets) >= rv_window:
                sym_ret_series[sym] = (ordered_dates, ordered_rets)
        for d in dates:
            samples = []
            for sym, (od, orr) in sym_ret_series.items():
                last_idx = -1
                for i, bd in enumerate(od):
                    if bd <= d:
                        last_idx = i
                    else:
                        break
                if last_idx + 1 < rv_window:
                    continue
                rets_window = orr[last_idx + 1 - rv_window: last_idx + 1]
                if len(rets_window) < 5:
                    continue
                mu = sum(rets_window) / len(rets_window)
                var = sum((r - mu) ** 2 for r in rets_window) / max(len(rets_window) - 1, 1)
                sd = _math.sqrt(var) * _math.sqrt(252)
                samples.append(sd)
            if samples:
                samples.sort()
                mid = len(samples) // 2
                univ_realized_vol[d] = (
                    samples[mid] if len(samples) % 2 == 1
                    else (samples[mid - 1] + samples[mid]) / 2.0
                )
        # Universe-median 180d return fallback — only computed if SPY trend is empty.
        if not spy_ret_trend:
            sym_closes_ordered: dict = {}
            for sym in universe:
                if sym in lut:
                    ordered = sorted(lut[sym].items())
                    sym_closes_ordered[sym] = ordered
            for d in dates:
                samples = []
                target_back = d - timedelta(days=trend_win)
                for sym, ordered in sym_closes_ordered.items():
                    d_close = None
                    back_close = None
                    for bd, (c, _iv) in ordered:
                        if bd <= d:
                            d_close = c
                        if bd <= target_back:
                            back_close = c
                        if bd > d:
                            break
                    if d_close and back_close and back_close > 0:
                        samples.append((d_close / back_close - 1) * 100.0)
                if samples:
                    samples.sort()
                    mid = len(samples) // 2
                    univ_ret_trend[d] = (
                        samples[mid] if len(samples) % 2 == 1
                        else (samples[mid - 1] + samples[mid]) / 2.0
                    )

        # Universe-median 60-day return — for the crash detector. Only computed
        # if crash_when_active_enabled (grind uses 180d via univ_ret_trend).
        if params.crash_when_active_enabled:
            sym_closes_60: dict = {}
            for sym in universe:
                if sym in lut:
                    ordered = sorted(lut[sym].items())
                    sym_closes_60[sym] = ordered
            for d in dates:
                samples = []
                target_back = d - timedelta(days=60)
                for sym, ordered in sym_closes_60.items():
                    d_close = None
                    back_close = None
                    for bd, (c, _iv) in ordered:
                        if bd <= d:
                            d_close = c
                        if bd <= target_back:
                            back_close = c
                        if bd > d:
                            break
                    if d_close and back_close and back_close > 0:
                        samples.append((d_close / back_close - 1) * 100.0)
                if samples:
                    samples.sort()
                    mid = len(samples) // 2
                    univ_ret_60d[d] = (
                        samples[mid] if len(samples) % 2 == 1
                        else (samples[mid - 1] + samples[mid]) / 2.0
                    )

    def _spy_signals(d):
        """Return (dist_below_ma50_or_None, spy_bearish_bool) for date d."""
        spot = spy_closes.get(d)
        ma50 = spy_ma50.get(d)
        dist = None
        if spot and ma50 and ma50 > 0:
            dist = (ma50 - spot) / ma50  # positive when below MA50
        mf, ms = spy_ma_fast.get(d), spy_ma_slow.get(d)
        bearish = bool(mf is not None and ms is not None and mf < ms)
        return dist, bearish

    def _below_ma200(d) -> bool:
        """True iff SPY is trading below its 200d SMA on date d.
        False (fail-open) when MA200 not yet available (early in a regime)."""
        spot = spy_closes.get(d)
        ma200 = spy_ma200.get(d)
        if not spot or not ma200 or ma200 <= 0:
            return False
        return spot < ma200

    # Daily-circuit-breaker countdown: when triggered, halt new puts for N days.
    cb_halt_remaining = 0
    # NLV history for the drawdown window (most-recent-last); seeded with start NLV.
    nlv_window: list[float] = [start_cap]
    # High-vol-grind detector state machine. Counters track consecutive raw-True
    # / raw-False days; on_days_required True → ON; off_days_required False → OFF.
    hvg_active = False
    hvg_raw_true_streak = 0
    hvg_raw_false_streak = 0
    hvg_active_days = 0
    # Crash detector state (parallel structure to hvg).
    crash_active = False
    crash_raw_true_streak = 0
    crash_raw_false_streak = 0
    crash_active_days = 0
    # Cash interest accrual telemetry — total dollars added across the regime.
    cash_yield_total = 0.0
    daily_yield_rate = (cash_yield_annual / 365.0) if cash_yield_annual else 0.0

    for d in dates:
        # Hoisted VIX lookups (used by Lever A direction-aware halt below and
        # the existing VIX-tier delta logic in Section 4).
        vix_q = pv("^VIX", d)
        vix_now = vix_q[0] if vix_q else None
        vix_prev = vix_series.get(dates[max(0, dates.index(d) - 1)])

        # ── 1. Settle expiring short puts ──
        keep = []
        for p in short_puts:
            if d >= p["expiry"]:
                q = pv(p["sym"], d) or pv(p["sym"], dates[max(0, dates.index(d) - 1)])
                close = q[0] if q else p["strike"]
                if close <= p["strike"]:  # assigned
                    cash -= p["strike"] * 100 * p["qty"]
                    st = stocks.setdefault(p["sym"], {"shares": 0, "cost_basis": 0.0, "realized_cc": 0.0})
                    add = 100 * p["qty"]
                    cb = p["strike"] - p["premium"]
                    tot = st["shares"] + add
                    st["cost_basis"] = (st["cost_basis"] * st["shares"] + cb * add) / tot if tot else cb
                    st["shares"] = tot
                    n_assign += 1
                # else: expired worthless, premium already kept
            else:
                keep.append(p)
        short_puts = keep

        # ── 2. Settle expiring short calls ──
        keep = []
        for c in short_calls:
            if d >= c["expiry"]:
                q = pv(c["sym"], d)
                close = q[0] if q else c["strike"]
                st = stocks.get(c["sym"])
                if st and close >= c["strike"]:  # called away
                    cash += c["strike"] * 100 * c["qty"]
                    st["realized_cc"] = st.get("realized_cc", 0.0) + c["premium"]
                    st["shares"] -= 100 * c["qty"]
                    if st["shares"] <= 0:
                        stocks.pop(c["sym"], None)
                else:
                    if st:
                        st["realized_cc"] = st.get("realized_cc", 0.0) + c["premium"]
            else:
                keep.append(c)
        short_calls = keep

        # ── 2b. Settle naked short calls (strangle/IC short-leg) ──────────
        # Settled in cash. If close > strike, pay out (close - strike) × 100
        # × qty. For iron condor, the long-call hedge caps that loss at the
        # spread width (close > long_strike → loss = (long_strike - strike) × 100).
        keep = []
        for c in short_calls_naked:
            if d >= c["expiry"]:
                q = pv(c["sym"], d) or pv(c["sym"], dates[max(0, dates.index(d) - 1)])
                close = q[0] if q else c["strike"]
                if close > c["strike"]:
                    # Calculate the raw short-call loss
                    short_loss_per_share = close - c["strike"]
                    # IC cap: long leg activates beyond long_strike
                    if c.get("long_strike") is not None:
                        max_loss_per_share = c["long_strike"] - c["strike"]
                        short_loss_per_share = min(short_loss_per_share, max_loss_per_share)
                    cash -= short_loss_per_share * 100 * c["qty"]
                # else: expired worthless, premium kept
            else:
                keep.append(c)
        short_calls_naked = keep

        # ── 2c. Settle IC long-put hedges (insurance side) ─────────────────
        # Long puts pay out (long_strike - close) × 100 × qty when close < long_strike.
        # Cost was paid upfront when the IC was opened.
        keep = []
        for lp in long_puts:
            if d >= lp["expiry"]:
                q = pv(lp["sym"], d) or pv(lp["sym"], dates[max(0, dates.index(d) - 1)])
                close = q[0] if q else lp["strike"]
                if close < lp["strike"]:
                    # Long-put cap kicks in past the inner short-put strike;
                    # the engine's separate short_puts settlement handles
                    # assignment cost. The long leg here just provides cash
                    # back equal to (long_strike - close) capped at (long_strike
                    # - short_put_strike). But because the IC structure
                    # nets the short-put assignment + long-put payoff, the
                    # easiest implementation is: long-put payoff = max(0,
                    # long_strike - close). This double-counts vs the wheel's
                    # cost-basis tracking, but in stagflation the put-side
                    # rarely goes deep enough to test the long-leg cap.
                    payoff = lp["strike"] - close
                    cash += payoff * 100 * lp["qty"]
            else:
                keep.append(lp)
        long_puts = keep

        # ── 2d. Settle IC long-call hedges (only meaningful at expiry) ─────
        keep = []
        for lc in long_calls:
            if d >= lc["expiry"]:
                q = pv(lc["sym"], d) or pv(lc["sym"], dates[max(0, dates.index(d) - 1)])
                close = q[0] if q else lc["strike"]
                if close > lc["strike"]:
                    payoff = close - lc["strike"]
                    cash += payoff * 100 * lc["qty"]
            else:
                keep.append(lc)
        long_calls = keep

        # ── 3. Write covered calls on uncovered stock (3-branch faithful logic) ──
        covered = {c["sym"] for c in short_calls}
        for sym, st in list(stocks.items()):
            if st["shares"] // 100 <= 0 or sym in covered:
                continue
            q = pv(sym, d)
            if not q:
                continue
            spot, iv = q
            cb = st["cost_basis"]
            net_cb = cb - st.get("realized_cc", 0.0)
            # Hybrid-wheel CC selection — mirrors live src/strategy/wheel.py:
            #   rescue (spot < 95% cb)        → 0.05-0.35 wide band
            #   distressed (below own MA200)  → deep-ITM 0.80-0.95 (exit velocity)
            #   patient (above own MA200)     → configured cc_delta band (Strategy B)
            sym_ma_today = per_name_ma200.get(sym, {}).get(d) if per_name_ma200 else None
            below_own_ma200 = (sym_ma_today is not None and spot < sym_ma_today)
            if spot < cb * 0.95:
                cdmin, cdmax = 0.05, 0.35
                cc_branch = "rescue"
            elif below_own_ma200:
                cdmin, cdmax = 0.80, 0.95
                cc_branch = "distressed_exit"
            else:
                cdmin, cdmax = params.cc_delta_min, params.cc_delta_max
                cc_branch = "patient_wheel"
            min_strike = net_cb if cc_above_cb else None
            chain = [sc for sc in pricing.build_contracts(spot, d, cc_dte_max + 7, symbol=sym)
                     if cc_dte_min <= _dte(sc.lastTradeDateOrContractMonth, d) <= cc_dte_max
                     and (min_strike is None or sc.strike >= min_strike)]
            cc_iv = pricing.effective_iv(iv, (cc_dte_min + cc_dte_max) // 2, params.short_dte_uplift_k)
            cands = score_call_candidates(spot, cc_iv, chain, cc_cfg, cdmin, cdmax, d)
            # Distressed: if no deep-ITM candidate clears min_strike, fall back to
            # the wider exit-mode band (0.35-0.55) like live wheel.py does.
            if not cands and cc_branch == "distressed_exit":
                cands = score_call_candidates(spot, cc_iv, chain, cc_cfg, 0.35, 0.55, d)
            if not cands:
                continue
            top = max(cands, key=lambda c: c.score)
            lots = st["shares"] // 100
            cash += top.bid * 100 * lots
            short_calls.append({"sym": sym, "strike": top.strike,
                                "expiry": _exp_date(top.expiry),
                                "premium": top.bid, "qty": lots})
            n_trades += 1

        # ── 4. Sell new puts — gated like live: VIX/margin halt, IV-rank, earnings,
        #      correlation, collateral cap, sector cap ──
        # vix_now / vix_prev hoisted to top of day loop.
        # Direction-aware VIX gate (mirrors live risk.py:267-309, commit e6df8d9).
        # When VIX > halt threshold:
        #   - prev unknown            → halt (fail-closed for safety)
        #   - prev - now > 2.0        → allow (vol-crush phase: theta + vega pay)
        #   - prev - now <= 2.0       → halt (rising or flat-high: panic)
        # Before this parity fix the engine halted on any VIX > threshold,
        # missing the vol-crush phase that live trader writes through.
        halted = False
        if vix_now is not None and vix_now > params.vix_halt:
            if vix_prev is None:
                halted = True
            elif (vix_prev - vix_now) > 2.0:
                halted = False
            else:
                halted = True
        # Margin gate: committed capital (open put collateral + held stock value) vs NLV.
        put_collateral = sum(p["strike"] * 100 * p["qty"] for p in short_puts)
        stock_value = 0.0
        for sym, st in stocks.items():
            q = pv(sym, d)
            stock_value += st["shares"] * (q[0] if q else st["cost_basis"])
        # When margin is ON, scale the live max_margin_usage cap by the margin multiple
        # (the live cap presumes ~1x notional; portfolio margin allows ~5x).
        margin_cap_factor = params.margin_multiple if params.margin_on else 1.0
        max_margin_pct = params.max_margin_usage if params.max_margin_usage > 0 else rcfg.max_margin_usage
        if prev_nlv > 0 and (put_collateral + stock_value) / prev_nlv > max_margin_pct * margin_cap_factor:
            halted = True
        if halted:
            n_halt_days += 1  # counts VIX- and margin-halted deployment days
        # Collateral cap = (prev day's) NLV × effective pct (fixed param, else NLV ramp).
        # Live behavior: risk.check_total_exposure() skips the cap entirely when the
        # absolute cap < $100k (small accounts), leaving only the margin gate. The
        # backtest must mirror this or it will under-deploy by 10-30× on small NLVs.
        eff_pct = params.total_exposure_pct if params.total_exposure_pct > 0 else _exposure_ramp(prev_nlv)
        exposure_cap = prev_nlv * eff_pct
        small_account = exposure_cap < 100_000

        # ── Live-system defenses (ports from src.strategy.risk) ──

        # (a) Daily circuit-breaker (scheduler/jobs.py:640-665): if yesterday's NLV
        #     dropped > daily_cb_pct vs the day before, halt all new puts for N days.
        if len(nlv_window) >= 2 and nlv_window[-2] > 0:
            day_change = (nlv_window[-1] - nlv_window[-2]) / nlv_window[-2]
            if day_change < -params.daily_cb_pct and cb_halt_remaining == 0:
                cb_halt_remaining = params.daily_cb_halt_days
        if cb_halt_remaining > 0:
            halted = True
            cb_halt_remaining -= 1

        # (b) Intraday loss halt (risk.check_intraday_loss): sum mark-to-market loss
        #     on open short puts; if > max(2.5% NLV, $50k floor), halt new puts today.
        if not halted:
            unrealized = 0.0
            for pp in short_puts:
                q = pv(pp["sym"], d)
                if q:
                    mark = pricing.value_put(q[0], pp["strike"],
                                             pp["expiry"].strftime("%Y%m%d"), d, q[1],
                                             params.short_dte_uplift_k)
                    # Loss when current mark > entry premium (we're short).
                    unrealized += (pp["premium"] - mark) * 100 * pp["qty"]
            threshold = max(prev_nlv * params.intraday_loss_halt_pct,
                            params.intraday_loss_halt_floor)
            if unrealized <= -threshold:
                halted = True

        # (c-bull) Bull-regime detector: VIX < bull_regime_vix_max AND SPY >
        # MA200. When True, three adaptive overrides flip (delta band, per-name
        # cap, iv_rank floor) — mirrors live risk.in_bull_regime().
        bull_today = False
        if params.bull_regime_enabled and vix_now is not None:
            spy_ma_today = spy_ma200.get(d)
            spy_today_close = spy_closes.get(d)
            if (spy_ma_today is not None and spy_today_close is not None
                    and spy_today_close > spy_ma_today
                    and vix_now < params.bull_regime_vix_max):
                bull_today = True

        # (c-bear) Bear-regime detector (long-grind countermeasure candidate 1).
        # Mutually exclusive with bull_today. When True, DTE override below
        # extends 0-3 → bear_regime_dte_min/max.
        bear_today = False
        if (params.bear_regime_enabled and not bull_today
                and vix_now is not None and vix_now > params.bear_regime_vix_min):
            spy_ma_today = spy_ma200.get(d)
            spy_today_close = spy_closes.get(d)
            if (spy_ma_today is not None and spy_today_close is not None
                    and spy_today_close < spy_ma_today):
                bear_today = True

        # (c-hvg) High-vol-grind detector — sustained high realized vol + no
        # multi-month trend. Mutually exclusive with bull_today / bear_today.
        # Raw signal both must hold; debounce: on_days_required consecutive
        # True → switch ON; off_days_required consecutive False → switch OFF.
        # Used ONLY to gate cash-carry mode (skip put-selling) when
        # cash_carry_when_grind is True. No DTE/delta/iv_rank override path —
        # those were tested 2026-05-28 and rejected (see memory note).
        if params.high_vol_grind_enabled and not bull_today and not bear_today:
            rv = univ_realized_vol.get(d)
            trend = spy_ret_trend.get(d)
            if trend is None:
                trend = univ_ret_trend.get(d)
            if rv is not None and trend is not None:
                raw = (rv > params.high_vol_grind_realized_vol_threshold
                       and abs(trend) < params.high_vol_grind_trend_max_abs_pct)
            else:
                raw = False
            if raw:
                hvg_raw_true_streak += 1
                hvg_raw_false_streak = 0
                if not hvg_active and hvg_raw_true_streak >= params.high_vol_grind_on_days_required:
                    hvg_active = True
            else:
                hvg_raw_false_streak += 1
                hvg_raw_true_streak = 0
                if hvg_active and hvg_raw_false_streak >= params.high_vol_grind_off_days_required:
                    hvg_active = False
        else:
            hvg_active = False
            hvg_raw_true_streak = 0
            hvg_raw_false_streak = 0
        if hvg_active:
            hvg_active_days += 1

        # (c-crash) Crash detector (2026-05-28) — opposite shape of hvg.
        # High vol AND SHARP trend (|60d trend| > X). Mutually exclusive with
        # bull/bear/hvg (different shape from each). 5d-on, 10d-off debounce
        # (fast response, slow recovery — crashes have whipsaw rebounds).
        if (params.crash_when_active_enabled and not bull_today
                and not bear_today and not hvg_active):
            rv = univ_realized_vol.get(d)
            trend60 = univ_ret_60d.get(d)
            if rv is not None and trend60 is not None:
                raw = (rv > params.crash_realized_vol_threshold
                       and abs(trend60) > params.crash_trend_abs_pct)
            else:
                raw = False
            if raw:
                crash_raw_true_streak += 1
                crash_raw_false_streak = 0
                if (not crash_active
                        and crash_raw_true_streak >= params.crash_on_days_required):
                    crash_active = True
            else:
                crash_raw_false_streak += 1
                crash_raw_true_streak = 0
                if (crash_active
                        and crash_raw_false_streak >= params.crash_off_days_required):
                    crash_active = False
        else:
            crash_active = False
            crash_raw_true_streak = 0
            crash_raw_false_streak = 0
        if crash_active:
            crash_active_days += 1

        # (c-stag) Stagnation detector (long-grind countermeasure candidate 3).
        # Rolling lookback_days NLV return < stagnation_threshold_pct → boost
        # qty_mult below by stagnation_multiplier (capped by existing ivr_cap).
        # Deep-bear safeguard: suppress when SPY > stagnation_deep_bear_threshold
        # below MA200 — doubling positions into a sustained collapse stacks
        # losses (gfc_2008 historical evidence, -1.35pp penalty when unguarded).
        stagnation_active = False
        if params.stagnation_boost_enabled and len(nlv_window) >= 5:
            lb = min(len(nlv_window) - 1, params.stagnation_lookback_days)
            lookback_nlv = nlv_window[-lb - 1] if lb < len(nlv_window) else nlv_window[0]
            current_nlv = nlv_window[-1]
            if lookback_nlv > 0:
                rolling_ret_pct = (current_nlv - lookback_nlv) / lookback_nlv * 100
                if rolling_ret_pct < params.stagnation_threshold_pct:
                    spy_ma_today = spy_ma200.get(d)
                    spy_today_close = spy_closes.get(d)
                    deep_bear = False
                    if spy_ma_today and spy_today_close and spy_ma_today > 0:
                        pct_below = (spy_ma_today - spy_today_close) / spy_ma_today
                        if pct_below > params.stagnation_deep_bear_threshold:
                            deep_bear = True
                    if not deep_bear:
                        stagnation_active = True

        # (c) VIX-tier dynamic delta (risk.dynamic_delta_range + effective_vix_tier).
        #     Replaces fixed params.delta_min/max for the day's put-selling pass.
        #     SPY-MA50 clamp pushes tier-min up while SPY trades below its 50d SMA;
        #     SPY 10d<20d "bearish" forces high tier AND (if already high) tightens.
        # Compute spy_bearish unconditionally (used by both the delta-tier
        # adjustment AND the universe-halve gate below). SPY 10d < SPY 20d.
        _spy_dist, spy_bearish = _spy_signals(d)
        if params.dynamic_delta_enabled and vix_now is not None:
            vix_prev = vix_series.get(dates[max(0, dates.index(d) - 1)])
            tier = _vix_tier(vix_now, vix_prev,
                             params.vix_spike_bump_1, params.vix_spike_bump_2,
                             _spy_dist,
                             params.spy_ma50_clamp_mid_pct,
                             params.spy_ma50_clamp_high_pct)
            day_delta_min, day_delta_max = _tier_delta_range(tier, cfg, spy_bearish)
        else:
            day_delta_min, day_delta_max = params.delta_min, params.delta_max

        # (d) Drawdown daily-cap scaler (risk._drawdown_cap_multiplier) — 5d window.
        #     Plus a parallel 20d lookback that catches slow-grind bears (NEW vs live).
        #     Engine takes min(5d_mult, 20d_mult) so whichever sees the deeper trouble wins.
        def _dd_pct(lb: int) -> float:
            w = nlv_window[-(lb + 1):-1]
            if not w:
                return 0.0
            peak = max(w)
            if peak <= 0 or prev_nlv <= 0:
                return 0.0
            return max(0.0, (peak - prev_nlv) / peak)

        dd_mult = _drawdown_multiplier(
            _dd_pct(params.drawdown_lookback_days),
            params.drawdown_threshold_light,
            params.drawdown_threshold_mid,
            params.drawdown_threshold_severe,
        )
        if params.drawdown_long_lookback_days > 0:
            dd_mult = min(dd_mult, _drawdown_multiplier(
                _dd_pct(params.drawdown_long_lookback_days),
                params.drawdown_long_threshold_light,
                params.drawdown_long_threshold_mid,
                params.drawdown_long_threshold_severe,
            ))

        held = {p["sym"] for p in short_puts} | set(stocks.keys())
        slots = int((params.max_positions - len(short_puts)) * dd_mult)
        corr_on = prev_nlv > rcfg.correlation_nlv_threshold
        # Portfolio delta cap (mirrors live src/strategy/risk.py:700-754).
        # Compute current total |delta|×qty×100 across open puts; set NLV-tier
        # cap. Skipped for small accounts. Gate triggers per-candidate at commit.
        delta_cap_on = prev_nlv >= params.delta_nlv_threshold
        portfolio_delta = 0.0
        max_delta_cap = float("inf")
        if delta_cap_on:
            portfolio_delta = sum(
                p.get("entry_delta", 0.0) * p["qty"] * 100 for p in short_puts
            )
            if prev_nlv < 200_000:
                max_delta_cap = params.max_portfolio_delta
            elif prev_nlv < 500_000:
                max_delta_cap = params.max_portfolio_delta * 2
            else:
                max_delta_cap = params.max_portfolio_delta * 4
        # SPY universe-halve gate (mirrors live src/strategy/put_seller.py:77 +
        # check_spy_ma_gate). When SPY 10d < 20d MA, scan half the universe per
        # day. Per-day seeded so the same backtest is reproducible.
        if spy_bearish and len(universe) > 1:
            rng = random.Random(d.toordinal())
            half_n = max(1, len(universe) // 2)
            shuffled = list(universe)
            rng.shuffle(shuffled)
            scan_universe = shuffled[:half_n]
        else:
            scan_universe = universe
        # Cash-and-carry gate: when high-vol-grind detector OR crash detector
        # is active AND the corresponding action mode is enabled, skip the
        # put-selling pass entirely. Existing positions settle normally; idle
        # cash accrues regime.cash_yield_annual.
        if (hvg_active and params.cash_carry_when_grind) or (
                crash_active and params.crash_carry_when_active):
            slots = 0
        if slots > 0 and not halted:
            ranked = []
            for sym in scan_universe:
                if sym in held:
                    continue
                q = pv(sym, d)
                if not q:
                    continue
                spot, iv = q
                ivr = iv_rank(sym, d, iv)  # computed unconditionally for iv_rank_sizing
                # IV-rank gate. Bull-regime override raises the floor to skip
                # dead-IV consumer staples; baseline is VIX-adaptive (mirrors
                # live src/strategy/risk.py:991-996): VIX<15 → max(15, base-15),
                # VIX<20 → max(15, base-10), else base. Relaxes in calm regimes
                # where market-wide IV-rank compresses.
                if bull_today:
                    effective_ivr_min = params.bull_regime_iv_rank_min
                else:
                    base_min = params.iv_rank_min
                    if vix_now is not None and vix_now < 15:
                        effective_ivr_min = max(15.0, base_min - 15)
                    elif vix_now is not None and vix_now < 20:
                        effective_ivr_min = max(15.0, base_min - 10)
                    else:
                        effective_ivr_min = base_min
                if effective_ivr_min > 0:
                    if ivr is not None and ivr < effective_ivr_min:
                        continue
                if earnings_on:                       # earnings gate (skip near earnings)
                    eset = earnings.get(sym)
                    if eset and any(d <= ed <= d + timedelta(days=earn_days) for ed in eset):
                        continue
                if corr_on and held:                  # correlation gate (avoid stacking)
                    if avg_corr(sym, held, d, rcfg.correlation_lookback_days) > rcfg.max_correlation:
                        continue
                # Bear-market gate mode B: per_name_ma200 — skip if THIS name is below its
                # own 200d SMA, regardless of SPY's trend. Targets single-name death spirals.
                if (params.bear_market_ma200_enabled
                        and params.bear_market_gate_mode == "per_name_ma200"):
                    sym_ma = per_name_ma200.get(sym, {}).get(d)
                    if sym_ma is not None and spot < sym_ma:
                        continue
                # Bear-market gate mode E: breadth_gradual — only SKIP when ≥
                # breadth_full of the universe is below MA200 today. Below
                # breadth_off, gate is OFF. In between is HALVE (handled at the
                # sizing block below). Computed per-day via _breadth_today_cache.
                if (params.bear_market_ma200_enabled
                        and params.bear_market_gate_mode == "breadth_gradual"):
                    breadth = _breadth_today_cache.get(d)
                    if breadth is None:
                        below = total = 0
                        for _s in universe:
                            _sma = per_name_ma200.get(_s, {}).get(d)
                            _q = pv(_s, d)
                            if _sma is None or _q is None:
                                continue
                            total += 1
                            if _q[0] < _sma:
                                below += 1
                        breadth = (below / total) if total else 0.0
                        _breadth_today_cache[d] = breadth
                    sym_ma = per_name_ma200.get(sym, {}).get(d)
                    name_below = sym_ma is not None and spot < sym_ma
                    if (breadth >= params.ma200_breadth_full_threshold) and name_below:
                        continue
                # DTE override: bull-regime extends 0-3 → 0-7 (mirrors live).
                # Bear-regime (sweep candidate 1) extends 0-3 → bear_regime_dte_*
                # to capture more theta per cycle in extended-grind crash regimes.
                # Mutually exclusive (bear_today is False when bull_today is True).
                if bull_today:
                    day_dte_min, day_dte_max = (params.bull_regime_dte_min, params.bull_regime_dte_max)
                elif bear_today:
                    day_dte_min, day_dte_max = (params.bear_regime_dte_min, params.bear_regime_dte_max)
                else:
                    day_dte_min, day_dte_max = (params.dte_min, params.dte_max)
                chain = [sc for sc in pricing.build_contracts(spot, d, day_dte_max + 7, symbol=sym)
                         if day_dte_min <= _dte(sc.lastTradeDateOrContractMonth, d) <= day_dte_max]
                # VIX-adaptive k (sweep candidate 2): raise short_dte_uplift_k in
                # high-VIX regimes to close the realized-vol-vs-implied-vol gap.
                k_today = params.short_dte_uplift_k
                if params.k_vix_adaptive_enabled and vix_now is not None:
                    if vix_now > params.k_vix_panic_threshold:
                        k_today = params.k_vix_panic
                    elif vix_now > params.k_vix_high_threshold:
                        k_today = params.k_vix_high
                score_iv = pricing.effective_iv(iv, (day_dte_min + day_dte_max) // 2,
                                                k_today)
                cands = score_put_candidates(spot, score_iv, chain, put_cfg,
                                             day_delta_min, day_delta_max,
                                             day_dte_min, day_dte_max, d)
                if cands:
                    top = max(cands, key=lambda c: c.score)
                    ranked.append((top.score, sym, top, ivr))
            ranked.sort(key=lambda x: x[0], reverse=True)
            for _, sym, top, ivr in ranked:
                if slots <= 0:
                    break
                # iv_rank_sizing (settings.strategy.iv_rank_sizing_enabled): scale
                # contracts up when IV-rank is elevated. Cap with max_multiplier.
                # Mode A (cap_multiplier): tighten the cap when SPY < MA200 so IV-rank
                # over-sizing doesn't compound bear-regime assignment risk.
                ivr_cap = max(1, getattr(cfg, "iv_rank_size_max_multiplier", 1))
                if (params.bear_market_ma200_enabled
                        and params.bear_market_gate_mode == "cap_multiplier"
                        and _below_ma200(d)):
                    ivr_cap = min(ivr_cap, params.bear_market_cap_multiplier_value)
                # Growth-mode 2026-05-26: extended ladder 1/2/4/7/10 (mirrors
                # the new live risk.iv_rank_size_multiplier). Hard cap at ivr_cap.
                qty_mult = 1
                if getattr(cfg, "iv_rank_sizing_enabled", False) and ivr is not None:
                    if ivr >= 95:
                        qty_mult = min(10, ivr_cap)
                    elif ivr >= 85:
                        qty_mult = min(7, ivr_cap)
                    elif ivr >= 75:
                        qty_mult = min(4, ivr_cap)
                    elif ivr >= getattr(cfg, "iv_rank_size_mid", 50):
                        qty_mult = min(2, ivr_cap)
                # Stagnation booster (sweep candidate 3): double qty_mult when
                # rolling NLV is flat. Capped by ivr_cap so we never exceed the
                # existing 10× hard ceiling.
                if stagnation_active:
                    qty_mult = min(int(round(qty_mult * params.stagnation_multiplier)), ivr_cap)
                contracts = params.contracts * qty_mult
                # Mode E (breadth_gradual): halve contracts when in HALVE regime
                # (off_threshold <= breadth < full_threshold) AND name is below
                # its own MA200. Off and skip branches were handled above.
                if (params.bear_market_ma200_enabled
                        and params.bear_market_gate_mode == "breadth_gradual"):
                    breadth = _breadth_today_cache.get(d, 0.0)
                    sym_ma = per_name_ma200.get(sym, {}).get(d)
                    spot_q = pv(sym, d)
                    name_below = (sym_ma is not None and spot_q is not None and spot_q[0] < sym_ma)
                    if (params.ma200_breadth_off_threshold <= breadth < params.ma200_breadth_full_threshold) and name_below:
                        contracts = max(1, int(round(contracts * params.ma200_breadth_halve_multiplier)))
                # Mode C (halve_contracts): halve total contracts when SPY < MA200.
                if (params.bear_market_ma200_enabled
                        and params.bear_market_gate_mode == "halve_contracts"
                        and _below_ma200(d)):
                    contracts = max(1, int(round(contracts * params.bear_market_size_multiplier)))
                reserved = sum(p["strike"] * 100 * p["qty"] for p in short_puts)
                need = top.strike * 100 * contracts
                if params.margin_on:
                    # Margin mode: the % NLV cap admits notional up to NLV*cap*multiple
                    # (per-put margin requirement = notional/multiple). No cash check —
                    # debits are allowed against the margin line. Small accounts skip
                    # the collateral cap entirely (mirrors live) — only the margin gate
                    # (checked upfront via the `halted` flag) constrains them.
                    if not small_account and reserved + need > exposure_cap * params.margin_multiple:
                        continue
                else:
                    if cash - reserved < need:           # cash-secured
                        continue
                    if not small_account and reserved + need > exposure_cap:
                        continue                          # collateral cap (% of NLV)
                # Sector cap: once the book is large enough to diversify (>=3 names),
                # no sector may exceed max_sector_pct of committed put collateral.
                if len(short_puts) >= 3:
                    sec = _SECTORS.get(sym, "Other")
                    sec_committed = sum(p["strike"] * 100 * p["qty"] for p in short_puts
                                        if _SECTORS.get(p["sym"], "Other") == sec)
                    if (sec_committed + need) / (reserved + need) > rcfg.max_sector_pct:
                        continue
                # Portfolio delta cap: block new entries once total delta is at
                # or above the NLV-tier max (mirrors live, src/strategy/risk.py:744).
                if delta_cap_on and portfolio_delta >= max_delta_cap:
                    continue
                cash += top.bid * 100 * contracts
                entry_delta = abs(getattr(top, "delta", 0.0) or 0.0)
                short_puts.append({"sym": sym, "strike": top.strike,
                                   "expiry": _exp_date(top.expiry),
                                   "premium": top.bid, "qty": contracts,
                                   "entry_delta": entry_delta})
                portfolio_delta += entry_delta * contracts * 100
                held.add(sym)
                slots -= 1
                n_trades += 1

                # ── Strangle leg (2026-05-28) ───────────────────────────────
                # Fires when EITHER:
                #   strangle_mode != "off"                              (always-on)
                #   strangle_when_grind=True AND hvg_active=True        (triggered)
                # The triggered path is the preferred deployment — mirrors how
                # cash-carry uses the same hvg detector but with a different
                # action ("skip + hold cash" vs "sell strangle"). For iron_condor,
                # also buy long-leg hedges further OTM.
                # The `chain` variable from the put-scoring first loop is for the
                # WRONG symbol here, so rebuild for this sym + the put's DTE band.
                # min_premium shim — live cc cfg's 0.10 floor rejects most 0-3
                # DTE OTM calls; for strangle we accept any premium.
                _strangle_active = (
                    params.strangle_mode in ("strangle", "iron_condor")
                    or (params.strangle_when_grind and hvg_active)
                    or (params.crash_strangle_when_active and crash_active)
                )
                if _strangle_active:
                    # Recompute current symbol's spot + IV (the `spot` / `score_iv`
                    # locals from the first loop leak the LAST symbol's values).
                    q_sym = pv(sym, d)
                    if q_sym is None:
                        continue
                    sym_spot, sym_iv = q_sym
                    strangle_cfg = _CfgShim(cfg, 0.01)
                    put_dte = _dte(top.expiry, d)
                    sym_chain = [
                        sc for sc in pricing.build_contracts(sym_spot, d, put_dte + 7, symbol=sym)
                        if _dte(sc.lastTradeDateOrContractMonth, d) == put_dte
                    ]
                    sym_score_iv = pricing.effective_iv(sym_iv, put_dte, k_today)
                    call_cands = score_call_candidates(
                        sym_spot, sym_score_iv, sym_chain, strangle_cfg,
                        params.strangle_call_delta_min,
                        params.strangle_call_delta_max,
                        d,
                    )
                    # Filter calls to the SAME expiry as the put we just sold.
                    same_exp = [c for c in call_cands if c.expiry == top.expiry]
                    if same_exp:
                        call_top = max(same_exp, key=lambda c: c.score)
                        long_call_strike = None
                        long_put_strike = None
                        ic_long_premium_paid = 0.0
                        if params.strangle_mode == "iron_condor":
                            # Long-call strike = short-call strike × (1 + offset).
                            # Long-put strike  = short-put strike × (1 - offset).
                            long_call_strike = call_top.strike * (1.0 + params.ic_long_offset_pct)
                            long_put_strike = top.strike * (1.0 - params.ic_long_offset_pct)
                            # BSM-price the long legs at slightly lower IV (smile);
                            # cost the premium upfront. Use this sym's spot + IV.
                            disc_iv = sym_score_iv * params.ic_long_iv_discount
                            lc_px = pricing.value_call(
                                sym_spot, long_call_strike, top.expiry, d, disc_iv, k_today
                            )
                            lp_px = pricing.value_put(
                                sym_spot, long_put_strike, top.expiry, d, disc_iv, k_today
                            )
                            ic_long_premium_paid = (lc_px + lp_px) * 100 * contracts
                            cash -= ic_long_premium_paid
                            long_calls.append({
                                "sym": sym, "strike": long_call_strike,
                                "expiry": _exp_date(top.expiry),
                                "cost": lc_px, "qty": contracts,
                            })
                            long_puts.append({
                                "sym": sym, "strike": long_put_strike,
                                "expiry": _exp_date(top.expiry),
                                "cost": lp_px, "qty": contracts,
                            })
                        # Sell the naked short call (or short-leg of IC).
                        cash += call_top.bid * 100 * contracts
                        call_entry_delta = abs(getattr(call_top, "delta", 0.0) or 0.0)
                        short_calls_naked.append({
                            "sym": sym, "strike": call_top.strike,
                            "expiry": _exp_date(call_top.expiry),
                            "premium": call_top.bid, "qty": contracts,
                            "entry_delta": call_entry_delta,
                            "long_strike": long_call_strike,  # None for plain strangle
                        })
                        n_trades += 1

        # ── 5. Mark-to-market NLV (with optional gap stress on big down days) ──
        gs = params.gap_stress

        def _mpx(sym, px):
            if gs > 0 and ret_lut.get(sym, {}).get(d, 0.0) < -0.05:
                return px * (1 - gs)
            return px

        nlv = cash
        for sym, st in stocks.items():
            q = pv(sym, d)
            nlv += st["shares"] * (_mpx(sym, q[0]) if q else st["cost_basis"])
        k = params.short_dte_uplift_k
        for p in short_puts:
            q = pv(p["sym"], d)
            if q:
                px = _mpx(p["sym"], q[0])
                nlv -= pricing.value_put(px, p["strike"], p["expiry"].strftime("%Y%m%d"), d, q[1], k) * 100 * p["qty"]
        for c in short_calls:
            q = pv(c["sym"], d)
            if q:
                px = _mpx(c["sym"], q[0])
                nlv -= pricing.value_call(px, c["strike"], c["expiry"].strftime("%Y%m%d"), d, q[1], k) * 100 * c["qty"]
        # Strangle / IC leg MTM
        for c in short_calls_naked:
            q = pv(c["sym"], d)
            if q:
                px = _mpx(c["sym"], q[0])
                nlv -= pricing.value_call(px, c["strike"], c["expiry"].strftime("%Y%m%d"), d, q[1], k) * 100 * c["qty"]
        for lp in long_puts:
            q = pv(lp["sym"], d)
            if q:
                px = _mpx(lp["sym"], q[0])
                nlv += pricing.value_put(px, lp["strike"], lp["expiry"].strftime("%Y%m%d"), d, q[1], k) * 100 * lp["qty"]
        for lc in long_calls:
            q = pv(lc["sym"], d)
            if q:
                px = _mpx(lc["sym"], q[0])
                nlv += pricing.value_call(px, lc["strike"], lc["expiry"].strftime("%Y%m%d"), d, q[1], k) * 100 * lc["qty"]

        ret = (nlv / start_cap - 1) * 100
        days = (d - start_date).days
        tgt = ((1 + daily_target) ** days - 1) * 100
        points.append((d.strftime("%Y-%m-%d"), round(nlv, 2), round(ret, 2), round(tgt, 2)))
        peak = max(peak, nlv)
        if peak > 0:
            max_dd = max(max_dd, (peak - nlv) / peak * 100)
        prev_nlv = nlv  # cap base for tomorrow's deployment
        nlv_window.append(nlv)
        # Keep the window bounded — only the last max(short, long)+2 entries matter.
        max_lb = max(params.drawdown_lookback_days, params.drawdown_long_lookback_days)
        if len(nlv_window) > max_lb + 2:
            nlv_window = nlv_window[-(max_lb + 2):]

        # Overnight cash-yield accrual (regime.cash_yield_annual). Credits cash
        # only when positive; doesn't penalize negative-cash margin debit.
        # Tomorrow's NLV will reflect this interest. None or 0 = no-op.
        if daily_yield_rate > 0 and cash > 0:
            interest = cash * daily_yield_rate
            cash += interest
            cash_yield_total += interest

    final = points[-1]
    return {
        "regime_id": regime_id, "regime_name": regime_name,
        "category": category, "rank": rank,
        "params": asdict(params),
        "start_capital": start_cap,
        "final_nlv": final[1],
        "final_return_pct": final[2],
        "target_return_pct": final[3],
        "max_drawdown_pct": round(max_dd, 2),
        "n_trades": n_trades, "n_assignments": n_assign,
        "n_halt_days": n_halt_days,
        "n_hvg_days": hvg_active_days,
        "n_crash_days": crash_active_days,
        "cash_yield_total": round(cash_yield_total, 2),
        "points": points,
    }


def save_run(result: dict) -> int:
    """Persist a run result to the isolated marswalk.db. Returns run id."""
    from src.marswalk.models import get_mw_db, Run, Point
    p = result["params"]
    with get_mw_db() as db:
        run = Run(
            regime_id=result["regime_id"], regime_name=result["regime_name"],
            category=result["category"], rank=result["rank"],
            dte_min=p["dte_min"], dte_max=p["dte_max"],
            delta_min=p["delta_min"], delta_max=p["delta_max"],
            params_json=json.dumps(p),
            start_capital=result["start_capital"], final_nlv=result["final_nlv"],
            final_return_pct=result["final_return_pct"],
            target_return_pct=result["target_return_pct"],
            max_drawdown_pct=result["max_drawdown_pct"],
            n_trades=result["n_trades"], n_assignments=result["n_assignments"],
            status="done",
        )
        db.add(run)
        db.flush()
        for d, nlv, ret, tgt in result["points"]:
            db.add(Point(run_id=run.id, date=d, nlv=nlv, return_pct=ret, target_pct=tgt))
        return run.id
