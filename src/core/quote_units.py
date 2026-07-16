"""Minor-unit quote normalisation — IBKR quotes some venues in a currency's MINOR unit.

IBKR reports the MAJOR currency code (GBP, ZAR) while actually quoting LSE in pence (GBX,
1/100 GBP) and the JSE in cents (ZAc, 1/100 ZAR). Nothing in the contract distinguishes the
two, so a raw quote must be divided at ingest — otherwise every downstream metric (SMAs, 52w
high/low, share-count math, notional caps) is 100x off for those venues. Symmetrically, an
order must be priced back in the SAME minor unit IBKR quoted, so the ingest division has to be
reversed at order time.

This was a live defect: Richemont (CFR, JSE) sat in the compounder watchlist at 397,199 —
that is R3,971.99 in cents, not R397,199. Share sizing divided a base-currency brick by the
100x-inflated price and produced 0 shares, so the name could never be bought. GBP was handled
correctly by an open-coded `currency == "GBP"` check at each site; ZAR was simply missed at all
of them. Keeping the set in one place means the next minor-unit venue (TASE quotes ILS in
agorot) is a one-line change rather than another four-site hunt.
"""

# currency code -> minor units per major unit. Only currencies VERIFIED to be quoted in their
# minor unit by IBKR belong here: adding one silently rescales every price for that currency.
_MINOR_UNIT_FACTOR: dict[str, float] = {
    "GBP": 100.0,  # LSE quotes pence (GBX)
    "ZAR": 100.0,  # JSE quotes cents (ZAc)
}


def minor_unit_factor(currency: str | None) -> float:
    """Minor units per major unit for `currency` — 1.0 when it is quoted in major units."""
    return _MINOR_UNIT_FACTOR.get((currency or "").upper(), 1.0)


def is_minor_unit_quoted(currency: str | None) -> bool:
    """True when IBKR quotes `currency` in its minor unit (LSE pence, JSE cents)."""
    return minor_unit_factor(currency) != 1.0


def quote_to_major(price: float | None, currency: str | None) -> float | None:
    """Normalise a raw IBKR quote to MAJOR units (pence->pounds, cents->rand).

    Apply once, at ingest. Pass-through for major-unit currencies and for None/0.
    """
    if not price:
        return price
    return price / minor_unit_factor(currency)


def major_to_quote(price: float | None, currency: str | None) -> float | None:
    """Convert a MAJOR-unit price back to the unit IBKR quotes — use when pricing an order.

    Exactly reverses quote_to_major, so the value sent equals IBKR's original quote scale.
    """
    if not price:
        return price
    return price * minor_unit_factor(currency)
