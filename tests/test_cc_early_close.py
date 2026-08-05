"""Deep-ITM covered-call early-close gate (Rule A, 2026-08-05).

Rule A automates Rain's manual move: when an assigned lot's stock has run far above
the CC strike and the call's remaining TIME VALUE has decayed to ~0, buy-to-close
the call + sell the stock early — same P&L, but the margin carry stops and capital
frees for new puts. The gate fires only when the give-up (remaining extrinsic) is
tiny AND the lot is genuinely deep ITM AND it's not about to self-resolve. This
predicate is shared logic mirrored by the MarsWalk engine's early-close pass.
"""
from src.strategy.profit_taker import deep_itm_early_close_triggered

# Live defaults: over=8%, max_extrinsic=1% of strike, min_dte=2.
KW = dict(over_pct=0.08, max_extrinsic_pct=0.01, min_dte=2)


def test_fires_deep_itm_with_near_zero_time_value():
    # Stock 20% over a 100 strike; call ask 20.30 → extrinsic 0.30 ≤ 1.0 (1% of 100).
    assert deep_itm_early_close_triggered(120.0, 100.0, 20.30, dte=10, **KW) is True


def test_no_fire_when_extrinsic_still_meaningful():
    # Same deep-ITM stock but the call still carries 2.00 of time value (> 1% of
    # strike) — the pullback-and-keep optionality isn't worthless yet, so we wait.
    assert deep_itm_early_close_triggered(120.0, 100.0, 22.00, dte=10, **KW) is False


def test_no_fire_when_not_deep_enough():
    # Only 3% over strike (< 8% pre-filter) even with zero time value → skip.
    assert deep_itm_early_close_triggered(103.0, 100.0, 3.00, dte=10, **KW) is False


def test_dte_floor_blocks_near_expiry_self_resolvers():
    # Deep ITM + zero extrinsic, but dte=1 (< min_dte=2): it self-resolves for free
    # at expiry, no reason to pay the spread.
    assert deep_itm_early_close_triggered(120.0, 100.0, 20.10, dte=1, **KW) is False
    assert deep_itm_early_close_triggered(120.0, 100.0, 20.10, dte=2, **KW) is True


def test_fails_closed_on_missing_spot():
    # No live spot (None/0) → never act (fail closed — mirrors the naked-short rule
    # of never operating on unknown state).
    assert deep_itm_early_close_triggered(None, 100.0, 20.10, dte=10, **KW) is False
    assert deep_itm_early_close_triggered(0.0, 100.0, 20.10, dte=10, **KW) is False


def test_boundary_extrinsic_is_inclusive():
    # extrinsic exactly == max_extrinsic_pct*strike (1.00 on a 100 strike) → fires.
    assert deep_itm_early_close_triggered(120.0, 100.0, 21.00, dte=10, **KW) is True
    # a hair more (1.01) → does not.
    assert deep_itm_early_close_triggered(120.0, 100.0, 21.01, dte=10, **KW) is False


def test_boundary_moneyness_is_inclusive():
    # spot exactly == strike*(1+over) (108 on 100 strike, over 8%) with ~0 extrinsic.
    assert deep_itm_early_close_triggered(108.0, 100.0, 8.00, dte=10, **KW) is True
    assert deep_itm_early_close_triggered(107.99, 100.0, 8.00, dte=10, **KW) is False


def test_disabled_gate_semantics_are_caller_side():
    # The predicate itself has no enabled flag — the live caller guards on
    # cc_early_close_enabled before calling. A valid deep-ITM setup still returns
    # True here; the OFF-by-default safety lives at the call site.
    assert deep_itm_early_close_triggered(130.0, 100.0, 30.20, dte=5, **KW) is True
