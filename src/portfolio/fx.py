"""Account FX helpers — normalise a foreign-currency amount to the account BASE currency.

The compounder sizes everything in the account base currency (EUR for U26413485): targets are a
fraction of NLV (base), the daily budget is base, the cash buffer is base. But a holding's
market value and an order's price come back from IBKR in the instrument's LOCAL currency (GBP for
an LSE name, USD for a US name). Comparing a £-denominated holding against a €-denominated target
without converting over-/under-sizes every foreign position by its FX rate — a strong-currency
name (GBP ≈ 1.16 €) gets over-bought, a weak one (USD ≈ 0.88 €) under-bought; only base-ccy names
are exact. These helpers convert at the boundary so all the sizing math stays in one currency.

Rates come from data/portfolio_account_cache.json["fx_rates"], written hourly by the portfolio
scheduler from IBKR's per-currency ExchangeRate. The quote is LOCAL→BASE (the base currency itself
is reported as 1.0), so a local amount converts to base by multiplying: amount * rate[currency].
"""
import json

_CACHE_PATH = "data/portfolio_account_cache.json"


def load_fx_rates() -> dict:
    """{currency: local→base rate} from the account cache, or {} if unavailable."""
    try:
        with open(_CACHE_PATH) as f:
            return json.load(f).get("fx_rates", {}) or {}
    except Exception:
        return {}


def base_ccy(rates: dict | None = None) -> str:
    """The account base currency = the one IBKR reports with ExchangeRate == 1.0 (EUR here)."""
    rates = rates if rates is not None else load_fx_rates()
    for c, r in (rates or {}).items():
        try:
            if abs(float(r) - 1.0) < 1e-9:
                return c
        except Exception:
            pass
    return "USD"


def has_rate(currency: str | None, rates: dict | None = None) -> bool:
    """True when `currency` can be priced into base (it IS base, or a usable rate is cached).

    Callers that SIZE MONEY (order share counts, deploy-budget accounting) must gate on this rather
    than lean on `rate_to_base`'s 1.0 fallback. IBKR only reports an ExchangeRate for currencies the
    account actually holds, so a first-ever INR/HKD/AUD/CHF buy prices at 1.0 — 2026-07-28: an ITC
    order sized ₹287.28/share as if it were €287.28, committing €56,307 of the day's budget for
    ~€600 of real exposure. Fail closed instead: skip the name until its rate is known.
    """
    if currency in (None, "", "BASE"):
        return True
    rates = rates if rates is not None else load_fx_rates()
    if currency == base_ccy(rates):
        return True
    try:
        return float((rates or {}).get(currency) or 0.0) > 0.0
    except Exception:
        return False


def rate_to_base(currency: str | None, rates: dict | None = None) -> float:
    """LOCAL→BASE multiplier for `currency`. 1.0 for the base currency, unknown, or a missing rate.

    The 1.0 fallback is a PASS-THROUGH, not a correct conversion — it is only safe for amounts that
    are already in base. Anything that turns money into share counts or into a budget number must
    call `has_rate()` first and refuse when it is False (see the ITC case in that docstring).
    """
    if currency in (None, "", "BASE"):
        return 1.0
    rates = rates if rates is not None else load_fx_rates()
    r = (rates or {}).get(currency)
    try:
        return float(r) if r else 1.0
    except Exception:
        return 1.0


def to_base(amount: float, currency: str | None, rates: dict | None = None) -> float:
    """Convert `amount` (in `currency`) to the account base currency. A missing rate or base/unknown
    currency passes through unscaled, matching the dashboard's _to_base convention."""
    if not amount:
        return 0.0
    return amount * rate_to_base(currency, rates)


def sum_base(pairs, rates: dict | None = None) -> float:
    """Sum an iterable of (currency, amount) into the account base currency. Used to total fills/
    transactions whose `amount` is stored in each row's LOCAL currency (so a raw SUM mixes currencies)."""
    rates = rates if rates is not None else load_fx_rates()
    return sum(to_base(amt, ccy, rates) for ccy, amt in pairs)
