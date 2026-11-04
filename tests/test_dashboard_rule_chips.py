"""The operator-facing rule list must carry the 2026-11-04 rules (live↔MarsWalk↔dashboard parity)."""
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "src" / "web" / "templates" / "dashboard.html"


def test_chips_present():
    t = HTML.read_text()
    for needle in ("Assigned-lot stop", "Package unwind", "breach gate", "Put strike floor",
                   "≤ 10% of NLV"):
        assert needle in t, needle
