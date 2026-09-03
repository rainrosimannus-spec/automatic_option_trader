"""Sector / home-country composition doughnuts on the portfolio page.

Two things are worth locking down: the FX normalisation (both legs, or the return % comes out of
two different scales) and the fold-to-Other rule (the palette only separates six hues, so a
seventh category MUST fold rather than be drawn).
"""
import types

import pytest

from src.portfolio.geography import (COMPANY_COUNTRY, home_country, unmapped_symbols)
from src.web.routes.portfolio import (_COMPOSITION_COLORS, _COMPOSITION_MAX_SLICES,
                                      _COMPOSITION_OTHER_COLOR, _build_composition)

FX = {"EUR": 1.0, "USD": 0.5, "JPY": 0.01}


def _h(symbol, sector="Technology", exchange="NASDAQ", currency="USD",
       invested=100.0, value=110.0):
    return types.SimpleNamespace(symbol=symbol, sector=sector, exchange=exchange,
                                 currency=currency, total_invested=invested, market_value=value)


def _mix(holdings, park="XEON"):
    return _build_composition(holdings, lambda h: (h.sector or "").strip() or "Unclassified",
                              FX, "EUR", park)


# ── FX ────────────────────────────────────────────────────────────────────────────────────────
def test_both_legs_are_fx_normalised():
    """A yen holding must not be added to a euro one at face value — on either leg."""
    rows = _mix([_h("6920", currency="JPY", invested=10_000.0, value=12_000.0)])
    assert rows[0]["basis"] == pytest.approx(100.0)
    assert rows[0]["value"] == pytest.approx(120.0)
    assert rows[0]["return_pct"] == pytest.approx(20.0)


def test_return_pct_survives_mixed_currencies():
    """Same +20% in two currencies aggregates to +20%, not to something scaled by the rate."""
    rows = _mix([_h("A", currency="USD", invested=1_000.0, value=1_200.0),
                 _h("B", currency="JPY", invested=100_000.0, value=120_000.0)])
    assert len(rows) == 1
    assert rows[0]["return_pct"] == pytest.approx(20.0)
    assert rows[0]["value"] == pytest.approx(0.5 * 1_200 + 0.01 * 120_000)


def test_percentages_sum_to_one_hundred():
    rows = _mix([_h("A", sector="Technology"), _h("B", sector="Energy"),
                 _h("C", sector="Financial")])
    assert sum(r["pct"] for r in rows) == pytest.approx(100.0)


# ── the park ETF is not a holding ─────────────────────────────────────────────────────────────
def test_parked_cash_etf_is_excluded():
    """It is ~3x the invested book — in the pie it would leave every real slice a fringe."""
    rows = _mix([_h("XEON", sector="", currency="EUR", invested=8_000_000.0, value=8_010_000.0),
                 _h("NVDA", invested=100.0, value=110.0)])
    assert [r["label"] for r in rows] == ["Technology"]
    assert rows[0]["value"] == pytest.approx(55.0)


def test_park_symbol_none_keeps_everything():
    rows = _mix([_h("XEON", sector="Cash", currency="EUR")], park=None)
    assert {r["label"] for r in rows} == {"Cash"}


# ── folding ───────────────────────────────────────────────────────────────────────────────────
def test_no_fold_when_categories_fit():
    rows = _mix([_h(f"S{i}", sector=f"Sector{i}") for i in range(_COMPOSITION_MAX_SLICES)])
    assert len(rows) == _COMPOSITION_MAX_SLICES
    assert "Other" not in [r["label"] for r in rows]


def test_seventh_category_folds_into_other():
    """The palette separates six hues; a seventh drawn slice would not be distinguishable."""
    holdings = [_h(f"S{i}", sector=f"Sector{i}", value=float(100 - i)) for i in range(9)]
    rows = _mix(holdings)
    assert len(rows) == _COMPOSITION_MAX_SLICES + 1
    assert rows[-1]["label"] == "Other"
    assert rows[-1]["members"] == ["Sector6", "Sector7", "Sector8"]
    assert rows[-1]["color"] == _COMPOSITION_OTHER_COLOR


def test_other_aggregates_value_and_return_of_its_members():
    holdings = [_h(f"S{i}", sector=f"Sector{i}", invested=1000.0, value=1000.0)
                for i in range(_COMPOSITION_MAX_SLICES)]
    holdings += [_h("X", sector="Tail1", invested=100.0, value=150.0),
                 _h("Y", sector="Tail2", invested=100.0, value=50.0)]
    other = _mix(holdings)[-1]
    assert other["label"] == "Other"
    assert other["basis"] == pytest.approx(0.5 * 200)
    assert other["value"] == pytest.approx(0.5 * 200)
    assert other["return_pct"] == pytest.approx(0.0)


def test_slices_are_value_ordered_and_coloured_in_palette_order():
    holdings = [_h("A", sector="Small", value=10.0), _h("B", sector="Big", value=1000.0)]
    rows = _mix(holdings)
    assert [r["label"] for r in rows] == ["Big", "Small"]
    assert [r["color"] for r in rows] == _COMPOSITION_COLORS[:2]


def test_blank_sector_becomes_unclassified_not_empty_label():
    rows = _mix([_h("GOOG", sector="")])
    assert rows[0]["label"] == "Unclassified"


def test_zero_basis_slice_reports_zero_return_not_a_crash():
    rows = _mix([_h("GIFT", invested=0.0, value=500.0)])
    assert rows[0]["return_pct"] == 0.0


def test_empty_holdings_give_no_slices():
    assert _build_composition([], lambda h: "x", FX, "EUR", "XEON") == []


# ── home country ──────────────────────────────────────────────────────────────────────────────
def test_override_beats_the_listing_venue():
    """Cameco is held on NYSE; without the override the pie would call it American."""
    assert home_country("CCJ", "NYSE", "USD") == "Canada"
    assert home_country("NU", "NYSE", "USD") == "Brazil"
    assert home_country("XRO", "ASX", "AUD") == "New Zealand"


def test_tokyo_and_toronto_are_not_merged():
    """Both are 'TSE'-ish in this schema — TSEJ is Tokyo, TSE is Toronto."""
    assert home_country("6920", "TSEJ", "JPY") == "Japan"
    assert home_country("SU", "TSE", "CAD") == "Canada"


def test_venue_resolves_shared_currency():
    """EUR spans a dozen venues, so the exchange has to decide."""
    assert home_country("ASML", "AEB", "EUR") == "Netherlands"
    assert home_country("XEON", "IBIS2", "EUR") == "Germany"


def test_unknown_venue_falls_back_to_currency():
    assert home_country("ZZZ", "NOPE", "GBP") == "United Kingdom"
    assert home_country("ZZZ", "NOPE", "EUR") == "Eurozone"
    assert home_country("ZZZ", "NOPE", "XYZ") == "Unknown"


def test_unmapped_symbols_flags_only_the_currency_fallback():
    rows = [_h("AAPL", exchange="NASDAQ"), _h("ZZZ", exchange="NOPE"),
            _h("CCJ", exchange="NOPE")]
    assert unmapped_symbols(rows) == ["ZZZ"]      # CCJ is covered by the override list


def test_every_override_is_a_real_country_string():
    assert all(v and v[0].isupper() for v in COMPANY_COUNTRY.values())
