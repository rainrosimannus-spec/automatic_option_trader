"""Marketable, fair-value-capped core-rung pricing (A2).

The core buy used to rest ~0.2% UNDER the last price, so on any uptick it never filled — it just rested
all day blocking the budget, and could drift green→yellow before the next 4h scan. Now the core slides
from a capped marketable PREMIUM (fills at the ask) at full urgency to a deep discount at zero urgency,
and is NEVER priced above fair value (the green/yellow boundary) — "a good buy at 323 may not be at 324".
"""
import pytest

from src.portfolio import compounder as cmp
from src.core.config import get_settings


def _cc():
    return get_settings().portfolio.compounder


def test_fair_value_price_formula():
    # both terms → anti-harmonic blend; one term → that level; neither → None
    assert cmp.fair_value_price(320.0, 454.0) == pytest.approx(2 / (1 / 320 + 1 / 454))
    assert cmp.fair_value_price(150.0, None) == pytest.approx(150.0)
    assert cmp.fair_value_price(None, 200.0) == pytest.approx(200.0)
    assert cmp.fair_value_price(None, None) is None


def test_high_urgency_green_is_marketable_premium():
    cc = _cc()
    last = 322.0
    # comfortably green: fair ≈ 376, well above last → the +premium cap binds (marketable, above last)
    core, frac = cmp.ladder_plan(last, urgency=1.0, is_leader=False, cc=cc,
                                 sma200=320.0, high_52w=454.0)[0]
    assert frac == 1.0
    assert core == pytest.approx(round(last * (1 + cc.entry_marketable_premium_pct / 100.0), 2))
    assert core > last                              # bids THROUGH the market so it fills


def test_never_bids_above_fair_value():
    cc = _cc()
    # barely green: fair just above last, so last+0.5% would cross into yellow → capped at fair
    last = 100.0
    fair = cmp.fair_value_price(100.0, 100.5)        # ≈ 100.25, below last*1.005 = 100.5
    core, _ = cmp.ladder_plan(last, urgency=1.0, is_leader=False, cc=cc,
                              sma200=100.0, high_52w=100.5)[0]
    assert core == pytest.approx(round(fair, 2))
    assert core <= round(last * (1 + cc.entry_marketable_premium_pct / 100.0), 2)


def test_low_urgency_is_deep_discount():
    cc = _cc()
    last = 100.0
    core, _ = cmp.ladder_plan(last, urgency=0.0, is_leader=False, cc=cc,
                              sma200=90.0, high_52w=200.0)[0]   # fair well above → no cap
    assert core == pytest.approx(round(last * (1 - cc.entry_max_discount_pct / 100.0), 2))
    assert core < last                               # patient bid, fine to miss


def test_core_price_monotonic_in_urgency():
    cc = _cc()
    last = 322.0
    prices = [cmp.ladder_plan(last, u, False, cc, sma200=320.0, high_52w=454.0)[0][0]
              for u in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert prices == sorted(prices)                  # higher urgency → higher (more aggressive) bid


def test_no_fair_inputs_uses_premium_uncapped():
    cc = _cc()
    last = 50.0
    core, _ = cmp.ladder_plan(last, urgency=1.0, is_leader=False, cc=cc)[0]   # no sma/high → no cap
    assert core == pytest.approx(round(last * (1 + cc.entry_marketable_premium_pct / 100.0), 2))
