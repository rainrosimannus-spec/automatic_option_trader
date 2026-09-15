"""Laggard re-fill gate (Rain, 2026-09-15).

Targets are a % of NLV, so a name once filled to 100% drifts back under target whenever its price
lags, and the gap-filler tops it up again and again. For names ranked BELOW laggard_refill_rank_min
that have ALREADY reached target once, the re-fill is skipped unless the price is at least
laggard_refill_drop_pct below the average purchase price. Never-filled names, the top-N, and a live
crash tranche are untouched. The buyer and the dashboard share one helper so they cannot drift.
"""
import types

from src.portfolio import compounder as cmp


# ── update_target_reached ──────────────────────────────────────────────────────
def test_reached_flag_set_on_first_full_and_kept_when_gap_reopens():
    r = cmp.update_target_reached({}, "ABC", held_value=98.5, tgt=100.0, today_iso="2026-09-15")
    assert r == {"ABC": "2026-09-15"}
    # price lags → holding now 80% of target: the flag must survive (that IS the case the gate judges)
    r = cmp.update_target_reached(r, "ABC", held_value=80.0, tgt=100.0, today_iso="2026-10-01")
    assert r == {"ABC": "2026-09-15"}


def test_reached_flag_not_set_below_full_and_cleared_on_close():
    r = cmp.update_target_reached({}, "ABC", held_value=90.0, tgt=100.0, today_iso="2026-09-15")
    assert r == {}
    r = cmp.update_target_reached({"ABC": "2026-09-01"}, "ABC", held_value=0.0, tgt=100.0,
                                  today_iso="2026-09-15")
    assert r == {}                       # sold out → forget; a re-buy starts fresh


def test_reached_is_pure_and_tolerant_parse():
    base = {"X": "2026-01-01"}
    out = cmp.update_target_reached(base, "Y", 100.0, 100.0, "2026-09-15")
    assert base == {"X": "2026-01-01"} and out == {"X": "2026-01-01", "Y": "2026-09-15"}
    assert cmp.parse_target_reached(None) == {}
    assert cmp.parse_target_reached("garbage") == {}
    assert cmp.parse_target_reached('["not", "a", "dict"]') == {}
    assert cmp.parse_target_reached('{"A": "2026-09-15"}') == {"A": "2026-09-15"}


# ── laggard_refill_blocked ─────────────────────────────────────────────────────
def test_low_rank_reached_blocked_until_15pct_below_avg_cost():
    kw = dict(rank=60, rank_min=50, reached_target=True, avg_cost=100.0, drop_pct=0.15)
    assert cmp.laggard_refill_blocked(price=95.0, **kw) is True     # −5%: skip
    assert cmp.laggard_refill_blocked(price=85.01, **kw) is True    # just above the line: skip
    assert cmp.laggard_refill_blocked(price=85.0, **kw) is False    # exactly −15%: buy
    assert cmp.laggard_refill_blocked(price=70.0, **kw) is False    # −30%: buy


def test_gate_only_for_rank_below_cutoff_and_only_after_first_fill():
    kw = dict(rank_min=50, price=95.0, avg_cost=100.0, drop_pct=0.15)
    assert cmp.laggard_refill_blocked(rank=50, reached_target=True, **kw) is False   # rank 50 itself is in
    assert cmp.laggard_refill_blocked(rank=1, reached_target=True, **kw) is False
    assert cmp.laggard_refill_blocked(rank=51, reached_target=True, **kw) is True
    assert cmp.laggard_refill_blocked(rank=51, reached_target=False, **kw) is False  # never filled → normal
    assert cmp.laggard_refill_blocked(rank=None, reached_target=True, **kw) is False # unranked → not our call
    assert cmp.laggard_refill_blocked(rank=51, reached_target=True, rank_min=0, price=95.0,
                                      avg_cost=100.0, drop_pct=0.15) is False       # 0 disables


def test_flagged_name_without_avg_cost_fails_closed():
    assert cmp.laggard_refill_blocked(60, 50, True, price=95.0, avg_cost=None, drop_pct=0.15) is True
    assert cmp.laggard_refill_blocked(60, 50, True, price=95.0, avg_cost=0.0, drop_pct=0.15) is True
    assert cmp.laggard_refill_blocked(60, 50, True, price=0.0, avg_cost=100.0, drop_pct=0.15) is True


# ── dashboard mirror ───────────────────────────────────────────────────────────
def _cc(rank_min=50, drop=0.15):
    return types.SimpleNamespace(
        rank_fund_weight=0.5, rank_mom_weight=0.5, cash_buffer_pct=0.0, base_pct=1.0,
        per_name_cap_pct=1.0, leader_top_frac=0.0, leader_cap_pct=None, conviction_power=1.0,
        per_name_abs_ceiling=None, sector_cap_pct=0.0, freeze_dropped_names=False,
        freeze_buffer_topk=0, laggard_refill_rank_min=rank_min, laggard_refill_drop_pct=drop)


def _rows(n):
    # n growth names with DESCENDING quality so rank == index+1; all USD, priced at 100
    return [types.SimpleNamespace(symbol=f"S{i:03d}", tier="growth", current_price=100.0,
                                  growth_score=100 - i, forward_growth_score=100 - i,
                                  quality_score=100 - i, valuation_score=50, dividend_total_return_score=0,
                                  risk_total_penalty=0, sma_200=110.0, high_52w=120.0, momentum_12_1=None,
                                  currency="USD", sector="", pending_removal=False)
            for i in range(n)]


def _act(signals, sym):
    return next(s["action"] for s in signals if s["symbol"] == sym)


def test_dashboard_marks_low_rank_refill_held_back_and_lifts_it_in_crash():
    rows = _rows(60)
    # everybody is underweight (held = half of target); S059 (rank 60) and S010 (rank 11) were once full
    tier_alloc = {"growth": 1.0, "breakthrough": 0.0, "dividend": 0.0}
    base = cmp.build_signals_from_watchlist(rows, {}, 6_000_000, _cc(), tier_alloc)
    tgt = {s["symbol"]: s["target"] for s in base}
    held = {s: t * 0.5 for s, t in tgt.items()}
    avg = {s: 100.0 for s in tgt}                      # bought at 100, now 100 → not −15%
    reached = {"S059": "2026-09-01", "S010": "2026-09-01"}
    sig = cmp.build_signals_from_watchlist(rows, held, 6_000_000, _cc(), tier_alloc,
                                           avg_cost=avg, reached=reached)
    assert _act(sig, "S059") == "held_back"            # rank 60, filled once, not −15%
    assert _act(sig, "S010") == "direct"               # rank 11 → gate doesn't apply
    assert _act(sig, "S058") == "direct"               # rank 59 but never filled → normal buy
    # −20% vs avg cost → re-fill allowed again
    avg["S059"] = 125.0
    sig = cmp.build_signals_from_watchlist(rows, held, 6_000_000, _cc(), tier_alloc,
                                           avg_cost=avg, reached=reached)
    assert _act(sig, "S059") == "direct"
    # crash tranche live → gate lifted (crash regime exceptions untouched)
    avg["S059"] = 100.0
    sig = cmp.build_signals_from_watchlist(rows, held, 6_000_000, _cc(), tier_alloc,
                                           avg_cost=avg, reached=reached, crash_active=True)
    assert _act(sig, "S059") == "direct"
    # a full name still reads "hold", never "held_back"
    held["S059"] = tgt["S059"]
    sig = cmp.build_signals_from_watchlist(rows, held, 6_000_000, _cc(), tier_alloc,
                                           avg_cost=avg, reached=reached)
    assert _act(sig, "S059") == "hold"
