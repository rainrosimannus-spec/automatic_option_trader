"""Crash leverage on the TARGET (2026-10-22). Ships OFF; these pin the behaviour and the invariants.

Why it exists: a -30% crash on a fully invested book bought ~EUR 96k (1.2% of NLV) and left the whole
EUR 1.17M margin sleeve untouched, because targets are a share of NLV — holdings and targets deflate
together so no gap opens and the funding is never consulted. The fix raises the target in a deep
drawdown. The ladder (7.5/11.25/15% at -30/-40/-50) beat all-at-30% at every recovery level in
scripts/compounder_crash_leverage_ab.py, because all30 spent the sleeve at the first leg and had
nothing left below -40%.
"""
import inspect

from src.portfolio import compounder as cmp
from src.portfolio.config import CompounderConfig

LADDER = [(0.50, 0.150), (0.40, 0.1125), (0.30, 0.075)]


# ── ships off ─────────────────────────────────────────────────────────────────

def test_ships_off_and_is_inert():
    """Live behaviour must be byte-identical until Rain enables it."""
    cc = CompounderConfig()
    assert cc.crash_leverage_enabled is False
    for dd in (0.0, 0.10, 0.25, 0.30, 0.45, 0.60, 0.95):
        assert cmp.crash_leverage(dd, cc.crash_leverage_ladder, cc.crash_leverage_enabled) == 0.0


def test_default_ladder_is_the_one_the_ab_chose():
    assert CompounderConfig().crash_leverage_ladder == LADDER


# ── the ladder ────────────────────────────────────────────────────────────────

def test_no_leverage_above_the_first_step():
    """Tiers 1 and 2 borrow nothing — a 10% or 20% drawdown happens most years and is not a crash."""
    for dd in (0.0, 0.05, 0.10, 0.1999, 0.20, 0.2999):
        assert cmp.crash_leverage(dd, LADDER, True) == 0.0, dd


def test_steps_fire_at_their_depths():
    assert cmp.crash_leverage(0.30, LADDER, True) == 0.075
    assert cmp.crash_leverage(0.399, LADDER, True) == 0.075
    assert cmp.crash_leverage(0.40, LADDER, True) == 0.1125
    assert cmp.crash_leverage(0.499, LADDER, True) == 0.1125
    assert cmp.crash_leverage(0.50, LADDER, True) == 0.150
    assert cmp.crash_leverage(0.80, LADDER, True) == 0.150      # no step beyond the last


def test_deepest_step_wins_regardless_of_order():
    assert cmp.crash_leverage(0.55, list(reversed(LADDER)), True) == 0.150


def test_disarms_on_recovery_because_it_keys_off_LIVE_drawdown():
    """The whole reason it takes dd and not tranches_fired: that counter is monotonic and never
    resets, so keying leverage to it would leave the book permanently levered after its first crash."""
    assert cmp.crash_leverage(0.45, LADDER, True) == 0.1125     # deep
    assert cmp.crash_leverage(0.32, LADDER, True) == 0.075      # recovering
    assert cmp.crash_leverage(0.12, LADDER, True) == 0.0        # recovered past the first step
    src = inspect.getsource(cmp.crash_leverage)
    assert "tranches_fired" not in src.split('"""')[2]          # not used in the body


def test_malformed_ladder_never_raises():
    """This sits in the scan's hot path — a bad config must not stop the book from buying."""
    for bad in ([("x", "y")], [()], [(0.3,)], [None], "nonsense", [(0.3, None)]):
        assert cmp.crash_leverage(0.9, bad, True) == 0.0
    assert cmp.crash_leverage(0.9, [("bad",), (0.4, 0.1125)], True) == 0.1125   # skips, keeps good
    assert cmp.crash_leverage(0.9, None, True) == 0.0
    assert cmp.crash_leverage(0.9, [], True) == 0.0


def test_negative_leverage_is_clamped_out():
    assert cmp.crash_leverage(0.9, [(0.3, -0.5)], True) == 0.0


# ── the measured scenario ─────────────────────────────────────────────────────

def test_it_actually_opens_a_gap_big_enough_to_need_margin():
    """The live measurement: fully deployed EUR 11M book, -30% crash. Off, the gap is ~EUR 96k and
    the EUR 1.17M sleeve is unreachable. On, the gap exceeds the cash and must be borrowed."""
    cc = CompounderConfig()
    nlv = 7_799_000                       # post-crash
    stocks = 7_469_000
    cash = 330_000
    investable = nlv * (1 - cc.cash_buffer_pct)

    off = investable * (cc.base_pct + (1 - cc.base_pct) * 1.0) - stocks
    assert 80_000 < off < 110_000, f"baseline gap moved: {off:,.0f}"

    lev = cmp.crash_leverage(0.30, cc.crash_leverage_ladder, True)
    on = investable * (cc.base_pct + (1 - cc.base_pct) * 1.0) * (1 + lev) - stocks
    assert on > 600_000, f"gap should need borrowing, got {on:,.0f}"
    spare_cash = max(0.0, cash - nlv * cc.cash_buffer_pct)
    assert on > spare_cash, "the point is that cash alone cannot fund it"
    # and it must stay inside the sleeve the loan gate can survive
    assert on < cc.crash_margin_pct * nlv + spare_cash


# ── the invariant that matters most ───────────────────────────────────────────

def test_leverage_is_applied_to_the_target_and_nowhere_near_nlv():
    """nlv is the denominator of BOTH safety gates (loan_pct = borrowed/nlv, margin_pct = maint/nlv)
    plus the sector cap, cash buffer, order bounds and crash_margin itself. Inflating it would make a
    real 15% loan read as 13% and fire the hard stops late. So the factor must touch live_invest only."""
    from src.portfolio.buyer import PortfolioBuyer
    src = inspect.getsource(PortfolioBuyer.run_compounder_scan)
    assert "cmp.crash_leverage(" in src
    # the one place it may appear is the live_invest line
    users = [ln.strip() for ln in src.splitlines() if "_crash_lev" in ln and not ln.strip().startswith("#")]
    assert any("live_invest" in ln for ln in users)
    for ln in users:
        assert "nlv =" not in ln and "investable =" not in ln, ln
    # nlv is still assigned exactly once, unmultiplied
    assigns = [ln.strip() for ln in src.splitlines() if ln.strip().startswith("investable = ")]
    assert assigns == ["investable = nlv * (1 - cc.cash_buffer_pct)"], assigns


def test_arming_is_logged_loudly():
    """Rain asked for labels: running levered must never be ambiguous."""
    from src.portfolio.buyer import PortfolioBuyer
    src = inspect.getsource(PortfolioBuyer.run_compounder_scan)
    assert "compounder_crash_leverage_armed" in src
    assert "compounder_crash_leverage_pct" in src          # state key for the dashboard
