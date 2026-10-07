"""Deep-ITM covered-call early-close gate (Rule A, 2026-08-05; rewritten 2026-10-07).

Rain's rule: the close fires ONLY when it beats assignment outright — selling the shares at
spot after buying the call back at the order's limit (ask + 5c) and paying commissions must
net at least the strike. That is the window where a deep-ITM call's time value is zero or
negative. The previous gate tolerated time value up to 0.5% of strike and, on 2026-10-07,
bought the ISRG 390 call back at 24.99 with the stock at 412.61: 2.5/share worse than being
called away two days later ($254). This predicate is mirrored by the MarsWalk engine.
"""
from src.strategy.profit_taker import deep_itm_early_close_triggered, early_close_limit

# Live defaults: over=5%, min_dte=2, fees 3c/share.
KW = dict(over_pct=0.05, min_dte=2, fees_per_share=0.03)


def test_the_isrg_close_that_cost_254_dollars_does_not_fire_any_more():
    # spot 412.61, strike 390, ask 24.50 → limit 24.55; 412.61 − 24.55 − 0.03 = 388.03 < 390
    assert deep_itm_early_close_triggered(412.61, 390.0, 24.50, dte=2, **KW) is False
    # even with the gate's own limit computed from the old 2% pad it would not fire
    assert deep_itm_early_close_triggered(412.61, 390.0, 24.50, dte=2, buy_limit=24.99, **KW) is False


def test_fires_only_when_the_ask_is_at_or_below_parity_after_pad_and_fees():
    # intrinsic 22.61; ask 22.50 → limit 22.55; 412.61 − 22.55 − 0.03 = 390.03 ≥ 390 → fire
    assert deep_itm_early_close_triggered(412.61, 390.0, 22.50, dte=2, **KW) is True
    # ask 22.60 → limit 22.65 → 389.93 < 390 → no
    assert deep_itm_early_close_triggered(412.61, 390.0, 22.60, dte=2, **KW) is False


def test_time_value_exactly_zero_does_not_fire_because_of_pad_and_fees():
    # ask == intrinsic (20.00 on spot 120 / strike 100): limit 20.05 → 120 − 20.05 − 0.03 < 100
    assert deep_itm_early_close_triggered(120.0, 100.0, 20.00, dte=10, **KW) is False
    # it fires once the ask is below parity by at least pad + fees (8c)
    assert deep_itm_early_close_triggered(120.0, 100.0, 19.92, dte=10, **KW) is True
    assert deep_itm_early_close_triggered(120.0, 100.0, 19.93, dte=10, **KW) is False


def test_explicit_buy_limit_is_what_the_gate_judges():
    # the live branch passes the exact limit it will send, so the order can't pay more than judged
    assert deep_itm_early_close_triggered(120.0, 100.0, 19.00, dte=10, buy_limit=19.97, **KW) is True
    assert deep_itm_early_close_triggered(120.0, 100.0, 19.00, dte=10, buy_limit=19.98, **KW) is False


def test_early_close_limit_is_ask_plus_five_cents_not_two_percent():
    assert early_close_limit(24.50) == 24.55          # old formula gave 24.99
    assert early_close_limit(0.10) == 0.15


def test_no_fire_when_not_deep_enough_even_if_cheap():
    assert deep_itm_early_close_triggered(104.0, 100.0, 3.50, dte=10, **KW) is False


def test_dte_floor_blocks_near_expiry_self_resolvers():
    assert deep_itm_early_close_triggered(120.0, 100.0, 19.50, dte=1, **KW) is False
    assert deep_itm_early_close_triggered(120.0, 100.0, 19.50, dte=2, **KW) is True


def test_fails_closed_on_missing_spot_or_ask():
    assert deep_itm_early_close_triggered(None, 100.0, 19.50, dte=10, **KW) is False
    assert deep_itm_early_close_triggered(0.0, 100.0, 19.50, dte=10, **KW) is False
    assert deep_itm_early_close_triggered(120.0, 100.0, 0.0, dte=10, **KW) is False


def test_fees_default_to_zero_when_not_given():
    # 120 − 19.95(limit for ask 19.90) = 100.05 ≥ 100 with no fees; 99.97 < 100 with 8c fees
    assert deep_itm_early_close_triggered(120.0, 100.0, 19.90, dte=10, over_pct=0.05, min_dte=2) is True
    assert deep_itm_early_close_triggered(120.0, 100.0, 19.90, dte=10, over_pct=0.05, min_dte=2, fees_per_share=0.08) is False


def test_marswalk_mirror_uses_the_same_rule():
    import inspect
    from src.marswalk import engine
    src = inspect.getsource(engine)
    assert "cc_early_close_max_extrinsic_pct" not in src
    assert "spot - buy_limit - params.cc_early_close_fees_per_share) < strike" in src
    assert engine.Params().cc_early_close_fees_per_share == 0.03
