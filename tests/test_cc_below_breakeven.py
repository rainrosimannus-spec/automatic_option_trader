"""Tests for the below-breakeven CC downtrend guard (G4) and its fail-safe.

The below-breakeven CC is the strict last resort that CAN lock a (capped) loss,
so its guard must be conservative: only True on a genuine, data-confirmed
downtrend, and False whenever data is missing/insufficient — never cap a loss
without positive confirmation.
"""
from __future__ import annotations

import src.strategy.wheel as wheel


def _patch_history(monkeypatch, prices):
    import src.portfolio.fmp as fmp
    monkeypatch.setattr(fmp, "get_price_history", lambda symbol, days=70: prices)


def test_downtrend_true_when_below_ma200_and_no_momentum(monkeypatch):
    # 250 closes trending down: MA200 well above spot, and price 20d ago > spot.
    prices = [400.0 - i * 0.5 for i in range(250)]   # 400 → 275, monotered down
    _patch_history(monkeypatch, prices)
    spot = prices[-1] - 5     # below last close → below MA200 and below 20d-ago
    assert wheel._confirmed_downtrend("LRCX", spot) is True


def test_downtrend_false_when_recovering(monkeypatch):
    # Deep dip then a sharp 20-day recovery: spot above its 20d-ago price → not a
    # confirmed downtrend even if still under MA200. Protects a recovering name.
    prices = [400.0 - i for i in range(230)]          # long way down to ~170
    prices += [prices[-1] + i * 8 for i in range(1, 25)]  # sharp 24-day bounce
    _patch_history(monkeypatch, prices)
    spot = prices[-1]
    # spot is above its close 20 days ago (bounce) → momentum up → guard False
    assert wheel._confirmed_downtrend("LRCX", spot) is False


def test_downtrend_false_on_missing_history(monkeypatch):
    _patch_history(monkeypatch, None)
    assert wheel._confirmed_downtrend("LRCX", 100.0) is False


def test_downtrend_false_on_insufficient_history(monkeypatch):
    _patch_history(monkeypatch, [100.0] * 50)   # <201 closes
    assert wheel._confirmed_downtrend("LRCX", 90.0) is False


def test_downtrend_false_on_zero_spot(monkeypatch):
    _patch_history(monkeypatch, [100.0 - i * 0.1 for i in range(250)])
    assert wheel._confirmed_downtrend("LRCX", 0.0) is False


def test_config_defaults_match_agreed_gates():
    from src.core.config import get_settings
    r = get_settings().risk
    assert r.cc_below_breakeven_enabled is True
    assert r.cc_bbe_min_age_days == 90
    assert r.cc_bbe_min_drawdown == 0.20
    assert r.cc_bbe_max_lock == 0.10


# ── Integration: drive _write_call so the below-breakeven fallback is exercised ──
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from src.strategy.wheel import WheelManager
from src.core.config import get_settings


def _bbe_candidate(strike):
    return SimpleNamespace(contract=SimpleNamespace(), strike=strike, expiry="20260907",
                           delta=0.03, bid=0.30, ask=0.40, mid=0.35, iv=1.2,
                           open_interest=50, score=1.0)


def _fake_self():
    return SimpleNamespace(cfg=get_settings().strategy, risk=get_settings().risk,
                           universe=SimpleNamespace(get_exchange=lambda s: "SMART",
                                                    get_currency=lambda s: "USD",
                                                    get_contract_size=lambda s: 100))


def _zombie_lot(age_days):
    # basis 404, spot 305 → ~-24% deep; opened age_days ago.
    return SimpleNamespace(symbol="LRCX", cost_basis=404.0, quantity=100,
                           wheel_exit_mode="rescue", total_premium_collected=0.0,
                           opened_at=datetime.utcnow() - timedelta(days=age_days))


def _wire(monkeypatch, *, spot, downtrend, token_off=True):
    # Only the below-breakeven call (min_strike ~= basis*0.90) yields a candidate;
    # every earlier band (velocity / rescue / token) returns None.
    def side(symbol, **kw):
        ms = kw.get("min_strike")
        if ms is not None and abs(ms - 404.0 * 0.90) < 1e-6:
            return _bbe_candidate(strike=ms + 1.0)
        return None
    monkeypatch.setattr(wheel, "screen_calls", MagicMock(side_effect=side))
    monkeypatch.setattr(wheel, "sell_covered_call",
                        lambda **k: SimpleNamespace(order=SimpleNamespace(orderId=1)))
    monkeypatch.setattr(wheel, "_realized_cc_premium_per_share", lambda db, pos: 0.0)
    monkeypatch.setattr(wheel, "_confirmed_downtrend", lambda s, p: downtrend)
    import src.broker.market_data as md
    monkeypatch.setattr(md, "get_stock_price", lambda *a, **k: spot)
    if token_off:
        monkeypatch.setattr(get_settings().risk, "cc_token_rescue_enabled", False)


def test_below_breakeven_fires_when_aged_deep_downtrend(monkeypatch):
    _wire(monkeypatch, spot=305.0, downtrend=True)
    ok = WheelManager._write_call(_fake_self(), MagicMock(), _zombie_lot(age_days=120), contracts=1)
    assert ok is True     # capped below-breakeven CC written on the genuine zombie


def test_below_breakeven_skipped_when_not_aged(monkeypatch):
    _wire(monkeypatch, spot=305.0, downtrend=True)
    ok = WheelManager._write_call(_fake_self(), MagicMock(), _zombie_lot(age_days=23), contracts=1)
    assert ok is False    # 23d < 90d gate → give recovery time, do not cap


def test_below_breakeven_skipped_when_recovering(monkeypatch):
    _wire(monkeypatch, spot=305.0, downtrend=False)   # G4 guard says not a downtrend
    ok = WheelManager._write_call(_fake_self(), MagicMock(), _zombie_lot(age_days=120), contracts=1)
    assert ok is False    # recovering name is never capped, even aged + deep
