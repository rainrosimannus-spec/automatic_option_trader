#!/usr/bin/env python
"""Compounder freeze-vs-keep A/B — how to treat a HELD name the monthly screener drops.

THE QUESTION
  Once a month a screener re-picks the top-N universe. A held name that falls out is NOT
  force-sold (tax/churn). What target should it carry until then?
    KEEP   (current live): it stays ranked and keeps a full target — new capital keeps
                           flowing in even though the screener rejected it.
    FREEZE (proposal):     cap its target at what's already invested (hold, don't add, don't
                           sell) and REDISTRIBUTE the freed target to current members.
    FREEZE+BUFFER:         same as FREEZE but only freeze once a name falls outside a wider
                           hysteresis band (top N*1.2), so a name that merely grazed the cut
                           line isn't frozen on a one-month wobble.

WHY A SIMULATED MONTHLY SCREENER
  The rank-backtest harness uses a STATIC universe (every name, all years) so nothing ever
  drops out — it cannot express this question at all. Here the "screener" re-selects the
  top-N by the SAME rank_score the live ranker uses (fundamental score + momentum pct),
  refreshed monthly. It's a proxy for the live AI/fundamental screen, but it's identical
  across all three arms, so the CROSS-ARM comparison is apples-to-apples (the only honest
  claim this harness makes — see the rank-backtest caveats, which all apply here too).

  Extra metric: legacy_mv_pct = capital sitting in HELD names that are no longer members.
  This is the "grandfather accumulation" failure mode — KEEP should let it creep up.

HONEST CAVEATS (inherited): survivorship-biased pool, static fundamental scores, US/SMART
  only, weekly fill-at-close, no fees/dividends/crash-reserve. Read cross-arm DELTAS, never
  the absolute levels.
"""
import sys, os, json, bisect
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import yaml
from src.portfolio import compounder as cmp
from src.portfolio.compounder import RankedName

CACHE = "data/compounder_backtest_prices.json"          # reuse the rank-backtest cache (no refetch)
OUT = "data/compounder_freeze_ab.jsonl"
START = "2019-01-01"

INITIAL = 50_000.0
CONTRIB_MONTHLY = 10_000.0
BUFFER_PCT = 0.03
PER_NAME_CAP = 0.06
LEADER_CAP = 0.10
LEADER_TOP_FRAC = 0.20
ABS_CEILING = 750_000.0
TIER_BUDGETS = {"growth": 0.65, "breakthrough": 0.30, "dividend": 0.05}
RANK_W_FUND, RANK_W_MOM = 0.70, 0.30
MAX_BUY_PCT = 0.02
MIN_BUY = 1_000.0
REBAL_EVERY = 5
WARMUP = 252

# LIVE-REALISTIC screener shape: on Rain's book the screen keeps ~98 of a ~109-name watchlist
# (≈90% members, only ~11 held drop-outs). The earlier run kept 40 of 85 (~47%) — far more churn
# than reality, which is why a member_count×1.2 buffer never bound live. Model the real shape:
# keep the top MEMBERSHIP_FRAC of the priceable pool each month, so only a handful ever roll off.
MEMBERSHIP_FRAC = 0.90
# TOP-K conviction buffer to sweep: a held drop-out keeps accumulating only while it still ranks in
# the top-K on the live rank; below K it's frozen. K ≈ the count conviction actually funds.
TOPK_SWEEP = (25, 40, 55)

# Crash / mean-reversion windows. The portfolio is seeded over the FULL history and metrics
# are measured only WITHIN the window, so each arm enters the crash with a realistic book.
# 2022 is the decisive test: a momentum REVERSAL where the high-momentum names FREEZE
# concentrates into got hit hardest and fallen names later snapped back — exactly where
# "redistribute to current leaders" should lose to "keep buying the drop" if the risk is real.
CRASH_WINDOWS = {
    "covid_crash":   ("2020-02-14", "2020-04-07"),   # pure COVID drawdown
    "covid_v":       ("2020-02-14", "2020-09-30"),   # crash + V recovery
    "bear_2022":     ("2022-01-03", "2022-10-14"),   # 2022 momentum-reversal drawdown
    "bear_recov_23": ("2022-01-03", "2023-07-31"),   # 2022 bear + 2023 recovery (mean-reversion)
}


