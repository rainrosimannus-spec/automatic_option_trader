"""Lever 3a — token covered call on deep-underwater lots that otherwise get NO call.

A lot far below breakeven (e.g. IREN at -20%) whose only >=-breakeven strikes sit below the 0.05Δ rescue
band would previously get no covered call at all (dead capital). The token-rescue fallback retries
screen_calls at 0.01Δ so a far-OTM strike with a real, fee-clearing bid still gets written — never below
breakeven. These tests drive one rescue lot through WheelManager._write_call with screen_calls mocked so
the standard band returns None and only the 0.01Δ retry yields a candidate.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import src.strategy.wheel as wheel_mod
from src.strategy.wheel import WheelManager
from src.core.config import get_settings


def _token_candidate():
    return SimpleNamespace(contract=SimpleNamespace(), strike=50.0, expiry="20260807",
                           delta=0.02, bid=0.35, ask=0.45, mid=0.40, iv=1.1,
                           open_interest=100, score=1.0)


def _fake_self():
    cfg = get_settings().strategy
    risk = get_settings().risk
    uni = SimpleNamespace(get_exchange=lambda s: "SMART", get_currency=lambda s: "USD",
                          get_contract_size=lambda s: 100)
    return SimpleNamespace(cfg=cfg, risk=risk, universe=uni)


def _deep_lot():
    # cost_basis 48.98, spot 39.13 -> ~-20% -> in_rescue (< 0.97*basis)
    return SimpleNamespace(symbol="IREN", cost_basis=48.98, quantity=100,
                           wheel_exit_mode="rescue", total_premium_collected=0.0)


def _wire(monkeypatch, screen_side_effect):
    monkeypatch.setattr(wheel_mod, "screen_calls", MagicMock(side_effect=screen_side_effect))
    monkeypatch.setattr(wheel_mod, "sell_covered_call",
                        lambda **k: SimpleNamespace(order=SimpleNamespace(orderId=999)))
    monkeypatch.setattr(wheel_mod, "_realized_cc_premium_per_share", lambda db, pos: 0.0)
    import src.broker.market_data as md
    monkeypatch.setattr(md, "get_stock_price", lambda *a, **k: 39.13)  # deep underwater


def test_token_rescue_covers_deep_lot(monkeypatch):
    calls = []
    def side(symbol, **kw):
        calls.append(kw.get("delta_min_override"))
        return _token_candidate() if kw.get("delta_min_override") == 0.01 else None
    _wire(monkeypatch, side)
    assert get_settings().risk.cc_token_rescue_enabled is True
    ok = WheelManager._write_call(_fake_self(), MagicMock(), _deep_lot(), contracts=1)
    assert ok is True                                  # a CC was written
    assert 0.01 in calls                               # the token band was tried
    # and it only reached the token band AFTER a standard-band (>=0.05) attempt returned None
    assert any(d and d >= 0.05 for d in calls if d is not None)


def test_token_rescue_off_leaves_uncovered(monkeypatch):
    monkeypatch.setattr(get_settings().risk, "cc_token_rescue_enabled", False)
    def side(symbol, **kw):
        return _token_candidate() if kw.get("delta_min_override") == 0.01 else None
    _wire(monkeypatch, side)
    ok = WheelManager._write_call(_fake_self(), MagicMock(), _deep_lot(), contracts=1)
    assert ok is False                                 # no token retry → stays uncovered
