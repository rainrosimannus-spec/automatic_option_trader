#!/usr/bin/env python
"""Compounder entry-TIMING A/B — buy at the OPEN (current) vs wait for the day and buy at the CLOSE.

THE QUESTION (Rain, 2026-07-15, prompted by today's CIEN buy)
  The live compounder fires on the first ~2h scan after the open and buys immediately at the prevailing
  price. Today it bought CIEN @ ~426 ~43 min after the open on a gap-down day; CIEN then slid to ~412.
  Is there an edge to WAITING each day until the session has picked a direction before buying — either
  (a) so the ranking can switch to a name whose discount survived the day, or (b) to pay a better price?

WHAT IT TESTS  (decision pipeline held IDENTICAL; ONLY the intraday execution point varies)
  open    : rank on close[t-1], fill at OPEN[t].          <- proxy for current live (buys early)
  defer   : SAME decision as `open` (close[t-1]),         <- isolates the pure price-timing effect (b):
            fill at CLOSE[t].                                 same names/notional, executed late instead.
  rerank  : rank on close[t] (uses today's move),         <- the full "wait for direction" proposal (a+b):
            fill at CLOSE[t].                                 today's action re-ranks AND re-prices entry.
  Reads:  open->defer = pure price timing ; defer->rerank = name-switch effect ; open->rerank = full.

FIDELITY  (same as compounder_yellowfix_ab.py — the trusted harness this is forked from)
  * Drives the REAL pure functions from src/portfolio/compounder.py and the REAL CompounderConfig.
  * Universe = LIVE portfolio_watchlist DB (USD names) with their real fundamental sub-scores.
  * Prices = true OHLC from FMP (data/compounder_ohlc_cache.json). Weekly-equivalent per-day pace.
  * €12M lump on day 0 (FX-neutral USD book, per Rain), no contributions. Strict-fair buy cap (baseline).

HONEST CAVEATS  (relative comparator — read the CROSS-VARIANT deltas, not absolute levels)
  * Survivorship + fundamental look-ahead: universe/sub-scores are TODAY's snapshot on past prices.
    Applied identically to all three variants, so the timing delta is apples-to-apples.
  * `rerank` decides ~at the close and fills at that same close (a ~15-min real-world approximation, not
    look-ahead). `defer`/`open` fill only if the day traded to the (marketable) limit; on filled orders
    the fill set is identical across open/defer so open-vs-defer is a clean price swap.
  * Foreign names excluded (FX-neutral). Dip-adder rungs omitted (core rung only); no dividends/fees.
"""
import sys, os, json, time, sqlite3
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.portfolio import compounder as cmp
from src.portfolio.compounder import NameInput, ReserveState
from src.portfolio.config import CompounderConfig

OHLC_CACHE = "data/compounder_ohlc_cache.json"
OUT = "data/compounder_entry_timing_ab.jsonl"
START_CAPITAL = 12_000_000.0     # EUR treated as USD book (FX-neutral)

CC = CompounderConfig()
TIER_BUDGETS = {"growth": CC.tier_growth, "breakthrough": CC.tier_breakthrough, "dividend": CC.tier_dividend}

WINDOWS = [
    ("melt_up_2025H2", "2025-12-08", "2026-06-26"),
    ("chop_2022",      "2022-01-03", "2022-08-01"),
    ("recovery_2024",  "2024-01-02", "2024-07-19"),
    ("full_2020_2026", "2020-01-02", "2026-06-26"),   # ~6.5y
    ("bull_2020_2023", "2020-06-01", "2023-06-01"),   # 3y
]
VARIANTS = ("open", "defer", "rerank")


def load_universe():
    con = sqlite3.connect("data/trades.db"); con.row_factory = sqlite3.Row
    rows = con.execute("""SELECT symbol,tier,sector,currency,growth_score,forward_growth_score,
        quality_score,valuation_score,dividend_total_return_score,risk_total_penalty
        FROM portfolio_watchlist""").fetchall()
    con.close()
    uni = []
    for r in rows:
        if (r["currency"] or "USD") != "USD":
            continue
        if r["symbol"] in ("XEON", "SGOV", "XFFE"):   # cash-park ETFs, never a compounder buy
            continue
        tier = r["tier"] if r["tier"] in TIER_BUDGETS else "growth"
        uni.append({
            "symbol": r["symbol"], "tier": tier, "sector": r["sector"] or "",
            "growth": r["growth_score"] or 0.0, "forward_growth": r["forward_growth_score"] or 0.0,
            "quality": r["quality_score"] or 0.0, "valuation": r["valuation_score"] or 0.0,
            "dividend_total_return": r["dividend_total_return_score"] or 0.0,
            "risk_penalty": r["risk_total_penalty"] or 0.0,
        })
    return uni


