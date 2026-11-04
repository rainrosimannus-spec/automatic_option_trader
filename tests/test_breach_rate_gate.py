"""Rule 4 (2026-11-04): per-name breach-rate gate — data-driven, no hand list."""
from src.core.config import RiskConfig
from src.strategy.realized_move import breach_rate


def test_breach_rate_counts_three_day_dips_over_threshold():
    # alternating 100/95: the 100-days see a 5% dip within 3 closes, the 95-days do not → 0.5
    series = [100.0, 95.0] * 12
    assert breach_rate(series, lookback=20, horizon=3, threshold=0.025) == 0.5
    # a 3%/day slide: every start day dips >2.5% by the next close → 1.0
    slide = [100.0 * (0.97 ** i) for i in range(30)]
    assert breach_rate(slide, lookback=20, horizon=3, threshold=0.025) == 1.0
    # a slow drift down of 0.5%/day never dips 2.5% within 3 days → 0.0
    drift = [100.0 * (0.995 ** i) for i in range(30)]
    assert breach_rate(drift, lookback=20, horizon=3, threshold=0.025) == 0.0


def test_breach_rate_none_on_short_history():
    assert breach_rate([100, 99, 98, 97], lookback=120, horizon=3, threshold=0.025) is None
    assert breach_rate([], lookback=120, horizon=3, threshold=0.025) is None


def test_default_ceiling_is_off():
    rc = RiskConfig()
    assert rc.put_universe_max_breach_rate == 1.0
    assert (rc.put_universe_breach_lookback, rc.put_universe_breach_horizon,
            rc.put_universe_breach_threshold) == (120, 3, 0.025)


def test_check_breach_rate_fail_open_and_block(monkeypatch):
    from src.strategy import risk as risk_mod
    rm = risk_mod.RiskManager.__new__(risk_mod.RiskManager)
    rm._closes_cache = {}
    rm.cfg = RiskConfig(put_universe_max_breach_rate=0.30)
    # no history → allowed (fail-open)
    monkeypatch.setattr(rm, "_daily_closes", lambda sym, n: None)
    assert rm.check_breach_rate("X").allowed is True
    # a 50% breach series → blocked
    monkeypatch.setattr(rm, "_daily_closes", lambda sym, n: [100.0, 95.0, 100.0, 100.0] * 40)
    chk = rm.check_breach_rate("X")
    assert chk.allowed is False and "breach rate" in chk.reason
    # ceiling 1.0 → never blocks, never even fetches
    rm.cfg = RiskConfig(put_universe_max_breach_rate=1.0)
    monkeypatch.setattr(rm, "_daily_closes", lambda sym, n: (_ for _ in ()).throw(AssertionError("fetched")))
    assert rm.check_breach_rate("X").allowed is True
