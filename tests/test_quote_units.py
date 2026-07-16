"""Minor-unit quote normalisation (the CFR "cents" bug).

IBKR reports the MAJOR currency code while quoting LSE in pence and the JSE in cents. GBP was
handled by an open-coded `currency == "GBP"` check at each ingest site; ZAR was missed at all of
them, so Richemont (CFR, JSE) sat in the watchlist at 397,199 — R3,971.99 in cents, 100x too
large. Share sizing divided a base brick by the inflated price and produced 0 shares, so the name
could never be bought.

These tests lock in: (1) the normalisation direction and which currencies are affected, (2) the
round-trip invariant that makes the fix safe — ingest divides, order placement multiplies back, so
what reaches IBKR is bit-for-bit its own quote. Break (2) and orders go out 100x mispriced.
"""
import pytest

from src.core.quote_units import (
    is_minor_unit_quoted,
    major_to_quote,
    minor_unit_factor,
    quote_to_major,
)


@pytest.mark.parametrize("ccy", ["GBP", "ZAR"])
def test_minor_unit_currencies_are_scaled(ccy):
    assert minor_unit_factor(ccy) == 100.0
    assert is_minor_unit_quoted(ccy) is True


@pytest.mark.parametrize("ccy", ["USD", "EUR", "CAD", "JPY", "HKD", "AUD", "INR", "CHF"])
def test_major_unit_currencies_pass_through(ccy):
    """A currency NOT quoted in minor units must never be rescaled — a false positive here
    silently divides every price for that currency by 100."""
    assert minor_unit_factor(ccy) == 1.0
    assert is_minor_unit_quoted(ccy) is False
    assert quote_to_major(1234.5, ccy) == 1234.5
    assert major_to_quote(1234.5, ccy) == 1234.5


def test_cfr_cents_to_rand():
    """The live defect: CFR quoted 397,199 cents is R3,971.99 (≈ $243 at ~16.3 USD/ZAR)."""
    assert quote_to_major(397199.0, "ZAR") == pytest.approx(3971.99)


def test_lse_pence_to_pounds():
    assert quote_to_major(1954.0, "GBP") == pytest.approx(19.54)


@pytest.mark.parametrize(
    "raw,ccy",
    [(397199.0, "ZAR"), (1954.0, "GBP"), (12437.0, "GBP"), (327.80, "USD"), (60.49, "CAD")],
)
def test_round_trip_returns_ibkr_original(raw, ccy):
    """Ingest then order-price must reproduce IBKR's own quote exactly — this is what lets the
    analyzer normalise while the executor still sends a correctly-priced order."""
    assert major_to_quote(quote_to_major(raw, ccy), ccy) == pytest.approx(raw)


def test_currency_code_is_case_insensitive():
    """IBKR/DB casing varies; a lowercase code must not skip normalisation."""
    assert quote_to_major(1954.0, "gbp") == pytest.approx(19.54)
    assert quote_to_major(397199.0, "zar") == pytest.approx(3971.99)


@pytest.mark.parametrize("bad", [None, 0, 0.0])
def test_missing_price_passes_through(bad):
    """No price is not a price of zero — callers pass None through unchanged."""
    assert quote_to_major(bad, "ZAR") == bad
    assert major_to_quote(bad, "ZAR") == bad


def test_unknown_currency_is_not_rescaled():
    """Fail-safe: an unrecognised code must pass through rather than be guessed at."""
    assert quote_to_major(100.0, "XXX") == 100.0
    assert quote_to_major(100.0, None) == 100.0
