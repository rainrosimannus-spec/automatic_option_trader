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


# ── Leverage gates: LOAN (borrowed/NLV) + MAINTENANCE CUSHION (maint/NLV) ────────────────────
from src.portfolio.config import CompounderConfig


def _gates(cc, loan_pct, maint_pct, capitulation=False):
    """Mirror of run_compounder_scan's two-gate block: returns (blocked, soft_scale)."""
    loan_limit = cc.loan_hard_limit_crash_pct if capitulation else cc.loan_hard_limit_pct
    maint_limit = cc.maint_hard_limit_crash_pct if capitulation else cc.maint_hard_limit_pct
    if loan_pct > loan_limit or maint_pct > maint_limit:
        return True, 0.0
    return False, min(cmp.leverage_derate(loan_pct, cc.loan_soft_floor_pct, loan_limit),
                      cmp.leverage_derate(maint_pct, cc.maint_soft_floor_pct, maint_limit))


def test_leverage_derate_curve():
    # 1.0 at/below the soft floor, linear to 0.0 at the hard limit, clamped past it
    assert cmp.leverage_derate(0.0, 10.0, 20.0) == 1.0
    assert cmp.leverage_derate(10.0, 10.0, 20.0) == 1.0
    assert cmp.leverage_derate(15.0, 10.0, 20.0) == 0.5
    assert cmp.leverage_derate(20.0, 10.0, 20.0) == 0.0
    assert cmp.leverage_derate(35.0, 10.0, 20.0) == 0.0
    # collapsed span fails CLOSED (no divide-by-zero, no accidental full-speed deployment)
    assert cmp.leverage_derate(30.0, 20.0, 20.0) == 0.0
    assert cmp.leverage_derate(10.0, 20.0, 20.0) == 1.0


def test_unlevered_fully_invested_book_is_not_braked():
    """THE REGRESSION: the old single gate keyed on maint/NLV at 40% hard / 25% soft, but a
    100%-cash-funded equity book carries ~25-30% maintenance. It would have de-rated, then blocked,
    the tail of a deployment with ZERO borrowing. The loan gate must read clean at loan_pct == 0."""
    cc = CompounderConfig()
    for maint_pct in (23.0, 27.0, 30.0, 35.0):          # the real range for this account
        blocked, scale = _gates(cc, loan_pct=0.0, maint_pct=maint_pct)
        assert blocked is False
        assert scale == 1.0


def test_loan_gate_derates_then_blocks_on_real_borrowing():
    cc = CompounderConfig()
    assert _gates(cc, loan_pct=10.0, maint_pct=30.0) == (False, 1.0)   # at the soft floor
    assert _gates(cc, loan_pct=15.0, maint_pct=30.0) == (False, 0.5)   # halfway to the hard limit
    # the 15%-of-NLV capitulation facility is inside the normal hard limit but past the soft floor
    blocked, scale = _gates(cc, loan_pct=15.0, maint_pct=30.0, capitulation=True)
    assert blocked is False and 0.0 < scale < 1.0
    # runaway loan blocks outright, and capitulation relaxes the limit rather than removing it
    assert _gates(cc, loan_pct=25.0, maint_pct=30.0)[0] is True
    assert _gates(cc, loan_pct=25.0, maint_pct=30.0, capitulation=True)[0] is False
    assert _gates(cc, loan_pct=31.0, maint_pct=30.0, capitulation=True)[0] is True


def test_maintenance_cushion_still_backstops_a_real_squeeze():
    """The cushion gate must not be toothless — it just moved to where a call actually threatens."""
    cc = CompounderConfig()
    assert _gates(cc, loan_pct=0.0, maint_pct=65.0) == (False, 1.0)
    assert _gates(cc, loan_pct=0.0, maint_pct=72.5)[1] == 0.5
    assert _gates(cc, loan_pct=0.0, maint_pct=85.0)[0] is True
    assert _gates(cc, loan_pct=0.0, maint_pct=85.0, capitulation=True)[0] is False   # 88% in crash
    assert _gates(cc, loan_pct=0.0, maint_pct=90.0, capitulation=True)[0] is True


def test_tighter_of_the_two_gates_wins():
    cc = CompounderConfig()
    # loan says 0.5, cushion says 1.0 → 0.5; and the reverse
    assert _gates(cc, loan_pct=15.0, maint_pct=60.0)[1] == 0.5
    assert _gates(cc, loan_pct=0.0, maint_pct=72.5)[1] == 0.5
    # both biting → the tighter one
    assert _gates(cc, loan_pct=12.5, maint_pct=76.25)[1] == 0.25


