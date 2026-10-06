"""End-to-end dry run of the monthly screen (UniverseScreener.screen_all) with a fake broker.

The screen cannot be rehearsed against the gateway, and the v2 breakthrough rules (size ceiling,
graduation to the growth tier) live in the middle of it. This runs the REAL screen_all — every
phase, every loop — with only the edges replaced: stock scoring (IBKR + FMP), the AI calls, FMP
lookups, the audit database and the yaml files (redirected to a temp copy). It exists to catch
the class of failure unit tests cannot: a NameError, a wrong variable, a broken hand-off between
phases.
"""
import shutil
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
su = pytest.importorskip("screen_universe")
import src.core.database as database_mod
import src.portfolio.models  # noqa: F401  (registers the audit tables on Base)
from src.core.models import Base

# name -> (score, passes growth gate, FMP market cap in USD)
SPECIAL = {
    # fresh breakthrough proposals
    "NEWBT":  (35.0, False, 4e9),      # classic breakthrough: stays in the tier
    "GRADNEW": (88.0, True, 12e9),     # growth-tier financials: must move to growth under v2
    "BIGNEW": (40.0, False, 80e9),     # above the $50B entry ceiling: refused under v2
    "TINY":   (30.0, False, 0.2e9),    # below the $500M floor: refused under both
    # existing members (present in the real anchor)
    "VRT":    (86.0, True, 97e9),      # member with growth-tier financials: graduates under v2
    "GEV":    (33.0, False, 263e9),    # member above the $100B retention ceiling: leaves under v2
    "ALAB":   (45.0, False, 60e9),     # member between $50B and $100B: stays
}
FRESH = ["NEWBT", "GRADNEW", "BIGNEW", "TINY"]


def _fake_score(symbol, exchange, currency):
    h = zlib.crc32(symbol.encode())
    score, gate, _cap = SPECIAL.get(symbol, (30 + h % 60, h % 5 != 0, None))
    s = su.StockScore(symbol=symbol, exchange=exchange, currency=currency)
    s.name, s.sector, s.price = symbol, "Technology", 100.0
    s.market_cap = _cap if _cap is not None else 20e9
    s.portfolio_score = s.forward_growth_score = float(score)
    s.growth_score = s.quality_score = s.valuation_score = 50.0
    s.growth_gate_ok = bool(gate)
    s.growth_gate_reason = "" if gate else "durable growth 2.0% < 8% floor"
    s.durable_growth_pct = 20.0 if gate else 2.0
    s.quality_pillar = 70.0 if gate else 30.0
    s.dividend_yield = 0.5
    s.dividend_total_return_score = float(40 + h % 40)
    s.options_available, s.options_score = True, float(score)
    return s


MEMBERS = ("VRT", "GEV", "ALAB")


def _seed_members(pool_path):
    """Make VRT, GEV and ALAB current breakthrough members in the TEMP copy of the pool.

    The scenarios below need three existing members with known roles. They used to be read from
    the live pool file, so the tests broke the day a real screen moved GEV out on the size
    ceiling and VRT up to the growth tier (2026-10-06). The roles are now set here."""
    pool = yaml.safe_load(pool_path.read_text())
    bt = pool.setdefault("breakthrough", []) or []
    stamp = max((str(e["last_run_at"]) for e in bt if e.get("last_run_at")), default="2026-01-01T00:00:00")
    have = {e.get("symbol"): e for e in bt}
    for sym in MEMBERS:
        e = have.get(sym)
        if e is None:
            e = {"symbol": sym, "exchange": "SMART", "currency": "USD", "name": sym,
                 "megatrend": "2 Compute infrastructure", "thesis_latest": "seeded for the dry run",
                 "first_seen": "2026-07", "last_seen": "2026-10", "appearance_count": 3}
            bt.append(e)
        e["last_run_at"] = stamp
    pool["breakthrough"] = bt
    for section in ("growth", "dividend"):
        pool[section] = [e for e in (pool.get(section) or []) if e.get("symbol") not in ("VRT", "GEV")]
    pool_path.write_text(yaml.safe_dump(pool, sort_keys=False, default_flow_style=False))


