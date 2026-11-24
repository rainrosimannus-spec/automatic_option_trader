#!/usr/bin/env python3
"""Replay the account's REAL put fills through MarsWalk's mechanics (2026-11-24 fidelity audit).

  .venv/bin/python scripts/marswalk_replay_real_fills.py [START END]     # default 2026-06-11 2026-11-03

Books exactly the SELL_PUT orders in data/options.db (grouped per symbol / strike / expiry / day at the
real fill price) with none of the engine's own screening, then lets assignment at the close, the
covered-call writer, exits and marks run. Read-only. 2026-11-24: -5.3% vs the account's -5% USD, so
the simulator's mechanics match; every gap between a sim universe and reality is contract SELECTION.
"""

import sys, importlib.util, sqlite3, collections, statistics as st
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("sweep", str(ROOT / "scripts" / "wheel_rules_sweep.py")); sweep = importlib.util.module_from_spec(spec); spec.loader.exec_module(sweep)
from marswalk.engine import Params, run_regime
from marswalk.data import load_market, load_earnings
reg = sweep.LIVE_REGIME
START, END = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("2026-06-11", "2026-11-03")
c = sqlite3.connect(f"file:{ROOT}/data/options.db?mode=ro", uri=True)
rows = c.execute("""select symbol, strike, expiry, substr(created_at,1,10) d, sum(quantity), sum(fill_price*quantity)/sum(quantity)
  from trades where trade_type='SELL_PUT' and date(created_at)>=? and date(created_at)<=? and strike>0 and expiry!=''
  group by symbol, strike, expiry, d""", (START, END)).fetchall()
forced = collections.defaultdict(list)
for sym, K, exp, d, q, px in rows:
    if sym == "WKL": continue
    forced[datetime.strptime(d, "%Y-%m-%d").date()].append({"sym": sym, "strike": K, "expiry": datetime.strptime(exp, "%Y%m%d").date(), "premium": px, "qty": int(q)})
uni = sorted({f["sym"] for fs in forced.values() for f in fs})
market = load_market(reg, uni); earn = load_earnings(uni)
missing = [s for s in uni if s not in market]
print(f"real orders {sum(len(v) for v in forced.values())}, contracts {sum(f['qty'] for v in forced.values() for f in v)}, names {len(uni)}, names without bars {missing}")
LIVE = {"wheel_stop_loss_enabled": True, "wheel_stop_loss_pct": 0.20, "cc_package_unwind_enabled": True, "max_single_name_notional_pct": 0.15}
for label, ov in (("real fills + sim mechanics (CC writer, no stop/unwind)", {}),
                  ("real fills + sim mechanics + live rules (stop 20%, unwind)", LIVE)):
    led = []
    res = run_regime(reg.id, reg.name, reg.category, reg.rank, uni, market, Params(**{**sweep._CRISIS, **ov, "start_capital": 220_000.0}), earnings=earn, ledger=led, forced_puts=dict(forced))
    puts = [e for e in led if e["kind"] == "put_sold"]; asg = [e for e in led if e["kind"] == "put_assigned"]; ccs = [e for e in led if e["kind"] == "cc_sold"]
    prem = sum(e["premium"] * 100 * e["qty"] for e in puts); intr = sum(max(0, e["strike"] - e["close"]) * 100 * e["qty"] for e in asg); ccp = sum(e["premium"] * 100 * e["qty"] for e in ccs)
    print(f"{label:<58} {res['final_return_pct']:+6.1f}%  DD {res['max_drawdown_pct']:4.1f}  puts {len(puts)} ctr {sum(e['qty'] for e in puts)} assigned {len(asg)} ({len(asg)/max(1,len(puts)):.0%})  prem ${prem:,.0f} intrinsic ${intr:,.0f}  CC ${ccp:,.0f} ({len(ccs)})  stops {res['n_stop_losses']} unwinds {res['n_cc_unwinds']}  uncovered lot-days {res['uncovered_lot_days']}")
    if label.startswith("real fills + sim mechanics ("):
        print("   assigned:", collections.Counter(e["sym"] for e in asg).most_common(12))
print("\nREAL: 121 orders / 258 ctr, assigned 33%, premium $42k, intrinsic $53.5k, CC $54k, 22% lot-days uncovered, NLV about −5% USD (Jul 1 → Nov 3)")
