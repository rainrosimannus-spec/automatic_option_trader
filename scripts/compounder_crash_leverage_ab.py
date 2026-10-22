#!/usr/bin/env python
"""Crash LEVERAGE A/B: current (no borrowing) vs all-at-30% vs a depth ladder.

WHAT IT ANSWERS
  Rain: "if there's a real crash we borrow to buy — that's the only time to use leverage with
  wisdom." Today it borrows nothing, because the target is a share of a FALLING NLV: a fully
  invested book's holdings and its targets deflate together, so no gap opens and the 15% margin
  sleeve is never reached. Measured 2026-10-22: a -30% crash on a fully deployed book buys ~EUR 96k
  (1.2% of NLV) and leaves EUR 1.17M of margin untouched.

  THE FIX UNDER TEST — inflate the TARGET (never NLV itself; nlv is the denominator of both safety
  gates, so inflating it would make a 15% loan read as 13% and the hard stops fire late):

      live_invest = investable x (base_pct + (1-base_pct)*unlocked) x (1 + crash_leverage)

  ARMS
    current : crash_leverage = 0, no margin            <- reproduces live behaviour (self-check)
    all30   : 15% once dd >= 30%                       <- spend the sleeve at the first deep tranche
    ladder  : 7.5% at -30%, 11.25% at -40%, 15% at -50% <- stage it, keep powder for later legs

  The ladder exists because NO signal separates "crash that recovers" from "crash that keeps
  falling" — Oct 2008 had VIX 80 and fell another ~25%. What protects you is not spending it all at
  the first -30%.

HONEST CAVEATS (read cross-ARM deltas, not absolute levels)
  * Reuses the yellowfix/crashbuy data plumbing and the REAL src/portfolio/compounder.py pure
    functions, so it inherits their survivorship and static-fundamental look-ahead. Those inflate
    ABSOLUTE returns identically for every arm.
  * Models the LOAN gate faithfully (soft de-rate from 10%, hard block at 30% borrowed/NLV in a
    crash). Does NOT model the MAINTENANCE-cushion gate: a sim has no broker margin requirement.
    That gate is an emergency backstop that prices out near ~35% maint/NLV at full crash leverage,
    well under its 88% crash limit, so omitting it should not flatter the levered arms — but it is
    an omission, not a proof.
  * CHARGES INTEREST on the loan (--rate, default 5%/yr, daily on negative cash). Without it
    leverage looks free. The live IBKR EUR rate should be checked before trusting the level.
  * Universe is TODAY's watchlist priced back through history, so "what it buys" is the shape of
    the response, not a claim about which names.
"""
import argparse, json, os, sys
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.portfolio import compounder as cmp                      # noqa: E402
from src.portfolio.compounder import NameInput, ReserveState                # noqa: E402
from src.portfolio.config import CompounderConfig                # noqa: E402
import compounder_yellowfix_ab as yf                             # noqa: E402

CC = CompounderConfig()
TIER_BUDGETS = {"growth": CC.tier_growth, "breakthrough": CC.tier_breakthrough,
                "dividend": CC.tier_dividend}
START_CAPITAL = yf.START_CAPITAL
OUT = "data/compounder_crash_leverage_ab.jsonl"

# depth -> extra leverage on the TARGET. Keys are drawdown fractions, checked deepest-first.
# Arms are callables of (dd, days_since_peak). days_since_peak is the velocity axis: 30 years of SPY
# say it predicts recovery hard — trough within 60 trading days recovers in 32-46, trough after 150
# days takes 664. Every deep (>=30%) drawdown in 30y was SLOW except COVID; every rapid one was too
# shallow to trigger a depth ladder (1998 -19%/30d, 2025 tariff -19%/34d, 2024 -8%, 2026 -9%).
#
# small30 exists to BREAK A CONFOUND: `ladder` differs from `all30` both by having a second step AND
# by starting smaller (7.5 vs 15). If small30 matches ladder, the deeper step earns nothing and the
# whole effect is "take less at the first tier".
ARMS = {
    "off":       lambda dd, dsp: 0.0,
    "all30":     lambda dd, dsp: 0.150 if dd >= 0.30 else 0.0,
    "small30":   lambda dd, dsp: 0.075 if dd >= 0.30 else 0.0,
    "ladder":    lambda dd, dsp: 0.1125 if dd >= 0.40 else (0.075 if dd >= 0.30 else 0.0),
    "velo+ladd": lambda dd, dsp: (0.150 if (dd >= 0.18 and dsp <= 45) else
                                  (0.1125 if dd >= 0.40 else (0.075 if dd >= 0.30 else 0.0))),
    "velo_only": lambda dd, dsp: 0.150 if (dd >= 0.18 and dsp <= 45) else 0.0,
}
# Report buckets for "how much it buys at each crash depth".
BUCKETS = [(0.00, 0.10, "0-10%"), (0.10, 0.20, "-10 to -20"), (0.20, 0.30, "-20 to -30"),
           (0.30, 0.40, "-30 to -40"), (0.40, 0.50, "-40 to -50"), (0.50, 9.99, "below -50")]

