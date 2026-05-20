"""
Bruno — collateral view aggregator.

The **only** allowed cross-product read from Bruno into Maggy/Winston tables.
Used by the (future) lender portal when a lender's loan has
`is_nlv_collateralized=True`; returns an aggregated view of MesiCap's brokerage
NLV (pool size, allocation pie, top-5 stocks, cash, asset coverage) per the
disclosure rules in `docs/governance.md` §5.3.

Body is not yet implemented because IBKR NLV reads are gated to Rasmus's clone
(see CLAUDE.md "Dev/prod separation" and the Phase 2 roadmap). This module
locks the signature and the staleness logic so Phase 2 can fill in the body
without rethinking the contract.

ARCHITECTURAL INVARIANT: this is the **single function** that the lender portal
process is permitted to use to read from Maggy/Winston tables. Adding a second
cross-product read path elsewhere in portal code is a privacy breach, not a
feature (see CLAUDE.md Don'ts).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.borrower.models import Loan, MovementType, get_session_factory


# Staleness thresholds (governance.md §5.3).
FRESH_HOURS = 24
HIDE_HOURS = 72


@dataclass
class TopStock:
    ticker: str
    value_eur: float
    pct_of_collateral: float


@dataclass
class CollateralView:
    """
    Aggregated, lender-safe view of MesiCap's brokerage NLV for one
    NLV-collateralized loan. Every field is an aggregate; no individual
    positions, no P&L, no holdings ranked 6+, no share counts (governance.md
    §5.3).

    Field names use "collateral" rather than "pool" because the latter trips
    the banned-terminology lint (LEGAL_CONTEXT.md §1-2 risk of fund/AIF
    misclassification). Internal docs may still use "pool"; lender-facing
    surfaces and the code that feeds them should not.
    """
    available: bool                         # False until IBKR NLV is wired
    staleness: str                          # 'fresh' | 'stale_banner' | 'stale_hidden' | 'not_ready'
    as_of: Optional[datetime] = None        # snapshot timestamp (UTC) or None
    currency: str = "EUR"
    collateral_nlv_eur: Optional[float] = None
    pct_stocks: Optional[float] = None      # allocation pie — sums to 100
    pct_cash: Optional[float] = None
    pct_other: Optional[float] = None       # options, bonds, etc. — lumped, never broken out
    top_stocks: List[TopStock] = field(default_factory=list)   # exactly top 5, no more
    cash_value_eur: Optional[float] = None
    cash_pct_of_collateral: Optional[float] = None
    loan_outstanding_eur: Optional[float] = None
    asset_coverage_ratio: Optional[float] = None    # collateral_nlv / loan_outstanding

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "staleness": self.staleness,
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "currency": self.currency,
            "collateral_nlv_eur": self.collateral_nlv_eur,
            "allocation": {"stocks": self.pct_stocks, "cash": self.pct_cash, "other": self.pct_other},
            "top_stocks": [{"ticker": s.ticker, "value_eur": s.value_eur, "pct_of_collateral": s.pct_of_collateral} for s in self.top_stocks],
            "cash": {"value_eur": self.cash_value_eur, "pct_of_collateral": self.cash_pct_of_collateral},
            "asset_coverage": {
                "loan_outstanding_eur": self.loan_outstanding_eur,
                "ratio": self.asset_coverage_ratio,
            },
        }


def is_collateral_viewable(loan: Loan) -> bool:
    """
    A loan unlocks the lender-side collateral view iff `is_nlv_collateralized`
    is set on the loan record. The portal route still consults
    `collateral_view().staleness` to decide whether to render the data, show a
    stale banner, or hide the panel entirely.
    """
    return bool(getattr(loan, "is_nlv_collateralized", False))


def _classify_staleness(as_of: Optional[datetime]) -> str:
    if as_of is None:
        return "not_ready"
    now = datetime.now(timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    age = now - as_of
    if age <= timedelta(hours=FRESH_HOURS):
        return "fresh"
    if age <= timedelta(hours=HIDE_HOURS):
        return "stale_banner"
    return "stale_hidden"


def _loan_outstanding_eur(loan: Loan) -> Optional[float]:
    """
    Sum of disbursements + restructure adjustments − repayments, expressed in
    the loan's currency. FX conversion to EUR is deferred to Phase 2 (needs an
    FX rate source). For EUR loans this is exact; non-EUR loans return None
    until the FX path is wired.
    """
    outstanding = 0.0
    for m in loan.movements:
        if m.movement_type == MovementType.DISBURSEMENT:
            outstanding += m.amount
        elif m.movement_type == MovementType.PRINCIPAL_RESTRUCTURE:
            outstanding += m.amount
        elif m.movement_type == MovementType.PRINCIPAL_REPAYMENT:
            outstanding -= m.amount
    if loan.currency == "EUR":
        return outstanding
    return None  # placeholder until FX rates wired


_BrunoSession = get_session_factory()


def collateral_view(loan_id: int) -> CollateralView:
    """
    Returns the lender-safe aggregated view for one loan. Caller (the lender
    portal route) should:
      1. Look up the loan and confirm the calling user owns it
      2. Call this function
      3. Render per `result.staleness`:
         - 'fresh'         → render numbers normally
         - 'stale_banner'  → render numbers with "snapshot delayed" banner
         - 'stale_hidden'  → render "temporarily unavailable" placeholder
         - 'not_ready'     → render "coming soon" placeholder (Phase 2)
    """
    session = _BrunoSession()
    try:
        loan = session.query(Loan).filter_by(id=loan_id).first()
        if not loan:
            return CollateralView(available=False, staleness="not_ready")
        if not is_collateral_viewable(loan):
            # Not collateralized — portal route should have 404'd before reaching here.
            return CollateralView(available=False, staleness="not_ready")

        outstanding_eur = _loan_outstanding_eur(loan)

        # Phase 2 fills in: read latest IBKR NLV snapshot from the shared
        # account table, position market values from positions table, classify
        # stock/cash/other, take top 5, compute coverage. Until then return
        # a not_ready CollateralView with the loan outstanding populated so
        # the rest of the portal page (term sheet, payments) can still render.
        return CollateralView(
            available=False,
            staleness="not_ready",
            as_of=None,
            loan_outstanding_eur=outstanding_eur,
        )
    finally:
        session.close()
