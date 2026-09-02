"""The dashboard's allocation must match the buy path's.

`build_signals_from_watchlist` is a SECOND implementation of the compounder allocation, used by
/watchlist and the Portfolio tab. It had drifted from `buyer.run_compounder` in four ways, all of
which made the page overstate targets. On 2026-09-02 ASML rendered as

    ASML  growth  ...  €174,496 target / €164,166 held / 6% underweight / buy

while the buyer was correctly holding it at 110% of a €148,463 target. Measured across the live
104-name book, buyer/dashboard target ratios ran 0.848-1.033 with a median of exactly 0.900 —
`base_pct`, the crash reserve the page was allocating and the buyer was not.
"""
from types import SimpleNamespace

from src.portfolio import compounder as cmp
from src.portfolio.config import PortfolioConfig

CC = PortfolioConfig().compounder
TIER_ALLOC = {"breakthrough": CC.tier_breakthrough,
              "dividend": CC.tier_dividend,
              "growth": CC.tier_growth}
NLV = 10_836_602.0
INVESTABLE = NLV * (1 - CC.cash_buffer_pct)


def _row(symbol, *, score=70.0, price=100.0, tier="growth", sector="Technology",
         currency="USD", pending_removal=False):
    return SimpleNamespace(
        symbol=symbol, tier=tier, sector=sector, currency=currency, current_price=price,
        growth_score=score, forward_growth_score=score, quality_score=score,
        valuation_score=score, dividend_total_return_score=score, risk_total_penalty=0.0,
        sma_200=price * 1.1, high_52w=price * 1.5, momentum_12_1=0.2,
        pending_removal=pending_removal, category="growth",
    )


def _book(n=40, **kw):
    """A spread of sectors so the 30%-of-NLV sector cap isn't what's under test."""
    sectors = ["Technology", "Financial", "Industrial", "Energy", "Healthcare"]
    return [_row(f"S{i:02d}", score=90 - i, sector=sectors[i % len(sectors)], **kw)
            for i in range(n)]


def _targets(rows, held=None, unlocked=0.0):
    out = cmp.build_signals_from_watchlist(rows, held or {}, NLV, CC, TIER_ALLOC,
                                           unlocked=unlocked)
    return {s["symbol"]: s["target"] for s in out}, {s["symbol"]: s["action"] for s in out}


def test_the_crash_reserve_is_not_allocated():
    """THE bug: the page allocated the full investable, the buyer allocates base_pct of it."""
    targets, _ = _targets(_book())
    assert sum(targets.values()) <= INVESTABLE * CC.base_pct * 1.001
    # and it is genuinely spending that sleeve, not just landing under it by accident
    assert sum(targets.values()) > INVESTABLE * CC.base_pct * 0.5


def test_unlocking_the_reserve_raises_targets_by_exactly_the_buyers_formula():
    """live_invest = investable x (base_pct + (1 - base_pct) x unlocked) — buyer.py:1178."""
    locked, _ = _targets(_book(), unlocked=0.0)
    freed, _ = _targets(_book(), unlocked=1.0)
    expected = (CC.base_pct + (1 - CC.base_pct) * 1.0) / CC.base_pct     # 1/0.9 = 1.111
    ratios = [freed[s] / locked[s] for s in locked if locked[s] > 0]
    assert ratios, "no targets to compare"
    assert max(ratios) - min(ratios) < 0.02          # uniform scale, no cap distortion
    assert abs(sum(ratios) / len(ratios) - expected) < 0.02


def test_an_unlocked_fraction_out_of_range_cannot_over_allocate():
    """The state key is a PERCENT; a caller forgetting to divide by 100 must not allocate 100x."""
    targets, _ = _targets(_book(), unlocked=100.0)
    assert sum(targets.values()) <= INVESTABLE * 1.001


def test_the_per_name_absolute_ceiling_binds():
    """cc.per_name_abs_ceiling was never passed, so the €750k cap could not bind on this page."""
    targets, _ = _targets(_book(n=3))                # 3 names splitting the book -> huge targets
    assert max(targets.values()) <= CC.per_name_abs_ceiling * 1.001


def test_the_sector_cap_binds():
    """apply_sector_caps was never called here, so one sector could read full size."""
    rows = [_row(f"T{i:02d}", score=90 - i, sector="Technology") for i in range(30)]
    targets, _ = _targets(rows)
    assert sum(targets.values()) <= CC.sector_cap_pct * NLV * 1.001


def test_a_name_on_an_unpermissioned_venue_gets_no_target_but_still_lists():
    """FSR and SBK were holding two of the dividend tier's fifteen slots, shrinking every real
    dividend name's target by ~14%, for budget the buyer will never spend on them."""
    rows = _book(n=10) + [_row("FSR", tier="dividend", currency="ZAR", sector="Financial"),
                          _row("INFY", tier="dividend", currency="INR", sector="Technology")]
    targets, actions = _targets(rows)
    assert targets["FSR"] == 0 and targets["INFY"] == 0
    assert actions["FSR"] == "—" and actions["INFY"] == "—"
    assert "FSR" in targets, "blocked names must still be listed, just with no target"


def test_blocked_names_do_not_shrink_everyone_elses_target():
    clean, _ = _targets([_row("A", tier="dividend", sector="Financial"),
                         _row("B", tier="dividend", sector="Energy")])
    withzar, _ = _targets([_row("A", tier="dividend", sector="Financial"),
                           _row("B", tier="dividend", sector="Energy"),
                           _row("FSR", tier="dividend", currency="ZAR", sector="Industrial")])
    assert withzar["A"] == clean["A"] and withzar["B"] == clean["B"]


def test_a_holding_past_its_target_reads_hold_not_buy():
    """ASML's actual shape: held above target must never render as an underweight buy."""
    rows = _book(n=10)
    targets, _ = _targets(rows)
    over = {"S00": targets["S00"] * 1.10}
    _, actions = _targets(rows, held=over)
    assert actions["S00"] == "hold"