WINDOWS = [
    ("covid_2020",     "2020-01-02", "2020-12-31"),   # -34% in 23 days, V-recovery
    ("bear_2022",      "2022-01-03", "2022-12-30"),   # slow -25% grind, never reaches -30%
    ("full_2020_2026", "2020-01-02", "2026-06-26"),   # everything compounds
]



# ── synthetic market paths ───────────────────────────────────────────────────────────────────────
# WHY: the cached universe starts 2019-01-02 and 0 of 126 names have pre-2009 history, so 2008 and
# 2000-02 are untestable. COVID is the only >=30% drawdown in the data and it spent TWO DAYS below
# -30% before V-recovering — the single most favourable environment for spending the whole sleeve at
# once, and it never exercises a ladder tier below -30%. These paths supply the missing shape: a
# market that falls past -30%, bounces, and falls FURTHER.
#
# METHOD, and its honest limit: SPY is forced onto the path exactly, and every name's OHLC is scaled
# by the SAME per-day factor f[i] = desired_SPY[i] / real_SPY[i]. So each name keeps its REAL
# idiosyncratic behaviour relative to the market (which names fall harder, sector rotation, dispersion)
# while the market index follows the imposed shape. What it is NOT: a claim that these names would
# have behaved this way in 2008. It tests the RESPONSE MECHANISM against a path shape, which is the
# thing the ladder-vs-all30 choice actually turns on.
# Each path is (trading_days, target_level) vertices, piecewise-linear, starting at 1.0.
SHAPES = {
    # control: COVID-like V. we already know all30 wins here.
    "v_covid":   [(0, 1.00), (23, 0.66), (140, 1.00), (240, 1.15)],
    # 2008-like: -20% grind, +10% bounce, then the real leg down to -50%, slow recovery.
    "bear_2008": [(0, 1.00), (190, 0.80), (230, 0.88), (350, 0.50), (600, 0.70), (1000, 1.00)],
    # 2000-02-like: three distinct legs to -49% over ~30 months, then a long climb.
    "bear_2000": [(0, 1.00), (130, 0.85), (180, 0.92), (350, 0.72), (410, 0.80),
                  (610, 0.51), (910, 0.75)],
    # 2025 tariff shock, from real SPY: -19.0% in 34 trading days, back to the peak in 55 more.
    "tariff_2025": [(0, 1.00), (34, 0.81), (89, 1.00), (200, 1.08)],
    # 2022, from real SPY: -25.4% over 195 days, recovered after 318 more. The slow trap the
    # velocity tier must NOT fire into.
    "bear_2022":   [(0, 1.00), (195, 0.746), (513, 1.00), (600, 1.03)],
}
# RECOVERY SENSITIVITY — the sign of the leverage result turns on how far the market comes back, which
# is the one thing unknowable when you borrow. Same multi-leg path to -49%, only the terminal level
# differs. These answer: "how complete does the recovery have to be for borrowing to have paid?"
for _r in (0.75, 0.85, 0.95, 1.05, 1.15):
    SHAPES[f"recov_{round(_r*100)}"] = [(0, 1.00), (130, 0.85), (180, 0.92), (350, 0.72),
                                      (410, 0.80), (610, 0.51), (910, _r)]


