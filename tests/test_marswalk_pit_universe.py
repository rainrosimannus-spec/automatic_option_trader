"""2026-11-24 fidelity audit: point-in-time universe + scan-order selection for MarsWalk."""
from datetime import date
from src.marswalk import pit_universe as pit
from src.marswalk.engine import Params


CONS = {"sp500": {"current": ["AAA", "BBB", "NEW"],
                  "changes": [{"date": "2026-01-10", "added": "NEW", "removed": "OLD"},
                              {"date": "2025-06-01", "added": "BBB", "removed": None}]},
        "ndx": {"current": ["QQQ1"], "changes": []}}


def test_members_asof_undoes_changes_after_the_date():
    assert pit.members_asof(CONS, "2026-02-01") == {"AAA", "BBB", "NEW", "QQQ1"}
    assert pit.members_asof(CONS, "2025-12-31") == {"AAA", "BBB", "OLD", "QQQ1"}   # NEW not yet in, OLD still in
    assert pit.members_asof(CONS, date(2025, 1, 1)) == {"AAA", "OLD", "QQQ1"}      # BBB not yet added


def _bars(sym, start_px, drift, days=300, vol=1e6):
    out, px = [], start_px
    d0 = date(2025, 1, 1)
    from datetime import timedelta
    for i in range(days):
        d = d0 + timedelta(days=i)
        if d.weekday() >= 5:
            continue
        px *= (1 + drift)
        out.append((d.isoformat(), round(px, 4), vol))
    return out


def test_screen_ranks_momentum_and_liquidity_only_with_past_data():
    bars = {"UP": _bars("UP", 100, 0.004, vol=5e7), "FLAT": _bars("FLAT", 100, 0.0, vol=5e6),
            "DOWN": _bars("DOWN", 100, -0.003, vol=5e7), "CHEAP": _bars("CHEAP", 10, 0.0, vol=1e9)}
    d = bars["UP"][-1][0]
    picks = pit.screen_asof(bars, {"UP", "FLAT", "DOWN", "CHEAP"}, d, n=2, lookback_days=200, min_price=30.0)
    assert "CHEAP" not in picks                      # below min_price
    assert picks[0] == "UP" and len(picks) == 2      # best momentum first, rank order kept
    assert pit.screen_asof(bars, {"UP"}, bars["UP"][10][0], n=5, lookback_days=200) == []   # not enough history


def test_month_starts_and_schedule_shape():
    dates = [b[0] for b in _bars("X", 100, 0.0, days=120)]
    ms = pit.month_starts("2025-01-15", "2025-04-30", dates)
    assert ms[0] == "2025-01-15" and all(m[:7] in ("2025-01", "2025-02", "2025-03", "2025-04") for m in ms)
    assert len(ms) == 4
    bars = {"UP": _bars("UP", 100, 0.004), "FLAT": _bars("FLAT", 100, 0.0)}
    cons = {"sp500": {"current": ["UP", "FLAT"], "changes": []}}
    sched = pit.build_schedule("2025-10-01", "2025-10-31", n=5, cons=cons, bars=bars, lookback_days=150)
    assert sched and isinstance(sched[0][0], date) and sched[0][1][0] == "UP"


def test_market_from_bars_has_iv_and_warmup():
    bars = {"UP": _bars("UP", 100, 0.004)}
    m = pit.market_from_bars(bars, ["UP"], "2025-06-02", "2025-10-31", warmup_days=60)
    assert "UP" in m and "_pre:UP" in m
    d, c, iv = m["UP"][0]
    assert d >= date(2025, 6, 2) and iv > 0
    assert all(row[2] == 0.0 for row in m["_pre:UP"])


def test_engine_schedule_and_selection_knobs():
    import inspect
    from src.marswalk import engine
    src = inspect.getsource(engine.run_regime)
    assert "universe_schedule: list | None = None" in src and "_members_asof(d)" in src
    assert 'getattr(params, "selection_mode", "best_first") == "best_first"' in src
    p = Params()
    assert p.selection_mode == "scan_order" and p.start_capital == 220_000.0
    assert Params(selection_mode="best_first").selection_mode == "best_first"


def test_forced_puts_replay_hook_present():
    import inspect
    from src.marswalk import engine
    src = inspect.getsource(engine.run_regime)
    assert "forced_puts: dict | None = None" in src
    assert 'for f in _forced.get(d, [])' in src and "slots = 0" in src
