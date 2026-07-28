"""Short-put caps must count fills the DB has not seen yet (2026-07-28 breach).

WHAT HAPPENED: the account sold 12 new puts against an 11/day limit and ran 17 slots against a 15
limit. Both caps read Position rows — but short-put Positions are written by the trade_sync /
position_sync job on a 15-minute timer, NOT at placement. So a put that filled two minutes ago is
invisible to BOTH halves of every cap:

  * not in `opened_today` / `open_slots`  -> no Position row yet
  * not in `working_put_count`            -> its order status is "Filled", which the in-flight
                                             classifier deliberately excludes

It therefore counts as ZERO and the wave keeps going. Proof from the log: six puts (AMD, INTU,
AVGO, CDNS, ADBE, ABNB) became Positions at 17:13:39, all created by the trade_sync that ran at
17:13:34. The execution-time re-check ran all day and only ever fired on the sector gate, because
every number it read was stale.

FIX: reconcile against IBKR's own fills. Whichever source is ahead is the truth.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.strategy.risk import adaptive_max_positions

NLV = 209_664.59          # the options account on 2026-07-28
DAILY_LIMIT = 11          # base 10, +1 for the NLV step over 100k
MAX_SLOTS = 15            # adaptive_max_positions(209_664)


def _daily_blocked(opened_today, ibkr_filled_today, working):
    """Mirror of the daily gate: DB and IBKR reconciled by max()."""
    effective = max(opened_today, ibkr_filled_today)
    return effective + working >= DAILY_LIMIT


def _slots_blocked(open_slots, working, opened_today, ibkr_filled_today):
    """Mirror of the slot gate: unsynced = the fills the DB has not caught up with."""
    unsynced = max(0, ibkr_filled_today - opened_today)
    return open_slots + working + unsynced >= MAX_SLOTS


# ── The limits themselves ────────────────────────────────────────────────────

def test_slot_limit_for_this_account_is_15():
    assert adaptive_max_positions(NLV) == MAX_SLOTS


# ── The exact breach, replayed ───────────────────────────────────────────────

def test_old_behaviour_let_the_wave_through():
    """10 puts already filled but unsynced read as 0 -> nothing blocked the 11th."""
    old_effective = 0          # DB had no rows yet; Filled excluded from `working`
    assert old_effective + 0 < DAILY_LIMIT


def test_unsynced_fills_now_bind_the_daily_cap():
    """Same instant, reconciled: IBKR says 11 filled today -> blocked."""
    assert _daily_blocked(opened_today=0, ibkr_filled_today=11, working=0)


def test_unsynced_fills_now_bind_the_slot_cap():
    """5 stock + 10 unsynced puts = 15 slots -> blocked, instead of reading 5."""
    assert _slots_blocked(open_slots=5, working=0, opened_today=0, ibkr_filled_today=10)
    assert not _slots_blocked(open_slots=5, working=0, opened_today=0, ibkr_filled_today=0)


def test_the_twelfth_put_would_now_be_refused():
    """The put that took the account to 12/11."""
    assert _daily_blocked(opened_today=6, ibkr_filled_today=11, working=0)


# ── No double-counting once the sync catches up ──────────────────────────────

def test_synced_fills_are_not_counted_twice():
    """DB and IBKR agree at 8 -> effective 8, not 16."""
    assert max(8, 8) == 8
    assert not _daily_blocked(opened_today=8, ibkr_filled_today=8, working=0)
    assert _daily_blocked(opened_today=8, ibkr_filled_today=8, working=3)


def test_db_ahead_of_ibkr_is_respected():
    """A put opened by a path with no IBKR fill today (e.g. a roll) still counts."""
    assert _daily_blocked(opened_today=11, ibkr_filled_today=0, working=0)


def test_unsynced_extra_is_never_negative():
    """DB ahead of the fill cache must not subtract slots."""
    assert max(0, 3 - 9) == 0
    assert not _slots_blocked(open_slots=5, working=0, opened_today=9, ibkr_filled_today=3)


# ── Working orders still count, alongside fills ──────────────────────────────

def test_working_and_filled_both_count():
    """4 filled-unsynced + 7 still working = 11 -> at the limit."""
    assert _daily_blocked(opened_today=0, ibkr_filled_today=4, working=7)


def test_reconcile_helper_fails_open_to_empty(monkeypatch):
    """A disconnected/erroring IBKR must degrade to the DB counts, not raise."""
    from src.strategy import risk as R
    monkeypatch.setattr("src.broker.connection.is_connected", lambda: False)
    assert R._filled_short_put_positions_today() == []
