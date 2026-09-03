"""Home country of a holding.

There is no country column anywhere in the portfolio schema and IBKR's contract details do not
carry one either, so it has to be derived. Three rules, most specific first:

  1. `COMPANY_COUNTRY` — an explicit override, for companies whose listing venue is NOT their home.
     A US listing is the compounder's DEFAULT route for foreign names (see substitute_us_twins),
     so this list is load-bearing, not a curiosity: without it Cameco reads as American.
  2. `EXCHANGE_COUNTRY` — the venue. Precise where a currency is shared (AEB and IBIS are both EUR).
  3. `CURRENCY_COUNTRY` — the fallback, for a venue code nobody has met yet.

MAINTENANCE: the override list is keyed by SYMBOL and the monthly screen rotates the universe, so
a new foreign-domiciled US listing arrives unlabelled and quietly reads as United States. That is
the failure mode to watch — it is silent. `unmapped_symbols()` names the holdings that fell all
the way through to the currency fallback, which is the cheap way to catch a new venue code; a
wrong-but-plausible United States is only caught by reading the list.
"""

# Companies whose home country differs from the exchange they are held on.
COMPANY_COUNTRY: dict[str, str] = {
    "NU": "Brazil",           # Nu Holdings — Cayman-incorporated, operates Brazil; NYSE-listed
    "IREN": "Australia",      # IREN Ltd (Iris Energy) — Sydney; NASDAQ-listed
    "CCJ": "Canada",          # Cameco — Saskatoon; held on NYSE, not TSX
    "2318": "China",          # Ping An — Shenzhen; H-share on SEHK
    "XRO": "New Zealand",     # Xero — Wellington; ASX-listed
}

EXCHANGE_COUNTRY: dict[str, str] = {
    "NYSE": "United States", "NASDAQ": "United States", "ARCA": "United States",
    "AMEX": "United States", "BATS": "United States", "IEX": "United States",
    "PINK": "United States", "SMART": "United States",
    "AEB": "Netherlands",
    "LSE": "United Kingdom",
    "TSEJ": "Japan",
    "TSE": "Canada",          # Toronto. Tokyo is TSEJ in this schema — do not merge the two.
    "SEHK": "Hong Kong",
    "ASX": "Australia",
    "IBIS": "Germany", "IBIS2": "Germany", "XETRA": "Germany",
    "SBF": "France", "BVME": "Italy", "BM": "Spain", "VSE": "Austria",
    "ENEXT.BE": "Belgium", "EBS": "Switzerland", "SWX": "Switzerland",
    "HEX": "Finland", "OSE": "Norway", "SFB": "Sweden", "CPH": "Denmark",
    "SGX": "Singapore", "NSE": "India", "JSE": "South Africa",
}

CURRENCY_COUNTRY: dict[str, str] = {
    "USD": "United States", "CAD": "Canada", "JPY": "Japan", "GBP": "United Kingdom",
    "HKD": "Hong Kong", "AUD": "Australia", "CHF": "Switzerland", "SEK": "Sweden",
    "DKK": "Denmark", "NOK": "Norway", "SGD": "Singapore", "INR": "India",
    "ZAR": "South Africa",
    # EUR spans a dozen venues, so it resolves by EXCHANGE above and only lands here when the
    # venue code is unknown. "Eurozone" is deliberately vague — better than guessing a country.
    "EUR": "Eurozone",
}

UNKNOWN = "Unknown"


def home_country(symbol: str | None, exchange: str | None, currency: str | None) -> str:
    """Best available home country. Override, then venue, then currency, then Unknown."""
    sym = (symbol or "").strip().upper()
    if sym in COMPANY_COUNTRY:
        return COMPANY_COUNTRY[sym]
    exch = (exchange or "").strip().upper()
    if exch in EXCHANGE_COUNTRY:
        return EXCHANGE_COUNTRY[exch]
    return CURRENCY_COUNTRY.get((currency or "").strip().upper(), UNKNOWN)


def unmapped_symbols(rows) -> list[str]:
    """Symbols resolved by the CURRENCY fallback or not at all — i.e. an unrecognised venue code.

    Does not catch a foreign company on a known US venue (that resolves confidently, and wrongly,
    to United States); only the override list catches those.
    """
    out = []
    for r in rows or []:
        sym = (getattr(r, "symbol", None) or "").strip().upper()
        if sym in COMPANY_COUNTRY:
            continue
        if (getattr(r, "exchange", None) or "").strip().upper() in EXCHANGE_COUNTRY:
            continue
        out.append(sym)
    return out
