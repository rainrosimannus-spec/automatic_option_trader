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


def rate_to_base(currency: str | None, rates: dict | None = None) -> float:
    """LOCAL→BASE multiplier for `currency`. 1.0 for the base currency, unknown, or a missing rate
    (fail-safe: never silently scales an amount we can't price)."""
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
