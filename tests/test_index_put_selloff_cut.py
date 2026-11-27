"""Tiered sell-off cut for the XSP sleeve (Rain 2026-11-27) + the stop switched off.

Model (scripts/xsp_sleeve_cut_model.py, 20 yrs of SPY OHLC): half-size new spreads after a −3% week
and none after a −5% week cost ~1 pp/yr compounded and cut the worst month from −28% to −14.5% of NLV
at the live 2×5% sizing. The 3× credit stop lost 12 pp/yr to whipsaws (scripts/xsp_stop_rule_model.py).
"""
from datetime import date
import re


def test_stop_ships_off_and_cut_ships_on():
    from src.core.config import IndexPutConfig
    c = IndexPutConfig()
    assert c.stop_mult == 0.0
    assert (c.selloff_cut_5d_pct, c.selloff_cut_factor, c.selloff_pause_5d_pct) == (0.03, 0.5, 0.05)


def test_five_day_change_uses_the_sixth_last_close():
    from src.strategy.index_puts import five_day_change
    assert five_day_change([100, 101, 102, 103, 104, 105, 97]) == 97 / 101 - 1   # 6 sessions back = index -6
    assert five_day_change([100, 101, 102, 103, 104]) is None                      # too short
    assert five_day_change([]) is None
    assert five_day_change(None) is None


def test_tiered_factor_boundaries():
    from src.strategy.index_puts import selloff_size_factor as f
    cut, fac, pause = 0.03, 0.5, 0.05
    assert f(None, cut, fac, pause) == 1.0          # no data → no cut (fail-open)
    assert f(+0.02, cut, fac, pause) == 1.0
    assert f(-0.0299, cut, fac, pause) == 1.0
    assert f(-0.03, cut, fac, pause) == 0.5         # at the line → half
    assert f(-0.0499, cut, fac, pause) == 0.5
    assert f(-0.05, cut, fac, pause) == 0.0         # at the pause line → nothing
    assert f(-0.20, cut, fac, pause) == 0.0
    # a tier set to 0 is disabled
    assert f(-0.10, cut, fac, 0.0) == 0.5
    assert f(-0.04, 0.0, fac, pause) == 1.0
    assert f(-0.06, 0.0, fac, pause) == 0.0
    assert f(-0.04, cut, 1.0, pause) == 1.0         # factor 1 = cut tier is a no-op


def test_sim_shares_the_predicate_and_ships_off():
    from src.marswalk import engine
    from src.strategy import index_puts as ip
    assert engine.selloff_size_factor is ip.selloff_size_factor
    p = engine.Params()
    assert (p.index_put_selloff_cut_pct, p.index_put_selloff_pause_pct) == (0.0, 0.0)   # byte-identical default
    assert p.index_put_selloff_cut_factor == 0.5


def test_live_change_drops_todays_partial_bar(monkeypatch):
    """The 10:10 scan sees today's row in FMP's EOD feed (live price as 'close'); the 5-day change must
    use the last five COMPLETED sessions only, and the result is cached for the day."""
    from src.strategy import index_puts as ip
    from src.portfolio import fmp
    today = date.today().isoformat()
    rows = [{"date": today, "close": 500.0}] + [
        {"date": f"2026-01-{d:02d}", "close": c} for d, c in
        ((2, 100.0), (5, 101.0), (6, 102.0), (7, 103.0), (8, 104.0), (9, 105.0), (12, 97.0))]
    calls = []
    monkeypatch.setattr(fmp, "_get", lambda ep, sym, params: calls.append(sym) or list(reversed(rows)))
    seller = ip.IndexPutSeller.__new__(ip.IndexPutSeller)
    seller.cfg = ip.IndexPutConfig() if hasattr(ip, "IndexPutConfig") else None
    seller._spy_5d_cache = None
    chg = seller._spy_change_5d()
    assert abs(chg - (97 / 101 - 1)) < 1e-12        # today's 500 ignored
    assert seller._spy_change_5d() == chg and calls == ["SPY"]   # cached, one fetch


def test_live_change_fails_open(monkeypatch):
    from src.strategy import index_puts as ip
    from src.portfolio import fmp
    def boom(*a, **k): raise RuntimeError("fmp down")
    monkeypatch.setattr(fmp, "_get", boom)
    seller = ip.IndexPutSeller.__new__(ip.IndexPutSeller)
    seller._spy_5d_cache = None
    assert seller._spy_change_5d() is None
    assert ip.selloff_size_factor(None, 0.03, 0.5, 0.05) == 1.0


def test_scan_source_applies_the_factor_before_the_dome_and_pauses_at_zero():
    """Structural check on the scan body: the cut multiplies qty BEFORE the worst-case clamp (so the dome
    never re-inflates a cut), a factor of 0 returns before any order, and the decision carries both numbers."""
    import inspect
    from src.strategy import index_puts as ip
    src = inspect.getsource(ip.IndexPutSeller.scan)
    i_cut = src.index("selloff_size_factor(chg5")
    i_dome = src.index("worst_case_headroom")
    i_pause = src.index('log.info("index_put_selloff_pause"')
    i_order = src.index("buy_index_put_open(")
    assert i_cut < i_dome < i_pause < i_order
    assert '"spy_change_5d"' in src and '"size_factor"' in src
    assert "qty = int(qty * size_factor)" in src


def test_dashboard_chip_describes_the_cut_and_no_stop():
    html = open("src/web/templates/dashboard.html", encoding="utf-8").read()
    assert "half size while SPY's 5-day change ≤ −3%, none below −5%" in html
    assert "no stop — the wing is the stop" in html
    assert "stop at 3× credit" not in html
