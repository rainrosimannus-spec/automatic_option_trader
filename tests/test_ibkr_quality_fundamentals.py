"""Quality figures from the broker's analyst-estimates report (RESC), and the rule that a name
with no real quality data fails the growth gate.

Before 2026-10-04 a non-US name had no quality inputs at all (FMP has none on our plan, the old
broker fallback carried revenue and dividends only). Its quality pillar was 50.8 built from
neutral defaults — just above the 50 floor — so it sat in the growth tier on growth alone.
"""
import pytest

from src.portfolio import growth_gate as gg
from src.portfolio.ibkr_fundamentals import (
    ROA_TO_ROIC_INTERCEPT, ROA_TO_ROIC_SLOPE, _percent_scale, _resc_actuals,
    parse_quality_fundamentals)


def _resc(measures: dict) -> str:
    """Build a RESC document. measures = {type: (unit, {year: value})}."""
    body = ""
    for mtype, (unit, years) in measures.items():
        periods = "".join(
            f'<FYPeriod periodType="A" fYear="{y}" endMonth="12" endCalYear="{y}">'
            f'<ActValue updated="x">{v}</ActValue></FYPeriod>' for y, v in years.items())
        body += f'<FYActual type="{mtype}" unit="{unit}">{periods}</FYActual>'
    return f"<REarnEstCons><Company/><Actuals><FYActuals>{body}</FYActuals></Actuals></REarnEstCons>"


SNAPSHOT = "<ReportSnapshot><CoGeneralInfo><SharesOut>1000000000.0</SharesOut></CoGeneralInfo></ReportSnapshot>"
YEARS = (2021, 2022, 2023, 2024, 2025)


def _company(**over):
    m = {
        "SREV": ("M", dict(zip(YEARS, (1000, 1100, 1210, 1331, 1464)))),          # +10% a year, $M
        "EBIT": ("U", dict(zip(YEARS, (200e6, 231e6, 266e6, 306e6, 366e6)))),     # 20% -> 25%
        "EIBT": ("M", dict(zip(YEARS, (190, 220, 250, 290, 322)))),               # pretax, $M -> 22%
        "ROAPCT": ("U", dict(zip(YEARS, (10e6, 12e6, 14e6, 16e6, 18e6)))),        # 10% .. 18%
        "ROEPCT": ("U", dict(zip(YEARS, (20e6, 20e6, 20e6, 20e6, 20e6)))),
        "EPS": ("U", dict(zip(YEARS, (1.0, 1.0, 1.0, 1.0, 1.0)))),
        "GPS": ("U", dict(zip(YEARS, (0.9, -0.2, 0.9, 1.0, 1.0)))),               # one GAAP loss year
        "BVPS": ("U", dict(zip(YEARS, (5.0, 5.0, 5.0, 5.0, 5.0)))),               # ROE = 20%
        "CFSHR": ("U", dict(zip(YEARS, (0.20, 0.22, 0.24, 0.30, 0.36)))),
        "SCEX": ("M", dict(zip(YEARS, (50, 50, 60, 60, 66)))),
        "GROSMGN": ("U", dict(zip(YEARS, (50.0, 51.0, 52.0, 53.0, 55.0)))),
    }
    m.update(over)
    return _resc({k: v for k, v in m.items() if v is not None})


def test_units_millions_and_plain_are_put_on_one_scale():
    a = _resc_actuals(_company())
    assert a["SREV"][2025] == pytest.approx(1464e6) and a["EBIT"][2025] == pytest.approx(366e6)


def test_operating_margin_is_the_lower_of_ebit_and_pretax_margin():
    q = parse_quality_fundamentals(_company(), SNAPSHOT)
    # EBIT margin 25.0%, pretax margin 22.0% -> 22.0 (analysts' EBIT is often adjusted upward)
    assert q["operating_margin_pct"] == pytest.approx(322 / 1464 * 100)
    assert q["operating_margin_trend"] == pytest.approx(322 / 1464 * 100 - 250 / 1210 * 100)


def test_margin_falls_back_to_the_one_measure_available():
    q = parse_quality_fundamentals(_company(EIBT=None), SNAPSHOT)
    assert q["operating_margin_pct"] == pytest.approx(25.0)


def test_return_on_capital_is_return_on_assets_on_fmps_roic_scale():
    q = parse_quality_fundamentals(_company(), SNAPSHOT)
    assert q["roic_5yr_avg"] == pytest.approx(ROA_TO_ROIC_SLOPE * 14.0 + ROA_TO_ROIC_INTERCEPT)
    assert q["roic_5yr_min"] == pytest.approx(ROA_TO_ROIC_SLOPE * 10.0 + ROA_TO_ROIC_INTERCEPT)