def synth_factor(shape, n_days):
    """Per-day multiplicative factor turning the real SPY series into the imposed path."""
    verts = SHAPES[shape]
    lvl = np.ones(n_days)
    for (d0, l0), (d1, l1) in zip(verts, verts[1:]):
        d1 = min(d1, n_days - 1)
        if d1 <= d0:
            continue
        lvl[d0:d1 + 1] = np.linspace(l0, l1, d1 - d0 + 1)
    if verts[-1][0] < n_days - 1:
        lvl[verts[-1][0]:] = verts[-1][1]
    return lvl


def apply_ai_bubble(dates, OP, HI, LO, CL, i0, i1, uni, ai_trough=0.40, rest_trough=0.90,
                    down_days=420, recover_days=900, recover_to=0.75):
    """SECTOR-SPECIFIC crash: AI/tech names collapse, the rest barely move, SPY lands in between.

    WHY THIS IS THE AWKWARD CASE. The crash gauge is SPY, but the book is 56.6% Technology-or-
    breakthrough — about 1.7x as AI-exposed as the index. So an AI de-rating takes the BOOK down far
    more than the GAUGE, and the leverage tiers (which read SPY) under-fire exactly when the book is
    hurt most. Dot-com is the precedent: Nasdaq -78%, SPY -49%, and the Nasdaq took 15 years back.
    Default recover_to=0.75 encodes "structural de-rating, not a dip".
    """
    ai = {u["symbol"] for u in uni
          if u.get("sector") == "Technology" or u.get("tier") == "breakthrough"}
    n = i1 - i0 + 1
    def path(trough):
        lvl = np.ones(n)
        d1 = min(down_days, n - 1)
        lvl[0:d1 + 1] = np.linspace(1.0, trough, d1 + 1)
        d2 = min(d1 + recover_days, n - 1)
        if d2 > d1:
            lvl[d1:d2 + 1] = np.linspace(trough, trough + (recover_to - trough), d2 - d1 + 1)
        lvl[d2:] = lvl[d2]
        return lvl
    lvl_ai, lvl_rest = path(ai_trough), path(rest_trough)
    # SPY is the cap-weighted blend; ~34% of the index is the AI-exposed sleeve.
    lvl_spy = 0.34 * lvl_ai + 0.66 * lvl_rest
    OP2, HI2, LO2, CL2 = {}, {}, {}, {}
    for sym in CL:
        tgt = lvl_spy if sym == "SPY" else (lvl_ai if sym in ai else lvl_rest)
        real = CL[sym][i0:i0 + n]
        base = real[~np.isnan(real)][0] if np.any(~np.isnan(real)) else 1.0
        f = np.where(real > 0, (base * tgt) / real, 1.0)
        for src, dst in ((OP, OP2), (HI, HI2), (LO, LO2), (CL, CL2)):
            a = src[sym].copy(); a[i0:i0 + n] = a[i0:i0 + n] * f; dst[sym] = a
    return OP2, HI2, LO2, CL2, lvl_spy, lvl_ai


def apply_synthetic(shape, dates, OP, HI, LO, CL, i0, i1):
    """Scale every name's OHLC from i0 onward so SPY traces `shape`. Warmup (<i0) untouched, so the
    252-day SMA/52w-high at i0 are real and the path starts from a genuine peak."""
    n = i1 - i0 + 1
    lvl = synth_factor(shape, n)
    spy_real = CL["SPY"][i0:i0 + n]
    base = spy_real[0]
    f = np.where(spy_real > 0, (base * lvl) / spy_real, 1.0)
    OP2, HI2, LO2, CL2 = {}, {}, {}, {}
    for sym in CL:
        for src, dst in ((OP, OP2), (HI, HI2), (LO, LO2), (CL, CL2)):
            a = src[sym].copy()
            a[i0:i0 + n] = a[i0:i0 + n] * f
            dst[sym] = a
    return OP2, HI2, LO2, CL2, lvl