def load_universe():
    d = yaml.safe_load(open("tools/discovered_pool.yaml"))
    out = []
    for tier, rows in (d or {}).items():
        for r in rows or []:
            if r.get("exchange") == "SMART" and (r.get("currency") or "USD") == "USD":
                out.append({"symbol": r["symbol"], "tier": tier, "score": float(r.get("score", 50))})
    return out


def build_matrix(cache, symbols):
    spy = cache.get("SPY", {})
    dates = sorted(spy.keys())
    if not dates:
        raise SystemExit("no SPY data in cache — run compounder_rank_backtest.py first to populate it")
    di = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    mat = {}
    for sym in symbols + ["SPY"]:
        s = cache.get(sym, {})
        arr = np.full(n, np.nan)
        last = np.nan
        for d, i in di.items():
            if d in s:
                last = s[d]
            arr[i] = last
        mat[sym] = arr
    return dates, mat


def _rank_at(t, syms, mat, score, tier):
    """Return (ranked list, rank_index dict) at day t, or (None, None) if no eligible names."""
    feats, moms = {}, []
    for s in syms:
        px = mat[s][t]
        if np.isnan(px) or px <= 0:
            continue
        win = mat[s][t - WARMUP + 1:t + 1]
        if np.isnan(win).any():
            continue
        sma200 = float(np.mean(mat[s][t - 199:t + 1]))
        high52 = float(np.nanmax(win))
        p0 = mat[s][t - 252] if t >= 252 else np.nan
        mom = (px / p0 - 1.0) if (not np.isnan(p0) and p0 > 0) else None
        feats[s] = (px, sma200, high52, mom)
        if mom is not None:
            moms.append(mom)
    if not feats:
        return None, None
    moms_sorted = sorted(moms)

    def mpct(v):
        if v is None or not moms_sorted:
            return 0.5
        return bisect.bisect_right(moms_sorted, v) / len(moms_sorted)

    ranked = []
    for s, (px, sma200, high52, mom) in feats.items():
        mp = mpct(mom)
        rk = RANK_W_FUND * score[s] + RANK_W_MOM * (mp * 100.0)
        ranked.append(RankedName(s, tier[s], round(score[s], 1), round(mp, 3),
                                 round(rk, 2), px, sma200, high52))
    ranked.sort(key=lambda r: -r.rank_score)
    return ranked, {r.symbol: i for i, r in enumerate(ranked)}


def _window_metrics(dates, idx, start, end):
    """Total TWR return and max drawdown of the index between two calendar dates."""
    lo = next((i for i, d in enumerate(dates) if d >= start), None)
    hi = next((i for i in range(len(dates) - 1, -1, -1) if dates[i] <= end), None)
    if lo is None or hi is None or hi <= lo:
        return None
    seg = idx[lo:hi + 1]
    peak = np.maximum.accumulate(seg)
    mdd = float(np.max((peak - seg) / peak))
    return {"ret_pct": round((seg[-1] / seg[0] - 1.0) * 100, 2),
            "mdd_pct": round(mdd * 100, 2)}


