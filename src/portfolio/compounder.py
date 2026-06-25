"""
Compounder Accumulation — long-horizon (10–20yr) terminal-wealth portfolio engine.

This replaces the dip-buyer brain. Philosophy (Bessembinder: a few extreme winners
create ~all long-run wealth):

  1. Rank the whole universe by 10x potential = fundamental quality/growth (from the
     periodically-refreshed watchlist scores) blended with 12-1 month momentum.
  2. Build conviction-weighted, per-name-capped target weights to the 25/15/60 tier
     proportions (breakthrough/dividend/growth of capital).
  3. Accumulate the full capital base-then-reserve: a base tranche deployed steadily
     (DCA) for time-in-market, plus a crash reserve held in cash that fires in tranches
     on market drawdowns (dry powder for an AI-bubble burst).
  4. For each underweight name, choose direct-buy (urgent / relatively cheap / crash
     active) vs put-sell (patient, when the name is extended) by price intensity.
  5. Hold — never trim winners; let concentration build as the tail runs.

Everything in this module is PURE (no IBKR/DB) and unit-tested. The orchestration that
touches IBKR/DB lives in PortfolioBuyer.run_compounder_scan().
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass


# ── Inputs / outputs ─────────────────────────────────────────
@dataclass
class NameInput:
    """One universe member: refreshed fundamental scores + live technicals."""
    symbol: str
    tier: str
    # fundamental scores (refreshed by the screener; 0–100)
    growth: float
    forward_growth: float
    quality: float
    valuation: float
    dividend_total_return: float
    risk_penalty: float
    # live technicals (from IBKR via the analyzer)
    price: float | None
    sma200: float | None
    high_52w: float | None
    momentum_12_1: float | None


@dataclass
class RankedName:
    symbol: str
    tier: str
    s10x: float          # fundamental 10x score (0–100)
    momentum_pct: float  # momentum percentile within universe (0–1)
    rank_score: float    # blended rank (0–100)
    price: float
    sma200: float | None
    high_52w: float | None


@dataclass
class ReserveState:
    peak: float           # trailing market (SPY) peak
    tranches_fired: int   # how many drawdown tranches have been unlocked (monotonic)


# ── 1. Ranking ───────────────────────────────────────────────
def fundamental_10x(n: NameInput) -> float:
    """Static 10x-potential score from the refreshed fundamental sub-scores."""
    if n.tier == "dividend":
        s = 0.45 * n.dividend_total_return + 0.30 * n.quality + 0.25 * n.growth
    else:  # growth / breakthrough
        s = 0.35 * n.growth + 0.30 * n.forward_growth + 0.25 * n.quality + 0.10 * n.valuation
    return max(0.0, s - (n.risk_penalty or 0.0))


def rank_universe(names: list[NameInput], w_fund: float = 0.70,
                  w_mom: float = 0.30) -> list[RankedName]:
    """Blend fundamental 10x score with momentum percentile. Names without a live
    price are dropped (can't be sized); names without momentum get a neutral 0.5."""
    have = [n for n in names if n.price and n.price > 0]
    moms = sorted(n.momentum_12_1 for n in have if n.momentum_12_1 is not None)

    def mom_pct(v: float | None) -> float:
        if v is None or not moms:
            return 0.5
        return bisect.bisect_right(moms, v) / len(moms)

    out: list[RankedName] = []
    for n in have:
        s10x = fundamental_10x(n)
        mp = mom_pct(n.momentum_12_1)
        rank = w_fund * s10x + w_mom * (mp * 100.0)
        out.append(RankedName(n.symbol, n.tier, round(s10x, 1), round(mp, 3),
                              round(rank, 2), n.price, n.sma200, n.high_52w))
    out.sort(key=lambda r: -r.rank_score)
    return out


# ── 2. Target weights (conviction-weighted, capped, redistributed) ──
def leader_symbols(ranked: list[RankedName], top_frac: float) -> set[str]:
    """The top `top_frac` of the ranked universe = our highest-conviction 'leaders'.
    These get a higher per-name cap and are always bought directly (never put-sold), so the
    engine never under-accumulates or caps the upside of the likely 10x names."""
    if top_frac <= 0 or not ranked:
        return set()
    n = max(1, int(round(len(ranked) * top_frac)))
    return {r.symbol for r in ranked[:n]}


def _cap_redistribute(weights: dict[str, float], budget: float,
                      caps: dict[str, float]) -> dict[str, float]:
    """Water-filling: allocate `budget` ∝ weights; any name exceeding its own cap (caps[s])
    is capped and its overflow redistributed to the rest. Leftover (if all capped) stays
    unallocated. `caps` is per-symbol so leaders can carry a higher cap than the rest."""
    syms = [s for s, w in weights.items() if w > 0]
    if not syms or budget <= 0:
        return {}
    alloc = {s: 0.0 for s in syms}
    capped: set[str] = set()
    for _ in range(len(syms) + 2):
        active = [s for s in syms if s not in capped]
        if not active:
            break
        budget_left = budget - sum(caps[s] for s in capped)
        if budget_left <= 0:
            break
        wsum = sum(weights[s] for s in active)
        if wsum <= 0:
            break
        prop = {s: budget_left * weights[s] / wsum for s in active}
        over = [s for s in active if prop[s] > caps[s]]
        if not over:
            for s in active:
                alloc[s] = prop[s]
            break
        capped.update(over)
    for s in capped:
        alloc[s] = caps[s]
    return alloc


def target_weights(ranked: list[RankedName], tier_budgets: dict[str, float],
                   investable: float, per_name_cap_pct: float = 0.06,
                   max_names: dict[str, int] | None = None,
                   leader_syms: set[str] | None = None,
                   leader_cap_pct: float | None = None,
                   conviction_power: float = 1.0) -> dict[str, float]:
    """Target dollars per name. Within each tier, target ∝ rank_score ** conviction_power,
    normalized to the tier's capital budget, capped per name. `conviction_power` > 1 concentrates
    the budget into the top-ranked names (up to the caps); 1.0 = the original near-flat weighting.
    Leaders (in `leader_syms`) carry the higher `leader_cap_pct`; everyone else is capped at
    `per_name_cap_pct`."""
    base_cap = per_name_cap_pct * investable
    lead_cap = (leader_cap_pct if leader_cap_pct is not None else per_name_cap_pct) * investable
    leaders = leader_syms or set()
    by_tier: dict[str, list[RankedName]] = {}
    for r in ranked:
        by_tier.setdefault(r.tier, []).append(r)

    targets: dict[str, float] = {}
    for tier, budget_frac in tier_budgets.items():
        members = by_tier.get(tier, [])
        if max_names and tier in max_names:
            members = members[:max_names[tier]]
        if not members:
            continue
        weights = {r.symbol: max(0.0, r.rank_score) ** conviction_power for r in members}
        caps = {r.symbol: (lead_cap if r.symbol in leaders else base_cap) for r in members}
        alloc = _cap_redistribute(weights, budget_frac * investable, caps)
        targets.update(alloc)
    return targets


# ── 3. Crash reserve (drawdown tranches) ─────────────────────
def reserve_update(state: ReserveState, market_price: float,
                   thresholds: tuple[float, ...] = (0.10, 0.20, 0.30)) -> tuple[ReserveState, float]:
    """Update trailing peak and fire tranches as drawdown crosses thresholds. Monotonic:
    tranches never un-fire (we don't re-lock dry powder once committed)."""
    peak = max(state.peak, market_price) if state.peak else market_price
    dd = (peak - market_price) / peak if peak > 0 else 0.0
    should_fire = sum(1 for t in thresholds if dd >= t)
    fired = max(state.tranches_fired, should_fire)
    return ReserveState(peak, fired), dd


def reserve_unlocked_fraction(fired: int, n_tranches: int = 3) -> float:
    return min(1.0, fired / n_tranches) if n_tranches > 0 else 0.0


def backstop_unlocked_fraction(days_since_start: int, start_days: int = 365,
                               bleed_days: int = 365) -> float:
    """Time-based backstop: if no crash tranche has fired by `start_days`, deploy the reserve
    slowly anyway, linearly over `bleed_days`, so the portfolio is never permanently
    under-invested in a long melt-up. Returns 0..1 (deployed via the patient base DCA, not the
    aggressive crash dump). Combine with the drawdown unlock via max()."""
    if bleed_days <= 0 or days_since_start <= start_days:
        return 0.0
    return min(1.0, (days_since_start - start_days) / bleed_days)


# ── 4. Fair-price signal + entry-mode choice ─────────────────
def fair_price_attractiveness(price: float, sma200: float | None,
                              high_52w: float | None) -> float:
    """Blend of (below own 200-day trend) and (pullback from 52-week high).
    Higher = relatively cheaper / better entry. 0 ≈ at trend / no pullback;
    positive = cheap; negative = extended above trend."""
    parts: list[float] = []
    if sma200 and sma200 > 0:
        parts.append(-(price / sma200 - 1.0))          # cheaper vs own trend → higher
    if high_52w and high_52w > 0:
        parts.append((high_52w - price) / high_52w)     # bigger pullback → higher
    if not parts:
        return 0.0
    return sum(parts) / len(parts)


def choose_entry_mode(attractiveness: float, underweight_frac: float, crash_active: bool,
                      direct_threshold: float = 0.0, urgent_underweight: float = 0.5,
                      is_leader: bool = False) -> str:
    """Direct buy when leader/urgent/relatively cheap/crash; put-sell (get paid to wait) only
    for non-leaders that are extended above fair price and not urgent. Leaders are always bought
    directly — never cap the upside of the names most likely to deliver the 10x."""
    if is_leader:
        return "direct"
    if crash_active:
        return "direct"
    if attractiveness >= direct_threshold:
        return "direct"
    if underweight_frac >= urgent_underweight:
        return "direct"
    return "put"


# ── 5. Daily deploy budget (base DCA + crash dump) ───────────
def build_signals_from_watchlist(rows, held: dict, nlv: float, cc, tier_alloc: dict) -> list[dict]:
    """Compute the dashboard signal table directly from watchlist DB rows (each with the
    fundamental scores + freshly-updated current_price/sma_200/high_52w/momentum_12_1).
    This makes the /watchlist + Portfolio views show the FULL ranked universe every page
    load, independent of whether a trading scan has persisted signals. `held` = symbol→
    current market value. `cc` = CompounderConfig; `tier_alloc` = {tier: budget fraction}."""
    names = []
    for w in rows:
        price = getattr(w, "current_price", None)
        if not price or price <= 0:
            continue
        tier = getattr(w, "tier", "growth") or "growth"
        names.append(NameInput(
            symbol=w.symbol, tier=(tier if tier in tier_alloc else "growth"),
            growth=getattr(w, "growth_score", 0) or 0.0,
            forward_growth=getattr(w, "forward_growth_score", 0) or 0.0,
            quality=getattr(w, "quality_score", 0) or 0.0,
            valuation=getattr(w, "valuation_score", 0) or 0.0,
            dividend_total_return=getattr(w, "dividend_total_return_score", 0) or 0.0,
            risk_penalty=getattr(w, "risk_total_penalty", 0) or 0.0,
            price=price, sma200=getattr(w, "sma_200", None),
            high_52w=getattr(w, "high_52w", None),
            momentum_12_1=getattr(w, "momentum_12_1", None),
        ))
    mom_known = {getattr(w, "symbol", None) for w in rows
                 if getattr(w, "momentum_12_1", None) is not None}
    ranked = rank_universe(names, cc.rank_fund_weight, cc.rank_mom_weight)
    rank_idx = {r.symbol: i + 1 for i, r in enumerate(ranked)}
    leaders = leader_symbols(ranked, getattr(cc, "leader_top_frac", 0.0))
    investable = max(0.0, nlv) * (1 - cc.cash_buffer_pct)
    targets = target_weights(ranked, tier_alloc, investable, cc.per_name_cap_pct,
                             leader_syms=leaders,
                             leader_cap_pct=getattr(cc, "leader_cap_pct", None),
                             conviction_power=getattr(cc, "conviction_power", 1.0))
    out = []
    for r in ranked:
        tgt = targets.get(r.symbol, 0.0)
        cur = held.get(r.symbol, 0.0)
        att = fair_price_attractiveness(r.price, r.sma200, r.high_52w)
        uw = (tgt - cur) / tgt if tgt > 0 else 0.0
        if tgt <= 0:
            action = "—"
        elif cur >= tgt * 0.98:
            action = "hold"
        elif att < 0:
            action = "wait"      # yellow — above fair price; skip until it's green
        else:
            action = "direct"    # green & underweight — buy in quality-rank order
        out.append({
            "symbol": r.symbol, "tier": r.tier, "rank": rank_idx[r.symbol],
            "rank_score": round(r.rank_score, 1), "s10x": round(r.s10x, 1),
            "mom_pct": (round(r.momentum_pct * 100, 0) if r.symbol in mom_known else None),
            "price": round(r.price, 2), "target": round(tgt), "current": round(cur),
            "underweight_pct": round(uw * 100, 0), "attractiveness": round(att, 3),
            "action": action,
        })
    return out


def daily_deploy_budget(investable: float, base_pct: float, dca_horizon_days: int,
                        unlocked_fraction: float, deployed: float, target_total: float,
                        crash_active: bool, free_cash: float) -> float:
    """How much new capital to put to work today.

    - Base tranche (base_pct of investable) is DCA'd over dca_horizon_days.
    - When a drawdown tranche is active, deploy the unlocked reserve fast (up to the full
      remaining gap to target) — that's the dry powder doing its job.
    Always bounded by the remaining gap to target and by free cash on hand.
    """
    remaining_gap = max(0.0, target_total - deployed)
    if crash_active:
        budget = remaining_gap
    else:
        base_pace = investable * base_pct / max(1, dca_horizon_days)
        budget = min(remaining_gap, base_pace)
    return max(0.0, min(budget, free_cash))


def single_buy_bounds(nlv: float, cc) -> tuple[float, float]:
    """NLV-scaled (min, max) per-name order size for the compounder.

    The portfolio account spans ~$50k → $11M+. Flat $5k/$100k bounds blocked all deployment below
    ~$4M (per-name targets stay under $5k), so each order is sized as a % of CURRENT NLV:
      eff_min = clamp(NLV * min_single_buy_pct, min_single_buy_floor, min_single_buy)  # 0.1%, in [$3k, $5k]
      eff_max = max(  NLV * max_single_buy_pct, eff_min)                               # 2%
    eff_min never drops below the HARD floor ($3,000 — no order is ever smaller; a name whose target gap
    is below it is skipped until its target grows with NLV) nor exceeds the cap ($5k, reached at ~$5M);
    eff_max scales freely. So below ~$150k NLV eff_min == eff_max == $3k (every order exactly the floor).
    """
    lo = max(min(nlv * cc.min_single_buy_pct, cc.min_single_buy), cc.min_single_buy_floor)
    hi = max(nlv * cc.max_single_buy_pct, lo)
    return lo, hi


def ladder_plan(core_price: float, urgency: float, is_leader: bool,
                cc) -> list[tuple[float, float]]:
    """Conviction-scaled DAY limit-ladder for one direct buy (pure).

    Returns a list of (limit_price, size_frac) rungs whose size_frac sum to the total
    bricks to deploy for this name:
      - The core rung is bid below the last price by a discount that scales INVERSELY with
        urgency: high-urgency names (underweight / leader / crash, urgency→1) bid near market
        so the position actually fills; low-urgency names (urgency→0) bid up to the max
        discount to lower cost (it is fine to miss). Core size_frac is 1.0.
      - Leaders (or, if ladder_leader_only_dips is False, every name) additionally get
        `ladder_rungs` dip-adder rungs stepped `ladder_step_pct` apart below the core, each
        sized `ladder_rung_frac` of the brick. These are *additive* — they raise the total
        deployed for the name above the core brick, and the caller funds them from reserve.

    urgency is clamped to [0, 1]. If ladder is disabled, returns a single rung at the base
    discount (preserving the legacy flat-under-bid behavior).
    """
    if core_price <= 0:
        return []
    u = max(0.0, min(1.0, urgency))
    base = cc.entry_base_discount_pct
    if not getattr(cc, "ladder_enabled", True):
        return [(round(core_price * (1 - base / 100.0), 2), 1.0)]

    span = max(0.0, cc.entry_max_discount_pct - base)
    core_disc = base + span * (1.0 - u)                       # high urgency → shallow
    core = round(core_price * (1 - core_disc / 100.0), 2)
    rungs: list[tuple[float, float]] = [(core, 1.0)]

    wants_dips = is_leader or not getattr(cc, "ladder_leader_only_dips", True)
    if wants_dips and cc.ladder_rungs > 0 and cc.ladder_rung_frac > 0:
        for k in range(1, cc.ladder_rungs + 1):
            price = round(core * (1 - k * cc.ladder_step_pct / 100.0), 2)
            if price > 0:
                rungs.append((price, cc.ladder_rung_frac))
    return rungs
