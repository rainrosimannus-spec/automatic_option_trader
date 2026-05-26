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
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta

from src.core.config import get_settings
from src.strategy.option_scoring import score_put_candidates, score_call_candidates
from src.marswalk import pricing

TARGET_ANNUAL = 0.24

# Sector map for the backtest universe (sector cap gate). Unknown -> "Other".
_SECTORS = {
    # Technology
    "AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology", "AVGO": "Technology",
    "ORCL": "Technology", "CRM": "Technology", "ADBE": "Technology", "AMD": "Technology",
    "INTC": "Technology", "CSCO": "Technology", "QCOM": "Technology", "TXN": "Technology",
    "IBM": "Technology", "NOW": "Technology", "INTU": "Technology",
    # Communication
    "GOOGL": "Communication", "META": "Communication", "NFLX": "Communication",
    "DIS": "Communication", "CMCSA": "Communication", "T": "Communication",
    "VZ": "Communication", "TMUS": "Communication",
    # Consumer Discretionary
    "AMZN": "ConsumerDisc", "TSLA": "ConsumerDisc", "HD": "ConsumerDisc", "MCD": "ConsumerDisc",
    "NKE": "ConsumerDisc", "LOW": "ConsumerDisc", "SBUX": "ConsumerDisc",
    "BKNG": "ConsumerDisc", "TJX": "ConsumerDisc",
    # Consumer Staples
    "PG": "ConsumerStaples", "KO": "ConsumerStaples", "PEP": "ConsumerStaples",
    "COST": "ConsumerStaples", "WMT": "ConsumerStaples", "PM": "ConsumerStaples",
    "MDLZ": "ConsumerStaples",
    # Financials
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials", "GS": "Financials",
    "MS": "Financials", "BLK": "Financials", "SCHW": "Financials", "AXP": "Financials",
    "C": "Financials",
    # Healthcare
    "JNJ": "Healthcare", "UNH": "Healthcare", "LLY": "Healthcare", "PFE": "Healthcare",
    "MRK": "Healthcare", "ABBV": "Healthcare", "TMO": "Healthcare", "ABT": "Healthcare",
    "DHR": "Healthcare", "BMY": "Healthcare",
    # Energy
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "SLB": "Energy",
    # Industrials
    "CAT": "Industrials", "BA": "Industrials", "HON": "Industrials", "GE": "Industrials",
    "UPS": "Industrials", "RTX": "Industrials", "DE": "Industrials",
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
    # ── Put selling ──
    dte_min: int = 5
    dte_max: int = 14
    delta_min: float = 0.15
    delta_max: float = 0.30
    put_min_premium: float = 0.0   # 0 = use settings.yaml default
    # ── Covered calls ──
    cc_dte_min: int = 5
    cc_dte_max: int = 21
    cc_delta_min: float = 0.20
    cc_delta_max: float = 0.40
    cc_min_premium: float = 0.0    # 0 = use settings.yaml default
    # ── Portfolio ──
    start_capital: float = 100_000.0
    max_positions: int = 10
    contracts: int = 1
    # ── Risk gates (model the live limits) ──
    total_exposure_pct: float = 0.0   # 0 = NLV ramp (20/25/30%); >0 = fixed cap %
    vix_halt: float = 30.0            # halt NEW puts when VIX > this (live high-VIX halt)
    iv_rank_min: float = 20.0         # require symbol IV-rank >= this to sell (0 = off)
    max_margin_usage: float = 0.0     # 0 = use live settings.risk.max_margin_usage (80%);
                                      # >0 = override (e.g. 0.60 for son's 60% cap).
    # ── Pricing model ──
    short_dte_uplift_k: float = 1.0   # near-expiry vol-premium uplift (0 = pure BSM)
    gap_stress: float = 0.0           # what-if: extra adverse mark on big down days
                                      # (>=5% drop) — models close understating an
                                      # intraday/overnight gap. 0 = off (historical).
    # ── Margin model ──
    margin_on: bool = False           # OFF (default) = cash-secured (notional<=NLV*cap).
                                      # ON = portfolio-margin proxy: per-put margin
                                      # requirement is notional/margin_multiple, so the
                                      # cap admits notional up to NLV*cap*margin_multiple.
    margin_multiple: float = 5.0      # IBKR portfolio-margin proxy (~5x typical OTM put)
    # ── Live-system defenses (ports from src.strategy.risk) ──
    # 0 = use the live config default (so the backtest mirrors production); >0 overrides.
    dynamic_delta_enabled: bool = True   # VIX-tiered delta range (mirrors risk.dynamic_delta_range)
    vix_spike_bump_1: float = 4.0        # VIX day-over-day spike that escalates tier +1
    vix_spike_bump_2: float = 6.0        # spike that escalates tier +2 (cap = high)
    drawdown_lookback_days: int = 5      # 5-day NLV-drawdown window
    drawdown_threshold_light: float = 0.02   # >2% DD: × 0.75 slots
    drawdown_threshold_mid: float = 0.05     # >5% DD: × 0.50 slots
    drawdown_threshold_severe: float = 0.10  # >10% DD: × 0.25 slots
    intraday_loss_halt_pct: float = 0.025    # halt new puts if MtM loss > 2.5% NLV
    intraday_loss_halt_floor: float = 50_000.0   # absolute $ floor on the halt threshold
    daily_cb_pct: float = 0.05               # daily NLV drop > 5% → full halt
    daily_cb_halt_days: int = 5              # halt persists for N trading days after trigger


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


def _vix_tier(vix: float, vix_prev: float | None, spike_bump_1: float, spike_bump_2: float) -> int:
    """Mirror risk.effective_vix_tier (no SPY/MA50 yet — phase 2).
    Returns 0=low (<20), 1=mid (20-25), 2=high (>=25). VIX>halt handled upstream."""
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
    return min(base + bump, 2)


def _tier_delta_range(tier: int, cfg) -> tuple[float, float]:
    """Mirror live VIX-tiered delta range from settings.strategy."""
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
    """Live collateral-cap ramp (mirrors risk._effective_total_exposure_pct).
    Lifted 2026-05-26 from 20/25/30 to 20/30/40 so big accounts don't drag below
    T-bills. <$2M unchanged (small accounts hit the <$100K cap exemption anyway)."""
    if nlv >= 4_000_000:
        return 0.40
    if nlv >= 2_000_000:
        return 0.30
    return 0.20


def run_regime(regime_id, regime_name, category, rank, universe, market, params: Params,
               earnings=None):
    """
    market: {symbol: [(date_obj, close, iv), ...]} (each symbol's bars).
    earnings: optional {symbol: set(date)} of historical earnings dates (gate off if None).
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

    # Per-symbol lookup + unified trading-date axis
    lut: dict[str, dict] = {}
    all_dates: set[date] = set()
    for sym, bars in market.items():
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
    stocks: dict[str, dict] = {}
    n_trades = 0
    n_assign = 0
    points = []
    peak = start_cap
    max_dd = 0.0
    daily_target = (1 + TARGET_ANNUAL) ** (1 / 365) - 1
    prev_nlv = start_cap  # cap base for next day's deployment (NLV is marked end-of-day)
    n_halt_days = 0

    # Per-symbol running IV min/max up to each date (for the IV-rank gate).
    iv_rank_lut: dict[str, dict] = {}
    for sym, bars in market.items():
        if sym == "^VIX":
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
        if sym == "^VIX":
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
    # Daily-circuit-breaker countdown: when triggered, halt new puts for N days.
    cb_halt_remaining = 0
    # NLV history for the drawdown window (most-recent-last); seeded with start NLV.
    nlv_window: list[float] = [start_cap]

    for d in dates:
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
            if spot < cb * 0.95:          # rescue branch (stock underwater)
                cdmin, cdmax = 0.05, 0.35
            else:                          # configured CC delta band
                cdmin, cdmax = params.cc_delta_min, params.cc_delta_max
            min_strike = net_cb if cc_above_cb else None
            chain = [sc for sc in pricing.build_contracts(spot, d, cc_dte_max + 7)
                     if cc_dte_min <= _dte(sc.lastTradeDateOrContractMonth, d) <= cc_dte_max
                     and (min_strike is None or sc.strike >= min_strike)]
            cc_iv = pricing.effective_iv(iv, (cc_dte_min + cc_dte_max) // 2, params.short_dte_uplift_k)
            cands = score_call_candidates(spot, cc_iv, chain, cc_cfg, cdmin, cdmax, d)
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
        vix_q = pv("^VIX", d)
        vix_now = vix_q[0] if vix_q else None
        halted = vix_now is not None and vix_now > params.vix_halt
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

        # (c) VIX-tier dynamic delta (risk.dynamic_delta_range + effective_vix_tier).
        #     Replaces fixed params.delta_min/max for the day's put-selling pass.
        if params.dynamic_delta_enabled and vix_now is not None:
            vix_prev = vix_series.get(dates[max(0, dates.index(d) - 1)])
            tier = _vix_tier(vix_now, vix_prev,
                             params.vix_spike_bump_1, params.vix_spike_bump_2)
            day_delta_min, day_delta_max = _tier_delta_range(tier, cfg)
        else:
            day_delta_min, day_delta_max = params.delta_min, params.delta_max

        # (d) Drawdown daily-cap scaler (risk._drawdown_cap_multiplier).
        #     Computes drawdown over last `drawdown_lookback_days` and scales slots.
        dd_window = nlv_window[-(params.drawdown_lookback_days + 1):-1]
        dd = 0.0
        if dd_window:
            peak = max(dd_window)
            if peak > 0 and prev_nlv > 0:
                dd = max(0.0, (peak - prev_nlv) / peak)
        dd_mult = _drawdown_multiplier(dd,
                                       params.drawdown_threshold_light,
                                       params.drawdown_threshold_mid,
                                       params.drawdown_threshold_severe)

        held = {p["sym"] for p in short_puts} | set(stocks.keys())
        slots = int((params.max_positions - len(short_puts)) * dd_mult)
        corr_on = prev_nlv > rcfg.correlation_nlv_threshold
        if slots > 0 and not halted:
            ranked = []
            for sym in universe:
                if sym in held:
                    continue
                q = pv(sym, d)
                if not q:
                    continue
                spot, iv = q
                if params.iv_rank_min > 0:           # IV-rank gate (only sell elevated IV)
                    ivr = iv_rank(sym, d, iv)
                    if ivr is not None and ivr < params.iv_rank_min:
                        continue
                if earnings_on:                       # earnings gate (skip near earnings)
                    eset = earnings.get(sym)
                    if eset and any(d <= ed <= d + timedelta(days=earn_days) for ed in eset):
                        continue
                if corr_on and held:                  # correlation gate (avoid stacking)
                    if avg_corr(sym, held, d, rcfg.correlation_lookback_days) > rcfg.max_correlation:
                        continue
                chain = [sc for sc in pricing.build_contracts(spot, d, params.dte_max + 7)
                         if params.dte_min <= _dte(sc.lastTradeDateOrContractMonth, d) <= params.dte_max]
                score_iv = pricing.effective_iv(iv, (params.dte_min + params.dte_max) // 2,
                                                params.short_dte_uplift_k)
                cands = score_put_candidates(spot, score_iv, chain, put_cfg,
                                             day_delta_min, day_delta_max,
                                             params.dte_min, params.dte_max, d)
                if cands:
                    top = max(cands, key=lambda c: c.score)
                    ranked.append((top.score, sym, top))
            ranked.sort(key=lambda x: x[0], reverse=True)
            for _, sym, top in ranked:
                if slots <= 0:
                    break
                reserved = sum(p["strike"] * 100 * p["qty"] for p in short_puts)
                need = top.strike * 100 * params.contracts
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
                cash += top.bid * 100 * params.contracts
                short_puts.append({"sym": sym, "strike": top.strike,
                                   "expiry": _exp_date(top.expiry),
                                   "premium": top.bid, "qty": params.contracts})
                held.add(sym)
                slots -= 1
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

        ret = (nlv / start_cap - 1) * 100
        days = (d - start_date).days
        tgt = ((1 + daily_target) ** days - 1) * 100
        points.append((d.strftime("%Y-%m-%d"), round(nlv, 2), round(ret, 2), round(tgt, 2)))
        peak = max(peak, nlv)
        if peak > 0:
            max_dd = max(max_dd, (peak - nlv) / peak * 100)
        prev_nlv = nlv  # cap base for tomorrow's deployment
        nlv_window.append(nlv)
        # Keep the window bounded — only the last (lookback+2) entries matter.
        if len(nlv_window) > params.drawdown_lookback_days + 2:
            nlv_window = nlv_window[-(params.drawdown_lookback_days + 2):]

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