def test_percentage_scale_is_recovered_from_the_report_itself():
    assert _percent_scale(_resc_actuals(_company())) == 1e6
    # Japanese reports store percentages multiplied by a billion.
    jp = _company(ROAPCT=("U", dict(zip(YEARS, (10e9, 12e9, 14e9, 16e9, 18e9)))),
                  ROEPCT=("U", dict(zip(YEARS, (20e9,) * 5))))
    assert _percent_scale(_resc_actuals(jp)) == 1e9
    q = parse_quality_fundamentals(jp, SNAPSHOT)
    assert q["roic_5yr_avg"] == pytest.approx(ROA_TO_ROIC_SLOPE * 14.0 + ROA_TO_ROIC_INTERCEPT)


def test_an_implausible_return_is_left_absent_not_scored():
    # No anchor (no EPS/BVPS) and a billion-scaled figure: 1e6 is assumed, the result is
    # absurd, and the figure must be dropped rather than scored.
    bad = _company(EPS=None, BVPS=None, GPS=None,
                   ROAPCT=("U", dict(zip(YEARS, (10e9, 12e9, 14e9, 16e9, 18e9)))))
    q = parse_quality_fundamentals(bad, SNAPSHOT)
    assert "roic_5yr_avg" not in q and "operating_margin_pct" in q


def test_free_cash_flow_trend_and_negative_years_need_the_share_count():
    q = parse_quality_fundamentals(_company(), SNAPSHOT)
    fcf = lambda cf, capex, rev: (cf * 1e9 - capex * 1e6) / (rev * 1e6) * 100
    assert q["fcf_margin_trend"] == pytest.approx(fcf(0.36, 66, 1464) - fcf(0.24, 60, 1210))
    assert q["fcf_negative_years_5yr"] == 0
    without = parse_quality_fundamentals(_company(), None)
    assert "fcf_margin_trend" not in without and "fcf_negative_years_5yr" not in without


def test_loss_years_use_gaap_eps_and_revenue_growth_is_compound():
    q = parse_quality_fundamentals(_company(), SNAPSHOT)
    assert q["net_income_negative_years_5yr"] == 1
    assert q["revenue_cagr_pct"] == pytest.approx(10.0, abs=0.05)
    assert q["revenue_yoy_pct"] == pytest.approx(10.0, abs=0.05)
    assert q["gross_margin_pct"] == 55.0 and q["gross_margin_trend"] == pytest.approx(3.0)


def test_figures_the_broker_does_not_have_stay_absent():
    q = parse_quality_fundamentals(_company(), SNAPSHOT)
    for missing in ("rd_intensity_pct", "share_dilution_pct_3yr", "goodwill_to_assets_change_pct"):
        assert missing not in q
    assert q["quality_source"] == "ibkr_resc" and q["quality_fiscal_year"] == 2025


@pytest.mark.parametrize("resc", [None, "", "<not xml", "<REarnEstCons><Actuals/></REarnEstCons>",
                                  _resc({"SREV": ("M", {2025: 100})})])
def test_no_or_unusable_report_gives_no_figures(resc):
    assert parse_quality_fundamentals(resc, SNAPSHOT) == {}


# ── the gate: unmeasured quality fails ───────────────────────────────────────

def test_unmeasured_quality_fails_even_when_the_neutral_score_clears_the_floor():
    ok, reason = gg.passes_entry_gate(25.0, 50.8, quality_measured=False)
    assert not ok and "quality unmeasured" in reason


def test_measured_quality_is_judged_on_its_score():
    assert gg.passes_entry_gate(25.0, 50.8, quality_measured=True) == (True, "")
    ok, reason = gg.passes_entry_gate(25.0, 38.4, quality_measured=True)      # Xero, real figures
    assert not ok and "quality 38 < 50" in reason


def test_growth_floor_is_reported_before_missing_quality():
    ok, reason = gg.passes_entry_gate(3.0, 50.8, quality_measured=False)
    assert not ok and "durable growth" in reason


def test_a_score_that_never_went_through_scoring_is_unmeasured():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    su = pytest.importorskip("screen_universe")
    s = su.StockScore(symbol="X")
    assert s.quality_measured is False and s.fundamentals_source == ""


def test_broker_figures_feed_the_screeners_quality_pillar():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    su = pytest.importorskip("screen_universe")
    q = parse_quality_fundamentals(_company(), SNAPSHOT)
    # 22% margin, ~16% return on capital, improving cash flow: a real, non-neutral pillar
    assert su._quality_pillar(q, "Technology") != su._quality_pillar({}, "Technology")
    assert su._score_compounding_quality(q) != 50.0 and su._score_operating_leverage(q) != 50.0
