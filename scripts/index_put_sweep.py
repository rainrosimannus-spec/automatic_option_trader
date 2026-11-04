"""Phase 2 — cash-settled index put sleeve sweep (XSP proxy = SPY closes, VIX-flat pricing).

Grid: sigma_k × DTE × budget share, in two modes:
  index_only  — the single-name wheel OFF, only the sleeve trades (what the sleeve earns alone)
  both        — sleeve beside the Phase-1 wheel (stop 8% + unwind + realised floor k1.0 + cap 15%/20%)
Baselines: wheel-only Phase-1 set, and the untouched legacy wheel.

Pricing is BSM at VIX × iv_mult with NO skew and NO short-DTE uplift — deliberately conservative:
real 0-2 DTE index puts 1-3% OTM trade above flat-VIX theory. Settlement is at the daily
close; the intraday path is not modelled. Fees: $1/contract/leg. Read relatively.

    .venv/bin/python scripts/index_put_sweep.py
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

import yaml  # noqa: E402

from marswalk.data import fetch_index_yahoo, fetch_vix_yahoo, load_earnings  # noqa: E402
from marswalk.regimes import load_config  # noqa: E402
from wheel_rules_sweep import (LIVE, LIVE_CAPITAL, LIVE_REGIME, REPLAY, run_one)  # noqa: E402

REGIMES = [REPLAY, LIVE, "bear_2022", "covid_2020", "q4_2018", "gfc_2008",
           "bull_2021", "volmageddon_2018", "tariff_2025"]

PHASE1 = {"wheel_stop_loss_enabled": True, "wheel_stop_loss_pct": 0.08,
          "cc_package_unwind_enabled": True,
          "put_otm_floor_mode": "realized", "put_otm_realized_k": 1.0,
          "max_single_name_notional_pct": 0.15, "max_single_name_one_lot_pct": 0.20}

CONFIGS: dict[str, dict] = {"legacy_wheel": {}, "phase1_wheel": dict(PHASE1)}
for k in (1.5, 2.0, 2.5, 3.0):
    for dte in (1, 2, 3):
        for b in (0.10, 0.20, 0.30):
            base = {"index_put_enabled": True, "index_put_sigma_k": k, "index_put_dte": dte,
                    "index_put_max_open": dte, "index_put_budget_pct": b}
            CONFIGS[f"idx_k{k}_d{dte}_b{b:.2f}"] = {**base, "index_put_only": True}
            CONFIGS[f"both_k{k}_d{dte}_b{b:.2f}"] = {**PHASE1, **base}

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
    if rid == LIVE:
        reg, uni, cap = LIVE_REGIME, live_universe, LIVE_CAPITAL
    else:
        reg, uni, cap = by_id[rid], base_universe, None
    key = "live" if rid == LIVE else "base"
    if key not in _CACHE["earn"]:
        _CACHE["earn"][key] = load_earnings(uni)
    r = run_one(reg, uni, _CACHE["earn"][key], CONFIGS[cname], start_capital=cap)
    return {"regime": rid, "config": cname, **r}


def main():
    out_path = Path("data") / f"index_put_sweep_{date.today().strftime('%Y%m%d')}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _, regimes = load_config()
    by_id = {r.id: r for r in regimes}
    for rid in REGIMES:
        reg = LIVE_REGIME if rid == LIVE else by_id[rid]
        try:
            fetch_index_yahoo(reg); fetch_vix_yahoo(reg)
        except Exception as e:
            print(f"  backfill {rid}: {e}")
    names = list(CONFIGS)
    if "--quick" in sys.argv:
        names = ["legacy_wheel", "phase1_wheel", "idx_k2.0_d2_b0.20", "both_k2.0_d2_b0.20"]
    jobs = [(rid, c) for rid in REGIMES for c in names]
    jobs.sort(key=lambda j: 0 if j[0] == REPLAY else 1)
    print(f"\nIndex-put sweep — {len(jobs)} runs across {os.cpu_count()} cores", flush=True)
    rows = []
    with ProcessPoolExecutor(max_workers=min(48, os.cpu_count() or 8)) as ex:
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
            if n % 50 == 0:
                print(f"  …{n}/{len(jobs)}", flush=True)
    _report(rows)
    print(f"\nwrote {out_path}")


def _report(rows):
    import marswalk.engine as eng  # noqa: F401
    for rid in REGIMES:
        rr = [r for r in rows if r["regime"] == rid and r.get("final_return_pct") is not None]
        if not rr:
            continue
        print(f"\n  {rid}")
        print(f"    {'config':22} {'return':>9} {'ann%':>7} {'maxDD':>6} {'idxPuts':>7} {'ITM':>4} "
              f"{'stops':>5} {'idxPrem$':>10} {'idxIntr$':>10} {'wheelPuts':>9}")
        for r in sorted(rr, key=lambda x: list(CONFIGS).index(x["config"])):
            print(f"    {r['config']:22} {r['final_return_pct']:>+8.2f}% {r['annualized_pct']:>+6.1f} "
                  f"{r['max_drawdown_pct']:>5.1f}% {r.get('n_index_puts', 0):>7d} {r.get('n_index_itm', 0):>4d} "
                  f"{r.get('n_index_stops', 0):>5d} {r.get('index_premium_sum', 0):>10,.0f} "
                  f"{r.get('index_intrinsic_sum', 0):>10,.0f} {r['n_puts_sold']:>9d}")


if __name__ == "__main__":
    main()
