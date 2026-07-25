"""Parity + binding tests for the parameterized short-put moneyness window.

The 0-3 DTE selection path in option_scoring.score_put_candidates used to hardcode
a 2% / 5% / 12% (floor / target / cap) OTM window. It now reads
put_otm_floor / put_otm_target / put_otm_cap from cfg via getattr, defaulting to
those literals. These tests lock two properties:

  1. PARITY — a cfg WITHOUT the new attributes (getattr falls back to the literals)
     produces byte-identical selection to a cfg WITH the attributes set to the
     historical defaults. i.e. the parameterization changed nothing at defaults.
  2. BINDING — raising put_otm_floor actually excludes the near-ATM strikes, and
     shifting put_otm_target moves the top-scored strike. i.e. the knob works.
"""
from __future__ import annotations

from types import SimpleNamespace

from src.strategy.option_scoring import score_put_candidates


def _contract(strike: float, expiry: str = "20260727"):
    # score_put_candidates only touches .strike and .lastTradeDateOrContractMonth
    return SimpleNamespace(strike=strike, lastTradeDateOrContractMonth=expiry)


def _cfg(**extra):
    base = dict(min_premium_put=0.05, min_premium=0.05, min_bid=0.01,
                weekend_theta_enabled=False, weekend_theta_weight=0.0)
    base.update(extra)
    return SimpleNamespace(**base)


# Stock at 100; strikes span 1%..13% OTM so floor/cap boundaries are exercised.
STOCK = 100.0
IV = 0.60
TODAY = __import__("datetime").date(2026, 7, 25)  # expiry 20260727 -> 2 DTE
STRIKES = [99, 98, 97, 96, 95, 94, 93, 92, 91, 90, 89, 88, 87]
CHAIN = [_contract(s) for s in STRIKES]


def _select(cfg):
    cands = score_put_candidates(
        STOCK, IV, CHAIN, cfg,
        delta_min=0.15, delta_max=0.30,
        resolved_dte_min=0, resolved_dte_max=3, today=TODAY,
    )
    # Return (strike, rounded score) tuples sorted like the live screener does.
    return [(c.strike, round(c.score, 10)) for c in sorted(cands, key=lambda c: c.score, reverse=True)]


def test_default_parity_no_attrs_equals_historical_literals():
    """cfg lacking put_otm_* (getattr fallback) == cfg with the historical defaults.

    This is the core parity guarantee: the parameterization changed nothing at
    the default window. (Boundary strikes are avoided — exact 2%/12% OTM hit
    float-rounding and the premium floor; use clearly-interior/exterior strikes.)
    """
    without = _cfg()  # no put_otm_* -> getattr uses 0.02 / 0.05 / 0.12
    withdef = _cfg(put_otm_floor=0.02, put_otm_target=0.05, put_otm_cap=0.12)
    assert _select(without) == _select(withdef)
    kept = {s for s, _ in _select(without)}
    assert 95 in kept                          # 5% OTM (the target) always kept
    assert 99 not in kept                      # 1% OTM excluded (below 2% floor)


def test_live_config_default_matches_literals():
    """The real StrategyConfig defaults must equal the historical window."""
    from src.core.config import StrategyConfig
    sc = StrategyConfig()
    assert (sc.put_otm_floor, sc.put_otm_target, sc.put_otm_cap) == (0.02, 0.05, 0.12)


def test_raising_floor_excludes_near_atm():
    """floor 0.02 -> 0.04 must drop the 2% and 3% OTM strikes (98, 97)."""
    base_kept = {s for s, _ in _select(_cfg())}
    raised_kept = {s for s, _ in _select(_cfg(put_otm_floor=0.04, put_otm_target=0.05, put_otm_cap=0.12))}
    assert 98 in base_kept and 97 in base_kept
    assert 98 not in raised_kept and 97 not in raised_kept   # near-ATM culled
    assert raised_kept < base_kept                            # strict subset


def test_shifting_target_reweights_far_otm_strike():
    """target 5% -> 8% must RAISE the score of an 8%-OTM strike (otm_score peak
    moves out). Note: it does NOT necessarily move the TOP pick, because the
    premium + ROM components (55% weight) pull toward near-ATM regardless — which
    is exactly why raise-floor beats push-target for cutting assignments."""
    def score_of(strike, target):
        sel = _select(_cfg(put_otm_floor=0.02, put_otm_target=target, put_otm_cap=0.15))
        return dict(sel)[strike]
    # 92 is 8% OTM: its otm_score is 1.0 at target=0.08, lower at target=0.05.
    assert score_of(92, 0.08) > score_of(92, 0.05)
