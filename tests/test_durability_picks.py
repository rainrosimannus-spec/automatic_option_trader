"""Durability picks — five growth-tier slots for scarce-human-labour businesses (Rain, 2026-10-24).

The growth tier keeps 60 names and 65% weight. Every growth candidate gets a durability index
from the monthly screen; the top five are guaranteed a slot even when the growth-heavy score puts
them below the cut-off. They remain ordinary growth rows for the buyer, with ONE exception: their
rank score is lifted to the growth tier's median so a 5%-grower is not sized at 0.3% of the book.
Pinned here: the durable gate (3% growth floor, same quality floor, $5B size floor), the pure
protection helper, the rank floor in the shared ranker, and buyer/dashboard parity on a book
containing picks.
"""
from types import SimpleNamespace

import pytest

from src.portfolio import compounder as cmp
from src.portfolio import growth_gate
from src.portfolio.config import PortfolioConfig

CC = PortfolioConfig().compounder
TIER_ALLOC = {"breakthrough": CC.tier_breakthrough, "dividend": CC.tier_dividend, "growth": CC.tier_growth}


# ── the durable gate ────────────────────────────────────────────────────────────────────────────

def test_wm_shape_passes_the_durable_gate_but_not_the_growth_gate():
    ok, _ = growth_gate.passes_durable_entry_gate(5.0, 70.0, 90e9)
    assert ok
    ok_g, why = growth_gate.passes_entry_gate(5.0, 70.0)
    assert not ok_g and "8%" in why


def test_quality_floor_is_not_relaxed_for_durability():
    ok, why = growth_gate.passes_durable_entry_gate(5.0, 35.0, 90e9)     # Boyd shape: scarce labour, no pricing
    assert not ok and "quality" in why
    ok, why = growth_gate.passes_durable_entry_gate(5.0, 70.0, 90e9, quality_measured=False)
    assert not ok and "unmeasured" in why


def test_size_and_growth_floors():
    ok, why = growth_gate.passes_durable_entry_gate(5.0, 70.0, 2e9)
    assert not ok and "market cap" in why
    ok, why = growth_gate.passes_durable_entry_gate(2.0, 70.0, 90e9)
    assert not ok and "3%" in why
    ok, why = growth_gate.passes_durable_entry_gate(None, 70.0, 90e9)
    assert not ok and "unknown" in why


# ── the protection helper (pure) ────────────────────────────────────────────────────────────────

def _sc(sym, growth_score, dur=None, gate=True):
    return SimpleNamespace(symbol=sym, portfolio_score=growth_score, durability_score=dur,
                           durable_gate_ok=True, growth_gate_ok=gate, tier="growth",
                           durability_pick=False)


def test_pick_inside_the_top_n_displaces_nobody():
    top = [_sc(f"G{i}", 90 - i, dur=10) for i in range(10)]
    top[3].durability_score = 95
    sel, picks, displaced = cmp_protect(top, top, 10, 1)
    assert [p.symbol for p in picks] == ["G3"] and displaced == [] and len(sel) == 10
    assert top[3].durability_pick and not any(s.durability_pick for s in sel if s.symbol != "G3")


def test_pick_from_outside_displaces_the_lowest_ordinary_member():
    top = [_sc(f"G{i}", 90 - i, dur=10) for i in range(10)]
    outsider = _sc("WM", 40, dur=99, gate=False)
    sel, picks, displaced = cmp_protect(top, top + [outsider], 10, 1)
    assert [p.symbol for p in picks] == ["WM"]
    assert [d.symbol for d in displaced] == ["G9"]           # score 81, the lowest ordinary member
    assert len(sel) == 10 and "WM" in {s.symbol for s in sel} and "G9" not in {s.symbol for s in sel}
    assert outsider.durability_pick and outsider.tier == "growth"


def test_five_picks_mixed_inside_and_outside():
    top = [_sc(f"G{i}", 90 - i, dur=float(i)) for i in range(60)]       # G59 has the highest index inside
    out = [_sc(f"O{i}", 30 - i, dur=100 - i) for i in range(4)]         # four outsiders beat everyone
    sel, picks, displaced = cmp_protect(top, top + out, 60, 5)
    assert {p.symbol for p in picks} == {"O0", "O1", "O2", "O3", "G59"}
    assert len(sel) == 60
    assert {d.symbol for d in displaced} == {"G55", "G56", "G57", "G58"}  # G59 is protected, so skip it
    assert sorted(sel, key=lambda s: -s.portfolio_score) == sel


def test_under_filled_tier_just_gains_the_picks_and_no_index_means_no_change():
    few = [_sc(f"G{i}", 90 - i, dur=10) for i in range(3)]
    sel, picks, displaced = cmp_protect(few, few + [_sc("WM", 40, dur=99)], 60, 5)
    assert len(sel) == 4 and displaced == [] and len(picks) == 4
    # index failed everywhere → no scores → nothing protected, tier untouched
    plain = [_sc(f"G{i}", 90 - i) for i in range(10)]
    sel, picks, displaced = cmp_protect(plain, plain, 10, 5)
    assert picks == [] and displaced == [] and [s.symbol for s in sel] == [s.symbol for s in plain]


def cmp_protect(selected, eligible, n, k):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    su = pytest.importorskip("screen_universe")
    return su.apply_durability_protection(list(selected), list(eligible), n, k)


# ── the rank floor in the shared ranker ─────────────────────────────────────────────────────────

def _name(sym, score, tier="growth", pick=False, mom=0.2):
    return cmp.NameInput(symbol=sym, tier=tier, growth=score, forward_growth=score, quality=score,
                         valuation=score, dividend_total_return=score, risk_penalty=0.0,
                         price=100.0, sma200=110.0, high_52w=150.0, momentum_12_1=mom,
                         durability_pick=pick)


