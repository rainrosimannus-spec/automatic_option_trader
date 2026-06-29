"""FX normalisation for the global compounder (the AZN "filled above target" bug).

Targets, the daily budget, the cash buffer and cash_room are all in the account BASE currency (EUR),
but a holding's market value and an order's price come from IBKR in the instrument's LOCAL currency.
Before this fix the executor sized shares as base_brick / LOCAL_price, over-buying a strong-currency
name (GBP ≈ 1.16 €) by its FX rate, and the maps compared a £-holding against a €-target unconverted.
These tests lock in: (1) the fx helper direction, (2) a base brick on a GBP name buys the right shares
and reports its spend in base, not inflated by the FX rate."""
from types import SimpleNamespace

import pytest

from src.portfolio import fx
from src.portfolio import compounder as cmp


_RATES = {"EUR": 1.0, "GBP": 1.16, "USD": 0.875, "CAD": 0.62}


def test_to_base_direction():
    # rate[ccy] is LOCAL->BASE, so a local amount multiplies up/down to base.
    assert fx.to_base(1000, "EUR", _RATES) == 1000
    assert fx.to_base(1000, "GBP", _RATES) == pytest.approx(1160)   # £ worth more than €
    assert fx.to_base(1000, "USD", _RATES) == pytest.approx(875)    # $ worth less than €
    assert fx.base_ccy(_RATES) == "EUR"
    assert fx.rate_to_base("GBP", _RATES) == 1.16


def test_sum_base_mixes_currencies_correctly():
    # Today's fills are stored per-row in LOCAL currency; a raw sum would mix £/$/€.
    pairs = [("GBP", 4693.0), ("USD", 2000.0), ("EUR", 1000.0)]
    expected = 4693 * 1.16 + 2000 * 0.875 + 1000
    assert fx.sum_base(pairs, _RATES) == pytest.approx(expected)
    assert fx.sum_base([], _RATES) == 0


def test_unknown_or_missing_rate_passes_through():
    # Fail-safe: never silently scale an amount we can't price.
    assert fx.to_base(1000, "JPY", _RATES) == 1000
    assert fx.to_base(1000, None, _RATES) == 1000
    assert fx.rate_to_base("ZZZ", _RATES) == 1.0


def _make_buyer(monkeypatch):
    """A PortfolioBuyer in suggestion mode with a fixed FX table and create_suggestion captured."""
    from src.core.config import get_settings
    from src.portfolio.buyer import PortfolioBuyer

    cfg = get_settings().portfolio
    cfg.suggestion_mode = True
    buyer = PortfolioBuyer(ib=SimpleNamespace(), cfg=cfg)

    monkeypatch.setattr(fx, "load_fx_rates", lambda: _RATES)
    monkeypatch.setattr("src.portfolio.buyer._ensure_event_loop", lambda: None)

    captured = {}
    import src.core.suggestions as sugg

    def _fake_create(**kw):
        captured.update(kw)
        return SimpleNamespace(id=1)

    monkeypatch.setattr(sugg, "create_suggestion", _fake_create)
    return buyer, cfg, captured


def test_gbp_brick_sizes_in_base_not_local(monkeypatch):
    buyer, cfg, captured = _make_buyer(monkeypatch)
    cc = cfg.compounder

    px = 142.79                      # AZN, GBP
    brick_base = 4693.0              # EUR brick
    stock = SimpleNamespace(symbol="AZN", exchange="LSE", currency="GBP", tier="growth")
    analysis = SimpleNamespace(current_price=px, signal_type="compounder_direct",
                               sma_200=136.0, rsi_14=62.0)

    core_base, total_base = buyer._execute_compounder_buy(
        stock, analysis, brick_base, urgency=1.0, is_leader=True, cash_room=1e9,
        rank=1, rank_score=70.0, rationale="t", min_buy=2000.0)

    # The ladder's core rung (price, frac) — so the test isn't coupled to its exact values.
    core_price, frac0 = cmp.ladder_plan(px, 1.0, True, cc)[0]
    expected_shares = int((brick_base * frac0) / (core_price * 1.16))   # base brick / BASE per-share

    assert captured["quantity"] == expected_shares
    assert captured["limit_price"] == pytest.approx(core_price)   # order still prices in LOCAL (£)
    # Returned spend is in BASE and never exceeds the brick — the old bug returned shares*price (£ as €),
    # i.e. ~1.16x the brick (~5400), which then over-charged the day's budget and over-shot the target.
    assert core_base == pytest.approx(expected_shares * core_price * 1.16)
    assert core_base <= brick_base + 1.0
    # Sanity: a GBP name buys FEWER shares than the naive base/local sizing would (the bug).
    assert expected_shares < int((brick_base * frac0) / core_price)
