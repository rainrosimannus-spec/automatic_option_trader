"""Tail-hedge sweep (2026-11-07): rolling SPY OTM put (live hedge.py rules) on top of the
Phase-1 wheel + XSP sleeve book.

Bases: wheel + naked sleeve (k2/1DTE/30% budget), wheel + 3% spread at 5% and 10% max loss.
Hedge grid: off | otm 5% × budget {2,4,8}% of premium × 30 DTE | otm 10% / 4% / 30 DTE |
otm 5% / 4% / 60 DTE. Priced at flat VIX (no skew) → the sim's hedge is cheaper than a real
one, so its benefit is an UPPER bound; its cost a lower bound.

    .venv/bin/python scripts/tail_hedge_sweep.py
"""
from __future__ import annotations

import json, os, sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src")); sys.path.insert(0, str(_ROOT))
import yaml  # noqa: E402
from marswalk.data import fetch_index_yahoo, fetch_vix_yahoo, load_earnings  # noqa: E402
from marswalk.regimes import load_config  # noqa: E402
from wheel_rules_sweep import LIVE, LIVE_CAPITAL, LIVE_REGIME, REPLAY, run_one  # noqa: E402
from index_put_sweep import PHASE1  # noqa: E402

REGIMES = [REPLAY, LIVE, "bear_2022", "covid_2020", "q4_2018", "gfc_2008", "bull_2021",
           "volmageddon_2018", "tariff_2025"]
SLEEVE = {"index_put_enabled": True, "index_put_sigma_k": 2.0, "index_put_dte": 1, "index_put_max_open": 1}
BASES = {
    "naked_b0.30": {**PHASE1, **SLEEVE, "index_put_budget_pct": 0.30},
    "spread_ml0.05": {**PHASE1, **SLEEVE, "index_put_spread_enabled": True, "index_put_spread_width_pct": 0.03,
                      "index_put_max_loss_pct": 0.05},
    "spread_ml0.10": {**PHASE1, **SLEEVE, "index_put_spread_enabled": True, "index_put_spread_width_pct": 0.03,
                      "index_put_max_loss_pct": 0.10},
}
HEDGES = {"nohedge": {}}
for b in (0.02, 0.04, 0.08):
    HEDGES[f"h5_b{b:.2f}_d30"] = {"hedge_enabled": True, "hedge_otm_pct": 0.05, "hedge_budget_pct": b, "hedge_dte_target": 30}
HEDGES["h10_b0.04_d30"] = {"hedge_enabled": True, "hedge_otm_pct": 0.10, "hedge_budget_pct": 0.04, "hedge_dte_target": 30}
HEDGES["h5_b0.04_d60"] = {"hedge_enabled": True, "hedge_otm_pct": 0.05, "hedge_budget_pct": 0.04, "hedge_dte_target": 60}
CONFIGS = {f"{bn}+{hn}": {**bo, **ho} for bn, bo in BASES.items() for hn, ho in HEDGES.items()}
_CACHE: dict = {}


def _job(spec):
    rid, cname = spec
    if "cfg" not in _CACHE:
        base_universe, regimes = load_config()
        live_universe = [s["symbol"] for s in yaml.safe_load(open("config/options_universe.yaml"))["stocks"]
                         if s.get("currency") == "USD"]
        _CACHE["cfg"] = (base_universe, {r.id: r for r in regimes}, live_universe); _CACHE["earn"] = {}
    base_universe, by_id, live_universe = _CACHE["cfg"]
    reg, uni, cap = (LIVE_REGIME, live_universe, LIVE_CAPITAL) if rid == LIVE else (by_id[rid], base_universe, None)
    key = "live" if rid == LIVE else "base"
    if key not in _CACHE["earn"]:
        _CACHE["earn"][key] = load_earnings(uni)
    r = run_one(reg, uni, _CACHE["earn"][key], CONFIGS[cname], start_capital=cap)
    return {"regime": rid, "config": cname, **r}


def main():
    out = Path("data") / f"tail_hedge_sweep_{date.today().strftime('%Y%m%d')}.jsonl"
    _, regimes = load_config(); by_id = {r.id: r for r in regimes}
    for rid in REGIMES:
        reg = LIVE_REGIME if rid == LIVE else by_id[rid]
        try:
            fetch_index_yahoo(reg); fetch_vix_yahoo(reg)
        except Exception as e:
            print("backfill", rid, e)
    jobs = [(rid, c) for rid in REGIMES for c in CONFIGS]
    jobs.sort(key=lambda j: 0 if j[0] == REPLAY else 1)
    print(f"Tail-hedge sweep — {len(jobs)} runs", flush=True)
    rows = []
    with ProcessPoolExecutor(max_workers=min(48, os.cpu_count() or 8)) as ex:
        futs = {ex.submit(_job, j): j for j in jobs}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result()
            except Exception as e:
                print("JOB FAILED", futs[fut], e); continue
            rows.append(r)
            with out.open("a") as fh:
                fh.write(json.dumps(r) + "\n")
            if n % 30 == 0:
                print(f"  …{n}/{len(jobs)}", flush=True)
    for rid in REGIMES:
        rr = [r for r in rows if r["regime"] == rid and r.get("final_return_pct") is not None]
        if not rr:
            continue
        print(f"\n  {rid}")
        print(f"    {'config':28} {'return':>9} {'ann%':>7} {'maxDD':>6} {'hedgeBuys':>9} {'hedgeCost$':>11} {'hedgeProc$':>11} {'Δ vs nohedge':>13}")
        for r in sorted(rr, key=lambda x: list(CONFIGS).index(x["config"])):
            base_name = r["config"].split("+")[0] + "+nohedge"
            b = next((x for x in rr if x["config"] == base_name), None)
            d = r["final_return_pct"] - b["final_return_pct"] if b else 0.0
            print(f"    {r['config']:28} {r['final_return_pct']:>+8.2f}% {r['annualized_pct']:>+6.1f} {r['max_drawdown_pct']:>5.1f}% "
                  f"{r.get('n_hedge_buys',0):>9d} {r.get('hedge_cost_sum',0):>11,.0f} {r.get('hedge_proceeds_sum',0):>11,.0f} {d:>+12.2f}pp")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
