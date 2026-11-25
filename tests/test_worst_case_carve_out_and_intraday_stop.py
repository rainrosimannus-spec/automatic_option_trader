"""2026-11-20 (Rain): (1) the XSP sleeve gets its own worst-case dome; (2) the assigned-lot stop
also runs every 30 min during RTH.

(1) Since 11-11 the sleeve shared the book's single dome; with bags on the book the headroom was
$500–$11k against ~$2,000 per spread → qty 0 on all 11 scans. `index_carve_out_pct` > 0 judges
single names + stock against cap_pct × NLV without the index stress, and the sleeve against its
own carve-out. 0 keeps the shared behaviour byte-for-byte (MarsWalk default).
(2) VRT sold 6.6 pts past the 20% stop on 11-20 because the once-a-day 15:45 check saw it once.
"""
from datetime import time as dtime
from pathlib import Path
from unittest.mock import patch

from src.core.config import WorstCaseCapConfig, RiskConfig, Settings
from src.marswalk.engine import Params
from src.strategy.worst_case import WorstCaseBook, split_headroom, headroom

ROOT = Path(__file__).resolve().parents[1]


# ── split_headroom ───────────────────────────────────────────────────────────────────────
def test_zero_carve_out_is_the_shared_dome():
    b = WorstCaseBook(single_puts=13400, stock=15688, index=0)
    s, i = split_headroom(b, 199_208, 0.20, 0.0)
    assert s == i == headroom(b.total, 199_208, 0.20)


def test_carve_out_frees_the_sleeve_without_freeing_the_bags():
    """Tonight's book: puts 13.4k + stock 15.7k on $199k. Shared dome gave the sleeve $10.7k;
    on 11-17/18 it was NEGATIVE. With a 5% carve-out the sleeve sees its own $9.96k, the bags
    keep their own 20% dome, and index stress never squeezes a single-name put."""
    b = WorstCaseBook(single_puts=29150, stock=24340, index=0)      # 11-18 snapshot: over the cap
    s, i = split_headroom(b, 210_159, 0.20, 0.05)
    assert s < 0                                 # single names still blocked, as before
    assert round(i) == round(0.05 * 210_159)     # sleeve: full carve-out available
    # one spread on → only the sleeve's dome shrinks
    b2 = WorstCaseBook(single_puts=29150, stock=24340, index=2019)
    s2, i2 = split_headroom(b2, 210_159, 0.20, 0.05)
    assert s2 == s and round(i2) == round(0.05 * 210_159 - 2019)


def test_risk_manager_scopes(monkeypatch):
    from src.strategy import risk as risk_mod
    rm = risk_mod.RiskManager.__new__(risk_mod.RiskManager); rm.cfg = RiskConfig()

    class _S:
        class worst_case_cap:
            enabled = True; cap_pct = 0.20; single_gap = 0.20; stock_gap = 0.20; index_gap = 0.10
            index_carve_out_pct = 0.05
    import src.core.config as cfgmod
    monkeypatch.setattr(cfgmod, "get_settings", lambda: _S)
    over = WorstCaseBook(single_puts=29150, stock=24340)
    monkeypatch.setattr(rm, "worst_case_book", lambda light=False: (over, 210_159.0))
    single_room, snap = rm.worst_case_headroom()
    index_room, _ = rm.worst_case_headroom(scope="index")
    assert single_room < 0 and index_room > 10_000
    assert snap["headroom_usd"] == round(single_room) and snap["index_headroom_usd"] == round(index_room)
    assert snap["index_carve_out_pct"] == 0.05 and snap["index_cap_usd"] == round(0.05 * 210_159)
    assert rm.check_worst_case_cap().allowed is False          # single names: still blocked
    assert rm.worst_case_clamp("X", 300.0, 1) == 0
    # the shared-dome contract (carve-out 0) is unchanged
    _S.worst_case_cap.index_carve_out_pct = 0.0
    a, sa = rm.worst_case_headroom(); b_, _ = rm.worst_case_headroom(scope="index")
    assert a == b_ == headroom(over.total, 210_159.0, 0.20) and sa["index_cap_usd"] is None


def test_sleeve_asks_for_the_index_scope_and_defaults_stay_off():
    assert 'worst_case_headroom(scope="index")' in (ROOT / "src/strategy/index_puts.py").read_text()
    assert WorstCaseCapConfig().index_carve_out_pct == 0.0
    assert Params().worst_case_index_carve_out_pct == 0.0
    eng = (ROOT / "src/marswalk/engine.py").read_text()
    assert '_wc_book(d, "index")' in eng and '"single" if _wc_carve > 0 else "all"' in eng


def test_live_yaml_values():
    s = Settings.load() if hasattr(Settings, "load") else None
    import yaml
    y = yaml.safe_load((ROOT / "config/settings.yaml").read_text())
    # 2026-11-25 (Rain): two overlapping 5% spreads → the sleeve's own dome is 10%
    assert y["worst_case_cap"]["index_carve_out_pct"] == 0.10
    assert y["index_puts"]["max_open"] == 2 and y["index_puts"]["max_loss_pct"] == 0.10
    assert y["risk"]["wheel_stop_intraday_enabled"] is True


# ── intraday stop ────────────────────────────────────────────────────────────────────────
def test_intraday_window_boundaries():
    from src.scheduler.jobs import _wheel_stop_intraday_window as w
    assert not w(dtime(9, 34))
    assert w(dtime(9, 35))
    assert w(dtime(12, 5))
    assert w(dtime(15, 35))
    assert not w(dtime(15, 40))       # the 15:45 close check owns the last pass
    assert not w(dtime(15, 45))


def test_intraday_job_respects_knob_and_window():
    from src.scheduler import jobs
    calls = []
    with patch.object(jobs, "_run_wheel_stop_check", lambda label="close": calls.append(label)):
        # knob off → nothing, even inside the window
        with patch("src.core.config.get_settings") as gs, patch.object(jobs, "_wheel_stop_intraday_window", return_value=True):
            gs.return_value.risk.wheel_stop_intraday_enabled = False
            jobs.job_wheel_stop_check_intraday()
        assert calls == []
        # knob on, outside the window → nothing
        with patch("src.core.config.get_settings") as gs, patch.object(jobs, "_wheel_stop_intraday_window", return_value=False):
            gs.return_value.risk.wheel_stop_intraday_enabled = True
            jobs.job_wheel_stop_check_intraday()
        assert calls == []
        # knob on, inside → the same routine as the close check, labelled intraday
        with patch("src.core.config.get_settings") as gs, patch.object(jobs, "_wheel_stop_intraday_window", return_value=True):
            gs.return_value.risk.wheel_stop_intraday_enabled = True
            jobs.job_wheel_stop_check_intraday()
        assert calls == ["intraday"]
        jobs.job_wheel_stop_check()
        assert calls == ["intraday", "close"]


def test_intraday_job_is_registered_and_chips_updated():
    j = (ROOT / "src/scheduler/jobs.py").read_text()
    assert 'id="wheel_stop_check_intraday"' in j and 'CronTrigger(hour="9-15", minute="5,35"' in j
    assert 'id="wheel_stop_check"' in j                       # the 15:45 close check still exists
    assert RiskConfig().wheel_stop_intraday_enabled is True
    t = (ROOT / "src/web/templates/dashboard.html").read_text()
    assert "every 30 min 09:35–15:35 ET plus the 15:45 close check" in t
    assert "own 5%-of-NLV carve-out" in t
