"""Home markets this account cannot trade.

One list, two enforcement points, so a name in one of these currencies can never be bought
by accident or be skipped by accident:

  * `tools/screen_universe.py` — the monthly screen reroutes such a name to a listing of the
    SAME company on a real US exchange, or drops it from the universe. That is the permanent
    fix, but it only runs once a month.
  * `src/portfolio/buyer.py` — the compounder drops it from the buy universe on every scan,
    so a name that is already in the watchlist (or gets there between screens) never reaches
    ranking, targets, the /watchlist star, FX funding or an order.

Before this list existed the two blocked currencies failed in two DIFFERENT accidental ways,
neither of them deliberate:

  ZAR reached IBKR. The buyer funds the currency BEFORE the order is acknowledged, so every
  attempt converted euros into rand and only then collected Error 460 "No trading
  permissions" — EUR 29,097 of it on 2026-09-01, for an FSR buy that could never exist. The
  rand then sat there, because the FX treasury only closes debits and never sweeps a positive
  foreign balance home.

  INR never got that far. IBKR deals no EUR.INR pair in either direction (the rupee is not
  convertible for a non-resident account), so `resolve_fx_rate` returned None and the buy was
  refused at sizing time — correct, but silent and invisible, and only ever a side effect of
  the rupee's convertibility rather than a decision about permissions.

Both left the name sitting in the roster holding a target and a share of the deploy budget it
could never spend. That is the real cost, and it is the same cost in both cases.

Neither of those accidents is a substitute for this list: `resolve_fx_rate` stays fail-closed
(it is what stops a missing rate being read as 1.0 — an ITC order once sized ₹287.28/share as
if it were €287.28), and the Error-460 cooldown stays too. They are the last lines of defence.
This list is the first one.

Verified against the live account with whatIf orders, which IBKR validates without
transmitting: INFY and TCS on NSE answer Error 460, exactly as FSR and SBK do on the JSE,
while AAPL answers with a margin figure. The same probe cleared HKD (SEHK), AUD (ASX) and JPY
(TSEJ) as tradable — do NOT add those here because their orders don't fill, which is a
board-lot sizing problem, not a permission one.

Take a currency OUT of this list only when the permission is actually granted, and confirm it
with the whatIf probe rather than inferring it from a fill.
"""
from __future__ import annotations

# ZAR — Johannesburg (JSE).  INR — India (NSE); the rupee segment is only available on IBKR's
# Indian entity, so this account can never hold it, whatever else changes.
UNTRADABLE_STOCK_CURRENCIES = frozenset({"ZAR", "INR"})


def is_untradable_currency(currency: str | None) -> bool:
    """True when this account has no permission for the venue that settles `currency`.

    An unknown or empty currency is NOT untradable — the caller's other gates handle those,
    and defaulting to "blocked" here would silently empty the buy universe if a row ever
    arrived without a currency."""
    return (currency or "").strip().upper() in UNTRADABLE_STOCK_CURRENCIES


def partition_tradable(rows):
    """Split watchlist rows into (tradable, blocked) by the venue that settles their currency.

    Pure and order-preserving. Rows only need a `.currency`; anything without one counts as
    tradable, so a malformed row is handled by the caller's own gates rather than silently
    disappearing from the buy universe here."""
    tradable, blocked = [], []
    for r in rows or []:
        target = blocked if is_untradable_currency(getattr(r, "currency", None)) else tradable
        target.append(r)
    return tradable, blocked