def _card_burn_cap(manual_cap, armed_date, published_cap):
    """Mirror of the dashboard budget card's cap resolution (src/web/routes/portfolio.py).
    Must match buyer._compounder_burn_in_cap's precedence: manual wins, else the auto-arm ramp,
    and the ramp is only live while an ARMED DATE is set."""
    if manual_cap > 0:
        return manual_cap
    if (armed_date or "").strip():
        return published_cap
    return 0.0


def test_card_burn_cap_precedence_matches_engine():
    # manual operator cap always wins, even with no armed date and no published ramp
    assert _card_burn_cap(300_000, "", 0) == 300_000
    assert _card_burn_cap(300_000, "2026-06-26", 9_000_000) == 300_000
    # auto-arm ramp applies only while armed
    assert _card_burn_cap(0, "2026-06-26", 9_000_000) == 9_000_000
    # THE BUG: disarmed burn-in leaves a stale published cap behind — must NOT clamp the card
    assert _card_burn_cap(0, "", 10_138_253) == 0.0


def test_card_burn_cap_clamps_budget_like_the_engine():
    # card clamp and engine clamp must agree given the same cap and committed capital
    for cap, committed, budget in ((300_000, 250_000, 120_000),
                                   (300_000, 100_000, 40_000),
                                   (300_000, 340_000, 120_000),
                                   (0.0, 9_000_000, 120_000)):
        engine = _burn_in_clamp(budget, cap, committed)
        card = min(budget, max(0.0, cap - committed)) if cap > 0 else budget
        assert engine == card


# ── Buy-queue head published for the dashboard star (src/portfolio/buyer.py) ──────────────
# The buyer now sorts the FULL ranked universe into `candidates`, publishes candidates[0] as
# `compounder_next_buy`, and derives the deploy `queue` by filtering to names whose market is
# open. These tests pin the two properties that makes safe: the sort key is unchanged, and
# filtering after the sort leaves the deploy order exactly as it was.

_QUEUE_KEY = lambda x: (0 if x[0] >= 0 else 1, -(x[3] - x[4]))   # noqa: E731 — mirrors buyer.py


def _cands(rows):
    """rows: (symbol, attractiveness, target, current) → sorted candidate tuples."""
    out = [(a, (t - c) / t if t > 0 else 0.0, sym, t, c) for sym, a, t, c in rows]
    out.sort(key=_QUEUE_KEY)
    return out


def test_next_buy_is_biggest_gap_green():
    c = _cands([("AAA", +0.05, 100_000, 90_000),    # green, gap 10k
                ("BBB", +0.02, 100_000, 40_000),    # green, gap 60k  ← head
                ("CCC", -0.10, 100_000, 0)])        # yellow, gap 100k — biggest, but yellow
    assert c[0][2] == "BBB"
    assert [x[2] for x in c] == ["BBB", "AAA", "CCC"]


def test_next_buy_falls_to_yellow_only_when_no_green_outstanding():
    # A yellow can only reach the head when every green is at target (and so out of the list) —
    # this is what keeps the star consistent with the greens_outstanding gate in the deploy loop.
    c = _cands([("CCC", -0.10, 100_000, 0), ("DDD", -0.01, 100_000, 50_000)])
    assert c[0][2] == "CCC"


def _now_head(cands, open_now, greens_outstanding, crash_active=False, priority_gate=True):
    """Mirror of the buyer's solid-star pick: head of the market-open slice, skipping yellows while
    any green is still outstanding (the gate the deploy loop applies)."""
    queue = [x for x in cands if x[2] in open_now]
    yellow_blocked = (not crash_active) and priority_gate and greens_outstanding
    return next((x[2] for x in queue if x[0] >= 0 or not yellow_blocked), "")


def test_market_open_filter_preserves_deploy_order():
    rows = [("AAA", +0.05, 100_000, 90_000), ("BBB", +0.02, 100_000, 40_000),
            ("EUR1", +0.03, 100_000, 20_000), ("CCC", -0.10, 100_000, 0)]
    c = _cands(rows)
    assert c[0][2] == "EUR1"                       # full-universe head — star shows this
    open_now = {"AAA", "BBB", "CCC"}               # EU shut: EUR1 is not buyable this scan
    queue = [x for x in c if x[2] in open_now]
    assert [x[2] for x in queue] == ["BBB", "AAA", "CCC"]   # same order the old code produced
    assert queue[0][2] != c[0][2]                  # ...and the star is NOT the name bought now


def test_empty_universe_publishes_no_star():
    c = _cands([])
    assert (c[0][2] if c else "") == ""


