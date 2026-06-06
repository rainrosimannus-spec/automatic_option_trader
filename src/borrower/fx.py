"""
Tiny FX helper for EUR-equivalence comparisons (governance.md §3.3 / quorum
threshold, future Headroom Calculator currency handling).

Source of truth is intentionally simple: a fallback rate dict for the four
currencies we actually see in Bruno today (EUR, USD, GBP, AUD). Production
can override per-currency rates via env (e.g. `FX_USD_TO_EUR=0.92`) and a
future Phase-3 follow-up can wire a live FX source.

This is NOT meant for revaluation or P&L accounting — only for threshold
comparisons where "approximately right" is good enough to gate workflow.
"""
from __future__ import annotations

import os
from typing import Optional


# Fallback rates: 1 unit of CURRENCY = N EUR. Reasonably current; overridable
# via env. Bruno only uses these for soft thresholds; never for posting amounts.
_FALLBACK_TO_EUR: dict[str, float] = {
    "EUR": 1.0,
    "USD": 0.92,
    "GBP": 1.17,
    "AUD": 0.61,
}


def to_eur(amount: Optional[float], currency: Optional[str]) -> Optional[float]:
    """Best-effort EUR-equivalent of `amount` in `currency`.
    Returns None on missing amount; falls back to identity on unknown currency."""
    if amount is None:
        return None
    code = (currency or "EUR").upper()
    if code == "EUR":
        return float(amount)
    env_override = os.environ.get(f"FX_{code}_TO_EUR")
    if env_override:
        try:
            return float(amount) * float(env_override)
        except ValueError:
            pass
    rate = _FALLBACK_TO_EUR.get(code)
    if rate is None:
        # Unknown currency — fall back to identity, with intent that the user
        # will notice the threshold is checked at face value
        return float(amount)
    return float(amount) * rate


def from_eur(amount: Optional[float], currency: Optional[str]) -> Optional[float]:
    """Best-effort conversion of an EUR `amount` into `currency` — the inverse
    of `to_eur`. Reuses `to_eur`'s rate (incl. env override) so a round-trip is
    consistent. Returns None on missing amount; identity on unknown currency."""
    if amount is None:
        return None
    code = (currency or "EUR").upper()
    if code == "EUR":
        return float(amount)
    rate = to_eur(1.0, code)        # EUR per 1 unit of `currency`
    if not rate:
        return float(amount)
    return float(amount) / rate