def build_matrix(cache, symbols):
    spy = cache.get("SPY", {})
    dates = sorted(spy.keys())
    if not dates:
        raise SystemExit("no SPY OHLC")
    di = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    OP, HI, LO, CL = {}, {}, {}, {}
    for sym in symbols + ["SPY"]:
        s = cache.get(sym, {})
        o = np.full(n, np.nan); h = np.full(n, np.nan); l = np.full(n, np.nan); c = np.full(n, np.nan)
        cc_ = np.nan
        for d, i in di.items():
            if d in s:
                o[i], h[i], l[i], c[i] = s[d]
                cc_ = s[d][3]
            else:
                c[i] = cc_   # forward-fill close only (for MTM); leave o/h/l NaN on non-trading days
        OP[sym], HI[sym], LO[sym], CL[sym] = o, h, l, c
    return dates, OP, HI, LO, CL


def _rank_day(uni, syms, tier, ninput, CL, d):
    """Build the ranked universe from data THROUGH close[d]. Returns (ranked, leaders) or (None, None)."""
    names = []
    for s in syms:
        px = CL[s][d]
        if np.isnan(px) or px <= 0:
            continue
        win = CL[s][d - 251:d + 1]
        if len(win) < 252 or np.isnan(win).any():   # require full 252d history
            continue
        sma200 = float(np.mean(CL[s][d - 199:d + 1]))
        high52 = float(np.nanmax(win))
        p0 = CL[s][d - 252] if d >= 252 else np.nan
        mom = (px / p0 - 1.0) if (not np.isnan(p0) and p0 > 0) else None
        u = ninput[s]
        names.append(NameInput(symbol=s, tier=tier[s], growth=u["growth"],
            forward_growth=u["forward_growth"], quality=u["quality"], valuation=u["valuation"],
            dividend_total_return=u["dividend_total_return"], risk_penalty=u["risk_penalty"],
            price=px, sma200=sma200, high_52w=high52, momentum_12_1=mom))
    if not names:
        return None, None
    ranked = cmp.rank_universe(names, CC.rank_fund_weight, CC.rank_mom_weight)
    leaders = cmp.leader_symbols(ranked, CC.leader_top_frac)
    return ranked, leaders


