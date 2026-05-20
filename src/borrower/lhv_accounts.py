"""
Registry of MesiCap's LHV bank accounts.

These IBANs are the canonical sources of cash truth (governance.md §1 row 2).
Every Bruno movement that references a bank reference is expected to belong to
one of these accounts. CAMT.053 ingestion (governance.md §4.1) only processes
files whose account IBAN matches an entry below.

Static config rather than a DB table for now: bank accounts don't change often,
and adding/removing one is a deliberate code change. If we ever need
multi-principal management, promote to bruno.db.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class LHVAccount:
    iban: str
    label: str           # Human-readable name (admin-side only — never shown to lenders)
    currency: str        # primary currency of the account
    role: str            # 'operational' | 'trading' | other
    notes: Optional[str] = None


ACCOUNTS: List[LHVAccount] = [
    LHVAccount(
        iban="EE187700771012126780",
        label="MesiCap Technologies OÜ",
        currency="EUR",
        role="operational",
        notes="Primary operating account. Loan disbursements + repayments flow here.",
    ),
    LHVAccount(
        iban="EE807700774012703391",
        label="Trader konto",
        currency="EUR",
        role="trading",
        notes="Trading sub-account. Holds float between strategies.",
    ),
]


def get_by_iban(iban: str) -> Optional[LHVAccount]:
    """Look up a registered account by IBAN. Returns None if unknown."""
    if not iban:
        return None
    iban_norm = iban.replace(" ", "").upper()
    for a in ACCOUNTS:
        if a.iban == iban_norm:
            return a
    return None


def is_known(iban: str) -> bool:
    return get_by_iban(iban) is not None
