"""2026-11-24: dated snapshots of config/options_universe.yaml (MarsWalk fidelity audit)."""
from datetime import date
from src.strategy.universe import snapshot_universe_file


def test_snapshot_written_once_per_content_change(tmp_path):
    src = tmp_path / "options_universe.yaml"; hist = tmp_path / "hist"
    src.write_text("stocks:\n  - symbol: AAPL\n")
    p1 = snapshot_universe_file(src, hist, today=date(2026, 11, 24))
    assert p1 is not None and p1.name == "2026-11-24.yaml" and p1.read_text() == src.read_text()
    assert snapshot_universe_file(src, hist, today=date(2026, 11, 25)) is None   # unchanged → nothing
    src.write_text("stocks:\n  - symbol: AAPL\n  - symbol: CIEN\n")
    p2 = snapshot_universe_file(src, hist, today=date(2026, 11, 25))
    assert p2 is not None and p2.name == "2026-11-25.yaml" and "CIEN" in p2.read_text()
    assert sorted(x.name for x in hist.glob("*.yaml")) == ["2026-11-24.yaml", "2026-11-25.yaml"]


def test_snapshot_is_fail_safe_on_missing_file(tmp_path):
    assert snapshot_universe_file(tmp_path / "nope.yaml", tmp_path / "hist") is None


def test_universe_manager_calls_snapshot():
    import inspect
    from src.strategy import universe
    assert "snapshot_universe_file()" in inspect.getsource(universe.UniverseManager.__init__)
