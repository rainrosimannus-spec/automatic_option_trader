"""Deep-stop sweep (2026-11-11): the stop as a BACKSTOP under Rain's net-basis call ladder.

Question: with the ladder (velocity CCs at net basis + package unwind) as the main exit, where
should the assigned-lot stop sit so it sells only what the ladder cannot rescue?
Levels: off, 0.15, 0.20, 0.25 — all with unwind ON, realised floor k1.0, cap 0.15/0.20, i.e. the
live set with the stop moved. 8% row kept for reference. Same regimes as the Phase-1 sweep.

Usage: .venv/bin/python scripts/deep_stop_sweep.py
"""
from __future__ import annotations
import json, os, sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date
from pathlib import Path
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT / "src")); sys.path.insert(0, str(_ROOT)); sys.path.insert(0, str(_ROOT / "scripts"))
import yaml  # noqa: E402
import wheel_rules_sweep as W  # noqa: E402
from marswalk.data import fetch_spy_yahoo, fetch_symbols_yahoo, fetch_vix_yahoo, load_earnings  # noqa: E402
from marswalk.regimes import load_config  # noqa: E402

BASE = {"cc_package_unwind_enabled": True, "put_otm_floor_mode": "realized", "put_otm_realized_k": 1.0,
        "max_single_name_notional_pct": 0.15, "max_single_name_one_lot_pct": 0.20,
        "put_reentry_cooldown_days": 5, "put_reentry_cooldown_scope": "stop"}
CONFIGS = {"ladder_only": {**BASE, "wheel_stop_loss_enabled": False}}
for pct in (0.08, 0.15, 0.20, 0.25):
    CONFIGS[f"stop_{pct:.2f}"] = {**BASE, "wheel_stop_loss_enabled": True, "wheel_stop_loss_pct": pct}
_CACHE: dict = {}


def _job(spec):
    rid, cname = spec
    if "cfg" not in _CACHE:
        base_universe, regimes = load_config()
        live_universe = [s["symbol"] for s in yaml.safe_load(open("config/options_universe.yaml"))["stocks"]
                         if s.get("currency") == "USD"]
        _CACHE["cfg"] = (base_universe, {r.id: r for r in regimes}, live_universe); _CACHE["earn"] = {}
    base_universe, by_id, live_universe = _CACHE["cfg"]
    reg, uni, cap = (W.LIVE_REGIME, live_universe, W.LIVE_CAPITAL) if rid == W.LIVE else (by_id[rid], base_universe, None)
    key = "live" if rid == W.LIVE else "base"
    if key not in _CACHE["earn"]:
        _CACHE["earn"][key] = load_earnings(uni)
    return {"regime": rid, "config": cname, **W.run_one(reg, uni, _CACHE["earn"][key], CONFIGS[cname], start_capital=cap)}


def report(rows):
    for rid in W.REGIMES:
        rr = [r for r in rows if r["regime"] == rid and r.get("final_return_pct") is not None]
        if not rr:
            continue
        base = next((r for r in rr if r["config"] == "ladder_only"), None)
        print(f"\n  {rid}")
        print(f"    {'config':12} {'return':>9} {'ann%':>7} {'maxDD':>6} {'puts':>5} {'asgn':>5} {'stops':>5} {'unw':>4} {'Δ vs ladder':>12}")
        for r in sorted(rr, key=lambda x: list(CONFIGS).index(x["config"])):
            d = (r["final_return_pct"] - base["final_return_pct"]) if base else 0.0
            print(f"    {r['config']:12} {r['final_return_pct']:>+8.2f}% {r['annualized_pct']:>+6.1f} {r['max_drawdown_pct']:>5.1f}% "
                  f"{r['n_puts_sold']:>5d} {r['n_assignments']:>5d} {r['n_stop_losses']:>5d} {r['n_cc_unwinds']:>4d} {d:>+11.2f}pp")


def main():
    out = Path("data") / f"deep_stop_sweep_{date.today().strftime('%Y%m%d')}.jsonl"
    live_universe = [s["symbol"] for s in yaml.safe_load(open("config/options_universe.yaml"))["stocks"] if s.get("currency") == "USD"]
    try:
        fetch_symbols_yahoo(W.LIVE_REGIME, live_universe); fetch_vix_yahoo(W.LIVE_REGIME); fetch_spy_yahoo(W.LIVE_REGIME)
    except Exception as e:
        print(f"  live regime backfill failed: {e}")
    jobs = [(rid, c) for rid in W.REGIMES for c in CONFIGS]
    rows = []
    with ProcessPoolExecutor(max_workers=min(16, os.cpu_count() or 8)) as ex:
        for fut in as_completed({ex.submit(_job, j): j for j in jobs}):
            try:
                r = fut.result()
            except Exception as e:
                print(f"  JOB FAILED: {e}"); continue
            rows.append(r)
            with out.open("a") as fh:
                fh.write(json.dumps(r) + "\n")
    report(rows); print(f"\nwrote {out}")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report([json.loads(l) for l in open(sys.argv[-1])])
    else:
        main()
