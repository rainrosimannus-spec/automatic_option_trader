"""Permanent members of the compounder universe (Rain, 2026-11-22).

A permanent member is a name Rain has put in the long-term book BY HAND and wants kept there
regardless of what the monthly screen scores it: the first is Berkshire Hathaway (BRK B) as a
"resilience pick" after the November 2026 AI unwind — cash-rich, diversified, the ballast the
rest of the book (57% AI-themed) does not have.

How it is carried, deliberately reusing the durability-pick rail instead of adding a parallel one:
  * the screen puts every permanent member in the GROWTH tier every run (it bypasses the growth
    gate, the durable gate and the market-cap floor; tier count unchanged, the lowest-scored
    ordinary member makes room, durability picks are never the victim);
  * the row carries `durability_pick=True`, which already means: review drop-off rules do not
    apply, and the ranker lifts the rank score to the growth tier's median so the position is
    sized like a mid growth name (~1% of the book), never a leader by this route;
  * the screen never deletes or flags the row, and the buyer re-creates it at the next scan if it
    is missing — so a name added to the list is in the book after one restart, not after the next
    first-Monday screen.
The rationale text starts with PERMANENT_RATIONALE_PREFIX; that prefix is how the dashboard tells
a hand-pinned member from an AI-chosen durability pick.
"""
from __future__ import annotations

from typing import Iterable

PERMANENT_RATIONALE_PREFIX = "Permanent member — "


def permanent_symbols(members: Iterable) -> set[str]:
    """Canonical symbols of the configured permanent members (dicts or models)."""
    from src.portfolio.symbols import canonical_symbol
    out: set[str] = set()
    for m in members or []:
        sym = m.get("symbol") if isinstance(m, dict) else getattr(m, "symbol", None)
        if sym:
            out.add(canonical_symbol(str(sym).strip()))
    return out


def permanent_rationale(member) -> str:
    """The watchlist rationale for a permanent member: prefix + the configured reason."""
    reason = member.get("reason") if isinstance(member, dict) else getattr(member, "reason", "")
    return PERMANENT_RATIONALE_PREFIX + (str(reason or "").strip() or "kept in the book by hand")


def is_permanent_rationale(text: str | None) -> bool:
    return bool(text) and str(text).startswith(PERMANENT_RATIONALE_PREFIX)
