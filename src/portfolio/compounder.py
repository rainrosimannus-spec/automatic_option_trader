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
def _cap_redistribute(weights: dict[str, float], budget: float, cap: float) -> dict[str, float]:
    """Water-filling: allocate `budget` ∝ weights; any name exceeding `cap` is capped
    and its overflow redistributed to the rest. Leftover (if all capped) stays unallocated."""
    syms = [s for s, w in weights.items() if w > 0]
    if not syms or budget <= 0:
        return {}
    alloc = {s: 0.0 for s in syms}
    capped: set[str] = set()
    for _ in range(len(syms) + 2):
        active = [s for s in syms if s not in capped]
        if not active:
            break
        budget_left = budget - cap * len(capped)
        if budget_left <= 0:
            break
        wsum = sum(weights[s] for s in active)
        if wsum <= 0:
            break
        prop = {s: budget_left * weights[s] / wsum for s in active}
        over = [s for s in active if prop[s] > cap]
        if not over:
            for s in active:
                alloc[s] = prop[s]
            break
        capped.update(over)
    for s in capped:
        alloc[s] = cap
    return alloc


def target_weights(ranked: list[RankedName], tier_budgets: dict[str, float],
                   investable: float, per_name_cap_pct: float = 0.06,
                   max_names: dict[str, int] | None = None) -> dict[str, float]:
    """Target dollars per name. Within each tier, target ∝ rank_score, normalized to the
    tier's capital budget, capped at per_name_cap_pct of the whole portfolio."""
    cap_dollars = per_name_cap_pct * investable
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
        weights = {r.symbol: max(0.0, r.rank_score) for r in members}
        alloc = _cap_redistribute(weights, budget_frac * investable, cap_dollars)
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
                      direct_threshold: float = 0.0, urgent_underweight: float = 0.5) -> str:
    """Direct buy when urgent/relatively cheap/crash; put-sell (get paid to wait) when the
    name is extended above fair price and not urgent."""
    if crash_active:
        return "direct"
    if attractiveness >= direct_threshold:
        return "direct"
    if underweight_frac >= urgent_underweight:
        return "direct"
    return "put"


# ── 5. Daily deploy budget (base DCA + crash dump) ───────────
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
