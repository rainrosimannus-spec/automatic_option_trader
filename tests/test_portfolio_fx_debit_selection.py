"""Which foreign balances the portfolio FX-treasury pass is allowed to act on.

Two rules, both about NOT touching money it shouldn't:

  1. Only DEBT. A positive foreign balance is left alone. The pass converts base->ccy to repay a
     loan, so running it on a credit would BUY more of a currency already held long. It is not, and
     must never become, a sweep of foreign cash back to base.
  2. Size the debt IN BASE before testing it against the dust threshold. `bal` is in the foreign
     currency while the threshold is a fraction of NLV (base), so the raw comparison mixed units --
     yen debits looked ~186x larger than they are and tripped it; sterling debits looked smaller and
     slipped under.

These mirror the selection block in PortfolioBuyer.manage_fx_treasury.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.portfolio import fx as pfx
from src.strategy.fx_treasury import plan_debit_close

RATES = {"EUR": 1.0, "USD": 0.8797164, "GBP": 1.169485, "JPY": 0.0053716, "CAD": 0.6232203}
NLV = 10_926_456.62          # base (EUR)
THR = 0.005                  # 0.5% of NLV = ~EUR54,632
BUF = 0.005


def _selected(cash: dict) -> list[str]:
    """Reproduce the selection: skip base, skip credits, skip unpriceable, size in base."""
    out = []
    for ccy, bal in cash.items():
        if ccy == "EUR":
            continue
        if bal >= 0:                       # rule 1 — never act on a credit
            continue
        if not pfx.has_rate(ccy, RATES):
            continue
        bal_base = pfx.to_base(bal, ccy, RATES)      # rule 2 — normalise before thresholding
        if plan_debit_close(bal_base, NLV, THR, BUF)["act"]:
            out.append(ccy)
    return out


# ── Rule 1: credits are untouchable ──────────────────────────────────────────

def test_positive_balances_are_never_selected():
    assert _selected({"USD": 500_000.0, "GBP": 12_345.0, "JPY": 9_000_000.0}) == []


def test_zero_balance_is_not_a_debit():
    assert _selected({"USD": 0.0}) == []


def test_a_credit_is_ignored_even_when_huge():
    """A big positive USD balance must not be converted back to EUR."""
    assert _selected({"USD": 5_000_000.0}) == []


def test_credits_and_debits_together_selects_only_the_debit():
    picked = _selected({"USD": 900_000.0, "GBP": -80_000.0, "CAD": 250_000.0})
    assert picked == ["GBP"]


# ── Rule 2: threshold is measured in base ────────────────────────────────────

def test_small_yen_debit_is_dust_and_skipped():
    """-Y1,000,000 is about -EUR5,371 — far under the ~EUR54.6k threshold. The raw comparison
    (-1,000,000 vs -54,632) wrongly treated it as actionable."""
    assert pfx.to_base(-1_000_000.0, "JPY", RATES) > -NLV * THR
    assert _selected({"JPY": -1_000_000.0}) == []


def test_large_sterling_debit_is_actionable():
    """-GBP50,000 is about -EUR58,474 — over the threshold. The raw comparison
    (-50,000 vs -54,632) wrongly treated it as dust."""
    assert pfx.to_base(-50_000.0, "GBP", RATES) < -NLV * THR
    assert _selected({"GBP": -50_000.0}) == ["GBP"]


def test_genuinely_large_yen_debit_still_acts():
    assert _selected({"JPY": -20_000_000.0}) == ["JPY"]      # ~-EUR107k


def test_unpriceable_currency_is_skipped_not_guessed():
    """No cached rate -> cannot size the debit in base -> leave it for a human."""
    assert _selected({"INR": -5_000_000.0}) == []


# ── Ordering: largest debit first, ranked in base ────────────────────────────

def test_debits_rank_by_base_value_not_raw_balance():
    cash = {"JPY": -20_000_000.0, "GBP": -200_000.0}          # ~-EUR107k vs ~-EUR234k
    ranked = sorted(
        [(c, pfx.to_base(b, c, RATES)) for c, b in cash.items()],
        key=lambda x: x[1],
    )
    assert [c for c, _ in ranked] == ["GBP", "JPY"]           # sterling is the bigger loan
    assert abs(cash["JPY"]) > abs(cash["GBP"])                # ...despite the larger raw number


# ── Rate resolution: IBKR cache -> IBKR FX pair -> refuse ────────────────────

def test_resolve_fx_rate_prefers_the_account_cache(monkeypatch):
    """Cached IBKR ExchangeRate wins; no pair lookup, no market data."""
    from src.portfolio import buyer as B
    monkeypatch.setattr("src.portfolio.fx.load_fx_rates", lambda: RATES)
    called = []
    monkeypatch.setattr(B, "_fx_rate_ccy_per_base",
                        lambda *a, **k: called.append(1) or 0.0)
    assert B.resolve_fx_rate(object(), "JPY", "EUR") == RATES["JPY"]
    assert called == []


def test_resolve_fx_rate_returns_none_when_no_pair_exists(monkeypatch):
    """INR: neither EURINR nor INREUR qualifies -> not dealable -> refuse to size.
    This is why an EXTERNAL rate source would not unblock India."""
    from src.portfolio import buyer as B
    monkeypatch.setattr("src.portfolio.fx.load_fx_rates", lambda: RATES)
    B._LIVE_FX_CACHE.pop("INR", None)

    class _IB:
        def qualifyContracts(self, c):      # IBKR resolves nothing for INR
            return []
    monkeypatch.setattr(B, "get_portfolio_lock", lambda: __import__("contextlib").nullcontext())
    assert B.resolve_fx_rate(_IB(), "INR", "EUR") is None


def test_resolve_fx_rate_falls_back_to_the_live_pair(monkeypatch):
    """HKD has no cached rate (never held) but IBKR quotes EUR.HKD — must NOT deadlock."""
    from src.portfolio import buyer as B
    monkeypatch.setattr("src.portfolio.fx.load_fx_rates", lambda: RATES)
    B._LIVE_FX_CACHE.pop("HKD", None)

    class _Pair:
        conId, symbol = 1234, "EUR"
    class _IB:
        def qualifyContracts(self, c):
            c.conId, c.symbol = _Pair.conId, _Pair.symbol
            return [c]
    monkeypatch.setattr(B, "get_portfolio_lock", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(B, "_fx_rate_ccy_per_base", lambda *a, **k: 9.1)   # HKD per EUR
    rate = B.resolve_fx_rate(_IB(), "HKD", "EUR")
    assert rate is not None and abs(rate - 1 / 9.1) < 1e-9                 # LOCAL->BASE
    assert B._LIVE_FX_CACHE["HKD"] == rate                                 # cached for reuse


def test_resolve_fx_rate_rejects_an_out_of_band_snapshot(monkeypatch):
    """A garbage/inverted quote must not become a share count."""
    from src.portfolio import buyer as B
    monkeypatch.setattr("src.portfolio.fx.load_fx_rates", lambda: RATES)
    B._LIVE_FX_CACHE.pop("ZAR", None)

    class _IB:
        def qualifyContracts(self, c):
            c.conId, c.symbol = 99, "EUR"
            return [c]
    monkeypatch.setattr(B, "get_portfolio_lock", lambda: __import__("contextlib").nullcontext())
    monkeypatch.setattr(B, "_fx_rate_ccy_per_base", lambda *a, **k: 1e-12)
    assert B.resolve_fx_rate(_IB(), "ZAR", "EUR") is None
