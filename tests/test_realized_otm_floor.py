"""Rule 3 (2026-11-04): per-name put strike floor from the realised 3-day move."""
from src.core.config import StrategyConfig
from src.strategy.realized_move import forward_declines, realized_otm_floor, resolve_otm_window

FLAT = dict(flat_floor=0.02, flat_target=0.05, flat_cap=0.12)
RK = dict(k=1.0, lookback=10, pctl=0.80, horizon=3)


def test_forward_declines_hand_series():
    c = [100, 98, 97, 99, 101]
    # i=0: min(98,97,99)/100 → 3% decline; i=1: min(97,99,101)/98 → ~1.02%
    d = forward_declines(c, 3)
    assert len(d) == 2
    assert abs(d[0] - 0.03) < 1e-9
    assert abs(d[1] - (1 - 97 / 98)) < 1e-9


def test_realized_floor_scales_with_k_and_respects_base():
    # a calm series: declines ≤ 1% → floor stays at the 2% base
    calm = [100 + (i % 2) * 0.5 for i in range(20)]
    assert realized_otm_floor(calm, base_floor=0.02, **RK) == 0.02
    # a wild series: every 3-day window dips 6% → floor ≈ 6% at k=1, 9% at k=1.5
    wild = []
    for i in range(10):
        wild += [100.0, 94.0]
    f1 = realized_otm_floor(wild, base_floor=0.02, **RK)
    f15 = realized_otm_floor(wild, base_floor=0.02, **{**RK, "k": 1.5})
    assert abs(f1 - 0.06) < 1e-6 and abs(f15 - 0.09) < 1e-6


def test_short_history_returns_none():
    assert realized_otm_floor([100, 99, 98], base_floor=0.02, **RK) is None


def test_flat_mode_is_byte_identical_to_the_configured_window():
    wild = [100.0, 90.0] * 10
    assert resolve_otm_window("flat", wild, **FLAT, **RK) == (0.02, 0.05, 0.12)
    assert resolve_otm_window("realized", None, **FLAT, **RK) == (0.02, 0.05, 0.12)
    assert resolve_otm_window("realized", [100, 99], **FLAT, **RK) == (0.02, 0.05, 0.12)


def test_realized_mode_lifts_floor_and_keeps_target_cap_above_it():
    wild = [100.0, 90.0] * 10          # 10% dips → floor 10%, target 10%, cap 13%
    floor, target, cap = resolve_otm_window("realized", wild, **FLAT, **RK)
    assert abs(floor - 0.10) < 1e-6 and target >= floor and cap >= floor + 0.03 - 1e-9


def test_config_default_is_flat():
    s = StrategyConfig()
    assert s.put_otm_floor_mode == "flat"
    assert (s.put_otm_floor, s.put_otm_target, s.put_otm_cap) == (0.02, 0.05, 0.12)