def pick_peak_start(CL, lo, hi):
    """First index in [lo,hi] where SPY sits at its own 252-day high — so an imposed path begins
    from a real peak and the gauge's drawdown equals the path's, not the path plus whatever the
    market was already doing. Falls back to the least-drawn-down index in range."""
    spy = CL["SPY"]
    best, best_ratio = lo, -1.0
    for i in range(lo, min(hi, len(spy) - 1)):
        w = spy[i - 251:i + 1]
        if len(w) < 252 or np.isnan(w).any():
            continue
        ratio = spy[i] / np.nanmax(w)
        if ratio >= 0.999:
            return i
        if ratio > best_ratio:
            best, best_ratio = i, ratio
    return best


def leverage_for(dd, schedule):
    for thresh, lev in schedule:
        if dd >= thresh:
            return lev
    return 0.0


def bucket_of(dd):
    for lo, hi, name in BUCKETS:
        if lo <= dd < hi:
            return name
    return BUCKETS[-1][2]


def simulate(uni, dates, OP, HI, LO, CL, i0, i1, schedule, rate, preinvested=False):
    syms = [u["symbol"] for u in uni]
    sector = {u["symbol"]: u["sector"] for u in uni}
    tier = {u["symbol"]: u["tier"] for u in uni}
    ninput = {u["symbol"]: u for u in uni}
    shares = {s: 0.0 for s in syms}
    cash = START_CAPITAL                      # goes NEGATIVE when borrowing — that is the loan
    rstate = ReserveState(peak=CL["SPY"][i0], tranches_fired=0)
    prem, maxd = CC.entry_marketable_premium_pct, CC.entry_max_discount_pct
    by_bucket, peak_loan, interest_paid, blocked_days, derate_days = {}, 0.0, 0.0, 0, 0
    pending, mv_series, rstate_dd, pk_idx = [], [], 0.0, i0

    if preinvested:
        # THE SCENARIO RAIN ASKED ABOUT: enter the window fully invested with NO dry powder, so the
        # crash has to be met with borrowing rather than with cash that happens to be lying around.
        # Without this the book is still DCA-ing at the crash and funds everything from cash — the
        # margin leg never engages and the A/B measures nothing (verified: peak loan 0.00M in all arms).
        names0 = []
        for s_ in syms:
            px0 = CL[s_][i0]
            if np.isnan(px0):
                continue
            win0 = CL[s_][i0 - 251:i0 + 1]
            if len(win0) < 252 or np.isnan(win0).any():
                continue
            u0 = ninput[s_]
            pp = CL[s_][i0 - 252] if i0 >= 252 else np.nan
            names0.append(NameInput(symbol=s_, tier=tier[s_], growth=u0["growth"],
                forward_growth=u0["forward_growth"], quality=u0["quality"], valuation=u0["valuation"],
                dividend_total_return=u0["dividend_total_return"], risk_penalty=u0["risk_penalty"],
                price=px0, sma200=float(np.mean(CL[s_][i0 - 199:i0 + 1])),
                high_52w=float(np.nanmax(win0)),
                momentum_12_1=((px0 / pp - 1.0) if (not np.isnan(pp) and pp > 0) else None)))
        rk0 = cmp.rank_universe(names0, CC.rank_fund_weight, CC.rank_mom_weight)
        ld0 = cmp.leader_symbols(rk0, CC.leader_top_frac)
        inv0 = START_CAPITAL * (1 - CC.cash_buffer_pct)
        t0 = cmp.target_weights(rk0, TIER_BUDGETS, inv0, CC.per_name_cap_pct, leader_syms=ld0,
                                leader_cap_pct=CC.leader_cap_pct,
                                conviction_power=CC.conviction_power,
                                abs_ceiling=CC.per_name_abs_ceiling)
        t0 = cmp.apply_sector_caps(t0, sector, CC.sector_cap_pct * START_CAPITAL)
        for s_, amt in t0.items():
            px0 = CL[s_][i0]
            if np.isnan(px0) or px0 <= 0:
                continue
            q0 = int(amt // px0)
            if q0 > 0:
                shares[s_] += q0; cash -= q0 * px0

    for d in range(i0, i1):
        # ── fills of yesterday's limits ───────────────────────────────────────
        still = []
        for s, lim, qty in pending:
            lo = LO[s][d]
            if not np.isnan(lo) and lo <= lim:
                shares[s] += qty; cash -= qty * lim
                by_bucket[bucket_of(rstate_dd)] = by_bucket.get(bucket_of(rstate_dd), 0.0) + qty * lim
            else:
                still.append((s, lim, qty))
        pending = still

        spy_c = CL["SPY"][d]
        if np.isnan(spy_c):
            continue
        prev_peak = rstate.peak
        rstate, dd = cmp.reserve_update(rstate, spy_c, tuple(CC.drawdown_tranches))
        if rstate.peak > prev_peak:
            pk_idx = d
        rstate_dd = dd
        unlocked = cmp.reserve_unlocked_fraction(rstate.tranches_fired, len(CC.drawdown_tranches))
        crash_active = dd >= CC.drawdown_tranches[0]

        # ── daily interest on the loan ────────────────────────────────────────
        if cash < 0:
            chg = -cash * rate / 252.0
            cash -= chg; interest_paid += chg
            peak_loan = max(peak_loan, -cash)

        # Build the day's ranked universe exactly as the crashbuy harness does (252d warmup).
        names = []
        for s in syms:
            px = CL[s][d]
            if np.isnan(px):
                continue
            win = CL[s][d - 251:d + 1]
            if len(win) < 252 or np.isnan(win).any():
                continue
            u = ninput[s]
            p0 = CL[s][d - 252] if d >= 252 else np.nan
            names.append(NameInput(symbol=s, tier=tier[s], growth=u["growth"],
                forward_growth=u["forward_growth"], quality=u["quality"], valuation=u["valuation"],
                dividend_total_return=u["dividend_total_return"], risk_penalty=u["risk_penalty"],
                price=px, sma200=float(np.mean(CL[s][d - 199:d + 1])), high_52w=float(np.nanmax(win)),
                momentum_12_1=((px / p0 - 1.0) if (not np.isnan(p0) and p0 > 0) else None)))
        if not names:
            continue
        ranked = cmp.rank_universe(names, CC.rank_fund_weight, CC.rank_mom_weight)
        price = {r.symbol: r.price for r in ranked}
        leaders = cmp.leader_symbols(ranked, CC.leader_top_frac)

        nlv = cash + sum(shares[s] * price[s] for s in syms if s in price)
        mv_series.append(nlv)
        if nlv <= 0:
            break
        investable = nlv * (1 - CC.cash_buffer_pct)
        lev = schedule(dd, d - pk_idx)
        live_invest = investable * (CC.base_pct + (1 - CC.base_pct) * unlocked) * (1.0 + lev)

        targets = cmp.target_weights(ranked, TIER_BUDGETS, live_invest, CC.per_name_cap_pct,
                                     leader_syms=leaders, leader_cap_pct=CC.leader_cap_pct,
                                     conviction_power=CC.conviction_power,
                                     abs_ceiling=CC.per_name_abs_ceiling)
        targets = cmp.apply_sector_caps(targets, sector, CC.sector_cap_pct * nlv)
        target_total = sum(targets.values())
        deployed = sum(shares[s] * price[s] for s in syms if s in price)

        # ── funding: cash above buffer + the crash margin sleeve, loan-gated ──
        borrowed = max(0.0, -cash)
        loan_pct = borrowed / nlv * 100.0
        loan_limit = CC.loan_hard_limit_crash_pct if lev > 0 else CC.loan_hard_limit_pct
        if loan_pct > loan_limit:
            blocked_days += 1
            continue
        soft = cmp.leverage_derate(loan_pct, CC.loan_soft_floor_pct, loan_limit)
        if soft < 1.0:
            derate_days += 1
        margin_room = max(0.0, lev * nlv - borrowed) if lev > 0 else 0.0
        free_cash = (max(0.0, cash - nlv * CC.cash_buffer_pct) + margin_room) * soft

        budget = cmp.daily_deploy_budget(investable, CC.base_pct, CC.dca_horizon_days, unlocked,
                    deployed, target_total, crash_active, free_cash, deployed_today=0.0,
                    lump_horizon_days=CC.lump_horizon_days, pace_throttle=1.0)
        min_buy, max_buy = cmp.single_buy_bounds(nlv, CC)
        remaining_gap = max(0.0, target_total - deployed)
        if not crash_active and budget < min_buy and remaining_gap >= min_buy and free_cash >= min_buy:
            budget = max(budget, min(remaining_gap, free_cash, min_buy))

        q = []
        for r in ranked:
            tgt = targets.get(r.symbol, 0.0)
            if tgt <= 0:
                continue
            cur = shares[r.symbol] * price[r.symbol]
            if cur >= tgt * 0.98:
                continue
            att = cmp.fair_price_attractiveness(r.price, r.sma200, r.high_52w)
            q.append((att, tgt - cur, r.symbol, tgt, cur))
        q.sort(key=lambda z: (0 if z[0] >= 0 else 1, -z[1]))

        spent = 0.0
        for att, gap, s, tgt, cur in q:
            if budget - spent < min_buy:
                break
            brick = min(max_buy, gap, budget - spent)
            if brick < min_buy:
                continue
            mkt = price[s]
            uw = (tgt - cur) / tgt if tgt > 0 else 0.0
            urgency = max(uw if att >= 0 else 0.0, 1.0 if s in leaders else 0.0,
                          1.0 if crash_active else 0.0)
            lim = mkt * (1.0 + prem * urgency) if urgency > 0 else mkt * (1.0 - maxd)
            qty = int(brick // lim)
            if qty <= 0:
                continue
            pending.append((s, lim, qty)); spent += qty * lim
        pending = pending[-400:]

    final_nlv = mv_series[-1] if mv_series else START_CAPITAL
    arr = np.array(mv_series) if mv_series else np.array([START_CAPITAL])
    dd_max = float(((np.maximum.accumulate(arr) - arr) / np.maximum.accumulate(arr)).max() * 100)
    return {"final": final_nlv, "ret_pct": (final_nlv / START_CAPITAL - 1) * 100,
            "max_dd_pct": dd_max, "peak_loan": peak_loan, "interest": interest_paid,
            "by_bucket": by_bucket, "blocked_days": blocked_days, "derate_days": derate_days}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=0.05, help="annual margin interest on the loan")
    ap.add_argument("--synthetic", choices=sorted(SHAPES), nargs="*",
                    help="run imposed market paths instead of the real windows")
    ap.add_argument("--ai-bubble", action="store_true",
                    help="sector-specific AI de-rating: AI names -60%, rest -10%, SPY in between")
    ap.add_argument("--preinvested", action="store_true",
                    help="enter each window FULLY INVESTED (no dry powder) — the scenario where "
                         "borrowing is the only funding left")
    a = ap.parse_args()
    uni = yf.load_universe()
    cache = yf.fetch_ohlc([u["symbol"] for u in uni])
    dates, OP, HI, LO, CL = yf.build_matrix(cache, [u["symbol"] for u in uni])
    didx = {d: i for i, d in enumerate(dates)}

    print(f"\nmode: {'FULLY INVESTED at window start (no dry powder)' if a.preinvested else 'all-cash start, DCA in'}")
    print(f"margin interest charged at {a.rate:.1%}/yr on the loan "
          f"| START {START_CAPITAL:,.0f} | {len(uni)} names\n")
    rows = []
    if a.ai_bubble:
        i0 = pick_peak_start(CL, 260, 520); i1 = min(len(dates) - 2, i0 + 1340)
        O2, H2, L2, C2, lspy, lai = apply_ai_bubble(dates, OP, HI, LO, CL, i0, i1, uni)
        print("=" * 104)
        print(f"  AI BUBBLE BURST   AI names trough -{(1-lai.min())*100:.0f}%   "
              f"SPY trough -{(1-lspy.min())*100:.0f}%   "
              f"SPY days below -30%: {int((lspy<=0.70).sum())}   book is 56.6% AI-exposed")
        print("=" * 104)
        hdr = f"  {'arm':<9}{'return':>9}{'max DD':>9}{'peak loan':>12}{'interest':>11}   " + \
              "".join(f"{b[2]:>13}" for b in BUCKETS)
        print(hdr); print("  " + "-" * (len(hdr) - 2))
        for arm, sched in ARMS.items():
            r = simulate(uni, dates, O2, H2, L2, C2, i0, i1, sched, a.rate, a.preinvested)
            cells = "".join(f"{r['by_bucket'].get(b[2], 0.0)/1e6:>12.2f}M" for b in BUCKETS)
            print(f"  {arm:<9}{r['ret_pct']:>8.1f}%{r['max_dd_pct']:>8.1f}%"
                  f"{r['peak_loan']/1e6:>11.2f}M{r['interest']/1e3:>10.0f}k   {cells}")
        print()
        return
    if a.synthetic:
        i0 = pick_peak_start(CL, 260, 520)   # a genuine 252-day high, after the warmup
        for shape in a.synthetic:
            n = len(SHAPES[shape][-1:][0]) and SHAPES[shape][-1][0] + 1
            i1 = min(len(dates) - 2, i0 + n)
            O2, H2, L2, C2, lvl = apply_synthetic(shape, dates, OP, HI, LO, CL, i0, i1)
            trough = (1.0 - lvl.min()) * 100
            d30 = int((lvl <= 0.70).sum()); d40 = int((lvl <= 0.60).sum()); d50 = int((lvl <= 0.50).sum())
            print("=" * 104)
            print(f"  SYNTHETIC {shape}   {i1-i0} trading days   trough -{trough:.0f}%   "
                  f"days below -30%: {d30}  -40%: {d40}  -50%: {d50}")
            print("=" * 104)
            hdr = f"  {'arm':<9}{'return':>9}{'max DD':>9}{'peak loan':>12}{'interest':>11}   " + \
                  "".join(f"{b[2]:>13}" for b in BUCKETS)
            print(hdr); print("  " + "-" * (len(hdr) - 2))
            for arm, sched in ARMS.items():
                r = simulate(uni, dates, O2, H2, L2, C2, i0, i1, sched, a.rate, a.preinvested)
                cells = "".join(f"{r['by_bucket'].get(b[2], 0.0)/1e6:>12.2f}M" for b in BUCKETS)
                print(f"  {arm:<9}{r['ret_pct']:>8.1f}%{r['max_dd_pct']:>8.1f}%"
                      f"{r['peak_loan']/1e6:>11.2f}M{r['interest']/1e3:>10.0f}k   {cells}")
                rows.append({"window": f"synth_{shape}", "arm": arm,
                             **{k: v for k, v in r.items() if k != "by_bucket"},
                             "by_bucket": r["by_bucket"], "rate": a.rate})
            print()
        with open(OUT, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"  buckets = EUR bought while SPY was that far below its peak. wrote {OUT}")
        return
    for wname, s, e in WINDOWS:
        i0 = next((didx[d] for d in dates if d >= s), None)
        i1 = next((didx[d] for d in dates if d >= e), len(dates) - 1)
        if i0 is None or i0 < 252:
            i0 = max(252, i0 or 252)
        print("=" * 104)
        print(f"  {wname}   {dates[i0]} -> {dates[i1]}")
        print("=" * 104)
        hdr = f"  {'arm':<9}{'return':>9}{'max DD':>9}{'peak loan':>12}{'interest':>11}   " + \
              "".join(f"{b[2]:>13}" for b in BUCKETS)
        print(hdr); print("  " + "-" * (len(hdr) - 2))
        for arm, sched in ARMS.items():
            r = simulate(uni, dates, OP, HI, LO, CL, i0, i1, sched, a.rate, a.preinvested)
            cells = "".join(f"{r['by_bucket'].get(b[2], 0.0)/1e6:>12.2f}M" for b in BUCKETS)
            print(f"  {arm:<9}{r['ret_pct']:>8.1f}%{r['max_dd_pct']:>8.1f}%"
                  f"{r['peak_loan']/1e6:>11.2f}M{r['interest']/1e3:>10.0f}k   {cells}")
            rows.append({"window": wname, "arm": arm, **{k: v for k, v in r.items() if k != "by_bucket"},
                         "by_bucket": r["by_bucket"], "rate": a.rate})
        print()
    with open(OUT, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"  buckets = EUR bought while SPY was that far below its peak. wrote {OUT}")


if __name__ == "__main__":
    main()
