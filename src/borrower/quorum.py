"""
Access quorum for loan activation (docs/governance.md §3.3).

Any loan with `principal_max` ≥ `QUORUM_THRESHOLD_EUR` requires approval from
ALL THREE board members before it can transition DRAFT → ACTIVE. The board can
only resolve matters unanimously, so quorum is 3-of-3, not a majority. Below the
threshold, single-principal action suffices (audit-logged as today).

Each board member has exactly one principal account, so approvals are counted by
distinct account id.

The threshold is currency-aware in spirit but currency-naive in v1:
principal_max is compared directly to the threshold regardless of currency.
For loans denominated in USD/AUD/GBP this slightly mis-classifies near the
boundary. FX-aware comparison is a Phase 3 follow-up; document deferred.

The module exposes pure functions only — actual approval row writes happen in
the route handlers so the audit log captures them with the request context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from src.borrower.fx import to_eur
from src.borrower.models import (
    LoanApproval, Loan, LoanStatus, PrincipalUser, get_session_factory,
)


# Threshold (EUR-equivalent face value) above which 2-of-N approval is required.
# Set per governance.md §3.3 proposal; can be raised via env override.
import os
QUORUM_THRESHOLD_EUR = float(os.environ.get("QUORUM_THRESHOLD_EUR", "25000.0"))
QUORUM_REQUIRED_APPROVERS = 3   # unanimous: all 3 board members must approve


@dataclass(frozen=True)
class QuorumState:
    required: bool                  # True iff this loan needs quorum (≥ threshold)
    threshold_eur: float
    needed: int                     # how many distinct approvers required
    have: int                       # how many distinct approvers we have
    approvers: List[dict]           # [{'principal_id', 'email', 'name', 'approved_at'}]
    can_activate: bool              # True iff have >= needed (or not required)

    @property
    def remaining(self) -> int:
        return max(0, self.needed - self.have)


def quorum_required(loan: Loan) -> bool:
    """Does this loan need quorum approval to activate?

    Compares face value converted to EUR-equivalent using the soft rates in
    src/borrower/fx.py — accurate enough to gate workflow, deliberately not
    used for accounting amounts."""
    eur_equiv = to_eur(loan.principal_max or 0.0, loan.currency or "EUR") or 0.0
    return eur_equiv >= QUORUM_THRESHOLD_EUR


def quorum_state(loan: Loan, session=None) -> QuorumState:
    """Compute the current approval state for a loan.

    Pass `session` if you already have a Bruno session open and want to share
    it; otherwise a fresh one is created and closed.
    """
    own_session = session is None
    if own_session:
        session = get_session_factory()()
    try:
        required = quorum_required(loan)
        approvers_rows = (
            session.query(LoanApproval, PrincipalUser)
            .join(PrincipalUser, LoanApproval.approver_id == PrincipalUser.id)
            .filter(LoanApproval.loan_id == loan.id)
            .order_by(LoanApproval.approved_at)
            .all()
        )
        # Dedupe by approver_id — only count distinct principals
        seen = set()
        approvers = []
        for appr, pu in approvers_rows:
            if pu.id in seen:
                continue
            seen.add(pu.id)
            approvers.append({
                "approval_id": appr.id,
                "principal_id": pu.id,
                "email": pu.email,
                "name": pu.name,
                "approved_at": appr.approved_at,
            })

        have = len(approvers)
        needed = QUORUM_REQUIRED_APPROVERS if required else 0
        can_activate = (not required) or have >= needed
        return QuorumState(
            required=required,
            threshold_eur=QUORUM_THRESHOLD_EUR,
            needed=needed,
            have=have,
            approvers=approvers,
            can_activate=can_activate,
        )
    finally:
        if own_session:
            session.close()


def has_approved(loan_id: int, principal_id: int, session=None) -> bool:
    """Has this principal already approved this loan?"""
    own_session = session is None
    if own_session:
        session = get_session_factory()()
    try:
        return session.query(LoanApproval).filter_by(
            loan_id=loan_id, approver_id=principal_id,
        ).first() is not None
    finally:
        if own_session:
            session.close()


def pending_approval_loans(principal_id: int) -> list:
    """Return DRAFT loans that (a) need quorum and (b) the given principal
    has not yet approved. Used by the principal's inbox panel."""
    session = get_session_factory()()
    try:
        drafts = session.query(Loan).filter(Loan.status == LoanStatus.DRAFT).all()
        result = []
        for ln in drafts:
            state = quorum_state(ln, session=session)
            if not state.required:
                continue
            if not any(a["principal_id"] == principal_id for a in state.approvers):
                result.append({
                    "loan": ln,
                    "state": state,
                })
        return result
    finally:
        session.close()
