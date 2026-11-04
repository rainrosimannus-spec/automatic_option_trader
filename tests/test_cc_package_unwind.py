"""Package unwind (Rule C, Rain's manual rule, 2026-11-04).

On a covered lot, when stock bid − (call ask + 5c) − fees clears the lot's NET basis by
min_profit_pct of the cost basis, the call is bought back and the shares sold together.
Rain did this by hand on VRT 2026-10-29, CTAS 10-27 and ISRG 10-07; the numbers below are
the real fills from data/options.db.
"""
from src.core.config import RiskConfig
from src.strategy.wheel_rules import package_unwind_triggered, package_unwind_net_basis

KW = dict(min_profit_pct=0.005, fees_per_share=0.03, pad=0.05)


def test_live_defaults_on():
    rc = RiskConfig()
    assert rc.cc_package_unwind_enabled is True
    assert rc.cc_package_unwind_min_profit_pct == 0.005


def test_net_basis_subtracts_realized_and_open_cc_premium():
    assert package_unwind_net_basis(100.0, 2.0, 3.0) == 95.0


def test_vrt_2026_10_29_manual_unwind_fires():
    # VRT 225C bought back at 33.44, stock sold at 251.10. Blended basis ≈ 220.8 (267.5 + 2×197.5
    # less put premium ~2.3); realized CC per share before this call ~ 4.0; this call's premium 7.5.
    assert package_unwind_triggered(251.10, 33.44, 218.5, 4.0, 7.5, **KW) is True


def test_ctas_2026_10_27_manual_unwind_fires():
    # CTAS 210C bought back 6.69, stock sold 213.04; basis 205 − 1.0 put premium; CC realized ~2.5; call 3.3
    assert package_unwind_triggered(213.04, 6.69, 204.0, 2.5, 3.3, **KW) is True


def test_below_net_basis_does_not_fire():
    # package 100 − 5.05 − 0.03 = 94.92 vs net basis 96 → no
    assert package_unwind_triggered(100.0, 5.0, 98.0, 1.0, 1.0, **KW) is False


def test_min_profit_margin_is_enforced():
    # package 100 − 2.05 − 0.03 = 97.92; net basis 97.5 → +0.42 < 0.5% of 100 → no
    assert package_unwind_triggered(100.0, 2.0, 99.5, 1.0, 1.0, **KW) is False
    # net basis 97.4 → +0.52 ≥ 0.5 → fire
    assert package_unwind_triggered(100.0, 2.0, 99.4, 1.0, 1.0, **KW) is True


def test_fails_closed_on_missing_quotes_or_basis():
    assert package_unwind_triggered(None, 2.0, 99.0, 1.0, 1.0, **KW) is False
    assert package_unwind_triggered(100.0, None, 99.0, 1.0, 1.0, **KW) is False
    assert package_unwind_triggered(100.0, 2.0, None, 1.0, 1.0, **KW) is False
    assert package_unwind_triggered(100.0, 0.0, 99.0, 1.0, 1.0, **KW) is False
