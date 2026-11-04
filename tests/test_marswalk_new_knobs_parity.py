"""The five 2026-11-04 rules ship in MarsWalk with defaults that leave every prior run
byte-identical (verified on tariff_2025 + svb_2023 against the HEAD engine on 2026-11-04)."""
from src.marswalk.engine import Params


def test_sim_defaults_are_inert():
    p = Params()
    assert p.wheel_stop_loss_enabled is False and p.wheel_stop_loss_pct == 0.08
    assert p.cc_package_unwind_enabled is False and p.cc_package_unwind_min_profit_pct == 0.005
    assert p.put_otm_floor_mode == "flat"
    assert p.put_universe_max_breach_rate == 1.0
    assert p.max_single_name_notional_pct == 0.25   # sim keeps the old cap; sweeps pass 0.10


def test_knobs_accept_overrides():
    p = Params(wheel_stop_loss_enabled=True, cc_package_unwind_enabled=True,
               put_otm_floor_mode="realized", put_universe_max_breach_rate=0.30,
               max_single_name_notional_pct=0.10)
    assert p.wheel_stop_loss_enabled and p.cc_package_unwind_enabled
    assert p.put_otm_floor_mode == "realized" and p.put_universe_max_breach_rate == 0.30
