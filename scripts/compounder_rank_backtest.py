#!/usr/bin/env python
"""Compounder buy-ranking backtest — gap-to-target vs quality-rank ordering, swept across
conviction_power (weight steepness).

WHAT IT MEASURES
  How the buy-ORDER (which underweight name to fill first with limited budget) and the weight
  steepness (conviction_power) affect terminal wealth, drawdown, and breadth — using the REAL pure
  functions from src/portfolio/compounder.py (target_weights, leaders, fair_price_attractiveness).

HONEST CAVEATS (this is a RELATIVE comparator, not an absolute return predictor):
  * Survivorship bias: the universe is TODAY's discovered_pool picks, run on past prices.
  * US/SMART names only; static fundamental scores (pool snapshot, no point-in-time fundamentals).
  * Weekly rebalance, fill-at-close, no fees/dividends, no crash-reserve (full deployment).
  A name only enters the investable set once it has >=252 trading days of history (handles IPOs and
  removes the worst look-ahead). All variants see the identical contribution schedule and data, so the
  cross-variant comparison is apples-to-apples even though absolute levels are inflated.
"""
import sys, os, json, time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import yaml, requests
from src.portfolio.fmp import get_fmp_key
from src.portfolio import compounder as cmp
from src.portfolio.compounder import RankedName

CACHE = "data/compounder_backtest_prices.json"
OUT = "data/compounder_rank_backtest.jsonl"
START = "2019-01-01"

# Sim knobs (mirror live CompounderConfig where it matters)
INITIAL = 50_000.0
CONTRIB_MONTHLY = 10_000.0
BUFFER_PCT = 0.03
PER_NAME_CAP = 0.06
LEADER_CAP = 0.10
LEADER_TOP_FRAC = 0.20
ABS_CEILING = 750_000.0
TIER_BUDGETS = {"growth": 0.65, "breakthrough": 0.30, "dividend": 0.05}
RANK_W_FUND, RANK_W_MOM = 0.70, 0.30
MAX_BUY_PCT = 0.02          # per-name brick cap per rebalance (% NLV), like single_buy_bounds
MIN_BUY = 1_000.0
REBAL_EVERY = 5             # trading days (weekly)
WARMUP = 252


def load_universe():
    d = yaml.safe_load(open("tools/discovered_pool.yaml"))
    out = []
    for tier, rows in (d or {}).items():
        for r in rows or []:
            if r.get("exchange") == "SMART" and (r.get("currency") or "USD") == "USD":
                out.append({"symbol": r["symbol"], "tier": tier, "score": float(r.get("score", 50))})
    return out


def fetch_prices(symbols):
    cache = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
    key = get_fmp_key()
    todo = [s for s in symbols + ["SPY"] if s not in cache]
    print(f"fetching {len(todo)} symbols (cached: {len(cache)})")
    for i, sym in enumerate(todo):
        url = (f"https://financialmodelingprep.com/stable/historical-price-eod/full"
               f"?symbol={sym}&from={START}&apikey={key}")
        try:
            r = requests.get(url, timeout=25); r.raise_for_status()
            data = r.json()
            rows = data if isinstance(data, list) else (data.get("historical", []) if data else [])
            series = {row["date"]: float(row["close"]) for row in rows
                      if row.get("close") and row.get("date")}
            cache[sym] = series
            json.dump(cache, open(CACHE, "w"))
            print(f"  {sym}: {len(series)} days ({i+1}/{len(todo)})")
            time.sleep(0.25)
        except Exception as e:
            print(f"  FAIL {sym}: {e}")
    return cache


def build_matrix(cache, symbols):
    """Master calendar = SPY dates. Each symbol forward-filled onto it; NaN before first listing."""
    spy = cache.get("SPY", {})
    dates = sorted(spy.keys())
    if not dates:
        raise SystemExit("no SPY data")
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
            arr[i] = last  # forward fill; stays NaN until first listing
        mat[sym] = arr
    return dates, mat