def simulate(uni, dates, OP, HI, LO, CL, i0, i1, variant):
    """variant in {'open','defer','rerank'}.
       open   : decide d=t-1, fill @ open[t]
       defer  : decide d=t-1, fill @ close[t]   (same orders as open, later fill)
       rerank : decide d=t,   fill @ close[t]   (today's info re-ranks + re-prices)"""
    syms = [u["symbol"] for u in uni]
    tier = {u["symbol"]: u["tier"] for u in uni}
    sector = {u["symbol"]: u["sector"] for u in uni}
    ninput = {u["symbol"]: u for u in uni}

    shares = {s: 0.0 for s in syms}
    cash = START_CAPITAL
    rstate = ReserveState(peak=CL["SPY"][i0 - 1] if i0 > 0 else CL["SPY"][i0], tranches_fired=0)

    prem = CC.entry_marketable_premium_pct
    maxd = CC.entry_max_discount_pct
    mv_series = []
    fill_vs_close = []   # fill_px / close[t]  — <1 means paid below the day's close
    fill_vs_open = []    # fill_px / open[t]

    for t in range(i0, i1 + 1):
        d = t if variant == "rerank" else t - 1     # decision-data index (no look-ahead: rerank fills at close[d]=close[t])

        ranked, leaders = _rank_day(uni, syms, tier, ninput, CL, d)
        if ranked is None:
            mv_series.append(cash + sum(shares[s] * CL[s][t] for s in syms if not np.isnan(CL[s][t])))
            continue
        price = {r.symbol: r.price for r in ranked}     # = close[d]
        s200 = {r.symbol: r.sma200 for r in ranked}
        h52 = {r.symbol: r.high_52w for r in ranked}

        # crash / reserve + pace throttle from SPY, all through close[d]
        rstate, dd = cmp.reserve_update(rstate, CL["SPY"][d], tuple(CC.drawdown_tranches))
        unlocked = cmp.reserve_unlocked_fraction(rstate.tranches_fired, len(CC.drawdown_tranches))
        crash_active = dd >= CC.drawdown_tranches[0]
        spy_sma200 = float(np.nanmean(CL["SPY"][d - 199:d + 1]))
        spy_ext = (CL["SPY"][d] / spy_sma200 - 1.0) * 100.0 if spy_sma200 > 0 else 0.0
        pace_throttle = 1.0
        if spy_ext > CC.deploy_throttle_start_pct:
            span = max(0.1, CC.deploy_throttle_full_pct - CC.deploy_throttle_start_pct)
            frac = min(1.0, (spy_ext - CC.deploy_throttle_start_pct) / span)
            pace_throttle = max(CC.deploy_throttle_floor, 1.0 - frac * (1.0 - CC.deploy_throttle_floor))

        nlv = cash + sum(shares[s] * CL[s][d] for s in syms if not np.isnan(CL[s][d]))
        investable = nlv * (1 - CC.cash_buffer_pct)
        live_invest = investable * (CC.base_pct + (1 - CC.base_pct) * unlocked)
        targets = cmp.target_weights(ranked, TIER_BUDGETS, live_invest, CC.per_name_cap_pct,
                    leader_syms=leaders, leader_cap_pct=CC.leader_cap_pct,
                    conviction_power=CC.conviction_power, abs_ceiling=CC.per_name_abs_ceiling)
        targets = cmp.apply_sector_caps(targets, sector, CC.sector_cap_pct * nlv)
        target_total = sum(targets.values())

        deployed = sum(shares[s] * price[s] for s in syms if s in price)
        free_cash = max(0.0, cash - nlv * CC.cash_buffer_pct)
        budget = cmp.daily_deploy_budget(investable, CC.base_pct, CC.dca_horizon_days, unlocked,
                    deployed, target_total, crash_active, free_cash, deployed_today=0.0,
                    lump_horizon_days=CC.lump_horizon_days, pace_throttle=pace_throttle)
        min_buy, max_buy = cmp.single_buy_bounds(nlv, CC)
        remaining_gap = max(0.0, target_total - deployed)
        if not crash_active and budget < min_buy and remaining_gap >= min_buy and free_cash >= min_buy:
            bp = cmp.base_daily_pace(investable, CC.base_pct, CC.dca_horizon_days, remaining_gap, 0.0,
                                     CC.lump_horizon_days, pace_throttle)
            if bp > 0:
                budget = max(budget, min(remaining_gap, free_cash, min_buy))
        armed_days = t - i0
        burn_cap = cmp.burn_in_ceiling(armed_days, CC.burn_in_ramp_days, CC.burn_in_floor, investable)
        if burn_cap > 0:
            budget = min(budget, max(0.0, burn_cap - deployed))

        # build queue: underweight, priceable; green (att>=0) before yellow, biggest-gap first
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
        q.sort(key=lambda x: (0 if x[0] >= 0 else 1, -x[1]))

        spent = 0.0
        for att, gap, s, tgt, cur in q:
            if budget - spent < min_buy:
                break
            brick = min(max_buy, gap, budget - spent)
            if brick < min_buy:
                continue
            mkt = price[s]                                     # close[d]
            uw = (tgt - cur) / tgt if tgt > 0 else 0.0
            urgency = max(uw if att >= 0 else 0.0, 1.0 if s in leaders else 0.0,
                          1.0 if crash_active else 0.0)
            u = max(0.0, min(1.0, urgency))
            core_pct = prem * u - maxd * (1.0 - u)
            core_raw = mkt * (1.0 + core_pct / 100.0)
            fair = cmp.fair_value_price(s200[s], h52[s])
            limit = core_raw if fair is None else min(core_raw, fair)   # baseline strict-fair cap

            # ---- execution: the ONLY thing that differs across variants ----
            op, lo, cl = OP[s][t], LO[s][t], CL[s][t]
            if np.isnan(cl):
                continue
            if variant == "open":
                if np.isnan(op) or np.isnan(lo) or lo > limit:
                    continue                                  # day never traded to the limit
                fill_px = min(limit, op)
            elif variant == "defer":
                if np.isnan(op) or np.isnan(lo) or lo > limit:
                    continue                                  # identical fill-set to `open`
                fill_px = min(limit, cl)                      # ...but pay the close
            else:  # rerank — decided at close[d]=close[t]; marketable-at-close
                if cl > limit:
                    continue
                fill_px = cl
            qty = brick / fill_px
            shares[s] += qty
            cash -= brick
            spent += brick
            fill_vs_close.append(fill_px / cl)
            if not np.isnan(op) and op > 0:
                fill_vs_open.append(fill_px / op)

        mv_series.append(cash + sum(shares[s] * CL[s][t] for s in syms if not np.isnan(CL[s][t])))

    mv = np.array(mv_series)
    peak = np.maximum.accumulate(mv)
    mdd = float(np.max((peak - mv) / peak)) if len(mv) else 0.0
    terminal = float(mv[-1])
    dep_fracs = []
    for k, t in enumerate(range(i0, i1 + 1)):
        sv = sum(shares[s] * CL[s][t] for s in syms if not np.isnan(CL[s][t]))
        dep_fracs.append(sv / mv[k] if mv[k] > 0 else 0.0)
    return {
        "terminal": round(terminal), "return_pct": round(100 * (terminal / START_CAPITAL - 1), 2),
        "max_dd_pct": round(100 * mdd, 2),
        "avg_deployed_pct": round(100 * float(np.mean(dep_fracs)), 1),
        "end_deployed_pct": round(100 * float(dep_fracs[-1]), 1),
        "end_cash": round(cash), "n_fills": len(fill_vs_close),
        "avg_fill_vs_close_pct": round(100 * (float(np.mean(fill_vs_close)) - 1), 3) if fill_vs_close else None,
        "avg_fill_vs_open_pct": round(100 * (float(np.mean(fill_vs_open)) - 1), 3) if fill_vs_open else None,
    }


