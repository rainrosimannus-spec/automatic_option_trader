"""Share-class symbol normalization.

Some companies list multiple share classes that are, for our purposes, the same
holding (e.g. Alphabet trades as both GOOGL class-A and GOOG class-C). Left
unnormalized they enter the universe and the watchlist twice and double-count the
position. Collapse every known alias to a single canonical ticker so the universe,
the watchlist, and the compounder only ever see one of them.

Keep GOOG for Alphabet — that's the class we actually hold.
"""
from __future__ import annotations

# alias ticker -> canonical ticker we keep
SYMBOL_ALIASES: dict[str, str] = {
    "GOOGL": "GOOG",   # Alphabet: keep the class-C ticker (the held position)
}


def canonical_symbol(symbol: str | None) -> str | None:
    """Map a share-class alias to the canonical ticker; pass everything else through."""
    if not symbol:
        return symbol
    return SYMBOL_ALIASES.get(symbol.upper().strip(), symbol)


# canonical ticker -> the alias tickers that collapse into it (e.g. GOOG -> ["GOOGL"])
_REVERSE_ALIASES: dict[str, list[str]] = {}
for _alias, _canon in SYMBOL_ALIASES.items():
    _REVERSE_ALIASES.setdefault(_canon, []).append(_alias)


def alias_expand(symbols) -> set[str]:
    """Expand a set of tickers to also include every known share-class sibling. Used to
    build a 'do not propose' exclusion list that covers all classes of a held name, so the
    LLM augmentation can't dodge an excluded ticker by naming its other share class."""
    out: set[str] = set()
    for s in symbols:
        if not s:
            continue
        canon = canonical_symbol(s)
        out.add(s)
        out.add(canon)
        out.update(_REVERSE_ALIASES.get(canon, []))
    return out


# ─────────────────────────────────────────────────────────────────────────────────────────────
# Listing aliases — two different companies that share one broker ticker
# ─────────────────────────────────────────────────────────────────────────────────────────────
# The system identifies a company by its ticker everywhere: watchlist rows, holdings, transactions,
# suggestions, orders. The broker does not: "SU" is Suncor in Toronto and Schneider Electric in
# Paris; "MRK" is Merck & Co in New York and Merck KGaA in Frankfurt; "SHL" is Siemens Healthineers
# in Frankfurt and Sonic Healthcare in Sydney. None of the second companies has another ticker on a
# venue the account trades (checked by ISIN at the broker, 2026-10-04), so without this layer one
# company of each pair has to be left out of the universe.
#
# The fix is an INTERNAL NAME that differs from the broker ticker for the second company only:
#
#     internal name   broker contract
#     "SU"            SU / TSE / CAD            (Suncor — unchanged, no alias)
#     "SU.PA"         SU / SBF / EUR / conId    (Schneider Electric — alias)
#
# Everything inside the system uses the internal name. Translation happens only at the broker
# boundary, in two directions, and every site goes through the two functions below:
#
#   OUT  `broker_stock(internal, exchange, currency)` wherever a contract is built for an order,
#        a quote, history or fundamentals. A site that forgot to translate would send "SU.PA" to
#        the broker, which rejects an unknown ticker — nothing trades. Safe failure.
#   IN   `internal_symbol(contract)` wherever a position, an open order or a fill is read back.
#        A site that forgot to translate would book Schneider shares onto Suncor's row — the
#        dangerous direction, which is why matching is by the broker's contract id first.
#
# With LISTING_ALIASES empty both functions are the identity, so the layer is inert until a name
# is registered. Existing holdings and watchlist rows never change name: the company already in
# the system keeps the bare ticker and the newcomer takes the suffixed one.
from dataclasses import dataclass


@dataclass(frozen=True)
class BrokerListing:
    symbol: str        # the ticker the broker knows
    exchange: str      # primary venue
    currency: str
    con_id: int        # broker contract id — the identity that cannot collide
    company: str


# internal name -> broker listing. Suffix follows the usual market convention (.PA Paris,
# .DE Xetra, .AX Sydney). Contract ids verified at the broker by ISIN on 2026-10-04.
LISTING_ALIASES: dict[str, BrokerListing] = {
    "SU.PA": BrokerListing("SU", "SBF", "EUR", 29612196, "Schneider Electric SE"),
    "MRK.DE": BrokerListing("MRK", "IBIS", "EUR", 1015559, "Merck KGaA"),
    "SHL.AX": BrokerListing("SHL", "ASX", "AUD", 12370176, "Sonic Healthcare Ltd"),
}


def is_aliased(symbol: str | None) -> bool:
    return bool(symbol) and symbol in LISTING_ALIASES


def broker_symbol(symbol: str | None) -> str | None:
    """The ticker to hand the broker for an internal name ("SU.PA" -> "SU"; others unchanged)."""
    alias = LISTING_ALIASES.get(symbol) if symbol else None
    return alias.symbol if alias else symbol


def broker_stock(symbol: str, exchange: str = "SMART", currency: str = "USD"):
    """ib_insync Stock contract for an internal name.

    For an ordinary name this is exactly Stock(symbol, exchange, currency). For an aliased one the
    ticker and currency come from the registry and the broker contract id is set, so the contract
    can only resolve to the intended company whatever routing exchange the caller asked for."""
    from ib_insync import Stock
    alias = LISTING_ALIASES.get(symbol)
    if alias is None:
        return Stock(symbol, exchange, currency)
    return Stock(alias.symbol, exchange or alias.exchange, alias.currency, conId=alias.con_id)


def internal_symbol(contract) -> str:
    """Internal name for a broker contract read back from positions, open orders or fills.

    Matches on the broker contract id first; if the contract carries none, on ticker + currency
    (unambiguous for every registered pair: the two companies trade in different currencies).
    Only stocks are translated — an option's `symbol` is its underlying and is left alone."""
    sym = (getattr(contract, "symbol", "") or "").strip()
    if not LISTING_ALIASES or getattr(contract, "secType", "STK") not in ("STK", "", None):
        return sym
    con_id = int(getattr(contract, "conId", 0) or 0)
    currency = (getattr(contract, "currency", "") or "").upper()
    for name, alias in LISTING_ALIASES.items():
        if con_id and con_id == alias.con_id:
            return name
    for name, alias in LISTING_ALIASES.items():
        if sym == alias.symbol and currency == alias.currency:
            return name
    return sym
