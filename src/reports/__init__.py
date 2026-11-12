"""
Account reporting — IBKR Flex archive + tax/statement PDF generation.

Two independent brokerage accounts are reported on:

    options    U25878705 (skxholdco login)   — the wheel trader
    portfolio  U26413485 (thenewroma login)  — the compounder

Neither internal ledger is a defensible source for a statement: the portfolio's
`portfolio_transactions` records only buys, and no table anywhere holds
dividends, interest, fees, withholding tax or FX conversions. IBKR's Flex
statements do, so this package archives raw Flex XML (`flex_archive`) and
renders the PDF off the archive (`account_statement`).
"""
from __future__ import annotations

__all__ = ["SIDES"]

SIDES = ("options", "portfolio")
