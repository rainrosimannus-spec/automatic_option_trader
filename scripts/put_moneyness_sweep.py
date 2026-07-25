"""Short-put MONEYNESS sweep (2026-07-25).

Rain reports too many put assignments and wants fewer WITHOUT dropping below the
~15.5%/yr the wheel has produced over the last 6 years. The real assignment-
distance knob for the 0-3 DTE USD book is the moneyness window in
option_scoring.py (2%/5%/12% floor/target/cap) — NOT delta (which MarsWalk's put
model is blind to, proven identical in wheel-assignment-lever3a). This sweep
varies that window, holding DTE fixed at 0-3 and the live crisis config fixed,
across BENIGN + CRASH regimes AND the decisive 6-year `live_replay_2020`.

Ship criterion (STRICT, from the approved plan): pick the LOWEST-assignment
variant that still annualizes >= 15.5% on live_replay_2020 AND does not worsen
crash max-DD materially. Prefer raise-floor over push-target when both qualify
(raise-floor keeps the 5% premium target, so it gives up less premium — the
fee-floor guardrail below).

FEE-FLOOR GUARDRAIL: MarsWalk fills at bid with NO commission floor
(engine.py ~966/1445), so it OVERSTATES far-OTM viability — small premiums that
would fail the live $0.50 min_premium_put look free here. We therefore report
avg_put_premium (put_bid_sum / n_puts_sold); a winner whose avg premium drifts
toward the live floor is suspect regardless of its in-sim return.

Also reports the TRUE put-assignment rate n_assignments / n_puts_sold (the old
n_assignments/n_trades was diluted by CC + strangle legs).

READ-ONLY against the live engine — only writes a results JSONL.

Usage:
    source .venv/bin/activate
    PYTHONPATH=src python3 scripts/put_moneyness_sweep.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path


def _as_date(v):
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v), "%Y-%m-%d").date()

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from marswalk.data import load_earnings, load_market
from marswalk.engine import Params, run_regime
from marswalk.regimes import load_config

BENIGN = ["bull_2021", "grind_2024h1", "chop_2023h2", "iran_war_2026"]
CRASH = ["covid_2020", "svb_2023", "q4_2018", "bear_2022", "gfc_2008", "stacked_2x", "blackout_3day"]
REPLAY = "live_replay_2020"          # decisive 6y continuous, run last & separately
TARGETS = BENIGN + CRASH

# Live crisis config (strangle on) — fixed; vary only the put moneyness window.
_CRISIS = {"high_vol_grind_enabled": True, "strangle_when_grind": True,
           "crash_when_active_enabled": True, "crash_strangle_when_active": True}

# (floor, target, cap). baseline reproduces the historical hardcoded window.
_WINDOWS = {
    "baseline": (0.02, 0.05, 0.12),   # current live
    "floor_3":  (0.03, 0.05, 0.12),   # kill the nearest-ATM tail only
    "floor_4":  (0.04, 0.06, 0.13),   # wider min-distance
    "target_7": (0.03, 0.07, 0.15),   # push the scorer peak OTM
    "target_8": (0.05, 0.08, 0.15),   # aggressively OTM (return-give-up check)
}
CONFIGS = {
    name: {**_CRISIS, "dte_min": 0, "dte_max": 3,
           "put_otm_floor": f, "put_otm_target": t, "put_otm_cap": c}
    for name, (f, t, c) in _WINDOWS.items()
}
BASELINE = "baseline"
MIN_ANNUAL = 15.5                     # strict ship floor (%/yr on the replay)
LIVE_PUT_FLOOR = 0.50                 # strategy.min_premium_put — fee-floor witness


def _annualized(final_return_pct: float, days: int) -> float:
    """Mirror the web route (marswalk.py:216-219)."""
    if days <= 0:
        return 0.0
    return ((1 + final_return_pct / 100.0) ** (365.0 / days) - 1) * 100.0


def run_one(reg, universe, earnings, overrides: dict) -> dict:
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
    n = res["n_trades"]
    nps = res.get("n_puts_sold", 0)
    days = (_as_date(reg.end) - _as_date(reg.start)).days
    return {
        "final_return_pct": res["final_return_pct"],
        "annualized_pct": _annualized(res["final_return_pct"], days),
        "max_drawdown_pct": res.get("max_drawdown_pct", 0),
        "n_trades": n,
        "n_assignments": res["n_assignments"],
        "n_puts_sold": nps,
        "put_assign_rate": (res["n_assignments"] / nps) if nps else None,
        "avg_put_premium": (res.get("put_bid_sum", 0.0) / nps) if nps else None,
        "assignment_rate_all": (res["n_assignments"] / n) if n else None,
        "n_crash_days": res.get("n_crash_days", 0),
    }


def _cell(r) -> str:
    if r["final_return_pct"] is None:
        return "ERR".rjust(22)
    ar = r["put_assign_rate"]
    ar_s = f"{ar:.2f}" if ar is not None else "n/a"
    return f"{r['final_return_pct']:+.1f}/{r['max_drawdown_pct']:.1f}/{ar_s}".rjust(22)


def _delta_block(title, group, results):
    cand = [c for c in CONFIGS if c != BASELINE]
    sums = {c: 0.0 for c in cand}
    dd_sums = {c: 0.0 for c in cand}
    print(f"  -- {title} --")
    for rid in group:
        base = results.get(rid, {}).get(BASELINE)
        if not base or base["final_return_pct"] is None:
            print(f"    {rid.ljust(16)} n/a"); continue
        row = []
        for c in cand:
            a = results[rid].get(c)
            if not a or a["final_return_pct"] is None:
                row.append(f"{c}=ERR".rjust(24)); continue
            dret = a["final_return_pct"] - base["final_return_pct"]
            ddd = a["max_drawdown_pct"] - base["max_drawdown_pct"]
            bar = base["put_assign_rate"] or 0.0
            aar = a["put_assign_rate"] or 0.0
            sums[c] += dret; dd_sums[c] += ddd
            row.append(f"{c}={dret:+.2f}/{ddd:+.1f}/{aar - bar:+.2f}".rjust(24))
        print(f"    {rid.ljust(16)}" + "  ".join(row))
    if len(group) > 1:
        print(f"  {title}-sum Δret:  " + "  ".join(f"{c}={sums[c]:+.2f}" for c in cand))
        print(f"  {title}-sum ΔDD :  " + "  ".join(f"{c}={dd_sums[c]:+.1f}" for c in cand))
    print()


def _verdict(results):
    """Apply the strict ship criterion to the REPLAY row."""
    rep = results.get(REPLAY, {})
    base = rep.get(BASELINE)
    print("=" * 122)
    print("VERDICT — live_replay_2020 (strict: annualized >= %.1f%%/yr AND fewer assignments)" % MIN_ANNUAL)
    print("=" * 122)
    if not base or base["final_return_pct"] is None:
        print("  baseline failed — cannot judge."); return
    print(f"  {'variant'.ljust(10)} {'annual%':>8} {'total%':>9} {'maxDD':>6} "
          f"{'n_asgn':>7} {'put_rate':>9} {'avg_prem':>9}  verdict")
    base_asgn = base["n_assignments"]
    qualifiers = []
    for name in CONFIGS:
        r = rep.get(name)
        if not r or r["final_return_pct"] is None:
            print(f"  {name.ljust(10)} {'ERR':>8}"); continue
        ann = r["annualized_pct"]
        prem = r["avg_put_premium"] or 0.0
        holds = ann >= MIN_ANNUAL
        fewer = r["n_assignments"] < base_asgn
        prem_ok = prem >= LIVE_PUT_FLOOR
        tag = []
        if name == BASELINE:
            tag.append("(baseline)")
        else:
            tag.append("HOLDS" if holds else "below-floor")
            tag.append("fewer-asgn" if fewer else "not-fewer")
            if not prem_ok:
                tag.append(f"PREM<{LIVE_PUT_FLOOR}!")
            if holds and fewer and prem_ok:
                qualifiers.append((r["n_assignments"], name, ann))
        print(f"  {name.ljust(10)} {ann:>7.1f}% {r['final_return_pct']:>+8.1f}% "
              f"{r['max_drawdown_pct']:>5.1f}% {r['n_assignments']:>7d} "
              f"{(r['put_assign_rate'] or 0):>9.3f} {prem:>9.2f}  {' '.join(tag)}")
    print()
    if qualifiers:
        qualifiers.sort()  # lowest n_assignments first
        n_asgn, name, ann = qualifiers[0]
        print(f"  >>> WINNER: {name} — {n_asgn} assignments (vs {base_asgn} baseline, "
              f"−{base_asgn - n_asgn}), {ann:.1f}%/yr. Prefer raise-floor if tied.")
    else:
        print("  >>> NO variant holds the 15.5%/yr floor with fewer assignments — SHIP NOTHING.")
        print("      The assignments are the return; present the tradeoff curve above.")
    print()


def main():
    universe, regimes = load_config()
    by_id = {r.id: r for r in regimes}
    earnings = load_earnings(universe)

    out_path = Path("data") / f"put_moneyness_sweep_{date.today().strftime('%Y%m%d')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)

    cfg_names = list(CONFIGS.keys())
    print()
    print(f"Put moneyness sweep — {len(TARGETS)}+1 regimes × {len(cfg_names)} configs "
          f"(base={BASELINE}); cell = ret/maxDD/put_asgn_rate")
    print("=" * 122)
    hdr = "regime".ljust(16) + "cat".ljust(8) + "  ".join(c.rjust(22) for c in cfg_names)
    print("  " + hdr)
    print("  " + "-" * len(hdr))

    results = {}
    for rid in TARGETS + [REPLAY]:
        reg = by_id.get(rid)
        if not reg:
            print(f"  {rid.ljust(16)} MISSING"); continue
        cat = "REPLAY" if rid == REPLAY else ("CRASH" if rid in CRASH else "benign")
        results[rid] = {}
        cols = []
        for cfg_name, ov in CONFIGS.items():
            r = run_one(reg, universe, earnings, ov)
            results[rid][cfg_name] = r
            with out_path.open("a") as fh:
                fh.write(json.dumps({"regime": rid, "config": cfg_name, **r}) + "\n")
            cols.append(_cell(r))
        print("  " + rid.ljust(16) + cat.ljust(8) + "  ".join(cols))

    print()
    print("=" * 122)
    print(f"Δ vs {BASELINE}  [Δret pp / ΔmaxDD pp / Δput_asgn_rate].  Want: fewer assignments, Δret ~0, no crash-DD blowup")
    print("=" * 122)
    _delta_block("CRASH", CRASH, results)
    _delta_block("BENIGN", BENIGN, results)
    _delta_block("REPLAY", [REPLAY], results)
    _verdict(results)
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
