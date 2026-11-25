"""The five 2026-11-04 rules ship in MarsWalk with defaults that leave every prior run
byte-identical (verified on tariff_2025 + svb_2023 against the HEAD engine on 2026-11-04)."""
from src.marswalk.engine import Params


def test_sim_defaults_are_inert():
    p = Params()
    assert p.wheel_stop_loss_enabled is False and p.wheel_stop_loss_pct == 0.08
    assert p.cc_package_unwind_enabled is False and p.cc_package_unwind_min_profit_pct == 0.005
    assert p.put_otm_floor_mode == "flat"
    assert p.put_universe_max_breach_rate == 1.0
    assert p.max_single_name_notional_pct == 0.25   # sim keeps the old cap; sweeps pass 0.10


def test_knobs_accept_overrides():
    p = Params(wheel_stop_loss_enabled=True, cc_package_unwind_enabled=True,
               put_otm_floor_mode="realized", put_universe_max_breach_rate=0.30,
               max_single_name_notional_pct=0.10)
    assert p.wheel_stop_loss_enabled and p.cc_package_unwind_enabled
    assert p.put_otm_floor_mode == "realized" and p.put_universe_max_breach_rate == 0.30


def test_sleeve_wing_and_hedge_defaults_are_inert():
    p = Params()
    assert p.index_put_enabled is False and p.index_put_spread_enabled is False
    assert p.index_put_spread_width_pct == 0.03 and p.index_put_max_loss_pct == 0.10
    assert p.hedge_enabled is False and p.hedge_otm_pct == 0.05 and p.hedge_budget_pct == 0.04


# ── 2026-11-24 fidelity audit (Rain): the sim printed +61% on the account's own window where the
# account did −5% (USD). Defaults now carry the measured corrections. ──

def test_fidelity_defaults_are_fill_calibrated():
    from src.marswalk import pricing
    p = Params()
    assert p.short_dte_uplift_k == 0.33 and pricing.SHORT_DTE_K == 0.33
    assert p.put_commission_per_contract == 0.80 and p.cc_commission_per_contract == 0.80
    assert p.put_strike_mode == "otm_pct" and p.put_strike_otm_pct == 0.025


def test_uplift_matches_measured_implied_over_realized():
    """162 real fills: implied/RV20 1.51 at 0-1 DTE, 1.21 at 2-3, 1.05 at 4-7."""
    from src.marswalk.pricing import effective_iv
    assert abs(effective_iv(1.0, 3) - 1.19) < 0.03
    assert abs(effective_iv(1.0, 0) - 1.33) < 0.01
    assert effective_iv(1.0, 7) == 1.0


def test_pick_put_candidate_otm_mode_and_score_mode():
    from types import SimpleNamespace as NS
    from src.marswalk.engine import _pick_put_candidate
    cands = [NS(strike=95.0, bid=0.60, score=10.0), NS(strike=97.0, bid=1.20, score=30.0),
             NS(strike=97.5, bid=1.40, score=25.0), NS(strike=92.0, bid=0.30, score=5.0)]
    p = Params()                                   # otm_pct 2.5% of 100 → target 97.5
    assert _pick_put_candidate(cands, 100.0, p).strike == 97.5
    p2 = Params(put_strike_otm_pct=0.03)           # target 97.0
    assert _pick_put_candidate(cands, 100.0, p2).strike == 97.0
    p3 = Params(put_strike_mode="score")           # old behaviour: best score
    assert _pick_put_candidate(cands, 100.0, p3).strike == 97.0 and _pick_put_candidate(cands, 100.0, p3).score == 30.0
    assert _pick_put_candidate(cands, 0.0, p).score == 30.0   # no spot → falls back to score


def test_ledger_is_optional_and_records_events():
    import inspect
    from src.marswalk import engine
    assert "ledger: list | None = None" in inspect.getsource(engine.run_regime)
    src = inspect.getsource(engine.run_regime)
    for kind in ("put_sold", "put_assigned", "put_expired", "cc_sold", "called_away", "stop", "cc_unwind"):
        assert f'_led("{kind}"' in src


def test_marswalk_page_start_nlv_is_the_money_put_in(monkeypatch):
    """2026-11-25 (Rain): the page's start capital = net deposits in USD (deposit-date FX), rounded to $1k;
    falls back to 220k while the deposit ledger is empty."""
    from src.web.routes import marswalk as m
    import src.web.routes.dashboard as dash
    monkeypatch.setattr(dash, "_get_total_invested", lambda: {"usd": 251116.7, "base": 220000.0})
    assert m._default_nlv() == 251_000.0
    monkeypatch.setattr(dash, "_get_total_invested", lambda: {"usd": 0.0, "base": 0.0})
    assert m._default_nlv() == 220_000.0
    monkeypatch.setattr(dash, "_get_total_invested", lambda: (_ for _ in ()).throw(RuntimeError("no ledger")))
    assert m._default_nlv() == 220_000.0
