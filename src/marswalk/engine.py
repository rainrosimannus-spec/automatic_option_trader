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
from datetime import date, datetime

from src.core.config import get_settings
from src.strategy.option_scoring import score_put_candidates, score_call_candidates
from src.marswalk import pricing

TARGET_ANNUAL = 0.24


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


def _exposure_ramp(nlv: float) -> float:
    """Live collateral-cap ramp (mirrors risk._effective_total_exposure_pct)."""
    if nlv >= 4_000_000:
        return 0.30
    if nlv >= 2_000_000:
        return 0.25
    return 0.20


def run_regime(regime_id, regime_name, category, rank, universe, market, params: Params):
    """
    market: {symbol: [(date_obj, close, iv), ...]} (each symbol's bars).
    Returns a result dict (summary + points). Does NOT write to any DB.
    """
    cfg = get_settings().strategy

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
            cands = score_call_candidates(spot, iv, chain, cc_cfg, cdmin, cdmax, d)
            if not cands:
                continue
            top = max(cands, key=lambda c: c.score)
            lots = st["shares"] // 100
            cash += top.bid * 100 * lots
            short_calls.append({"sym": sym, "strike": top.strike,
                                "expiry": _exp_date(top.expiry),
                                "premium": top.bid, "qty": lots})
            n_trades += 1

        # ── 4. Sell new puts — gated like live: VIX halt, IV-rank, collateral cap ──
        vix_q = pv("^VIX", d)
        vix_now = vix_q[0] if vix_q else None
        halted = vix_now is not None and vix_now > params.vix_halt
        if halted:
            n_halt_days += 1
        # Collateral cap = (prev day's) NLV × effective pct (fixed param, else NLV ramp)
        eff_pct = params.total_exposure_pct if params.total_exposure_pct > 0 else _exposure_ramp(prev_nlv)
        exposure_cap = prev_nlv * eff_pct
        held = {p["sym"] for p in short_puts} | set(stocks.keys())
        slots = params.max_positions - len(short_puts)
        if slots > 0 and not halted:
            ranked = []
            for sym in universe:
                if sym in held:
                    continue
                q = pv(sym, d)
                if not q:
                    continue
                spot, iv = q
                if params.iv_rank_min > 0:           # IV-rank gate (live: only sell elevated IV)
                    ivr = iv_rank(sym, d, iv)
                    if ivr is not None and ivr < params.iv_rank_min:
                        continue
                chain = [sc for sc in pricing.build_contracts(spot, d, params.dte_max + 7)
                         if params.dte_min <= _dte(sc.lastTradeDateOrContractMonth, d) <= params.dte_max]
                cands = score_put_candidates(spot, iv, chain, put_cfg,
                                             params.delta_min, params.delta_max,
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
                if cash - reserved < need:           # cash-secured
                    continue
                if reserved + need > exposure_cap:    # collateral cap (% of NLV)
                    continue
                cash += top.bid * 100 * params.contracts
                short_puts.append({"sym": sym, "strike": top.strike,
                                   "expiry": _exp_date(top.expiry),
                                   "premium": top.bid, "qty": params.contracts})
                held.add(sym)
                slots -= 1
                n_trades += 1

        # ── 5. Mark-to-market NLV ──
        nlv = cash
        for sym, st in stocks.items():
            q = pv(sym, d)
            nlv += st["shares"] * (q[0] if q else st["cost_basis"])
        for p in short_puts:
            q = pv(p["sym"], d)
            if q:
                nlv -= pricing.value_put(q[0], p["strike"], p["expiry"].strftime("%Y%m%d"), d, q[1]) * 100 * p["qty"]
        for c in short_calls:
            q = pv(c["sym"], d)
            if q:
                nlv -= pricing.value_call(q[0], c["strike"], c["expiry"].strftime("%Y%m%d"), d, q[1]) * 100 * c["qty"]

        ret = (nlv / start_cap - 1) * 100
        days = (d - start_date).days
        tgt = ((1 + daily_target) ** days - 1) * 100
        points.append((d.strftime("%Y-%m-%d"), round(nlv, 2), round(ret, 2), round(tgt, 2)))
        peak = max(peak, nlv)
        if peak > 0:
            max_dd = max(max_dd, (peak - nlv) / peak * 100)
        prev_nlv = nlv  # cap base for tomorrow's deployment

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
