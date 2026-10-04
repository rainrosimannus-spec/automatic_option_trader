"""Breakthrough-scan prompt versions. The prompt decides which companies get proposed, so the
default is pinned here: flipping it is a selection change that needs Rain's go after a
side-by-side run (scripts/screener_ai_side_by_side.py --prompts v1 v2)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
su = pytest.importorskip("screen_universe")


def test_default_prompt_version_is_the_approved_one():
    # 2026-10-04: switched from "v1" on Rain's go ("switch, commit, push").
    assert su.BREAKTHROUGH_PROMPT_VERSION == "v2"
    assert su._build_breakthrough_prompt() == su._build_breakthrough_prompt("v2")


def test_both_versions_build_and_carry_the_exclusion_list():
    for v in ("v1", "v2"):
        p = su._build_breakthrough_prompt(v)
        assert "{excluded_symbols}" not in p and "AAPL" in p      # pool names injected
        assert "Return raw JSON array only" in p
        assert "{{" not in p and "}}" not in p                    # output-field braces rendered


def test_unknown_version_fails_loudly():
    with pytest.raises(KeyError):
        su._build_breakthrough_prompt("v9")


def test_v2_adds_the_missing_themes_and_the_tier_boundary():
    p = su._build_breakthrough_prompt("v2")
    for needle in ("TIER BOUNDARY", "18. Robotics & physical AI", "19. Power grid",
                   "20. Medical technology", "21. EM consumer", "22. Structural shortages",
                   "23. Food security", "24. Tokenised finance", "**Wildcard:**"):
        assert needle in p, needle
    assert "TIER BOUNDARY" not in su._build_breakthrough_prompt("v1")


def test_v2_has_a_50b_ceiling_and_a_size_rule_that_agrees_with_it():
    # Rain, 2026-10-04, from the twelve-year size study: above the equivalent of $50B the
    # tenfold rate more than halves. v1 asked for names up to $500B while excluding anything
    # above $200B — a contradiction that had both models proposing GE Vernova.
    p = su.BREAKTHROUGH_PROMPT_TEMPLATE_V2
    assert "market cap above {ceiling} today" in p and "No name above {ceiling}" in p
    assert "$50B-$500B" not in p and "$200B" not in p and "$2T" not in p
    assert "$50B-$500B" in su._build_breakthrough_prompt("v1")


def test_spcx_is_in_the_growth_candidate_pool():
    assert "SPCX" in su._get_growth_universe()["US"]["symbols"]
    assert "SPCX" in su._build_breakthrough_prompt("v2")       # hence excluded from the scan


def test_v2_keeps_indian_and_south_african_companies_but_not_their_venues():
    # Rain, 2026-10-04: leave the STOCKS in when a twin trades on a venue we can use.
    p = su._build_breakthrough_prompt("v2")
    assert "India (NSE, INR)" not in p                       # v1 sent the model to an untradable venue
    assert "Indian and South African COMPANIES are wanted" in p
    assert "US ADR" in p and "GDR" in p and "leave it out" in p


# ── size ceiling: $50B entry / $100B retention, scaled with the overall market ──

from types import SimpleNamespace as _NS


@pytest.fixture
def index(monkeypatch):
    """Control the world-index quote and FMP profiles the ceiling code reads."""
    state = _NS(level=su.BREAKTHROUGH_CEILING_REF_LEVEL, caps={}, fail=False)

    def fake_fmp(endpoint, symbol, params={}):
        if endpoint == "quote" and symbol == su.BREAKTHROUGH_CEILING_INDEX:
            if state.fail:
                return None
            return [{"priceAvg200": state.level}]
        if endpoint == "profile":
            return [{"marketCap": state.caps.get(symbol, 0), "isEtf": False}]
        return []
    monkeypatch.setattr(su, "_fmp_get", fake_fmp)
    monkeypatch.setattr(su, "_ceiling_memo", {})
    return state


def test_ceiling_at_the_reference_level_is_50b_and_100b(index):
    entry, retention, _ = su.breakthrough_ceilings()
    assert (entry, retention) == (50e9, 100e9)


def test_ceiling_grows_with_the_market_and_compresses_with_it(index):
    index.level = su.BREAKTHROUGH_CEILING_REF_LEVEL * 1.30
    assert su.breakthrough_ceilings()[:2] == (65e9, 130e9)
    su._ceiling_memo.clear()
    index.level = su.BREAKTHROUGH_CEILING_REF_LEVEL * 0.60        # a 40% compression
    assert su.breakthrough_ceilings()[:2] == (30e9, 60e9)


def test_ceiling_falls_back_to_the_reference_when_the_index_cannot_be_read(index):
    index.fail = True
    entry, retention, note = su.breakthrough_ceilings()
    assert (entry, retention) == (50e9, 100e9) and "reference ceiling used" in note
    assert su._ceiling_memo == {}                                  # a failed read is not remembered


def test_ceiling_scale_is_clamped_against_a_bad_quote(index):
    index.level = su.BREAKTHROUGH_CEILING_REF_LEVEL * 100
    assert su.breakthrough_ceilings()[0] == 200e9                  # x4 at most


def test_v2_prompt_states_the_current_ceiling(index):
    index.level = su.BREAKTHROUGH_CEILING_REF_LEVEL * 1.30
    p = su._build_breakthrough_prompt("v2")
    assert "market cap above $65B today" in p and "$10B-$65B market cap" in p


@pytest.mark.parametrize("cap_bn,member,ok", [
    (40, False, True),      # new name under the entry ceiling
    (60, False, False),     # new name above it
    (60, True, True),       # existing member between $50B and $100B stays
    (263, True, False),     # existing member above the retention ceiling leaves (GE Vernova)
])
def test_eligibility_applies_entry_and_retention_ceilings(index, cap_bn, member, ok):
    index.caps["X"] = cap_bn * 1e9
    entry, retention, _ = su.breakthrough_ceilings()
    got, reason = su._check_breakthrough_eligibility(
        "X", 0, currency="USD", ceiling_usd=retention if member else entry)
    assert got is ok
    if not ok:
        assert "size ceiling" in reason and su._reject_category(reason) == "market_cap_ceiling"


def test_ceiling_is_not_enforced_in_code_for_non_usd_names(index):
    # The scorer's market cap for a KRW or JPY name is in local currency (or a guess), and FMP's
    # profile for a bare foreign ticker may be another company — so code stays out of it.
    index.caps["042660"] = 9e12
    ok, _ = su._check_breakthrough_eligibility("042660", 9e12, currency="KRW", ceiling_usd=50e9)
    assert ok


def test_v1_rules_have_no_ceiling(index):
    index.caps["GEV"] = 263e9
    assert su._check_breakthrough_eligibility("GEV", 263e9)[0] is True


# ── graduation: growth-tier financials belong to the growth tier ──

def _score(sym, score, gate, div=0.0, tier="growth"):
    return _NS(symbol=sym, portfolio_score=score, growth_gate_ok=gate, dividend_yield=div, tier=tier)


def test_graduates_only_with_the_gate_and_a_score_above_the_cut():
    assert su._graduates_to_growth(_score("VRT", 65.6, True), 54.0)
    assert not su._graduates_to_growth(_score("CIEN", 53.7, True), 54.0)     # passes gate, misses the cut
    assert not su._graduates_to_growth(_score("ALAB", 60.2, False), 54.0)    # high score, fails the gate
    assert not su._graduates_to_growth(_score("PAYER", 70.0, True, div=3.0), 54.0)


def test_growth_cutoff_is_the_last_filled_slot_or_zero_when_slots_are_free():
    pool = [_score(f"S{i}", 90 - i, True) for i in range(5)] + [_score("FAIL", 99, False),
            _score("BT", 95, True, tier="breakthrough"), _score("DIV", 97, True)]
    assert su._growth_cutoff_score(pool, {"DIV"}, 3) == 88       # third best gate-passer
    assert su._growth_cutoff_score(pool, {"DIV"}, 60) == 0.0     # tier under-filled


def test_graduates_join_the_right_growth_pool_region():
    assert su._growth_region_for("SMART", "USD") == "US"
    jp = next(r for r, p in su.CANDIDATE_POOLS.items() if p.get("currency") == "JPY")
    assert su._growth_region_for(su.CANDIDATE_POOLS[jp]["exchange"], "JPY") == jp
    assert su._growth_region_for("XYZ", "PLN") == "GRAD_XYZ_PLN"
