"""Standalone verification for the execution-time budget re-check.

Run: .venv/bin/python tools/verify_budget_recheck.py
Uses a throwaway temp SQLite DB and stubbed account/universe — touches nothing live.
"""
import os, sys, tempfile
from datetime import datetime
from types import SimpleNamespace

# Point the engine at a temp DB BEFORE anything imports get_db.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()

import src.core.database as database
from sqlalchemy import create_engine
database._engine = create_engine(f"sqlite:///{_tmp.name}",
                                 connect_args={"check_same_thread": False})
database._SessionLocal = None
database.init_db()

from src.core.database import get_db
from src.core.models import Position, PositionStatus, SystemState
import src.strategy.risk as riskmod
from src.strategy.risk import RiskManager, adaptive_max_positions

NLV = 117_824.0  # son's account; slot cap tier (<$200K) = 10

def fake_account(**over):
    base = dict(net_liquidation=NLV, buying_power=299_852.0,
                excess_liquidity=51_965.0, cash_balance=-352.0,
                maintenance_margin=65_859.0, available_funds=51_965.0)
    base.update(over)
    return SimpleNamespace(**base)

# Patch account + vix in the risk module namespace.
riskmod.get_account_summary = lambda: fake_account()
riskmod.get_vix = lambda: 18.4  # < 20 → vix_factor 1.0

class StubUniverse:
    """Minimal universe: everything 'TECH' except names in _other."""
    _other = {"XOM"}
    def get_sector(self, sym):
        return "ENERGY" if sym in self._other else "TECH"
    def symbols_in_sector(self, sector):
        # Pretend the whole growth pool is TECH.
        tech = ["SNOW","DDOG","NET","ENPH","PLTR","RKLB","ROKU","SHOP","IBM","NEWSYM","AAA","BBB","CCC"]
        return tech if sector == "TECH" else ["XOM"]
    # check_position_size may call these — keep permissive.
    def get_stock(self, sym): return None

def reset_positions():
    with get_db() as db:
        db.query(Position).delete()
        db.commit()

def add_pos(symbol, ptype, qty=1, strike=None, cost_basis=None):
    with get_db() as db:
        db.add(Position(symbol=symbol, status=PositionStatus.OPEN, position_type=ptype,
                        strike=strike, quantity=qty, cost_basis=cost_basis,
                        opened_at=datetime.utcnow()))
        db.commit()

def working_put(symbol, strike=100.0, status="Submitted"):
    """Fake an IBTrade-like working SELL-put order."""
    return SimpleNamespace(
        contract=SimpleNamespace(symbol=symbol, strike=strike, right="P",
                                 lastTradeDateOrContractMonth="20260612"),
        order=SimpleNamespace(action="SELL", totalQuantity=1),
        orderStatus=SimpleNamespace(status=status),
    )

rm = RiskManager(StubUniverse())
results = []
def check(name, cond):
    results.append((name, cond))
    print(("PASS " if cond else "FAIL ") + name)

# ── Test A: slot budget binds WITH in-flight working orders ──
# 8 open slot-consumers + 2 working puts = 10 >= cap 10 → BLOCK.
reset_positions()
for i in range(8):
    add_pos(f"S{i}", "stock", qty=100, cost_basis=10.0)
r = rm.can_open_put_budget_recheck("NEWSYM", [working_put("AAA"), working_put("BBB")])
check("A slot budget blocks at 8 open + 2 working >= 10",
      (not r.allowed) and "slot budget" in r.reason)

# ── Test B: same DB, but only 1 working order → 8+1=9 < 10, slot gate passes ──
r = rm.can_open_put_budget_recheck("NEWSYM", [working_put("AAA")])
check("B slot gate passes at 9 (<10) — does not block on slots",
      "slot budget" not in (r.reason or ""))

# ── Test C: per-name dup (existing OPEN short_put for the symbol) ──
reset_positions()
add_pos("DUPSYM", "short_put", qty=1, strike=100.0)
r = rm.can_open_put_budget_recheck("DUPSYM", [])
check("C per-name dup blocks when an OPEN short_put exists",
      (not r.allowed) and "per-name dup" in r.reason)

# ── Test C2: per-name dup via a WORKING order (not yet a Position) ──
reset_positions()
r = rm.can_open_put_budget_recheck("WIPSYM", [working_put("WIPSYM")])
check("C2 per-name dup blocks on a working order for the same symbol",
      (not r.allowed) and "per-name dup" in r.reason)

# ── Test D: sector augmentation — working orders push sector over the limit ──
# Sector limit is NLV-tiered now (no anchor): at NLV $117,824 (>=$100K) it returns
# max_sector_pct = 30% directly, so this exercises the 30% cap + the in-flight aug.
reset_positions()
# 4 TECH + 6 non-TECH-ish (use ENERGY 'XOM' duplicated names won't work; emulate via stock in TECH and many totals)
for i in range(2):
    add_pos("PLTR", "short_put", qty=1, strike=100.0)   # 2 TECH
for i in range(8):
    add_pos("XOM", "stock", qty=100, cost_basis=10.0)    # 8 ENERGY
# total=10, tech=2 → (2+1)/(10+1)=27% < 30% adaptive → NEWSYM(TECH) alone OK.
r_noextra = rm.check_sector_exposure("NEWSYM")
# Add 3 in-flight TECH working orders → tech=5, total=13 → (5+1)/(13+1)=43% > 30% → BLOCK.
r_extra = rm.check_sector_exposure("NEWSYM",
                                   extra_open_symbols=["AAA","BBB","CCC"])
check("D sector passes without in-flight, blocks once working TECH added",
      r_noextra.allowed and (not r_extra.allowed) and "Sector" in r_extra.reason)

# ── Test E: reserve gate (bc2439e) untouched — cash<0 but excess_liq high PASSES ──
r = rm.check_buying_power()
check("E check_buying_power PASSES with cash -352 / excess_liq 51,965",
      r.allowed)
# And blocks when excess liquidity falls below the floor.
riskmod.get_account_summary = lambda: fake_account(excess_liquidity=1_000.0)
r = rm.check_buying_power()
check("E2 check_buying_power BLOCKS when excess_liq below 15% reserve",
      not r.allowed)
riskmod.get_account_summary = lambda: fake_account()  # restore

os.unlink(_tmp.name)
ok = all(c for _, c in results)
print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
sys.exit(0 if ok else 1)
