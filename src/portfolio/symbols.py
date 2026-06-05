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
