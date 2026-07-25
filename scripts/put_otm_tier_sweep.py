"""VIX-tiered OTM-floor A/B (2026-07-25).

The flat moneyness sweep (put_moneyness_sweep.py) proved no FLAT farther-OTM
window holds 15.5%/yr — benign-market assignments ARE the return. But it also
showed farther-OTM wins on BOTH return and drawdown in every CRASH regime. This
sweep tests the natural consequence: a VIX-TIERED OTM window (Params
put_otm_tier_enabled) that keeps the aggressive near-ATM 2%/5%/12% window on
calm/bull days (to hold the benign return) and widens it to *_stress values on
stress days (VIX >= stress_vix OR SPY 10d<20d bearish) — cutting the assignments
that become dead capital in falling names, where they actually hurt.

Ship criterion (from the plan): a tier variant WINS only if on live_replay_2020
it holds annualized >= 15.5%/yr AND has fewer assignments than baseline (and
does not worsen crash DD). If it clears that, it is strictly better than every
flat variant — fewer assignments for free.

READ-ONLY against the live engine — only writes a results JSONL.

Usage:
    source .venv/bin/activate
    PYTHONPATH=src python3 scripts/put_otm_tier_sweep.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from marswalk.data import load_earnings, load_market
from marswalk.engine import Params, run_regime
from marswalk.regimes import load_config

BENIGN = ["bull_2021", "grind_2024h1", "chop_2023h2", "iran_war_2026"]
CRASH = ["covid_2020", "svb_2023", "q4_2018", "bear_2022", "gfc_2008", "stacked_2x", "blackout_3day"]
REPLAY = "live_replay_2020"
TARGETS = BENIGN + CRASH

_CRISIS = {"high_vol_grind_enabled": True, "strangle_when_grind": True,
           "crash_when_active_enabled": True, "crash_strangle_when_active": True}

# Calm window is always the live 2%/5%/12%; variants differ in the STRESS window
# and the stress trigger. tier disabled = baseline (flat).
_BASE = {**_CRISIS, "dte_min": 0, "dte_max": 3}
CONFIGS = {
    "baseline":     {**_BASE, "put_otm_tier_enabled": False},
    # stress window (floor/target/cap), stress VIX trigger:
    "tier_mild":    {**_BASE, "put_otm_tier_enabled": True, "put_otm_stress_vix": 20.0,
                     "put_otm_floor_stress": 0.03, "put_otm_target_stress": 0.06, "put_otm_cap_stress": 0.13},
    "tier_std":     {**_BASE, "put_otm_tier_enabled": True, "put_otm_stress_vix": 20.0,
                     "put_otm_floor_stress": 0.04, "put_otm_target_stress": 0.07, "put_otm_cap_stress": 0.15},
    "tier_strong":  {**_BASE, "put_otm_tier_enabled": True, "put_otm_stress_vix": 20.0,
                     "put_otm_floor_stress": 0.05, "put_otm_target_stress": 0.08, "put_otm_cap_stress": 0.16},
    "tier_bearonly": {**_BASE, "put_otm_tier_enabled": True, "put_otm_stress_vix": 99.0,
                     "put_otm_floor_stress": 0.04, "put_otm_target_stress": 0.07, "put_otm_cap_stress": 0.15},
}
BASELINE = "baseline"
MIN_ANNUAL = 15.5
LIVE_PUT_FLOOR = 0.50


def _as_date(v):
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v), "%Y-%m-%d").date()


def _annualized(final_return_pct, days):
    if days <= 0:
        return 0.0
    return ((1 + final_return_pct / 100.0) ** (365.0 / days) - 1) * 100.0


def run_one(reg, universe, earnings, overrides):
    market = load_market(reg, universe)
    if not market:
        return {"final_return_pct": None, "error": "no_data"}
    if getattr(reg, "halts", None):
        from marswalk.synthetic import apply_halts
        market = apply_halts(market, reg.halts, gap_open_pct=getattr(reg, "gap_open_pct", 0.0) or 0.0)
    if getattr(reg, "shocks", None):
        from marswalk.synthetic import apply_shocks
        market = apply_shocks(market, reg.shocks)
    pm = getattr(reg, "price_multiplier", None)
    if pm and pm != 1.0:
        market = {sym: [(d, c * pm, iv) for (d, c, iv) in bars] for sym, bars in market.items()}
    p = Params(**overrides)
    res = run_regime(reg.id, reg.name, reg.category, reg.rank, universe, market, p,
                     earnings=earnings, cash_yield_annual=getattr(reg, "cash_yield_annual", None))
    if not res:
        return {"final_return_pct": None, "error": "engine_returned_none"}
    n = res["n_trades"]; nps = res.get("n_puts_sold", 0)
    days = (_as_date(reg.end) - _as_date(reg.start)).days
    return {
        "final_return_pct": res["final_return_pct"],
        "annualized_pct": _annualized(res["final_return_pct"], days),
        "max_drawdown_pct": res.get("max_drawdown_pct", 0),
        "n_trades": n, "n_assignments": res["n_assignments"], "n_puts_sold": nps,
        "put_assign_rate": (res["n_assignments"] / nps) if nps else None,
        "avg_put_premium": (res.get("put_bid_sum", 0.0) / nps) if nps else None,
    }


def _cell(r):
    if r["final_return_pct"] is None:
        return "ERR".rjust(22)
    ar = r["put_assign_rate"]; ar_s = f"{ar:.2f}" if ar is not None else "n/a"
    return f"{r['final_return_pct']:+.1f}/{r['max_drawdown_pct']:.1f}/{ar_s}".rjust(22)


def _delta_block(title, group, results):
    cand = [c for c in CONFIGS if c != BASELINE]
    sums = {c: 0.0 for c in cand}; dd_sums = {c: 0.0 for c in cand}
    print(f"  -- {title} --")
    for rid in group:
        base = results.get(rid, {}).get(BASELINE)
        if not base or base["final_return_pct"] is None:
            print(f"    {rid.ljust(16)} n/a"); continue
        row = []
        for c in cand:
            a = results[rid].get(c)
            if not a or a["final_return_pct"] is None:
                row.append(f"{c}=ERR".rjust(26)); continue
            dret = a["final_return_pct"] - base["final_return_pct"]
            ddd = a["max_drawdown_pct"] - base["max_drawdown_pct"]
            aar = (a["put_assign_rate"] or 0.0) - (base["put_assign_rate"] or 0.0)
            sums[c] += dret; dd_sums[c] += ddd
            row.append(f"{c}={dret:+.1f}/{ddd:+.1f}/{aar:+.2f}".rjust(26))
        print(f"    {rid.ljust(16)}" + "  ".join(row))
    if len(group) > 1:
        print(f"  {title}-sum Δret:  " + "  ".join(f"{c}={sums[c]:+.1f}" for c in cand))
        print(f"  {title}-sum ΔDD :  " + "  ".join(f"{c}={dd_sums[c]:+.1f}" for c in cand))
    print()


def _verdict(results):
    rep = results.get(REPLAY, {}); base = rep.get(BASELINE)
    print("=" * 124)
    print("VERDICT — live_replay_2020 (WIN: annualized >= %.1f%%/yr AND fewer assignments than baseline)" % MIN_ANNUAL)
    print("=" * 124)
    if not base or base["final_return_pct"] is None:
        print("  baseline failed."); return
    print(f"  {'variant'.ljust(13)} {'annual%':>8} {'total%':>9} {'maxDD':>6} "
          f"{'n_asgn':>7} {'put_rate':>9} {'avg_prem':>9}  verdict")
    base_asgn = base["n_assignments"]; winners = []
    for name in CONFIGS:
        r = rep.get(name)
        if not r or r["final_return_pct"] is None:
            print(f"  {name.ljust(13)} {'ERR':>8}"); continue
        ann = r["annualized_pct"]; prem = r["avg_put_premium"] or 0.0
        holds = ann >= MIN_ANNUAL; fewer = r["n_assignments"] < base_asgn
        tag = ["(baseline)"] if name == BASELINE else [
            "HOLDS" if holds else "below-floor",
            "fewer-asgn" if fewer else "not-fewer",
        ]
        if name != BASELINE and prem < LIVE_PUT_FLOOR:
            tag.append(f"PREM<{LIVE_PUT_FLOOR}!")
        if name != BASELINE and holds and fewer and prem >= LIVE_PUT_FLOOR:
            winners.append((r["n_assignments"], name, ann))
        print(f"  {name.ljust(13)} {ann:>7.1f}% {r['final_return_pct']:>+8.1f}% "
              f"{r['max_drawdown_pct']:>5.1f}% {r['n_assignments']:>7d} "
              f"{(r['put_assign_rate'] or 0):>9.3f} {prem:>9.2f}  {' '.join(tag)}")
    print()
    if winners:
        winners.sort()
        n_asgn, name, ann = winners[0]
        print(f"  >>> WINNER: {name} — {n_asgn} assignments (vs {base_asgn} baseline, "
              f"−{base_asgn - n_asgn}) at {ann:.1f}%/yr. Strictly better than any flat window.")
    else:
        print("  >>> No tier variant holds 15.5%/yr with fewer assignments on the replay.")
    print()


def main():
    universe, regimes = load_config()
    by_id = {r.id: r for r in regimes}
    earnings = load_earnings(universe)
    out_path = Path("data") / f"put_otm_tier_sweep_{date.today().strftime('%Y%m%d')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)

    cfg_names = list(CONFIGS.keys())
    print()
    print(f"VIX-tiered OTM sweep — {len(TARGETS)}+1 regimes × {len(cfg_names)} configs "
          f"(base={BASELINE}); cell = ret/maxDD/put_asgn_rate")
    print("=" * 124)
    hdr = "regime".ljust(16) + "cat".ljust(8) + "  ".join(c.rjust(22) for c in cfg_names)
    print("  " + hdr); print("  " + "-" * len(hdr))

    results = {}
    for rid in TARGETS + [REPLAY]:
        reg = by_id.get(rid)
        if not reg:
            print(f"  {rid.ljust(16)} MISSING"); continue
        cat = "REPLAY" if rid == REPLAY else ("CRASH" if rid in CRASH else "benign")
        results[rid] = {}; cols = []
        for cfg_name, ov in CONFIGS.items():
            r = run_one(reg, universe, earnings, ov)
            results[rid][cfg_name] = r
            with out_path.open("a") as fh:
                fh.write(json.dumps({"regime": rid, "config": cfg_name, **r}) + "\n")
            cols.append(_cell(r))
        print("  " + rid.ljust(16) + cat.ljust(8) + "  ".join(cols))

    print()
    print("=" * 124)
    print(f"Δ vs {BASELINE}  [Δret pp / ΔmaxDD pp / Δput_asgn_rate]")
    print("=" * 124)
    _delta_block("CRASH", CRASH, results)
    _delta_block("BENIGN", BENIGN, results)
    _delta_block("REPLAY", [REPLAY], results)
    _verdict(results)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
