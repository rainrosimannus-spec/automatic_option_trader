"""Package unwind (Rule C, Rain's manual rule, 2026-11-04).

On a covered lot, when stock bid − (call ask + 5c) − fees clears the lot's NET basis by
min_profit_pct of the cost basis, the call is bought back and the shares sold together.
Rain did this by hand on VRT 2026-10-29, CTAS 10-27 and ISRG 10-07; the numbers below are
the real fills from data/options.db.
"""
from src.core.config import RiskConfig
from src.strategy.wheel_rules import (package_unwind_triggered, package_unwind_net_basis,
                                      package_unwind_dte_ok)

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


# ── 2026-11-24 (Rain): the unwind is a bag rule — calls with at least a week to run only ──

def test_dte_floor_defaults_to_seven_days_live_and_sim():
    assert RiskConfig().cc_package_unwind_min_dte == 7
    from src.marswalk.engine import Params
    assert Params().cc_package_unwind_min_dte == 7


def test_dte_floor_blocks_the_last_week_and_fails_closed():
    assert package_unwind_dte_ok(3, 7) is False      # ADSK 260C, 3 days left on earnings eve
    assert package_unwind_dte_ok(2, 7) is False      # ISRG 390C 10-07
    assert package_unwind_dte_ok(7, 7) is True
    assert package_unwind_dte_ok(24, 7) is True      # CTAS 10-27
    assert package_unwind_dte_ok(29, 7) is True      # VRT 10-29
    assert package_unwind_dte_ok(None, 7) is False
    assert package_unwind_dte_ok("x", 7) is False
    assert package_unwind_dte_ok(0, 0) is True       # floor 0 = old behaviour


def test_live_rule_c_passes_dte_and_gates_on_it():
    import inspect
    from src.strategy import profit_taker
    src = inspect.getsource(profit_taker.ProfitTaker)
    assert "self._package_unwind(db, pos, live_ask, opt_exchange, currency, rc, dte=dte)" in src
    body = inspect.getsource(profit_taker.ProfitTaker._package_unwind)
    assert "package_unwind_dte_ok(dte, min_dte)" in body
    assert body.index("package_unwind_dte_ok(dte, min_dte)") < body.index("get_stock_live_quote(")


def test_short_dte_lot_is_not_unwound_live():
    """3 days left: _package_unwind returns False before touching quotes or the DB."""
    from unittest.mock import MagicMock
    from src.strategy.profit_taker import ProfitTaker
    pt = ProfitTaker.__new__(ProfitTaker)
    db = MagicMock()
    pos = MagicMock(symbol="ADSK", strike=260.0, expiry="20261127", entry_premium=3.3)
    rc = RiskConfig()
    assert ProfitTaker._package_unwind(pt, db, pos, 12.80, "SMART", "USD", rc, dte=3) is False
    db.query.assert_not_called()


def test_marswalk_unwind_skips_calls_inside_the_floor():
    import inspect
    from src.marswalk import engine
    src = inspect.getsource(engine)
    assert "package_unwind_dte_ok((c[\"expiry\"] - d).days" in src


def test_earnings_eve_bar_ships_off():
    assert RiskConfig().cc_package_unwind_earnings_eve_enabled is False