def main():
    uni = load_universe()
    print(f"universe: {len(uni)} USD watchlist names")
    if not os.path.exists(OHLC_CACHE):
        raise SystemExit(f"missing {OHLC_CACHE} — run compounder_yellowfix_ab.py first to populate it")
    cache = json.load(open(OHLC_CACHE))
    uni = [u for u in uni if cache.get(u["symbol"])]
    dates, OP, HI, LO, CL = build_matrix(cache, [u["symbol"] for u in uni])
    di = {d: i for i, d in enumerate(dates)}
    print(f"calendar {dates[0]} -> {dates[-1]} ({len(dates)}d), {len(uni)} names priced\n")

    def idx_on_or_after(day):
        for dd in dates:
            if dd >= day:
                return di[dd]
        return None
    def idx_on_or_before(day):
        prev = None
        for dd in dates:
            if dd <= day:
                prev = di[dd]
        return prev

    all_rows = []
    for wname, wstart, wend in WINDOWS:
        i0 = idx_on_or_after(wstart); i1 = idx_on_or_before(wend)
        if i0 is None or i1 is None or i1 <= i0:
            print(f"[skip {wname}: window not in data]"); continue
        i0 = max(i0, 252)   # warmup guard for 252d ranking window
        print(f"=== {wname}  {dates[i0]} -> {dates[i1]}  ({i1 - i0 + 1} trading days) ===")
        for variant in VARIANTS:
            res = simulate(uni, dates, OP, HI, LO, CL, i0, i1, variant)
            res.update({"window": wname, "variant": variant,
                        "start": dates[i0], "end": dates[i1], "days": i1 - i0 + 1})
            all_rows.append(res)
            print(f"  {variant:<7} ret {res['return_pct']:>8}%  MDD {res['max_dd_pct']:>6}%  "
                  f"avgDep {res['avg_deployed_pct']:>5}%  fills {res['n_fills']:>5}  "
                  f"fill_vs_close {str(res['avg_fill_vs_close_pct']):>7}%  "
                  f"fill_vs_open {str(res['avg_fill_vs_open_pct']):>7}%")
        print()

    with open(OUT, "w") as f:
        for r in all_rows:
            f.write(json.dumps(r) + "\n")
    print(f"saved -> {OUT}\n")

    print("SUMMARY — return% by variant, and deltas vs `open` (current live proxy):")
    print(f"  {'window':<16}{'open':>10}{'defer':>10}{'rerank':>10}   {'defer-open':>11}{'rerank-open':>12}   winner")
    for wname, _, _ in WINDOWS:
        rr = {r["variant"]: r for r in all_rows if r["window"] == wname}
        if len(rr) < 3:
            continue
        o, de, re = rr["open"]["return_pct"], rr["defer"]["return_pct"], rr["rerank"]["return_pct"]
        win = max([("open", o), ("defer", de), ("rerank", re)], key=lambda x: x[1])[0]
        print(f"  {wname:<16}{o:>9}%{de:>9}%{re:>9}%   {de-o:>+10.2f}{re-o:>+11.2f}   {win}")
    print("\nMDD% by variant:")
    print(f"  {'window':<16}{'open':>10}{'defer':>10}{'rerank':>10}")
    for wname, _, _ in WINDOWS:
        rr = {r["variant"]: r for r in all_rows if r["window"] == wname}
        if len(rr) < 3:
            continue
        print(f"  {wname:<16}{rr['open']['max_dd_pct']:>9}%{rr['defer']['max_dd_pct']:>9}%{rr['rerank']['max_dd_pct']:>9}%")
    print("\nREAD: open->defer = pure price-timing (same orders, open vs close fill). "
          "defer->rerank = name-switch effect.\n"
          "     fill_vs_open>0 means the close was ABOVE the open on filled days (deferring paid MORE).\n"
          "NOTE: survivorship + fundamental look-ahead inflate ABSOLUTE levels; read cross-variant deltas.")


if __name__ == "__main__":
    main()