def simulate(universe, dates, mat, arm, conviction_power, membership_n, topk):
    n = len(dates)
    syms = [u["symbol"] for u in universe]
    tier = {u["symbol"]: u["tier"] for u in universe}
    score = {u["symbol"]: u["score"] for u in universe}

    shares = {s: 0.0 for s in syms}
    cash = INITIAL
    contributed = INITIAL
    cur_month = dates[0][:7]

    member_set, screen_month = set(), None
    mv_series, contrib_flow = [], []
    eff_samples, legacy_samples = [], []
    freeze_events = 0

    for t in range(n):
        c_today = 0.0
        if dates[t][:7] != cur_month:
            cur_month = dates[t][:7]
            cash += CONTRIB_MONTHLY
            contributed += CONTRIB_MONTHLY
            c_today = CONTRIB_MONTHLY

        if t >= WARMUP and (t % REBAL_EVERY == 0):
            ranked, rank_idx = _rank_at(t, syms, mat, score, tier)
            if ranked:
                price = {r.symbol: r.price for r in ranked}
                # ── Monthly screener: keep the top membership_n by rank, refreshed monthly ──
                if screen_month != dates[t][:7]:
                    screen_month = dates[t][:7]
                    member_set = {r.symbol for r in ranked[:membership_n]}

                held = {s for s in syms if shares[s] > 0}
                # Which held-non-members are FROZEN (held flat, no new buys) this arm?
                if arm == "keep":
                    frozen = set()                                  # nobody frozen
                    keep_band = member_set | held                   # legacy names keep full targets
                elif arm == "freeze":
                    # TOP-K conviction buffer: a held drop-out keeps buying ONLY while it still ranks
                    # in the top-`topk` on the live rank; otherwise it's frozen (held flat). Absolute
                    # cutoff, not member_count×mult — the latter never binds when members are ~90% of
                    # the universe (the live shape), which is why the multiplier version froze nothing.
                    in_topk = {r.symbol for r in ranked[:topk]}
                    frozen = {s for s in held if s not in member_set and s not in in_topk}
                    keep_band = member_set | (held & in_topk)
                else:
                    raise ValueError(arm)
                freeze_events += len(frozen)

                # Fresh target allocation flows ONLY to keep_band names (frozen budget redistributes).
                alloc = [r for r in ranked if r.symbol in keep_band]
                leaders = cmp.leader_symbols(alloc, LEADER_TOP_FRAC)
                targets = cmp.target_weights(
                    alloc, TIER_BUDGETS, (cash + sum(shares[s] * mat[s][t]
                                                     for s in syms if not np.isnan(mat[s][t]))) * (1 - BUFFER_PCT),
                    PER_NAME_CAP, leader_syms=leaders, leader_cap_pct=LEADER_CAP,
                    conviction_power=conviction_power, abs_ceiling=ABS_CEILING)
                # Frozen held names: target pinned to current invested MV (hold — never buy, never sell).
                for s in frozen:
                    targets[s] = shares[s] * price.get(s, mat[s][t])

                nlv = cash + sum(shares[s] * mat[s][t] for s in syms if not np.isnan(mat[s][t]))
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
                q.sort(key=lambda x: (0 if x[0] >= 0 else 1, -x[1]))   # gap-to-target (live default)

                budget = max(0.0, cash - nlv * BUFFER_PCT)
                max_buy = max(MIN_BUY, nlv * MAX_BUY_PCT)
                for att, gap, s, tgt, cur in q:
                    if budget < MIN_BUY:
                        break
                    brick = min(max_buy, gap, budget)
                    if brick < MIN_BUY:
                        continue
                    shares[s] += brick / price[s]
                    cash -= brick
                    budget -= brick

        mv = cash + sum(shares[s] * mat[s][t] for s in syms if not np.isnan(mat[s][t]))
        mv_series.append(mv)
        contrib_flow.append(c_today)
        if t >= WARMUP and t % REBAL_EVERY == 0 and member_set:
            vals = np.array([shares[s] * mat[s][t] for s in syms
                             if not np.isnan(mat[s][t]) and shares[s] > 0])
            if vals.sum() > 0:
                eff_samples.append((vals.sum() ** 2) / (vals ** 2).sum())
            invested_mv = sum(shares[s] * mat[s][t] for s in syms
                              if shares[s] > 0 and not np.isnan(mat[s][t]))
            legacy_mv = sum(shares[s] * mat[s][t] for s in syms
                            if shares[s] > 0 and s not in member_set and not np.isnan(mat[s][t]))
            if invested_mv > 0:
                legacy_samples.append(legacy_mv / invested_mv)

    mv_series = np.array(mv_series); contrib_flow = np.array(contrib_flow)
    idx = [1.0]
    for t in range(1, n):
        prev = mv_series[t - 1]
        r = ((mv_series[t] - contrib_flow[t]) / prev - 1.0) if prev > 0 else 0.0
        idx.append(idx[-1] * (1 + r))
    idx = np.array(idx)
    yrs = n / 252.0
    twr_cagr = idx[-1] ** (1 / yrs) - 1 if idx[-1] > 0 else float("nan")
    rets = np.diff(idx) / idx[:-1]
    vol = float(np.std(rets) * np.sqrt(252))
    peak = np.maximum.accumulate(idx)
    mdd = float(np.max((peak - idx) / peak))
    windows = {name: _window_metrics(dates, idx, s, e) for name, (s, e) in CRASH_WINDOWS.items()}
    return {
        "arm": arm, "conviction_power": conviction_power,
        "membership_n": membership_n, "topk": topk,
        "windows": windows,
        "terminal_nlv": round(float(mv_series[-1])),
        "total_invested": round(contributed),
        "multiple": round(float(mv_series[-1]) / contributed, 3),
        "twr_cagr_pct": round(twr_cagr * 100, 2),
        "vol_pct": round(vol * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "sharpe_like": round(twr_cagr / vol, 3) if vol > 0 else None,
        "eff_holdings_end": round(float(eff_samples[-1]), 1) if eff_samples else None,
        "eff_holdings_avg": round(float(np.mean(eff_samples)), 1) if eff_samples else None,
        "legacy_mv_pct_end": round(float(legacy_samples[-1]) * 100, 1) if legacy_samples else None,
        "legacy_mv_pct_avg": round(float(np.mean(legacy_samples)) * 100, 1) if legacy_samples else None,
        "freeze_events": freeze_events,
    }


def _label(topk):
    return "keep" if topk is None else ("total_freeze" if topk == 0 else f"freeze_top{topk}")


def main():
    uni = load_universe()
    if not os.path.exists(CACHE):
        raise SystemExit(f"{CACHE} missing — run scripts/compounder_rank_backtest.py once to populate prices")
    cache = json.load(open(CACHE))
    uni = [u for u in uni if cache.get(u["symbol"])]
    dates, mat = build_matrix(cache, [u["symbol"] for u in uni])
    membership_n = round(MEMBERSHIP_FRAC * len(uni))
    print(f"universe: {len(uni)} names with data | calendar {dates[0]} → {dates[-1]} "
          f"({len(dates)} days)\nscreener keeps top {membership_n} (~{MEMBERSHIP_FRAC:.0%}) "
          f"→ ~{len(uni)-membership_n} held drop-outs/month (live-realistic, low churn)\n"
          f"arms: keep vs total_freeze (K=0) vs top-K buffer K∈{TOPK_SWEEP}\n")

    # topk=None → keep (baseline); topk=0 → total freeze; topk>0 → top-K conviction buffer.
    variants = [None, 0] + list(TOPK_SWEEP)
    results = []
    for cp in (1.0, 1.75):
        for topk in variants:
            arm = "keep" if topk is None else "freeze"
            res = simulate(uni, dates, mat, arm, cp, membership_n, (0 if topk is None else topk))
            res["label"] = _label(topk)
            results.append(res)
            print(f"  cp={cp:<4} {res['label']:<14} → mult {res['multiple']:>5}x  "
                  f"CAGR {res['twr_cagr_pct']:>6}%  MDD {res['max_drawdown_pct']:>5}%  "
                  f"effN {res['eff_holdings_end']:>4}  frozen/scan~{res['freeze_events']}")

    with open(OUT, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    for cp in (1.0, 1.75):
        print(f"\ncp={cp}  {'arm':<15}{'mult':>7}{'CAGR%':>8}{'vol%':>7}{'MDD%':>7}"
              f"{'Sharpe':>8}{'effN':>6}{'legacy%avg':>11}")
        print("-" * 71)
        for r in sorted([x for x in results if x["conviction_power"] == cp], key=lambda x: -x["multiple"]):
            print(f"      {r['label']:<15}{r['multiple']:>7}{r['twr_cagr_pct']:>8}"
                  f"{r['vol_pct']:>7}{r['max_drawdown_pct']:>7}{str(r['sharpe_like']):>8}"
                  f"{str(r['eff_holdings_end']):>6}{str(r['legacy_mv_pct_avg']):>11}")

    # ── Crash / mean-reversion stress: return% / maxDD% within each window (cp=1.75 = live) ──
    arms_order = [_label(v) for v in variants]
    print(f"\nCRASH STRESS (cp=1.75) — return% / maxDD% within window")
    hdr = f"{'window':<16}" + "".join(f"{a:>16}" for a in arms_order)
    print(hdr); print("-" * len(hdr))
    base = {r["label"]: r for r in results if r["conviction_power"] == 1.75}
    for wname in CRASH_WINDOWS:
        row = f"{wname:<16}"
        for a in arms_order:
            w = base[a]["windows"].get(wname)
            cell = f"{w['ret_pct']:+.1f}/{w['mdd_pct']:.0f}" if w else "n/a"
            row += f"{cell:>16}"
        print(row)

    print(f"\nsaved → {OUT}")
    print("Live shape (~90% members) → low churn, so expect SMALL deltas. total_freeze = no buffer; "
          "top-K keeps buying a drop-out still ranking in the top-K.")


if __name__ == "__main__":
    main()
