"""Concentration caps for the compounder target sizing (findings #4).

Covers the per-name absolute $ ceiling on top of the pct caps, and the per-sector cap that
scales an over-concentrated sector's targets down (new-buy sizing only)."""
from src.portfolio import compounder as cmp
from src.portfolio.compounder import RankedName


def _ranked(n: int):
    return [RankedName(f"S{i}", "growth", 90 - i, 0.5, 90 - i, 100.0, 90.0, 110.0)
            for i in range(n)]


def test_abs_ceiling_binds_above_pct_cap():
    ranked = _ranked(5)
    tb = {"growth": 1.0}
    # leader pct cap = 10% of $20M = $2M; the $750k absolute ceiling must bind instead.
    t = cmp.target_weights(ranked, tb, 20_000_000, 0.06, leader_syms={"S0"},
                           leader_cap_pct=0.10, conviction_power=1.75,
                           abs_ceiling=750_000)
    assert round(t["S0"]) == 750_000
    # every name is ceilinged, so none exceeds the absolute cap
    assert all(v <= 750_000 + 1 for v in t.values())


def test_abs_ceiling_noop_at_small_nlv():
    ranked = _ranked(5)
    tb = {"growth": 1.0}
    base = cmp.target_weights(ranked, tb, 100_000, 0.06, leader_syms={"S0"},
                              leader_cap_pct=0.10, conviction_power=1.75)
    ceil = cmp.target_weights(ranked, tb, 100_000, 0.06, leader_syms={"S0"},
                              leader_cap_pct=0.10, conviction_power=1.75,
                              abs_ceiling=750_000)
    assert base == ceil  # $750k ceiling never binds on a $100k book


def test_sector_cap_scales_overweight_sector():
    tgt = {"A": 400_000.0, "B": 400_000.0, "C": 400_000.0, "D": 200_000.0}
    sec = {"A": "semis", "B": "semis", "C": "semis", "D": "software"}
    capped = cmp.apply_sector_caps(tgt, sec, 0.30 * 2_000_000)  # cap = $600k
    assert round(sum(capped[s] for s in "ABC")) == 600_000
    assert capped["D"] == 200_000.0  # under-cap sector untouched
    # proportional scale-down preserves within-sector ordering
    assert capped["A"] == capped["B"] == capped["C"]


def test_sector_cap_skips_unknown_sector():
    # blank sector can't be attributed → never capped (avoids lumping unrelated names)
    assert cmp.apply_sector_caps({"X": 999.0}, {"X": ""}, 100.0) == {"X": 999.0}


def test_sector_cap_disabled_when_nonpositive():
    tgt = {"A": 5.0, "B": 5.0}
    assert cmp.apply_sector_caps(tgt, {"A": "x", "B": "x"}, 0.0) == tgt


def _budget(deployed, target=900_000.0, investable=1_000_000.0, crash=False,
            deployed_today=0.0, lump_horizon=126, throttle=1.0):
    return cmp.daily_deploy_budget(
        investable, 0.9, 21, 0.0, deployed, target, crash, free_cash=10_000_000.0,
        deployed_today=deployed_today, lump_horizon_days=lump_horizon, pace_throttle=throttle)


def test_lump_deploys_far_slower_than_routine_topup():
    # Fresh full lump (nothing deployed, gap == whole base): should pace over ~lump_horizon (126d),
    # i.e. ~base/126 per day — NOT the ~base/21 the old fixed horizon gave.
    lump_day = _budget(deployed=0.0)
    assert abs(lump_day - 900_000 / 126) < 50          # ≈ $7,143/day
    assert lump_day < (900_000 / 21) / 5               # >5x slower than the old 21-day pace
    # A small routine top-up (gap tiny) still deploys quickly — the whole small gap at once.
    topup_day = _budget(deployed=895_000.0)            # $5k gap
    assert abs(topup_day - 5_000) < 1                   # deploys the small gap, not stretched


def test_froth_throttle_scales_base_pace():
    full = _budget(deployed=0.0, throttle=1.0)
    quarter = _budget(deployed=0.0, throttle=0.25)
    assert abs(quarter - full * 0.25) < 1               # throttle multiplies the base pace
    assert _budget(deployed=0.0, throttle=0.0) == 0.0   # hard pause when throttle floored to 0


def test_base_daily_pace_matches_budget_and_can_fall_below_min_order():
    # at $11M a froth-throttled fresh lump paces well below a $44k min order → would stall without accrual
    inv = 10_670_000.0   # 11M * 0.97
    full = cmp.base_daily_pace(inv, 0.9, 21, remaining_gap=inv * 0.9, deployed_today=0.0,
                               lump_horizon_days=126, pace_throttle=1.0)
    throttled = cmp.base_daily_pace(inv, 0.9, 21, remaining_gap=inv * 0.9, deployed_today=0.0,
                                    lump_horizon_days=126, pace_throttle=0.25)
    assert abs(full - inv * 0.9 / 126) < 50          # ≈ $76k/day, fine
    assert abs(throttled - full * 0.25) < 1          # throttle scales it
    assert throttled < 44_000 < full                 # throttled pace is BELOW the $44k min order


