"""Post-sale re-entry cooldown sweep (2026-11-10).

Question (Rain): with the −8% assigned-lot stop live, does re-selling a put on the same
name right after the lot was sold earn "the easy money" of the regain, or does it rebuild
the bag? The live fills say 4 of 11 such re-entries were assigned and two became the CIEN
and VRT bags — but those followed profitable manual exits, not stops. So two scopes:

  scope "stop"      bar the name for N trading days only after the stop fired
  scope "any_sale"  also after unwind / early-close / called-away

N ∈ {5, 10, 20}; baseline = the Phase-1 live set (stop 0.08 + unwind + realized k1.0 +
cap 0.15/0.20) with cooldown 0, so every Δ is against what runs live today.
Reuses the regimes, crisis flags and live-window regime of scripts/wheel_rules_sweep.py.

Usage:
    .venv/bin/python scripts/reentry_cooldown_sweep.py
"""
from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

import yaml  # noqa: E402

import wheel_rules_sweep as W  # noqa: E402
from marswalk.data import fetch_spy_yahoo, fetch_symbols_yahoo, fetch_vix_yahoo, load_earnings  # noqa: E402
from marswalk.regimes import load_config  # noqa: E402

LIVE_SET = {"wheel_stop_loss_enabled": True, "wheel_stop_loss_pct": 0.08,
            "cc_package_unwind_enabled": True,
            "put_otm_floor_mode": "realized", "put_otm_realized_k": 1.0,
            "max_single_name_notional_pct": 0.15, "max_single_name_one_lot_pct": 0.20}

CONFIGS: dict[str, dict] = {"live_set_cd0": dict(LIVE_SET)}
for scope in ("stop", "any_sale"):
    for n in (5, 10, 20):
        CONFIGS[f"{scope}_cd{n}"] = {**LIVE_SET, "put_reentry_cooldown_days": n,
                                     "put_reentry_cooldown_scope": scope}
# reference: the OLD averaging-down world (no stop, no unwind, no cooldown)
CONFIGS["old_no_stop"] = {}

_CACHE: dict = {}


def _job(spec):
    rid, cname = spec
    if "cfg" not in _CACHE:
        base_universe, regimes = load_config()
        live_universe = [s["symbol"] for s in
                         yaml.safe_load(open("config/options_universe.yaml"))["stocks"]
                         if s.get("currency") == "USD"]
        _CACHE["cfg"] = (base_universe, {r.id: r for r in regimes}, live_universe)
        _CACHE["earn"] = {}
    base_universe, by_id, live_universe = _CACHE["cfg"]
    if rid == W.LIVE:
        reg, uni, cap = W.LIVE_REGIME, live_universe, W.LIVE_CAPITAL
    else:
        reg, uni, cap = by_id[rid], base_universe, None
    key = "live" if rid == W.LIVE else "base"
    if key not in _CACHE["earn"]:
        _CACHE["earn"][key] = load_earnings(uni)
    r = W.run_one(reg, uni, _CACHE["earn"][key], CONFIGS[cname], start_capital=cap)
    return {"regime": rid, "config": cname, **r}


def main():
    out_path = Path("data") / f"reentry_cooldown_sweep_{date.today().strftime('%Y%m%d')}.jsonl"
    _, _ = load_config()
    live_universe = [s["symbol"] for s in yaml.safe_load(open("config/options_universe.yaml"))["stocks"]
                     if s.get("currency") == "USD"]
    try:
        fetch_symbols_yahoo(W.LIVE_REGIME, live_universe)
        fetch_vix_yahoo(W.LIVE_REGIME)
        fetch_spy_yahoo(W.LIVE_REGIME)
    except Exception as e:
        print(f"  live regime backfill failed: {e}")
    jobs = [(rid, c) for rid in W.REGIMES for c in CONFIGS]
    jobs.sort(key=lambda j: 0 if j[0] == W.REPLAY else 1)
    print(f"Re-entry cooldown sweep — {len(jobs)} runs across {os.cpu_count()} cores", flush=True)
    rows = []
    with ProcessPoolExecutor(max_workers=min(16, os.cpu_count() or 8)) as ex:
        futs = {ex.submit(_job, j): j for j in jobs}
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result()
            except Exception as e:
                print(f"  JOB FAILED {futs[fut]}: {e}")
                continue
            rows.append(r)
            with out_path.open("a") as fh:
                fh.write(json.dumps(r) + "\n")
            if n % 10 == 0:
                print(f"  …{n}/{len(jobs)}", flush=True)
    report(rows)
    print(f"\nwrote {out_path}")


def report(rows):
    for rid in W.REGIMES:
        rr = [r for r in rows if r["regime"] == rid and r.get("final_return_pct") is not None]
        if not rr:
            continue
        base = next((r for r in rr if r["config"] == "live_set_cd0"), None)
        print(f"\n  {rid}")
        print(f"    {'config':16} {'return':>9} {'ann%':>7} {'maxDD':>6} {'puts':>5} {'asgn':>5} "
              f"{'asg%':>5} {'stops':>5} {'cdskip':>6} {'Δ vs live set':>13}")
        for r in sorted(rr, key=lambda x: list(CONFIGS).index(x["config"])):
            d = (r["final_return_pct"] - base["final_return_pct"]) if base else 0.0
            ar = r["put_assign_rate"]
            print(f"    {r['config']:16} {r['final_return_pct']:>+8.2f}% {r['annualized_pct']:>+6.1f} "
                  f"{r['max_drawdown_pct']:>5.1f}% {r['n_puts_sold']:>5d} {r['n_assignments']:>5d} "
                  f"{(100 * ar if ar else 0):>4.0f}% {r['n_stop_losses']:>5d} {r.get('n_cooldown_skips', 0):>6d} "
                  f"{d:>+12.2f}pp")


if __name__ == "__main__":
    if "--report" in sys.argv:
        rows = [json.loads(l) for l in open(sys.argv[-1])]
        report(rows)
    else:
        main()