@pytest.fixture
def dry_run(monkeypatch, tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for f in ("discovered_pool.yaml", "evicted_names.yaml"):
        shutil.copy(ROOT / "tools" / f, tools / f)
    monkeypatch.setattr(su, "__file__", str(tools / "screen_universe.py"))
    _seed_members(tools / "discovered_pool.yaml")

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    monkeypatch.setattr(database_mod, "get_session_factory", lambda: sessionmaker(bind=eng))

    def fake_fmp(endpoint, symbol, params={}):
        if endpoint == "quote":
            return [{"priceAvg200": su.BREAKTHROUGH_CEILING_REF_LEVEL}]
        if endpoint == "profile":
            return [{"marketCap": SPECIAL.get(symbol, (0, 0, 20e9))[2], "isEtf": False}]
        return []
    monkeypatch.setattr(su, "_fmp_get", fake_fmp)
    monkeypatch.setattr(su, "_ceiling_memo", {})
    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    monkeypatch.setattr(su.UniverseScreener, "_probe_broker", lambda self: None)   # no broker here
    monkeypatch.setattr(su.UniverseScreener, "_score_stock",
                        lambda self, symbol, exchange, currency: _fake_score(symbol, exchange, currency))
    monkeypatch.setattr(su, "enforce_stock_venue_policy", lambda stocks, ib, **kw: stocks)

    # AI calls
    monkeypatch.setattr(su, "_get_breakthrough_candidates", lambda: [
        {"symbol": s, "exchange": "SMART", "currency": "USD", "name": s,
         "megatrend": "22. Structural shortages & bottlenecks", "thesis": f"thesis {s}"} for s in FRESH])
    seen = {}
    real_builder = su._build_breakthrough_selection_prompt

    def spy_builder(fresh, anchor):
        seen["fresh"], seen["anchor"] = list(fresh), list(anchor)
        return real_builder(fresh, anchor)
    monkeypatch.setattr(su, "_build_breakthrough_selection_prompt", spy_builder)
    monkeypatch.setattr(su, "_call_claude_for_selection", lambda prompt: {
        "selected": [{"symbol": e["symbol"], "reasoning": "r"}
                     for e in (seen["fresh"] + seen["anchor"])][:25],
        "group_reasoning": "g"})
    monkeypatch.setattr(su, "_get_growth_swaps", lambda *a, **k: [
        {"symbol": "SWAPOK", "exchange": "SMART", "currency": "USD", "region": "US", "thesis": "t"}])
    monkeypatch.setattr(su, "_get_dividend_swaps", lambda *a, **k: [])

    def run(version):
        monkeypatch.setattr(su, "BREAKTHROUGH_PROMPT_VERSION", version)
        screener = su.UniverseScreener(ib=SimpleNamespace())
        universe, options, all_scores = screener.screen_all()
        tiers = {t: [s.symbol for s in universe if s.tier == t]
                 for t in ("breakthrough", "growth", "dividend")}
        pool = yaml.safe_load((tools / "discovered_pool.yaml").read_text()) or {}
        return SimpleNamespace(screener=screener, universe=universe, tiers=tiers, seen=seen,
                               options=options, all_scores=all_scores,
                               pool=pool, rejects={r["symbol"]: r for r in screener._rejects})
    return run


def _anchor_symbols():
    return {e["symbol"] for e in su._load_breakthrough_anchor()}


def test_the_members_these_scenarios_rely_on_are_in_the_anchor(dry_run):
    assert set(MEMBERS) <= _anchor_symbols()


@pytest.mark.parametrize("version", ["v1", "v2"])
def test_screen_runs_end_to_end_and_tiers_are_well_formed(dry_run, version):
    r = dry_run(version)
    all_syms = [s.symbol for s in r.universe]
    # One ticker = one listing (tests/test_pool_ticker_collisions.py), so the bare ticker is
    # unique across the whole universe.
    assert len(all_syms) == len(set(all_syms)), "a ticker landed in the universe twice"
    by_tier = {t: set(v) for t, v in r.tiers.items()}
    assert not (by_tier["breakthrough"] & by_tier["growth"]), "overlap between breakthrough and growth"
    assert 0 < len(r.tiers["growth"]) <= 60
    assert 0 < len(r.tiers["breakthrough"]) <= 25
    assert len(r.tiers["dividend"]) <= 15
    assert all(s.growth_gate_ok for s in r.universe if s.tier == "growth")
    assert "TINY" not in all_syms                       # $500M floor, both versions
    assert any(e["symbol"] == "SWAPOK" for e in r.pool["growth"]) == (_fake_score("SWAPOK", "SMART", "USD").growth_gate_ok
        and "SWAPOK" in all_syms or any(e["symbol"] == "SWAPOK" for e in r.pool["growth"]))  # swap path ran without error


def test_v1_behaviour_is_unchanged(dry_run):
    r = dry_run("v1")
    assert "GEV" in r.tiers["breakthrough"]              # no ceiling
    assert "VRT" in r.tiers["breakthrough"]              # no graduation
    assert "BIGNEW" in r.tiers["breakthrough"]           # no entry ceiling
    assert "GRADNEW" in r.tiers["breakthrough"]
    assert r.screener._breakthrough_graduated == []
    assert not any(x.get("category") == "market_cap_ceiling" for x in r.screener._rejects)


def test_v2_new_name_above_the_entry_ceiling_is_refused(dry_run):
    r = dry_run("v2")
    assert "BIGNEW" not in [s.symbol for s in r.universe]
    assert r.rejects["BIGNEW"]["category"] == "market_cap_ceiling"


def test_v2_member_above_the_retention_ceiling_leaves_before_selection(dry_run):
    r = dry_run("v2")
    assert "GEV" not in {e["symbol"] for e in r.seen["anchor"]}     # never offered to the selection
    assert "GEV" not in [s.symbol for s in r.universe]
    assert r.rejects["GEV"]["category"] == "market_cap_ceiling"


def test_v2_member_between_the_two_ceilings_stays(dry_run):
    r = dry_run("v2")
    assert "ALAB" in r.tiers["breakthrough"]


def test_v2_fresh_name_with_growth_financials_moves_to_growth(dry_run):
    r = dry_run("v2")
    assert "GRADNEW" in r.tiers["growth"] and "GRADNEW" not in r.tiers["breakthrough"]
    assert "GRADNEW" not in {e["symbol"] for e in r.seen["fresh"]}  # took no selection slot
    assert any(e["symbol"] == "GRADNEW" and e["region"] == "US" for e in r.pool["growth"])


def test_v2_member_with_growth_financials_moves_to_growth(dry_run):
    r = dry_run("v2")
    assert "VRT" in r.tiers["growth"] and "VRT" not in r.tiers["breakthrough"]
    grads = [g["symbol"] for g in r.screener._breakthrough_graduated]
    assert {"GRADNEW", "VRT"} <= set(grads) and len(grads) == len(set(grads))
    assert not set(grads) & set(r.tiers["breakthrough"])
    # it graduated BEFORE the selection call, so it took no slot there
    assert "VRT" not in {e["symbol"] for e in r.seen["anchor"]}
    assert any(e["symbol"] == "VRT" for e in r.pool["growth"])
    # ...and it is not refreshed in the breakthrough history, so it drops out of next month's anchor
    vrt = next(e for e in r.pool["breakthrough"] if e["symbol"] == "VRT")
    newest = max(e["last_run_at"] for e in r.pool["breakthrough"] if e.get("last_run_at"))
    assert vrt["last_run_at"] != newest


def test_v2_ordinary_breakthrough_name_is_untouched(dry_run):
    r = dry_run("v2")
    assert "NEWBT" in r.tiers["breakthrough"]


def test_dry_run_never_touches_the_real_pool_file(dry_run):
    before = (ROOT / "tools" / "discovered_pool.yaml").read_bytes()
    dry_run("v2")
    assert (ROOT / "tools" / "discovered_pool.yaml").read_bytes() == before


def test_v2_each_member_is_scored_once(dry_run, monkeypatch):
    calls = []
    real = su.UniverseScreener._score_stock
    monkeypatch.setattr(su.UniverseScreener, "_score_stock",
                        lambda self, symbol, exchange, currency: (calls.append(symbol), real(self, symbol, exchange, currency))[1])
    dry_run("v2")
    anchor = _anchor_symbols()
    assert max(calls.count(s) for s in anchor) == 1


def test_v2_pre_score_failure_of_a_member_does_not_stop_the_screen(dry_run, monkeypatch):
    real = su.UniverseScreener._score_stock

    def flaky(self, symbol, exchange, currency):
        if symbol == "ALAB":
            raise RuntimeError("gateway hiccup")
        return real(self, symbol, exchange, currency)
    monkeypatch.setattr(su.UniverseScreener, "_score_stock", flaky)
    r = dry_run("v2")
    assert len(r.tiers["breakthrough"]) > 0 and len(r.tiers["growth"]) > 0
    assert "ALAB" not in [s.symbol for s in r.universe]      # could not be scored, so not placed


def test_v2_rules_failing_to_initialise_fall_back_to_v1_behaviour(dry_run, monkeypatch):
    monkeypatch.setattr(su, "breakthrough_ceilings", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    # the prompt builder also asks for the ceiling; the scan itself is faked here, so only the
    # pipeline's own initialisation is under test
    r = dry_run("v2")
    assert "GEV" in r.tiers["breakthrough"] and r.screener._breakthrough_graduated == []


def test_both_companies_of_a_shared_ticker_are_scored_under_their_own_names(dry_run):
    # Suncor ("SU") and Schneider Electric ("SU.PA") are separate candidates end to end.
    r = dry_run("v2")
    scored = {(s.symbol, s.currency) for s in r.all_scores}
    assert ("SU", "CAD") in scored and ("SU.PA", "EUR") in scored
    assert ("MRK", "USD") in scored and ("MRK.DE", "EUR") in scored
    assert ("SHL", "EUR") in scored and ("SHL.AX", "AUD") in scored


def test_aliased_names_are_kept_out_of_the_options_universe(dry_run):
    from src.portfolio.symbols import is_aliased
    r = dry_run("v2")
    assert not [s.symbol for s in r.options if is_aliased(s.symbol)]
