"""Concentration caps for the compounder target sizing (findings #4).

Covers the per-name absolute $ ceiling on top of the pct caps, and the per-sector cap that
scales an over-concentrated sector's targets down (new-buy sizing only)."""
from src.portfolio import compounder as cmp
from src.portfolio.compounder import RankedName


def _ranked(n: int):
    return [RankedName(f"S{i}", "growth", 90 - i, 0.5, 90 - i, 100.0, 90.0, 110.0)
            for i in range(n)]


def test_abs_ceiling_binds_above_pct_cap():
    ranked = _ranked(5)
    tb = {"growth": 1.0}
    # leader pct cap = 10% of $20M = $2M; the $750k absolute ceiling must bind instead.
    t = cmp.target_weights(ranked, tb, 20_000_000, 0.06, leader_syms={"S0"},
                           leader_cap_pct=0.10, conviction_power=1.75,
                           abs_ceiling=750_000)
    assert round(t["S0"]) == 750_000
    # every name is ceilinged, so none exceeds the absolute cap
    assert all(v <= 750_000 + 1 for v in t.values())


def test_abs_ceiling_noop_at_small_nlv():
    ranked = _ranked(5)
    tb = {"growth": 1.0}
    base = cmp.target_weights(ranked, tb, 100_000, 0.06, leader_syms={"S0"},
                              leader_cap_pct=0.10, conviction_power=1.75)
    ceil = cmp.target_weights(ranked, tb, 100_000, 0.06, leader_syms={"S0"},
                              leader_cap_pct=0.10, conviction_power=1.75,
                              abs_ceiling=750_000)
    assert base == ceil  # $750k ceiling never binds on a $100k book


def test_sector_cap_scales_overweight_sector():
    tgt = {"A": 400_000.0, "B": 400_000.0, "C": 400_000.0, "D": 200_000.0}
    sec = {"A": "semis", "B": "semis", "C": "semis", "D": "software"}
    capped = cmp.apply_sector_caps(tgt, sec, 0.30 * 2_000_000)  # cap = $600k
    assert round(sum(capped[s] for s in "ABC")) == 600_000
    assert capped["D"] == 200_000.0  # under-cap sector untouched
    # proportional scale-down preserves within-sector ordering
    assert capped["A"] == capped["B"] == capped["C"]


def test_sector_cap_skips_unknown_sector():
    # blank sector can't be attributed → never capped (avoids lumping unrelated names)
    assert cmp.apply_sector_caps({"X": 999.0}, {"X": ""}, 100.0) == {"X": 999.0}


def test_sector_cap_disabled_when_nonpositive():
    tgt = {"A": 5.0, "B": 5.0}
    assert cmp.apply_sector_caps(tgt, {"A": "x", "B": "x"}, 0.0) == tgt


def test_queue_orders_green_first_then_by_underweight_gap():
    # mirrors the run_compounder_scan queue sort key: (green-first, then biggest underweight $ gap).
    # tuples: (attractiveness, gap$). gap = tgt - cur; bigger gap fills first.
    rows = [
        (-0.02, 9000.0),   # yellow, huge gap
        (0.05, 3000.0),    # green, small gap
        (0.01, 8000.0),    # green, big gap
        (-0.10, 1000.0),   # yellow, small gap
    ]
    rows.sort(key=lambda x: (0 if x[0] >= 0 else 1, -x[1]))
    # all greens (biggest gap first) before any yellow — greens never wait behind a yellow
    assert rows == [(0.01, 8000.0), (0.05, 3000.0), (-0.02, 9000.0), (-0.10, 1000.0)]