def simulate(universe, dates, mat, ordering, conviction_power):
    n = len(dates)
    syms = [u["symbol"] for u in universe]
    tier = {u["symbol"]: u["tier"] for u in universe}
    score = {u["symbol"]: u["score"] for u in universe}

    shares = {s: 0.0 for s in syms}
    cash = INITIAL
    contributed = INITIAL
    cur_month = dates[0][:7]

    mv_series, contrib_flow = [], []
    eff_n_samples = []

    for t in range(n):
        # monthly contribution at first master date of a new month
        c_today = 0.0
        if dates[t][:7] != cur_month:
            cur_month = dates[t][:7]
            cash += CONTRIB_MONTHLY
            contributed += CONTRIB_MONTHLY
            c_today = CONTRIB_MONTHLY

        # rebalance weekly, after warmup
        if t >= WARMUP and (t % REBAL_EVERY == 0):
            ranked = []
            moms = []
            feats = {}
            for s in syms:
                px = mat[s][t]
                if np.isnan(px) or px <= 0:
                    continue
                win = mat[s][t - WARMUP + 1:t + 1]
                if np.isnan(win).any():       # require full 252-day history (post-IPO seasoning)
                    continue
                sma200 = float(np.mean(mat[s][t - 199:t + 1]))
                high52 = float(np.nanmax(win))
                p0 = mat[s][t - 252] if t >= 252 else np.nan
                mom = (px / p0 - 1.0) if (not np.isnan(p0) and p0 > 0) else None
                feats[s] = (px, sma200, high52, mom)
                if mom is not None:
                    moms.append(mom)
            if feats:
                moms_sorted = sorted(moms)
                def mpct(v):
                    if v is None or not moms_sorted:
                        return 0.5
                    import bisect
                    return bisect.bisect_right(moms_sorted, v) / len(moms_sorted)
                for s, (px, sma200, high52, mom) in feats.items():
                    mp = mpct(mom)
                    rk = RANK_W_FUND * score[s] + RANK_W_MOM * (mp * 100.0)
                    ranked.append(RankedName(s, tier[s], round(score[s], 1), round(mp, 3),
                                             round(rk, 2), px, sma200, high52))
                ranked.sort(key=lambda r: -r.rank_score)

                nlv = cash + sum(shares[s] * mat[s][t] for s in syms if not np.isnan(mat[s][t]))
                investable = nlv * (1 - BUFFER_PCT)
                leaders = cmp.leader_symbols(ranked, LEADER_TOP_FRAC)
                targets = cmp.target_weights(ranked, TIER_BUDGETS, investable, PER_NAME_CAP,
                                             leader_syms=leaders, leader_cap_pct=LEADER_CAP,
                                             conviction_power=conviction_power, abs_ceiling=ABS_CEILING)
                rank_idx = {r.symbol: i for i, r in enumerate(ranked)}
                price = {r.symbol: r.price for r in ranked}

                # build the underweight queue
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
                # GREEN (att>=0) before YELLOW, then within-band by the chosen key
                if ordering == "rank":
                    q.sort(key=lambda x: (0 if x[0] >= 0 else 1, rank_idx[x[2]]))
                else:  # gap-to-target: biggest underweight $ first
                    q.sort(key=lambda x: (0 if x[0] >= 0 else 1, -x[1]))

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
        if t >= WARMUP and t % REBAL_EVERY == 0:
            vals = np.array([shares[s] * mat[s][t] for s in syms if not np.isnan(mat[s][t]) and shares[s] > 0])
            if vals.sum() > 0:
                eff_n_samples.append((vals.sum() ** 2) / (vals ** 2).sum())

    # time-weighted return index (strip contributions so they aren't counted as return)
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
    return {
        "ordering": ordering, "conviction_power": conviction_power,
        "terminal_nlv": round(float(mv_series[-1])),
        "total_invested": round(contributed),
        "multiple": round(float(mv_series[-1]) / contributed, 3),
        "twr_cagr_pct": round(twr_cagr * 100, 2),
        "vol_pct": round(vol * 100, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "sharpe_like": round(twr_cagr / vol, 3) if vol > 0 else None,
        "eff_holdings_end": round(float(eff_n_samples[-1]), 1) if eff_n_samples else None,
        "eff_holdings_avg": round(float(np.mean(eff_n_samples)), 1) if eff_n_samples else None,
    }


def main():
    uni = load_universe()
    print(f"universe: {len(uni)} US names")
    cache = fetch_prices([u["symbol"] for u in uni])
    # drop names with no data
    uni = [u for u in uni if cache.get(u["symbol"])]
    dates, mat = build_matrix(cache, [u["symbol"] for u in uni])
    print(f"calendar: {dates[0]} → {dates[-1]} ({len(dates)} days), {len(uni)} names with data\n")

    results = []
    for cp in (1.0, 1.2, 1.75):
        for order in ("rank", "gap"):
            res = simulate(uni, dates, mat, order, cp)
            results.append(res)
            print(f"  cp={cp:<4} {order:<4} → mult {res['multiple']:>5}x  "
                  f"CAGR {res['twr_cagr_pct']:>6}%  vol {res['vol_pct']:>5}%  "
                  f"MDD {res['max_drawdown_pct']:>5}%  effN {res['eff_holdings_end']}")

    with open(OUT, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")

    print(f"\n{'cp':<5}{'order':<6}{'mult':>7}{'CAGR%':>8}{'vol%':>7}{'MDD%':>7}{'Sharpe':>8}{'effN':>6}")
    print("-" * 54)
    for r in sorted(results, key=lambda x: -x["multiple"]):
        print(f"{r['conviction_power']:<5}{r['ordering']:<6}{r['multiple']:>7}"
              f"{r['twr_cagr_pct']:>8}{r['vol_pct']:>7}{r['max_drawdown_pct']:>7}"
              f"{str(r['sharpe_like']):>8}{str(r['eff_holdings_end']):>6}")
    print(f"\nsaved → {OUT}")
    print("NOTE: survivorship-biased absolute levels; read the CROSS-VARIANT deltas, not the levels.")


if __name__ == "__main__":
    main()