def test_hollow_and_solid_stars_split_when_leaders_market_is_shut():
    rows = [("6920", +0.06, 100_000, 10_000),      # green, gap 90k — full-universe head, Tokyo shut
            ("NVDA", +0.02, 100_000, 40_000),      # green, gap 60k — biggest tradeable gap
            ("CCC", -0.10, 100_000, 0)]            # yellow
    c = _cands(rows)
    assert c[0][2] == "6920"                                     # ☆ hollow — next in line
    assert _now_head(c, {"NVDA", "CCC"}, greens_outstanding=True) == "NVDA"   # ★ solid — buying now


def test_no_solid_star_when_only_shut_greens_remain():
    # Every open-market name is yellow while a green is outstanding elsewhere → the loop buys
    # NOTHING this scan, so there must be no solid star claiming otherwise.
    c = _cands([("6920", +0.06, 100_000, 10_000), ("CCC", -0.10, 100_000, 0)])
    assert _now_head(c, {"CCC"}, greens_outstanding=True) == ""
    # ...but a crash tranche bypasses the priority gate, so the yellow becomes buyable again
    assert _now_head(c, {"CCC"}, greens_outstanding=True, crash_active=True) == "CCC"


def test_stars_collapse_to_one_when_the_leader_is_tradeable():
    c = _cands([("NVDA", +0.02, 100_000, 40_000), ("CCC", -0.10, 100_000, 0)])
    assert c[0][2] == _now_head(c, {"NVDA", "CCC"}, greens_outstanding=True) == "NVDA"


# ── Core-rung sizing floor (src/portfolio/buyer.py _execute_compounder_buy) ───────────────
# A gap-closing order is sized EXACTLY at its floor (the caller passes min_buy=_eff_floor, which
# IS the whole remaining gap once that gap is under min_buy). int() share truncation then makes the
# achievable notional strictly smaller, so an exact-equality test rejected every such order — the
# 2026-07-30 escape hatch never fired. The floor now tolerates a shortfall of under one share.

def _core_shares(core_amount, core_floor, price, rate, one_share_tolerance=True):
    """Mirror of the core-rung sizing/floor test. Returns shares, or 0 if the rung is dropped."""
    shares = int(core_amount / (price * rate))
    notional_base = shares * price * rate
    floor = core_floor
    if one_share_tolerance and core_amount >= floor:
        floor = max(0.0, floor - price * rate)
    return shares if (shares > 0 and notional_base >= floor) else 0


def test_gap_closing_order_survives_share_truncation():
    # THE BUG, with FSR's real numbers: gap EUR 28,967, ZAR 95.55/share at 0.0485 -> EUR 4.634.
    args = (28_967.0, 28_967.0, 95.55, 0.0485)
    assert _core_shares(*args, one_share_tolerance=False) == 0     # old behaviour: always rejected
    assert _core_shares(*args) == 6250                             # fixed: the gap-closer places


def test_every_sub_min_buy_gap_was_rejected_before_the_fix():
    # Not an FSR quirk — exact-equality fails for any price that doesn't divide the gap evenly.
    for gap, price, rate in ((28_671.0, 95.55, 0.0485),   # FSR
                             (27_512.0, 214.30, 0.0485),  # SBK
                             (13_654.0, 118.40, 1.0),     # WKL (base ccy)
                             (34_847.0, 1042.17, 0.86)):  # LLY
        assert _core_shares(gap, gap, price, rate, one_share_tolerance=False) == 0
        assert _core_shares(gap, gap, price, rate) > 0


def test_tolerance_is_at_most_one_share_so_dust_is_still_blocked():
    # A genuine dust gap under the $2k absolute floor must STILL be rejected — one share of a
    # EUR 4.63 name cannot bridge a EUR 100 shortfall.
    assert _core_shares(1_900.0, 2_000.0, 95.55, 0.0485) == 0
    # ...including a name whose single share (EUR 896) is far larger than the EUR 100 shortfall:
    # the tolerance is gated on the REQUEST clearing the floor, so a sub-floor ask gets none of it.
    assert _core_shares(1_900.0, 2_000.0, 1_042.17, 0.86) == 0


def test_normal_above_min_buy_order_is_unaffected():
    # brick >> floor: truncation is irrelevant, both behaviours agree.
    for tol in (True, False):
        assert _core_shares(76_281.0, 43_385.0, 1_042.17, 0.86, one_share_tolerance=tol) == 85


def test_exactly_divisible_gap_needs_no_tolerance():
    # price*rate divides the gap evenly -> notional == floor exactly; accepted either way.
    assert _core_shares(10_000.0, 10_000.0, 100.0, 1.0, one_share_tolerance=False) == 100
    assert _core_shares(10_000.0, 10_000.0, 100.0, 1.0) == 100


