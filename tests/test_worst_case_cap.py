"""Total worst-case cap (Rain 2026-11-11): the dome over the whole book."""
from src.core.config import WorstCaseCapConfig, Settings
from src.marswalk.engine import Params
from src.strategy.worst_case import (stress_single_put, stress_stock, stress_index_spread, stress_index_naked,
                                     book_worst_case, headroom, contracts_within)


def test_stress_pieces():
    assert stress_single_put(300.0, 1, 0.20) == 6000.0
    assert stress_stock(100.0, 500, 0.20) == 10000.0
    assert stress_index_spread(730.0, 708.0, 0.90, 10) == (22.0 - 0.9) * 100 * 10
    assert stress_index_spread(730.0, 708.0, 30.0, 10) == 0.0          # credit ≥ width → no loss
    assert stress_index_naked(730.0, 5, 0.10) == 36500.0


def test_book_and_headroom_match_tonights_account():
    cfg = WorstCaseCapConfig(cap_pct=0.15)
    puts = [{"symbol": "INTU", "strike": 317.5, "qty": 1}, {"symbol": "DG", "strike": 131.0, "qty": 2},
            {"symbol": "GOOG", "strike": 277.5, "qty": 1}]
    stocks = [{"symbol": "ADBE", "mark": 236.7, "shares": 100}, {"symbol": "DECK", "mark": 89.79, "shares": 500},
              {"symbol": "VRT", "mark": 194.22, "shares": 200}]
    b = book_worst_case(puts, stocks, [], cfg)
    assert round(b.single_puts) == 17140 and round(b.stock) == 21482 and b.index == 0.0
    room = headroom(b.total, 222_800, 0.15)
    assert room < 0                                   # tonight's book is OVER a 15% cap → no new puts
    assert headroom(b.total, 222_800, 0.20) > 5000    # at 20% there is room for one small lot
    assert contracts_within(6000.0, 6350.0, 1) == 0 and contracts_within(13000.0, 6350.0, 3) == 2
    assert contracts_within(100.0, 0.0, 3) == 3       # zero-loss candidate never clamped


def test_defaults_off_everywhere():
    assert Settings().worst_case_cap.enabled is False
    p = Params()
    assert p.worst_case_cap_pct == 0.0 and p.worst_case_single_gap == 0.20


def test_live_gate_blocks_when_over_and_clamps(monkeypatch):
    from src.strategy import risk as risk_mod
    from src.strategy.worst_case import WorstCaseBook
    rm = risk_mod.RiskManager.__new__(risk_mod.RiskManager)
    from src.core.config import RiskConfig; rm.cfg = RiskConfig()

    class _WC:  # stand-in settings
        class worst_case_cap:
            enabled = True; cap_pct = 0.15; single_gap = 0.20; stock_gap = 0.20; index_gap = 0.10
    import src.core.config as cfgmod
    monkeypatch.setattr(cfgmod, "get_settings", lambda: _WC)
    over = WorstCaseBook(single_puts=17140, stock=21500)
    monkeypatch.setattr(rm, "worst_case_book", lambda: (over, 222_800.0))
    chk = rm.check_worst_case_cap()
    assert chk.allowed is False and "worst-case cap" in chk.reason
    assert rm.worst_case_clamp("X", 300.0, 2) == 0
    small = WorstCaseBook(stock=10_000)
    monkeypatch.setattr(rm, "worst_case_book", lambda: (small, 222_800.0))
    assert rm.check_worst_case_cap().allowed is True
    assert rm.worst_case_clamp("X", 300.0, 5) == 3      # headroom 23,420 / 6,000 per contract


def test_gate_is_in_can_open_put_and_sleeve_reads_headroom():
    src = open("src/strategy/risk.py").read()
    body = src[src.index("def can_open_put("):]
    assert "check_worst_case_cap" in body[:body.index("for check in checks")]
    assert "worst_case_headroom" in open("src/strategy/index_puts.py").read()
    assert "worst_case_clamp" in open("src/strategy/put_seller.py").read()
