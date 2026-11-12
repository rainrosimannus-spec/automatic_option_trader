"""IPO Rider v2 pure-logic tests.

Phase 1 = opening-day scalp gated on the day-1 CLOSE-vs-OPEN tell + a vol-scaled trailing stop.
Phase 2 = post-lockup hand-off, whose reliability hinges on parsing the REAL lock-up period from the
SEC prospectus (lock-ups are 90/120/180d, not uniform)."""
from src.ipo.trader import flip_decision, FLIP_TRAIL_MIN_PCT, FLIP_TRAIL_MAX_PCT
from src.ipo.lockup import extract_lockup_days


# ── Phase 1: flip_decision (real debut OHLC) ─────────────────────────────────────────────
def test_flip_enters_on_strong_close_spcx():
    # SPCX 2026-06-12: open 150, high 176.52, low 149.34, close 160.95 → closed +7.3% above open
    d = flip_decision(150.0, 176.52, 149.34, 160.95)
    assert d["enter"] is True
    assert abs(d["trail_pct"] - 18.1) < 0.5          # trail tracks the ~18% day-1 range


def test_flip_skips_weak_close_cbrs():
    # CBRS 2026-05-14: open 350, high 386.34, low 300, close 311.07 → closed -11% BELOW open → no flip
    d = flip_decision(350.0, 386.34, 300.0, 311.07)
    assert d["enter"] is False


def test_flip_skips_marginal_close():
    # +2% close is below the 3% threshold → skip
    assert flip_decision(100.0, 103.0, 98.0, 102.0)["enter"] is False


def test_trail_clamped_low_and_high():
    # tiny range → clamped up to the floor
    assert flip_decision(100.0, 102.0, 99.0, 104.0)["trail_pct"] == FLIP_TRAIL_MIN_PCT
    # huge range → clamped to the cap
    assert flip_decision(100.0, 150.0, 90.0, 110.0)["trail_pct"] == FLIP_TRAIL_MAX_PCT


def test_flip_no_open_is_safe():
    assert flip_decision(0.0, 0.0, 0.0, 0.0)["enter"] is False


# ── Phase 2: SEC lock-up period extraction ───────────────────────────────────────────────
def test_lockup_canonical_180():
    txt = "The holders agreed not to sell for a period of 180 days after the date of this prospectus."
    assert extract_lockup_days(txt) == (180, "confirmed")


def test_lockup_canonical_90():
    txt = "...lock-up agreements expire 90 days after the date of this prospectus, subject to extension."
    assert extract_lockup_days(txt) == (90, "confirmed")


def test_lockup_ignores_stray_short_clause():
    # a stray "14 days notice" must NOT outvote the real 180-day lock-up (the bug the naive regex hit)
    txt = ("The underwriters may release shares on 14 days notice. Each holder is subject to a lock-up "
           "for a period of 180 days after the date of this prospectus.")
    assert extract_lockup_days(txt) == (180, "confirmed")


def test_lockup_period_phrasing_confirmed():
    txt = "Shares are subject to lock-up restrictions for a period of 120 days following the offering."
    assert extract_lockup_days(txt) == (120, "confirmed")


def test_lockup_implausible_value_rejected():
    # 30 days is not a standard IPO lock-up length → not accepted
    txt = "a lock-up of 30 days applies to certain affiliates"
    assert extract_lockup_days(txt) == (None, "none")


def test_lockup_absent():
    assert extract_lockup_days("no relevant restrictions are described here") == (None, "none")


# ── 2026-11-12: the rider had never bought a share ───────────────────────────────────────
from datetime import datetime, timedelta

import pytest

from src.ipo.trader import (lockup_step, flip_sell_qty, us_session_minute,
                            LOCKUP_DIP_WINDOW_DAYS, FLIP_DRY_RUN)


def test_alert_manager_has_send():
    # All seven IPO alert call sites call .send(); it did not exist, so no IPO alert was ever delivered.
    from src.core.alerts import AlertManager, AlertConfig
    sent = []
    am = AlertManager.__new__(AlertManager)
    am.cfg = None
    am._send = lambda message, priority="default", tags="": sent.append((message, priority, tags))
    am.send(title="T", body="B", priority="high", tags="ipo")
    assert sent == [("T\nB", "high", "ipo")]


@pytest.mark.parametrize("today,has_pre,expected", [
    ("2026-10-20", False, "wait"),          # more than 2 weeks before expiry
    ("2026-10-28", False, "record_pre"),    # inside the 2 weeks before
    ("2026-10-28", True, "wait"),
    ("2026-11-10", True, "watch_dip"),      # expiry day (CBRS, the one genuine trigger)
    ("2026-11-10", False, "backfill_pre"),  # no reference taken in time → historical close, not today's
    ("2026-12-10", True, "watch_dip"),      # last day of the window
    ("2026-12-11", True, "expired"),
])
def test_lockup_step_cbrs(today, has_pre, expected):
    assert lockup_step(today, "2026-11-10", has_pre) == expected


def test_lockup_step_stale_expiries_never_trigger():
    # OMDA: expiry 2025-12-06, "triggered" 2026-07-24. BOLT: expiry 2021-05-06, "triggered" 2026-08-17.
    assert lockup_step("2026-07-24", "2025-12-06", True) == "expired"
    assert lockup_step("2026-07-24", "2025-12-06", False) == "expired"
    assert lockup_step("2026-08-17", "2021-05-06", True) == "expired"
    assert LOCKUP_DIP_WINDOW_DAYS == 30


def test_flip_sell_never_exceeds_position():
    assert flip_sell_qty(50, 50.0) == 50
    assert flip_sell_qty(50, 20.0) == 20     # partly sold already
    assert flip_sell_qty(50, 0.0) == 0       # stop already filled → nothing to sell
    assert flip_sell_qty(50, -50.0) == 0     # never add to a short
    assert flip_sell_qty(50, None) == 0      # position unknown → fail closed
    assert flip_sell_qty(None, 50.0) == 0


def test_us_session_minute():
    import pytz
    et = pytz.timezone("America/New_York")
    assert us_session_minute(et.localize(datetime(2026, 11, 12, 9, 50))) == 20      # Thursday
    assert us_session_minute(et.localize(datetime(2026, 11, 12, 22, 0))) is None    # evening
    assert us_session_minute(et.localize(datetime(2026, 11, 12, 9, 0))) is None     # pre-open
    assert us_session_minute(et.localize(datetime(2026, 11, 14, 11, 0))) is None    # Saturday


def test_flip_ships_in_dry_run():
    assert FLIP_DRY_RUN is True


def test_handoff_protected_symbols():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.ipo.models import IpoWatchlist, handoff_protected_symbols
    eng = create_engine("sqlite://")
    IpoWatchlist.__table__.create(eng)
    db = sessionmaker(bind=eng)()
    now = datetime(2026, 11, 12)
    def row(t, status, when, order_id=None):
        db.add(IpoWatchlist(company_name=t, expected_ticker=t, status=status,
                            lockup_entry_time=when, lockup_order_id=order_id))
    row("CBRS", "lockup_done", now - timedelta(days=2))        # handed off two days ago → protected
    row("AKTS", "lockup_done", now - timedelta(days=124))      # past the grace period
    row("SPCX", "lockup_waiting", None)                        # not handed off
    row("OLDP", "lockup_done", now - timedelta(days=2), 77)    # old path: rider bought it itself
    db.commit()
    assert handoff_protected_symbols(db, now=now) == {"CBRS"}
