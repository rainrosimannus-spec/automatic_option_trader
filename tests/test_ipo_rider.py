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