# ── Deploy-queue budget priority (floor_verdict) ────────────────────────────
#
# Live regression, 2026-09-02. The 18:12 scan logged
#     compounder_no_budget_today budget=24943 min_buy=43346
# and then bought OKLO — rank 104 of 104. RKLB (rank 31) sat at the head of the queue with the
# biggest gap in the book and was skipped, along with the 62 names between them, because a name
# whose gap exceeds min_buy carries min_buy as its floor while OKLO's floor is its own small gap.
# So on a short-budget scan the ONLY affordable names are the smallest ones, at the bottom.
#
# The numbers below are the real ones from that scan.

_MIN_BUY = 43_346.0        # 0.4% of $10.84M NLV
_MAX_BUY = 216_732.0       # 2% of NLV
_HARD_FLOOR = 2_000.0      # cc.min_single_buy_floor


def _eff_floor(gap, min_buy=_MIN_BUY, hard_floor=_HARD_FLOOR):
    """The gap-closing escape hatch: a name whose whole gap is under min_buy may close it."""
    return min_buy if gap >= min_buy else max(gap, hard_floor)


def _walk(queue, budget, in_window=lambda _s: True):
    """Walk the deploy queue the way the buy loop does, using the REAL floor_verdict.

    Only the surrounding loop is reproduced here (the loop itself lives inside a ~400-line
    method); every stop/skip/buy decision comes from the shipped function. `queue` is
    (symbol, gap) in queue order — greens sorted by gap, biggest first."""
    spent, bought = 0.0, []
    for sym, gap in queue:
        if spent >= budget:
            break
        brick = min(_MAX_BUY, gap, budget - spent)
        verdict = cmp.floor_verdict(brick, _eff_floor(gap), budget - spent)
        if verdict == "stop":
            break
        if verdict == "skip":
            continue
        if not in_window(sym):
            continue                      # late-session defer — same day, keeps its claim
        bought.append(sym)
        spent += brick
    return bought


def test_a_leader_the_budget_cannot_fund_stops_the_scan():
    """RKLB's real numbers: gap $103,329, floor $43,346, only $24,943 of allowance left."""
    assert cmp.floor_verdict(24_943, 43_346, 24_943) == "stop"


def test_the_bottom_of_the_queue_is_not_fed_from_the_leaders_budget():
    """The exact 18:12 scan. OKLO is affordable on its own — that is the whole trap — but it
    must never be reached, because the budget that would buy it is RKLB's."""
    assert cmp.floor_verdict(19_393, 19_393, 24_943) == "buy"      # affordable in isolation
    queue = [("RKLB", 103_329), ("NBIS", 101_261), ("BWXT", 100_353), ("OKLO", 19_393)]
    assert _walk(queue, budget=24_943) == []                        # was ['OKLO']


def test_foreign_names_keep_their_session_window_advantage():
    """The 04:19 scan, budget $82,445. The US greens ahead are deferred to their own window,
    and Tokyo/Sydney fill behind them exactly as before — this fix must not touch that."""
    queue = [("RKLB", 103_329), ("NBIS", 101_261), ("6920", 42_797), ("XRO", 37_017),
             ("OKLO", 19_393)]
    open_now = {"6920", "XRO"}
    assert _walk(queue, budget=82_445, in_window=lambda s: s in open_now) == ["6920", "XRO"]


def test_the_scan_stops_once_the_remainder_is_too_small_for_anyone_ahead():
    """Tail of that same 04:19 walk: after 6920 and XRO there is $2,631 left, so OKLO's
    gap-closing order is refused rather than part-filled."""
    assert cmp.floor_verdict(2_631, 19_393, 2_631) == "stop"


def test_dust_is_skipped_and_the_walk_continues_past_it():
    """A gap under the absolute hard floor is the NAME being too small, not the budget —
    it must not stop a scan that can still fund everything behind it."""
    assert cmp.floor_verdict(900, 2_000, 90_000) == "skip"
    queue = [("DUST", 900), ("RKLB", 103_329)]
    assert _walk(queue, budget=90_000) == ["RKLB"]


def test_the_leader_takes_the_budget_when_it_can_be_funded():
    queue = [("RKLB", 103_329), ("OKLO", 19_393)]
    assert _walk(queue, budget=60_000) == ["RKLB"]


def test_an_order_that_exactly_meets_its_floor_is_bought():
    assert cmp.floor_verdict(43_346, 43_346, 43_346) == "buy"
    assert cmp.floor_verdict(19_393, 19_393, 19_393) == "buy"


def test_a_budget_equal_to_the_floor_never_stops_the_scan():
    """The stop is for a budget that is genuinely SHORT; equality still funds the name."""
    assert cmp.floor_verdict(19_393, 19_393, 43_346) == "buy"
    assert cmp.floor_verdict(1_000, 2_000, 2_000) == "skip"     # dust, budget not the problem
