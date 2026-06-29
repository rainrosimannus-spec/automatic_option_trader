"""Unit tests for the pre-buy FX-conversion decision (`_fx_conversion_plan`).

This is the pure, IBKR-free core of the foreign-currency funding fix: it decides whether to place an
IDEALPRO conversion and, if so, the correct BUY/SELL action + quantity given which side the canonical
IBKR pair is quoted. The live bug it replaces built the pair backwards (Forex('HKDEUR'), which IBKR
rejects with Error 200) so every foreign compounder buy churned 'approved' forever.
"""
from src.portfolio.buyer import _fx_conversion_plan


IDEALPRO_MIN_BASE = 22000.0   # ≈ USD 25k, expressed in base ccy (EUR)


def test_below_min_convert_is_funded():
    # Shortfall under the bother-to-convert floor → nothing to do.
    p = _fx_conversion_plan("EUR", "HKD", 500.0, 8.7, "EUR", IDEALPRO_MIN_BASE, min_convert=1000.0)
    assert p["place"] is False
    assert p["reason"] == "funded"


def test_no_rate_is_nonblocking():
    # Can't price the leg → don't place an order; caller proceeds via auto-FX.
    p = _fx_conversion_plan("EUR", "HKD", 50000.0, 0.0, "EUR", IDEALPRO_MIN_BASE)
    assert p["place"] is False
    assert p["reason"] == "no_rate"


def test_below_idealpro_minimum_lets_autofx_fund():
    # The live case: HKD 5,161 ≈ EUR 593 at 8.7 HKD/EUR, far below the IDEALPRO minimum.
    p = _fx_conversion_plan("EUR", "HKD", 5161.0, 8.7, "EUR", IDEALPRO_MIN_BASE)
    assert p["place"] is False
    assert p["reason"] == "below_min"
    assert round(p["base_value"]) == 593


def test_above_minimum_pair_symbol_is_base_sells_base():
    # Canonical pair EUR.HKD (symbol == base) → SELL EUR to receive HKD; qty in EUR.
    shortfall_hkd = 300000.0
    rate = 8.7                      # HKD per EUR
    p = _fx_conversion_plan("EUR", "HKD", shortfall_hkd, rate, "EUR", IDEALPRO_MIN_BASE)
    assert p["place"] is True
    assert p["action"] == "SELL"
    assert p["qty"] == int(round((shortfall_hkd / rate) * 1.01))
    assert p["base_value"] > IDEALPRO_MIN_BASE


def test_above_minimum_pair_symbol_is_ccy_buys_ccy():
    # Canonical pair EUR.USD with base USD, ccy EUR (symbol == ccy) → BUY EUR; qty in EUR.
    shortfall_eur = 50000.0
    rate = 0.92                     # EUR per USD
    p = _fx_conversion_plan("USD", "EUR", shortfall_eur, rate, "EUR", IDEALPRO_MIN_BASE)
    assert p["place"] is True
    assert p["action"] == "BUY"
    assert p["qty"] == int(round(shortfall_eur * 1.01))


def test_qty_is_int():
    p = _fx_conversion_plan("EUR", "HKD", 300000.0, 8.7, "EUR", IDEALPRO_MIN_BASE)
    assert isinstance(p["qty"], int)
