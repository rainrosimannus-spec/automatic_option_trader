"""Portfolio page: the drop-out triangle only for a held name the last screen ranked beyond reach of
its tier (growth >100, dividend >25, breakthrough >40) or did not rank at all (2026-11-04)."""
import json

from src.web.routes.portfolio import _within_tier_reach, _load_tier_rankings, TIER_REACH


def _rank(n, prefix):
    return [[f"{prefix}{i}", 100.0 - i * 0.1] for i in range(1, n + 1)]


def test_near_miss_hides_triangle_and_real_dropout_shows_it():
    rankings = {"growth": _rank(130, "G"), "dividend": _rank(40, "D"), "breakthrough": _rank(55, "B")}
    assert _within_tier_reach("G75", "growth", rankings) is True        # AMD-like: rank 75 of 110
    assert _within_tier_reach("G100", "growth", rankings) is True
    assert _within_tier_reach("G101", "growth", rankings) is False
    assert _within_tier_reach("D25", "dividend", rankings) is True
    assert _within_tier_reach("D26", "dividend", rankings) is False
    assert _within_tier_reach("B40", "breakthrough", rankings) is True
    assert _within_tier_reach("B41", "breakthrough", rankings) is False


def test_unranked_unknown_tier_or_old_run_log_keep_the_triangle():
    rankings = {"growth": _rank(10, "G")}
    assert _within_tier_reach("ARM", "growth", rankings) is False          # gate failure: not in the list
    assert _within_tier_reach("G5", None, rankings) is False               # no tier known
    assert _within_tier_reach("G5", "dividend", rankings) is False         # tier list missing
    assert _within_tier_reach("G5", "growth", {}) is False                 # run log predates rankings
    assert TIER_REACH == {"growth": 100, "dividend": 25, "breakthrough": 40}


def test_load_tier_rankings_from_run_log(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert _load_tier_rankings() == {}
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "screener_last_run.json").write_text(json.dumps(
        {"status": "success", "tier_rankings": {"growth": [["AMD", 64.8]]}}))
    assert _load_tier_rankings() == {"growth": [["AMD", 64.8]]}
    (tmp_path / "data" / "screener_last_run.json").write_text('{"status": "error"}')
    assert _load_tier_rankings() == {}
