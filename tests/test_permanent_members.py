"""Permanent members — hand-pinned names kept in the growth tier every screen (Rain, 2026-11-22).

BRK B is the first: a "resilience pick" after the November 2026 AI unwind. The member rides the
durability-pick rail (durability_pick=True on the row): review-exempt, rank lifted to the growth
median. Pinned here: the config default, the pure placement helper in the screener (never exceeds
the tier count, never displaces a durability pick), the buyer's row insert (idempotent, un-flags a
pending removal), and the median floor on a zero-score row.
"""
from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import Base
from src.portfolio import compounder as cmp
from src.portfolio.buyer import PortfolioBuyer
import src.portfolio.buyer as buyer_mod
from src.portfolio.config import PortfolioConfig, PermanentMember
from src.portfolio.models import PortfolioWatchlist
from src.portfolio.permanent import (PERMANENT_RATIONALE_PREFIX, is_permanent_rationale,
                                     permanent_rationale, permanent_symbols)
from tools.screen_universe import apply_permanent_members


# ── config + helpers ────────────────────────────────────────────────────────────────────────────

def test_brk_b_is_the_default_permanent_member():
    cfg = PortfolioConfig()
    assert permanent_symbols(cfg.permanent_members) == {"BRK B"}
    m = cfg.permanent_members[0]
    assert (m.exchange, m.currency) == ("SMART", "USD")
    r = permanent_rationale(m)
    assert r.startswith(PERMANENT_RATIONALE_PREFIX) and "resilience" in r
    assert is_permanent_rationale(r) and not is_permanent_rationale("Durability pick — x")


def test_permanent_symbols_canonicalise_and_accept_dicts():
    assert permanent_symbols([{"symbol": "GOOGL"}, PermanentMember(symbol=" BRK B ")]) == {"GOOG", "BRK B"}
    assert permanent_rationale({"symbol": "X"}) == PERMANENT_RATIONALE_PREFIX + "kept in the book by hand"


# ── screener placement (pure) ───────────────────────────────────────────────────────────────────

def _sc(sym, score, pick=False):
    return SimpleNamespace(symbol=sym, portfolio_score=score, tier="growth", durability_pick=pick,
                           permanent_member=False, growth_gate_ok=True)


def test_member_outside_the_cut_displaces_the_lowest_ordinary_name_not_a_durability_pick():
    sel = [_sc(f"G{i}", 90 - i) for i in range(10)]
    sel[9].durability_pick = True                       # the lowest-scored is a protected pick
    brk = _sc("BRK B", 12.0)
    out, displaced = apply_permanent_members(sel, [brk], 10)
    assert len(out) == 10
    assert "BRK B" in {s.symbol for s in out}
    assert [d.symbol for d in displaced] == ["G8"]      # G9 (pick) survives, G8 makes room
    assert brk.durability_pick and brk.permanent_member and brk.tier == "growth"


def test_member_already_inside_displaces_nobody():
    sel = [_sc(f"G{i}", 90 - i) for i in range(10)]
    sel[4].symbol = "BRK B"
    out, displaced = apply_permanent_members(sel, [sel[4]], 10)
    assert displaced == [] and len(out) == 10 and sel[4].durability_pick


def test_under_filled_tier_just_gains_the_member():
    sel = [_sc(f"G{i}", 90 - i) for i in range(3)]
    out, displaced = apply_permanent_members(sel, [_sc("BRK B", 1.0)], 10)
    assert displaced == [] and len(out) == 4


# ── buyer: the row exists after one scan ────────────────────────────────────────────────────────

def _buyer(monkeypatch, members, rows=()):
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    sess = sessionmaker(bind=eng)()
    for r in rows:
        sess.add(PortfolioWatchlist(**r))
    sess.flush()

    @contextmanager
    def fake_get_db():
        yield sess

    monkeypatch.setattr(buyer_mod, "get_db", fake_get_db)
    b = PortfolioBuyer.__new__(PortfolioBuyer)
    b.cfg = SimpleNamespace(permanent_members=members, cash_yield_symbol="XEON")
    return b, sess


def test_buyer_inserts_the_missing_row_once(monkeypatch):
    cfg = PortfolioConfig()
    b, sess = _buyer(monkeypatch, cfg.permanent_members)
    assert b._ensure_permanent_members() == ["BRK B"]
    assert b._ensure_permanent_members() == []          # idempotent
    rows = sess.query(PortfolioWatchlist).all()
    assert len(rows) == 1
    w = rows[0]
    assert (w.symbol, w.tier, w.category, w.exchange, w.currency) == ("BRK B", "growth", "growth", "SMART", "USD")
    assert w.durability_pick and is_permanent_rationale(w.rationale) and not w.pending_removal


def test_buyer_repins_a_row_the_screen_had_flagged(monkeypatch):
    cfg = PortfolioConfig()
    b, sess = _buyer(monkeypatch, cfg.permanent_members, rows=[dict(
        symbol="BRK B", name="Berkshire", exchange="SMART", currency="USD", tier="growth",
        category="growth", durability_pick=False, pending_removal=True,
        pending_removal_reason="No longer in screened universe")])
    assert b._ensure_permanent_members() == []
    w = sess.query(PortfolioWatchlist).one()
    assert w.durability_pick and not w.pending_removal and w.pending_removal_reason is None
    assert is_permanent_rationale(w.rationale)


def test_buyer_with_no_members_touches_nothing(monkeypatch):
    b, sess = _buyer(monkeypatch, [])
    assert b._ensure_permanent_members() == [] and sess.query(PortfolioWatchlist).count() == 0


# ── sizing: a zero-score permanent row is lifted to the growth median, never above ──────────────

def test_zero_score_member_is_sized_at_the_growth_median():
    names = [cmp.NameInput(symbol=f"G{i}", tier="growth", growth=60 + i, forward_growth=50, quality=70,
                           valuation=50, dividend_total_return=0.0, risk_penalty=0.0, price=100.0, sma200=95.0, high_52w=110.0, momentum_12_1=0.1 * i)
             for i in range(9)]
    names.append(cmp.NameInput(symbol="BRK B", tier="growth", growth=0, forward_growth=0, quality=0,
                               valuation=0, dividend_total_return=0.0, risk_penalty=0.0, price=500.0, sma200=480.0, high_52w=520.0, momentum_12_1=None,
                               durability_pick=True))
    ranked = cmp.rank_universe(names, durability_floor_q=0.50)
    brk = next(r for r in ranked if r.symbol == "BRK B")
    # The floor is the median of the tier's PRE-lift ranks (the member's raw rank included).
    raw_ranks = [r.raw_rank_score if r.raw_rank_score is not None else r.rank_score for r in ranked]
    assert brk.raw_rank_score is not None and brk.raw_rank_score < brk.rank_score
    assert abs(brk.rank_score - cmp.durability_rank_floor(raw_ranks, 0.50)) < 0.05
    assert [r.symbol for r in ranked][0] != "BRK B"      # never a leader by this route
