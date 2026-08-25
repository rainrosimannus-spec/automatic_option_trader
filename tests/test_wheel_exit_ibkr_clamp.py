"""Exit path must clamp DB stock lots to real IBKR holdings (2026-08-25 CTAS incident).

CTAS held 200 shares at IBKR, fully covered by 2 covered calls, but its DB stock row
still read 400 — an Aug-22 call-away delivered 200 shares out yet never decremented the
row. `check_pre_market_exit` sized off the stale 400: lots_needed 4 − covered 2 = 2
phantom "uncovered" lots, so it fired a sell that (after the held-share order clamp) sold
the 200 COVERED shares, turning the 2 covered calls into naked calls.

The CC writer already reconciled the same read via `_ibkr_share_verdict`; the exit path
did not. These tests pin the shared decision: with the clamp, a fully-covered name whose
DB count is stale produces lots_to_cover <= 0 and the exit does NOT fire.
"""
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.models import Base, Position, PositionStatus
from src.strategy.wheel import _ibkr_share_verdict, _covered_call_contracts


def _session():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng)()


def _cc_position(db, qty, opened_at):
    db.add(Position(symbol="CTAS", status=PositionStatus.OPEN, position_type="covered_call",
                    strike=215.0, expiry="20260925", quantity=qty, entry_premium=4.13,
                    opened_at=opened_at))
    db.flush()


def _lots_to_cover(db, symbol, shares):
    """Mirror the exit path's uncovered-lot arithmetic for a given share count."""
    return shares // 100 - _covered_call_contracts(db, symbol)


def test_stale_db_count_without_clamp_would_fire():
    # The bug: sized off the stale DB 400 with 2 CCs => 2 phantom uncovered lots => exit fires.
    db = _session()
    _cc_position(db, qty=2, opened_at=datetime.utcnow() - timedelta(days=8))
    assert _lots_to_cover(db, "CTAS", 400) == 2  # > 0 => would have fired the erroneous sell


def test_clamp_to_ibkr_shares_makes_exit_skip():
    # The fix: reconcile DB 400 -> real IBKR 200 first; 200 fully covered => 0 uncovered => skip.
    db = _session()
    _cc_position(db, qty=2, opened_at=datetime.utcnow() - timedelta(days=8))
    verdict, real = _ibkr_share_verdict({"CTAS": 200}, "CTAS", 400)
    assert verdict == "clamp" and real == 200
    assert _lots_to_cover(db, "CTAS", real) <= 0  # exit does NOT fire


def test_gone_shares_skip():
    # IBKR shows the name entirely sold (< 1 lot) => skip, never a phantom re-sell.
    db = _session()
    _cc_position(db, qty=2, opened_at=datetime.utcnow() - timedelta(days=8))
    verdict, real = _ibkr_share_verdict({"CTAS": 0}, "CTAS", 400)
    assert verdict == "skip" and real is None


def test_empty_ibkr_read_proceeds_on_db():
    # A transient/failed portfolio read ({} or None) must NOT skip real work — proceed on DB,
    # the sell_stock order clamp still backstops any actual sale.
    verdict, real = _ibkr_share_verdict(None, "CTAS", 400)
    assert verdict == "ok" and real == 400


def test_genuinely_uncovered_still_fires():
    # Guard against over-correction: real IBKR 200 shares with NO covered call is a true
    # uncovered lot the exit should still act on.
    db = _session()
    verdict, real = _ibkr_share_verdict({"CTAS": 200}, "CTAS", 200)
    assert verdict == "ok" and real == 200
    assert _lots_to_cover(db, "CTAS", real) == 2  # > 0 => exit correctly still fires