def test_pick_below_the_median_is_lifted_to_it_and_nothing_else_moves():
    names = [_name(f"G{i}", 90 - i) for i in range(20)] + [_name("WM", 30, pick=True)]
    plain = {r.symbol: r.rank_score for r in cmp.rank_universe(names, durability_floor_q=None)}
    ranked = cmp.rank_universe(names, durability_floor_q=0.50)
    by = {r.symbol: r for r in ranked}
    floor = cmp.durability_rank_floor([plain[s] for s in plain], 0.50)
    assert by["WM"].rank_score == pytest.approx(floor, abs=0.01)
    assert by["WM"].raw_rank_score == pytest.approx(plain["WM"], abs=0.01)
    assert by["WM"].durability_pick
    for s, v in plain.items():
        if s != "WM":
            assert by[s].rank_score == pytest.approx(v, abs=1e-9) and by[s].raw_rank_score is None


def test_pick_above_the_median_keeps_its_own_rank():
    names = [_name(f"G{i}", 60 - i) for i in range(20)] + [_name("CTAS", 95, pick=True)]
    plain = {r.symbol: r.rank_score for r in cmp.rank_universe(names, durability_floor_q=None)}
    by = {r.symbol: r for r in cmp.rank_universe(names, durability_floor_q=0.50)}
    assert by["CTAS"].rank_score == pytest.approx(plain["CTAS"]) and by["CTAS"].raw_rank_score is None


def test_floor_uses_growth_rows_only_and_never_touches_other_tiers():
    names = ([_name(f"G{i}", 50 - i) for i in range(10)]
             + [_name(f"B{i}", 95 - i, tier="breakthrough") for i in range(10)]
             + [_name("DIV", 20, tier="dividend", pick=True)]       # a flag on a non-growth row is inert
             + [_name("WM", 10, pick=True)])
    plain = {r.symbol: r.rank_score for r in cmp.rank_universe(names, durability_floor_q=None)}
    by = {r.symbol: r for r in cmp.rank_universe(names, durability_floor_q=0.50)}
    growth_floor = cmp.durability_rank_floor([plain[f"G{i}"] for i in range(10)] + [plain["WM"]], 0.50)
    assert by["WM"].rank_score == pytest.approx(growth_floor, abs=0.01)
    assert by["WM"].rank_score < min(plain[f"B{i}"] for i in range(10))   # a pick never becomes a leader this way
    assert by["DIV"].rank_score == pytest.approx(plain["DIV"])


def test_durability_rank_floor_is_a_real_quantile():
    assert cmp.durability_rank_floor([10, 20, 30, 40, 50], 0.5) == 30
    assert cmp.durability_rank_floor([10, 20, 30, 40], 0.5) == 25
    assert cmp.durability_rank_floor([], 0.5) is None
    assert cmp.durability_rank_floor([10, 20], 1.0) == 20


# ── buyer / dashboard parity with picks on the book ─────────────────────────────────────────────

def _row(symbol, *, score=70.0, tier="growth", pick=False, sector="Technology"):
    return SimpleNamespace(
        symbol=symbol, tier=tier, sector=sector, currency="USD", current_price=100.0,
        growth_score=score, forward_growth_score=score, quality_score=score, valuation_score=score,
        dividend_total_return_score=score, risk_total_penalty=0.0, sma_200=110.0, high_52w=150.0,
        momentum_12_1=0.2, pending_removal=False, category="growth", durability_pick=pick,
        durability_score=(80.0 if pick else None), rationale=None)


def test_dashboard_twin_sizes_a_pick_like_the_buyer_would():
    """The buyer builds NameInput(durability_pick=...) and calls rank_universe with the same
    quantile; here we reproduce that path by hand and compare targets with the dashboard twin."""
    sectors = ["Technology", "Financial", "Industrial", "Energy", "Healthcare"]
    rows = [_row(f"S{i:02d}", score=90 - i, sector=sectors[i % 5]) for i in range(40)]
    rows.append(_row("WM", score=25, pick=True, sector="Industrial"))
    nlv = 10_000_000.0
    page = {s["symbol"]: s for s in cmp.build_signals_from_watchlist(rows, {}, nlv, CC, TIER_ALLOC)}
    assert page["WM"]["durability_pick"] is True and page["WM"]["raw_rank_score"] is not None
    # buyer-side reproduction
    names = [cmp.NameInput(symbol=r.symbol, tier=r.tier, growth=r.growth_score, forward_growth=r.forward_growth_score,
                           quality=r.quality_score, valuation=r.valuation_score,
                           dividend_total_return=r.dividend_total_return_score, risk_penalty=0.0,
                           price=r.current_price, sma200=r.sma_200, high_52w=r.high_52w,
                           momentum_12_1=r.momentum_12_1, durability_pick=r.durability_pick) for r in rows]
    ranked = cmp.rank_universe(names, CC.rank_fund_weight, CC.rank_mom_weight,
                               durability_floor_q=CC.durability_rank_floor_quantile)
    wm = next(r for r in ranked if r.symbol == "WM")
    assert wm.rank_score == pytest.approx(page["WM"]["rank_score"], abs=0.1)
    # and the lifted pick is sized like a median growth name, not like the bottom of the tier
    mid = sorted((s["target"] for s in page.values() if s["tier"] == "growth"))
    assert page["WM"]["target"] >= mid[len(mid) // 4]
    without = {s["symbol"]: s for s in cmp.build_signals_from_watchlist(
        [r for r in rows if r.symbol != "WM"] + [_row("WM", score=25, pick=False, sector="Industrial")],
        {}, nlv, CC, TIER_ALLOC)}
    assert page["WM"]["target"] > without["WM"]["target"] * 1.5
