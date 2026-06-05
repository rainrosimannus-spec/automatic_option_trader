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
