"""Shadow selector (2026-11-25): the simulator's price-only pick, paper-booked beside the live screener."""
import json
from datetime import date
from src.core.config import ShadowSelectorConfig, Settings
from src.strategy import shadow_selector as sh
from src.marswalk import pit_universe as pit


def test_config_defaults_dry_and_wired():
    c = ShadowSelectorConfig()
    assert c.enabled and c.otm_pct == 0.025 and c.per_scan == 2 and c.qty == 1
    assert Settings().shadow_selector.scan_times_et == ["10:40", "14:40"]


def test_pick_contract_mirrors_the_sim_rule():
    by_exp = {"20261128": [95.0, 96.0, 97.0, 98.0, 99.0, 101.0], "20261126": [97.0, 98.0]}
    # longest expiry, strike nearest 2.5% below 100 → 97.5 is between 97 and 98 → tie → higher strike
    assert sh.pick_contract(by_exp, 100.0, 0.025, date(2026, 11, 25)) == ("20261128", 98.0)
    assert sh.pick_contract(by_exp, 100.0, 0.03, date(2026, 11, 25)) == ("20261128", 97.0)
    assert sh.pick_contract({"20261128": [101.0, 102.0]}, 100.0, 0.025, date(2026, 11, 25)) is None   # nothing below spot
    assert sh.pick_contract({}, 100.0, 0.025, date(2026, 11, 25)) is None


def test_slots_and_settlement():
    assert sh.slots_available(0, 10, 2) == 2
    assert sh.slots_available(9, 10, 2) == 1
    assert sh.slots_available(10, 10, 2) == 0
    sell = {"kind": "sell", "date": "2026-11-25", "sym": "ABC", "strike": 98.0, "expiry": "20261128", "premium": 0.80, "qty": 1}
    otm = sh.settle_row(sell, 100.0, 1.18)
    assert otm["itm"] is False and otm["pnl"] == round(80 - 1.18, 2)
    itm = sh.settle_row(sell, 95.0, 1.18)
    assert itm["itm"] is True and itm["intrinsic"] == 3.0 and itm["pnl"] == round((0.80 - 3.0) * 100 - 1.18, 2)
    rows = [sell, otm]
    assert sh.unsettled(rows) == []
    assert sh.unsettled([sell]) == [sell]


def test_settle_books_only_past_expiries_with_a_close(tmp_path, monkeypatch):
    log = tmp_path / "shadow.jsonl"
    sh.append_jsonl(log, {"kind": "sell", "date": "2026-11-20", "sym": "ABC", "strike": 98.0, "expiry": "20261121", "premium": 0.5, "qty": 1})
    sh.append_jsonl(log, {"kind": "sell", "date": "2026-11-24", "sym": "DEF", "strike": 50.0, "expiry": "20261128", "premium": 0.4, "qty": 1})
    sel = sh.ShadowSelector.__new__(sh.ShadowSelector)
    sel.cfg = ShadowSelectorConfig(log_path=str(log))
    fetched = []

    def fake_fetch(sym, a, b):
        fetched.append(sym)
        return [{"date": "2026-11-21", "close": 97.0}]
    out = sel.settle(today=date(2026, 11, 25), fetch=fake_fetch)
    assert [r["sym"] for r in out] == ["ABC"] and out[0]["itm"] and out[0]["intrinsic"] == 1.0
    assert fetched == ["ABC"]                                  # DEF not expired yet → not fetched
    assert len(sh.unsettled(sh.read_jsonl(log))) == 1         # DEF still open


def test_pit_refresh_and_current_screen(tmp_path, monkeypatch):
    cons = tmp_path / "cons.json"; db = tmp_path / "pit.db"
    monkeypatch.setattr(pit, "fetch_constituents", lambda path=None: (cons.write_text(json.dumps(
        {"sp500": {"current": ["UP", "FLAT", "CHEAP"], "changes": []}, "ndx": {"current": [], "changes": []}})),
        json.loads(cons.read_text()))[1])
    from datetime import timedelta
    today = date.today()

    def fake_fetch(sym, a, b):
        px = {"UP": 100.0, "FLAT": 100.0, "CHEAP": 10.0}[sym]
        drift = {"UP": 0.004, "FLAT": 0.0, "CHEAP": 0.0}[sym]
        out = []
        for i in range(400, -1, -1):
            d = today - timedelta(days=i)
            if d.weekday() < 5:
                px *= (1 + drift)
                out.append({"date": d.isoformat(), "close": round(px, 4), "volume": 1e6})
        return out
    res = pit.refresh_recent(days=400, db_path=db, cons_path=cons, sleep_s=0, fetch=fake_fetch)
    assert res["members"] == 3 and res["rows"] > 600 and res["empty"] == []
    assert pit.latest_bar_date(db) is not None
    picks = pit.current_screen(n=5, db_path=db, cons_path=cons, lookback_days=200)
    assert picks[0] == "UP" and "CHEAP" not in picks