def test_accrual_banks_small_pace_into_one_min_order():
    # bank a sub-min daily pace over ceil(min_buy/pace) days → one fee-efficient min_buy chunk
    import math
    base_pace, min_buy = 19_000.0, 44_000.0
    accrual_days = max(1, math.ceil(min_buy / base_pace))     # ceil(2.3) = 3
    assert accrual_days == 3
    # nothing deployed in the window → bank clears the floor → deploy ~min_buy
    banked_fresh = base_pace * accrual_days - 0.0
    assert banked_fresh >= min_buy
    # just deployed a chunk this window → bank below floor → wait (no stall, no churn)
    banked_after = base_pace * accrual_days - 44_000.0
    assert banked_after < min_buy


def _burn_in_clamp(budget, cap, deployed_eff):
    """Mirror of run_compounder_scan's burn-in clamp: cap TOTAL committed capital at `cap`."""
    if cap <= 0:
        return budget                                  # disabled
    return min(budget, max(0.0, cap - deployed_eff))


def test_burn_in_cap_clamps_budget_to_remaining_room():
    # cap $300k, already committed $250k → only $50k of room left regardless of the day's budget
    assert _burn_in_clamp(120_000, cap=300_000, deployed_eff=250_000) == 50_000
    # under the cap with a small budget → unaffected
    assert _burn_in_clamp(40_000, cap=300_000, deployed_eff=100_000) == 40_000
    # at/over the cap → no new deployment (parking still runs in the real scan)
    assert _burn_in_clamp(120_000, cap=300_000, deployed_eff=300_000) == 0.0
    assert _burn_in_clamp(120_000, cap=300_000, deployed_eff=340_000) == 0.0
    # disabled (0) is a no-op — full budget passes through
    assert _burn_in_clamp(120_000, cap=0.0, deployed_eff=9_000_000) == 120_000


def test_burn_in_ceiling_ramps_floor_to_full_then_lifts():
    floor, inv, ramp = 250_000.0, 10_670_000.0, 21
    # day 0 → hold at the floor
    assert cmp.burn_in_ceiling(0, ramp, floor, inv) == floor
    # midway → roughly half of (full - floor) above the floor
    mid = cmp.burn_in_ceiling(ramp // 2, ramp, floor, inv)
    assert abs(mid - (floor + (10 / 21) * (inv - floor))) < 1
    # last day inside the window → just below full
    assert cmp.burn_in_ceiling(ramp - 1, ramp, floor, inv) < inv
    # window elapsed → 0.0 (no cap; caller disarms)
    assert cmp.burn_in_ceiling(ramp, ramp, floor, inv) == 0.0
    assert cmp.burn_in_ceiling(ramp + 5, ramp, floor, inv) == 0.0
    # ceiling never dips below the floor even with a tiny investable
    assert cmp.burn_in_ceiling(0, ramp, floor, investable=100_000.0) == floor


def _should_arm(total_dep, seen, trigger):
    """Mirror of the buyer's arm trigger: a cumulative-deposit jump >= trigger arms the burn-in."""
    return (total_dep - seen) >= trigger


def test_burn_in_arms_on_large_deposit_only():
    # a $1M deposit on top of the $60k baseline arms it; small/zero deltas do not
    assert _should_arm(1_060_000, 60_000, 500_000) is True
    assert _should_arm(60_000, 60_000, 500_000) is False        # no new deposit
    assert _should_arm(110_000, 60_000, 500_000) is False       # $50k top-up, below trigger
    # a deposit-table purge (total drops) never arms
    assert _should_arm(0, 60_000, 500_000) is False


def test_crash_dump_ignores_lump_stretch_and_throttle():
    # In a fired tranche, deploy the full remaining gap regardless of lump horizon / froth throttle.
    b = _budget(deployed=100_000.0, crash=True, throttle=0.0, lump_horizon=126)
    assert b == 800_000.0                                # full remaining gap (900k target - 100k)


def test_queue_orders_green_first_then_by_underweight_gap():
    # mirrors the run_compounder_scan queue sort key: (green-first, then biggest underweight $ gap).
    # tuples: (attractiveness, gap$). gap = tgt - cur; bigger gap fills first.
    rows = [
        (-0.02, 9000.0),   # yellow, huge gap
        (0.05, 3000.0),    # green, small gap
        (0.01, 8000.0),    # green, big gap
        (-0.10, 1000.0),   # yellow, small gap
    ]
    rows.sort(key=lambda x: (0 if x[0] >= 0 else 1, -x[1]))
    # all greens (biggest gap first) before any yellow — greens never wait behind a yellow
    assert rows == [(0.01, 8000.0), (0.05, 3000.0), (-0.02, 9000.0), (-0.10, 1000.0)]
