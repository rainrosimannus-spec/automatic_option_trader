"""XSP dry-run chain log (Rain 2026-11-10): the strike sweep on real premiums.

Day 1 of the dry run showed the sim's 2σ strike (3.1% OTM, 1 DTE) bid 0.11 / 0.05 — the flat-vol
sim had priced it as worth selling. So the dry run now quotes the whole chain from the money to
3.5% OTM every scan and records the settlement next morning; scripts/xsp_chain_report.py joins them.
"""
import json
from datetime import date

from src.core.config import IndexPutConfig
from src.strategy.index_puts import (chain_strikes, append_jsonl, read_jsonl, unsettled_expiries,
                                     IndexPutSeller)


def test_chain_strikes_from_the_money_down():
    strikes = [float(k) for k in range(700, 781)]
    ks = chain_strikes(752.3, strikes, 0.035)
    assert ks[0] == 752.0 and ks[-1] == 726.0          # 752.3 × 0.965 = 725.97 → 726 is the last
    assert ks == sorted(ks, reverse=True)
    assert chain_strikes(0.0, strikes, 0.035) == [] and chain_strikes(750.0, [], 0.035) == []


def test_jsonl_roundtrip_and_unsettled(tmp_path):
    p = tmp_path / "xsp_chain.jsonl"
    append_jsonl(p, {"type": "chain", "expiry": "20261111", "quotes": []})
    append_jsonl(p, {"type": "chain", "expiry": "20261112", "quotes": []})
    append_jsonl(p, {"type": "settle", "expiry": "20261111", "settle_px": 750.0})
    rows = read_jsonl(p)
    assert len(rows) == 3
    assert unsettled_expiries(rows, "20261113") == ["20261112"]
    assert unsettled_expiries(rows, "20261112") == []        # today's expiry not yet past
    assert read_jsonl(tmp_path / "missing.jsonl") == []


def test_config_defaults():
    c = IndexPutConfig()
    assert c.chain_log_enabled is True and c.chain_log_max_otm == 0.035
    assert c.chain_log_path.endswith("xsp_chain.jsonl")


def _seller(tmp_path, dry_run=True):
    s = IndexPutSeller.__new__(IndexPutSeller)
    s.cfg = IndexPutConfig(enabled=True, dry_run=dry_run, chain_log_path=str(tmp_path / "c.jsonl"))
    s.risk = None
    return s


def test_log_chain_writes_one_row_per_scan(tmp_path, monkeypatch):
    from src.broker import market_data as md
    monkeypatch.setattr(md, "get_index_option_chain_quotes",
                        lambda sym, exp, ks, right, exch, tc: {k: (round(0.05 + (752 - k) * -0.01 + 1.0, 2), 1.3)
                                                               for k in ks})
    s = _seller(tmp_path)
    n = s._log_chain("20261111", 752.3, 0.012, [float(k) for k in range(700, 781)])
    rows = read_jsonl(s.cfg.chain_log_path)
    assert n == 27 and len(rows) == 1
    r = rows[0]
    assert r["type"] == "chain" and r["expiry"] == "20261111" and r["spot"] == 752.3
    assert r["quotes"][0]["strike"] == 752.0 and r["quotes"][0]["otm_pct"] == 0.04
    assert all(q["bid"] is not None for q in r["quotes"])


def test_log_chain_is_off_when_live_or_disabled(tmp_path, monkeypatch):
    s = _seller(tmp_path, dry_run=False)
    assert s._log_chain("20261111", 752.3, 0.012, [750.0]) == 0
    s2 = _seller(tmp_path)
    s2.cfg = IndexPutConfig(enabled=True, dry_run=True, chain_log_enabled=False,
                            chain_log_path=str(tmp_path / "c.jsonl"))
    assert s2._log_chain("20261111", 752.3, 0.012, [750.0]) == 0
    assert read_jsonl(tmp_path / "c.jsonl") == []


def test_settle_chain_log_appends_once(tmp_path, monkeypatch):
    s = _seller(tmp_path)
    append_jsonl(s.cfg.chain_log_path, {"type": "chain", "expiry": "20261111", "quotes": []})
    monkeypatch.setattr(s, "_settlement_price", lambda exp, gph: 749.5)
    assert s._settle_chain_log("20261112", None) == ["20261111"]
    assert s._settle_chain_log("20261112", None) == []          # already settled
    rows = read_jsonl(s.cfg.chain_log_path)
    assert [r["type"] for r in rows] == ["chain", "settle"] and rows[1]["settle_px"] == 749.5


def test_report_buckets(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("rep", "scripts/xsp_chain_report.py")
    rep = importlib.util.module_from_spec(spec); spec.loader.exec_module(rep)
    quotes = [{"strike": 752.0, "otm_pct": 0.0, "bid": 2.0, "ask": 2.2},
              {"strike": 745.0, "otm_pct": 0.93, "bid": 0.8, "ask": 0.9},
              {"strike": 730.0, "otm_pct": 2.93, "bid": 0.1, "ask": 0.15},
              {"strike": 727.0, "otm_pct": 3.32, "bid": 0.05, "ask": 0.1}]
    rows = [{"type": "chain", "expiry": "20261111", "spot": 752.0, "quotes": quotes},
            {"type": "settle", "expiry": "20261111", "settle_px": 748.0}]
    agg, n_settled, n_open, n_exp = rep.summarize(rows, 0.03)
    assert (n_settled, n_open, n_exp) == (1, 0, 1)
    atm = agg["0.0-0.5%"]
    assert atm["n"] == 1 and atm["breach"] == 1 and atm["intr"] == 4.0        # 752 put, settle 748
    assert agg["0.5-1.0%"]["breach"] == 0 and agg["0.5-1.0%"]["prem"] == 0.8
    # 752 short with 3% wing → 727 wing at ask 0.1: net 1.9, payoff 1.9 − 4 + 0 = −2.1
    assert abs(atm["sp_net"] - (-2.1)) < 1e-9
