"""Growth-tier entry gate: a growth name needs BOTH quality and growth (Rain, 2026-10-01).

Figures are the live FMP readings of 2026-10-01 that motivated the change, so each test pins a
real decision: MKTX (the complaint) out, the fast growers the old scorer buried back in.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from src.portfolio import growth_gate as gg


# ── measurement ──────────────────────────────────────────────────────────────

def test_compound_rate_is_compound_not_total_over_years():
    # 100 -> 408 over four years. The old "average" read (408-100)/100/4 = 77%/yr.
    cagr = gg.compound_growth_pct([408, 300, 200, 150, 100])
    assert cagr == pytest.approx(42.1, abs=0.1)


def test_compound_rate_needs_two_positive_endpoints():
    assert gg.compound_growth_pct([100]) is None
    assert gg.compound_growth_pct([100, None, 0]) is None
    assert gg.compound_growth_pct([]) is None


def test_legacy_simple_average_converts_to_compound():
    assert gg.simple_avg_to_compound_pct(77.0, years=4) == pytest.approx(42.0, abs=0.2)


def test_durable_growth_caps_at_the_weaker_window():
    # LULU: strong history, dead now -> the dead window rules.
    assert gg.durable_growth_pct(15.4, 1.7) == pytest.approx(2.55, abs=0.01)
    # AAPL: dead history, growing now -> history rules just the same.
    assert gg.durable_growth_pct(3.3, 14.2) == pytest.approx(4.95, abs=0.01)


def test_acceleration_is_not_penalised_below_the_mean():
    # AMD: 20.5%/yr history, 39.5% trailing. Mean 30.0, cap 30.75 -> the mean stands.
    assert gg.durable_growth_pct(20.5, 39.5) == pytest.approx(30.0)


def test_shrinking_revenue_has_no_durable_growth():
    assert gg.durable_growth_pct(5.5, -2.1) == 0.0   # LII


def test_durable_growth_with_one_or_no_window():
    assert gg.durable_growth_pct(None, 12.0) == 12.0
    assert gg.durable_growth_pct(9.0, None) == 9.0
    assert gg.durable_growth_pct(None, None) is None


def test_growth_score_is_monotone_and_never_punishes_speed():
    xs = [-5, 0, 3, 5, 8, 10, 15, 20, 25, 30, 60, 200]
    ys = [gg.growth_strength_score(x) for x in xs]
    assert ys == sorted(ys)
    assert ys[0] == 0 and ys[-1] == 100
    # The old scorer gave a 5% grower 60/100 and a 40% grower less than a 25% grower.
    assert gg.growth_strength_score(5) == 20
    assert gg.growth_strength_score(40) >= gg.growth_strength_score(25)


def test_recent_half_and_ttm_windows():
    q = [110, 108, 100, 100, 100, 100, 100, 100]          # newest first
    assert gg.recent_half_growth_pct(q) == pytest.approx(9.0)
    assert gg.ttm_growth_pct(q) == pytest.approx(4.5)
    assert gg.recent_half_growth_pct(q[:5]) is None
    assert gg.ttm_growth_pct(q[:7]) is None


# ── entry gate ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,durable,quality,ok", [
    ("MKTX high quality, no growth", 4.2, 65.3, False),
    ("ZTS elite quality, no growth", 2.1, 88.0, False),
    ("ARM growth without quality",  20.6, 49.0, False),
    ("AMD both",                    30.0, 53.0, True),
    ("MELI both",                   44.1, 54.3, True),
    ("exactly on both floors",       8.0, 50.0, True),
    ("unknown growth",              None, 90.0, False),
    ("unknown quality",             20.0, None, False),
])
def test_entry_gate_needs_both_pillars(name, durable, quality, ok):
    passed, reason = gg.passes_entry_gate(durable, quality)
    assert passed is ok, name
    assert (reason == "") is ok


# ── sell-side verdict (pure; wired into the review separately) ───────────────

def test_real_dropoff_needs_the_latest_half_year_to_confirm():
    v = gg.growth_verdict(4.9, 3.4, recent_half_pct=2.0)        # MKTX
    assert not v.passes_growth_floor and v.real_dropoff


def test_reaccelerating_name_fails_entry_but_is_not_a_dropoff():
    v = gg.growth_verdict(3.3, 14.2, recent_half_pct=13.0)      # AAPL
    assert not v.passes_growth_floor and not v.real_dropoff


def test_missing_evidence_never_makes_a_dropoff():
    assert not gg.growth_verdict(4.9, 3.4, recent_half_pct=None).real_dropoff
    assert not gg.growth_verdict(None, None, recent_half_pct=None).real_dropoff
    assert not gg.growth_verdict(None, None).passes_growth_floor


def test_growing_name_is_neither():
    v = gg.growth_verdict(14.8, 16.0, recent_half_pct=15.0)     # MA
    assert v.passes_growth_floor and not v.real_dropoff


# ── screener integration ─────────────────────────────────────────────────────

su = pytest.importorskip("screen_universe")

MKTX = dict(revenue_cagr_pct=4.9, revenue_yoy_pct=3.43, revenue_avg_pct=5.28,
            roic_5yr_avg=18.4, roic_5yr_min=16.3, operating_margin_pct=40.5,
            operating_margin_trend=-1.23, rd_intensity_pct=0.0, rd_intensity_trend=0.0,
            fcf_margin_trend=6.57, share_dilution_pct_3yr=-2.23,
            goodwill_to_assets_change_pct=4.08)


def test_screener_growth_subscore_uses_durable_growth():
    assert su._durable_growth(MKTX) == pytest.approx(4.165, abs=0.01)
    assert su._score_revenue_durability(MKTX) < 20          # was 59.9
    assert su._score_revenue_durability({}) == 50.0         # unknown stays neutral in the blend


def test_screener_gate_rejects_mktx_on_growth_not_quality():
    q = su._quality_pillar(MKTX, "Consumer, Non-cyclical")
    assert q >= gg.QUALITY_FLOOR                             # quality is fine...
    ok, reason = gg.passes_entry_gate(su._durable_growth(MKTX), q)
    assert not ok and "durable growth" in reason             # ...growth is what is missing


def test_screener_falls_back_to_legacy_average_when_no_compound_rate():
    fmp = dict(revenue_avg_pct=77.2, revenue_yoy_pct=46.0)   # MELI, pre-change fundamentals
    assert su._durable_growth(fmp) == pytest.approx(44.0, abs=0.5)


def test_stockscore_defaults_fail_the_gate():
    # A score object that never went through the gate must not be admitted by default.
    s = su.StockScore(symbol="X")
    assert s.growth_gate_ok is False and s.durable_growth_pct is None


def test_augmentation_prompt_states_the_gate():
    p = su._build_growth_augmentation_prompt([], [], 0.0, set())
    assert "ENTRY GATE" in p and f"{gg.GROWTH_FLOOR_PCT:.0f}%" in p


# ── why a held name left the tier: written by the screen, read by the review ──

def test_removal_reason_round_trips_the_quality_score_and_the_substance():
    failures = ["return on capital 3.8% over five years, below the 8% it takes to cover the cost of that capital",
                "net loss in 2 of the last five years"]
    r = gg.removal_reason({"detail": "quality 38 < 50 floor", "quality": 38.4, "quality_failures": failures})
    assert r.startswith("growth gate: quality 38.4 below the 50 floor [in substance: ")
    score, substance = gg.quality_dropout_from_reason(f"No longer in screened universe — {r}. Pending removal.")
    assert score == 38.4 and substance == "; ".join(failures)


def test_removal_reason_without_substance_still_carries_the_score():
    r = gg.removal_reason({"detail": "quality 47 < 50 floor", "quality": 47.0})
    assert r == "growth gate: quality 47.0 below the 50 floor"
    assert gg.quality_dropout_from_reason(r + ". Pending removal.") == (47.0, "")


def test_removal_reason_for_growth_and_for_missing_data_is_not_a_quality_drop():
    growth = gg.removal_reason({"detail": "durable growth 4.2% < 8% floor", "quality": 65.3})
    nodata = gg.removal_reason({"detail": "quality unmeasured — no return-on-capital or operating-margin data", "quality": 50.8})
    assert growth == "growth gate: durable growth 4.2% < 8% floor" and "unmeasured" in nodata
    for r in (growth, nodata, None, "", "No longer in screened universe. Pending removal."):
        assert gg.quality_dropout_from_reason(r) is None


def test_sell_threshold_is_well_under_the_entry_floor():
    assert gg.QUALITY_SELL_THRESHOLD == 40.0 and gg.QUALITY_FLOOR - gg.QUALITY_SELL_THRESHOLD >= 10
    assert gg.is_quality_selloff(40.0) and gg.is_quality_selloff(18.7)
    assert not gg.is_quality_selloff(40.1) and not gg.is_quality_selloff(49.9)
    assert not gg.is_quality_selloff(None)


def test_failures_in_substance_are_facts_about_the_business():
    xero = {"roic_5yr_avg": 3.8, "net_income_negative_years_5yr": 2, "fcf_negative_years_5yr": 0, "operating_margin_pct": 11.5}
    out = gg.essential_quality_failures(xero)
    assert len(out) == 2 and "return on capital 3.8%" in out[0] and "net loss in 2" in out[1]
    meituan = {"roic_5yr_avg": 2.1, "net_income_negative_years_5yr": 3, "fcf_negative_years_5yr": 2, "operating_margin_pct": -5.7}
    assert len(gg.essential_quality_failures(meituan)) == 4
    sound = {"roic_5yr_avg": 21.4, "net_income_negative_years_5yr": 0, "fcf_negative_years_5yr": 0, "operating_margin_pct": 34.6}
    assert gg.essential_quality_failures(sound) == []
    assert gg.essential_quality_failures({}) == []          # missing data is never a failure
