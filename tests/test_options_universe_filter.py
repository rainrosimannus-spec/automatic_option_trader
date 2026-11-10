"""Options-universe filter (Rain 2026-11-11): the wheel's put list is the screener's list minus
early-stage tier and gap-prone names, ≤50, ranked by options_score. Screener untouched."""
from src.core.config import OptionsUniverseFilterConfig, Settings
from src.strategy.universe_filter import compute_metrics, judge, select_options_universe


def _calm(n=260, start=100.0, drift=0.0004):
    out = [start]
    for i in range(n):
        out.append(out[-1] * (1 + drift + (0.004 if i % 2 else -0.0035)))
    return out


def test_metrics_on_calm_and_gappy_series():
    m = compute_metrics(_calm())
    assert m["ann_vol"] < 0.10 and m["max_dd_1y"] < 0.05 and m["worst_day"] > -0.01 and m["bars"] == 261
    gappy = _calm(); gappy[200:] = [c * 0.80 for c in gappy[200:]]        # one −20% day
    g = compute_metrics(gappy)
    assert g["worst_day"] < -0.19 and g["max_dd_1y"] > 0.19
    assert compute_metrics(_calm(30)) is None                           # too short → None


def test_judge_rules():
    cfg = OptionsUniverseFilterConfig()
    calm = compute_metrics(_calm())
    assert judge({"symbol": "MSFT", "tier": "growth", "options_liquidity": 80}, calm, cfg) == (True, [])
    ok, why = judge({"symbol": "CRCL", "tier": "breakthrough", "options_liquidity": 80}, calm, cfg)
    assert not ok and why == ["tier:breakthrough"]
    gappy = dict(calm, worst_day=-0.22, max_dd_1y=0.5, ann_vol=0.9, breach_rate=0.6)
    ok, why = judge({"symbol": "IREN", "tier": "growth", "options_liquidity": 80}, gappy, cfg)
    assert not ok and {w.split(":")[0] for w in why} == {"vol", "dd1y", "gap", "breach"}
    ok, why = judge({"symbol": "ISRG", "tier": "growth"}, dict(calm, last_close=410.0), cfg)
    assert not ok and why[0].startswith("price:410")
    ok, why = judge({"symbol": "X", "tier": "growth", "options_liquidity": 30}, calm, cfg)
    assert not ok and why[0].startswith("liquidity")
    # fail-open: no history → kept, flagged
    assert judge({"symbol": "NEW", "tier": "growth"}, None, cfg) == (True, ["no_history"])
    assert judge({"symbol": "NEW", "tier": "breakthrough"}, None, cfg) == (False, ["tier:breakthrough"])


def test_select_ranks_by_score_and_caps():
    cfg = OptionsUniverseFilterConfig(max_names=2)
    calm = compute_metrics(_calm())
    entries = [{"symbol": "A", "tier": "growth", "options_score": 70, "rank": 3},
               {"symbol": "B", "tier": "growth", "options_score": 90, "rank": 1},
               {"symbol": "C", "tier": "breakthrough", "options_score": 95, "rank": 2},
               {"symbol": "D", "tier": "dividend", "options_score": 80, "rank": 4}]
    kept, dropped = select_options_universe(entries, {e["symbol"]: calm for e in entries}, cfg)
    assert [e["symbol"] for e in kept] == ["B", "D"]
    assert {e["symbol"]: why for e, why in dropped} == {"C": ["tier:breakthrough"], "A": ["rank>2"]}


def test_ships_off_and_manager_noop_when_off(monkeypatch):
    assert Settings().options_universe_filter.enabled is False
    from src.strategy.universe import UniverseManager
    from src.core.config import StockEntry
    stocks = [StockEntry(symbol="MSFT", name="x", sector="t", exchange="SMART", currency="USD", category="growth")]
    assert UniverseManager._apply_options_filter(stocks) == stocks
