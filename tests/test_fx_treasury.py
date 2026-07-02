"""FX treasury decision logic (options account, EUR-base, wheels in USD).

Covers: (1) the USD debit-close is self-sizing and scale-invariant — identical behavior at 10x NLV,
which is the "what if I add €1M tomorrow" case; (2) it skips trivial debits and never acts on positive
USD (one-directional); (3) parking keeps a working buffer and skips dust; (4) the FX BUY/SELL+qty plan
picks the right side of the canonical pair.
"""
from datetime import datetime, timezone

from src.strategy.fx_treasury import (
    plan_debit_close, plan_park, fx_conversion_plan, etf_market_open,
)


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


# ── etf_market_open (parking gates on Xetra+LSE hours; debit-close does not) ──

def test_market_open_weekday_in_window():
    assert etf_market_open(_utc(2026, 7, 2, 14, 0)) is True   # Thu 14:00 UTC (the daily slot)


def test_market_open_edges():
    assert etf_market_open(_utc(2026, 7, 2, 8, 0)) is True     # 08:00 open
    assert etf_market_open(_utc(2026, 7, 2, 16, 30)) is True   # 16:30 LSE close
    assert etf_market_open(_utc(2026, 7, 2, 7, 59)) is False   # before open
    assert etf_market_open(_utc(2026, 7, 2, 16, 31)) is False  # after LSE close


def test_market_closed_evening_and_weekend():
    assert etf_market_open(_utc(2026, 7, 2, 22, 0)) is False   # Thu 22:00 UTC (restart-outside-hours case)
    assert etf_market_open(_utc(2026, 7, 4, 14, 0)) is False   # Saturday
    assert etf_market_open(_utc(2026, 7, 5, 14, 0)) is False   # Sunday


# ── plan_debit_close ─────────────────────────────────────────────────────────

def test_debit_close_acts_when_debit_exceeds_threshold():
    # NLV 218,880; USD −72,769 (real snapshot). Threshold 0.5% = ~1,094 → acts.
    r = plan_debit_close(usd_cash=-72_769, nlv=218_880, threshold_pct=0.005, buffer_pct=0.005)
    assert r["act"] is True
    # need = debit + 0.5% buffer = 72,769 + 1,094
    assert round(r["need_usd"]) == round(72_769 + 218_880 * 0.005)


def test_debit_close_skips_trivial_debit():
    # A tiny debit under the threshold (self-cures on next call-away) → no action.
    r = plan_debit_close(usd_cash=-500, nlv=218_880, threshold_pct=0.005, buffer_pct=0.005)
    assert r["act"] is False and r["reason"] == "within_threshold"


def test_debit_close_never_acts_on_positive_usd():
    # One-directional: positive USD is the working float; never convert USD→EUR.
    r = plan_debit_close(usd_cash=27_500, nlv=218_880, threshold_pct=0.005, buffer_pct=0.005)
    assert r["act"] is False


def test_debit_close_is_scale_invariant_the_1m_case():
    # Rain's question: add €1M → NLV ~1.22M. With the wheel scaled up, a proportional debit must
    # trigger IDENTICAL relative behavior — no dollar constant pins it to the small account.
    small = plan_debit_close(usd_cash=-72_769, nlv=218_880, threshold_pct=0.005, buffer_pct=0.005)
    # 10x everything:
    big = plan_debit_close(usd_cash=-727_690, nlv=2_188_800, threshold_pct=0.005, buffer_pct=0.005)
    assert small["act"] is big["act"] is True
    # need_usd scales exactly 10x
    assert round(big["need_usd"], 2) == round(small["need_usd"] * 10, 2)
    # And a debit that is BELOW threshold at the big NLV (but would exceed the small one) is skipped —
    # proving the gate tracks NLV, not an absolute figure.
    assert plan_debit_close(-5_000, 2_188_800, 0.005, 0.005)["act"] is False  # 5k < 0.5% of 2.19M
    assert plan_debit_close(-5_000, 218_880, 0.005, 0.005)["act"] is True      # 5k > 0.5% of 219k


# ── plan_park ────────────────────────────────────────────────────────────────

def test_park_sweeps_excess_above_working_buffer():
    # 130k liquid EUR, NLV 218,880, keep 2% (~4,378) working → park ~125,622.
    amt = plan_park(liquid_cash=130_000, nlv=218_880, working_pct=0.02, min_amount=5_000)
    assert round(amt) == round(130_000 - 218_880 * 0.02)


def test_park_skips_when_excess_below_min():
    # Only a hair above the working buffer → below min_amount → don't churn.
    amt = plan_park(liquid_cash=218_880 * 0.02 + 1_000, nlv=218_880, working_pct=0.02, min_amount=5_000)
    assert amt == 0.0


def test_park_skips_when_cash_below_working_buffer():
    amt = plan_park(liquid_cash=2_000, nlv=218_880, working_pct=0.02, min_amount=5_000)
    assert amt == 0.0


# ── fx_conversion_plan ───────────────────────────────────────────────────────

def test_fx_plan_buys_usd_when_pair_symbol_is_ccy():
    # Canonical pair quoted USD.EUR (symbol==ccy) → BUY USD directly, qty in USD.
    p = fx_conversion_plan("EUR", "USD", shortfall_ccy=73_863, rate_ccy_per_base=1.1377,
                           pair_symbol="USD", idealpro_min_base=22_000)
    assert p["place"] and p["action"] == "BUY"
    assert p["qty"] == int(round(73_863 * 1.01))


def test_fx_plan_sells_base_when_pair_symbol_is_base():
    # Canonical pair quoted EUR.USD (symbol==base) → SELL EUR to receive USD, qty in EUR.
    p = fx_conversion_plan("EUR", "USD", shortfall_ccy=73_863, rate_ccy_per_base=1.1377,
                           pair_symbol="EUR", idealpro_min_base=22_000)
    assert p["place"] and p["action"] == "SELL"
    assert p["qty"] == int(round(73_863 / 1.1377 * 1.01))


def test_fx_plan_below_idealpro_min_defers_to_autofx():
    # A small leg (€1,758 base value) is under the IDEALPRO minimum → don't place; let IBKR auto-FX.
    p = fx_conversion_plan("EUR", "USD", shortfall_ccy=2_000, rate_ccy_per_base=1.1377,
                           pair_symbol="EUR", idealpro_min_base=22_000)
    assert p["place"] is False and p["reason"] == "below_min"


def test_fx_plan_no_rate_is_non_blocking():
    p = fx_conversion_plan("EUR", "USD", shortfall_ccy=73_863, rate_ccy_per_base=0.0,
                           pair_symbol="EUR", idealpro_min_base=22_000)
    assert p["place"] is False and p["reason"] == "no_rate"
