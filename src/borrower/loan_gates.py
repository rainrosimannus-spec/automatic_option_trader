"""
Bruno loan-policy gate — trailing-trading-return aware.

Reads `AccountSnapshot` rows from Maggy's `data/trades.db` to compute the
trailing annualized NLV return on two windows (90d and 365d). Uses the LOWER
of the two ("either trips") as the binding metric and emits two verdicts:

    new_loan_status:
      GO    — binding ≥ target (24% target = the same hard-coded value
              dashboard.py:84 uses; LOAN_GATE_TARGET_PCT below)
      BLOCK — binding < target, with sufficient history (≥90 daily snapshots)
      WARN  — insufficient history (fail-open: form NOT blocked)

    repay_recommendations:
      list of RepayRec(loan_id, contract_ref, rate_pct, outstanding, reason)
      for every ACTIVE Bruno loan whose interest_rate_annual > binding.

Pure, side-effect-free. Caller decides what to do with the verdict (UI banner,
form block, scheduler proposal generation). See src/scheduler/jobs.py
job_loan_gate_proposals + src/web/routes/borrower.py for the consumers.

Dependency direction is one-way: this module reads Maggy's AccountSnapshot
schema only, never imports from src.strategy.* or other live-trading code.
The query pattern mirrors src/strategy/risk.py:352 _rolling_nlv_return_pct
but is reproduced inline so Bruno doesn't pull in Maggy code paths.

This module is admin-side only (it reads cross-DB). The lender portal at
/lenders/* MUST NOT import it — see src/borrower/CLAUDE.md privacy invariant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from src.core.database import get_db
from src.core.models import AccountSnapshot
from src.borrower.models import Loan, LoanStatus, get_session_factory


# Target trading return — kept in sync with src/web/routes/dashboard.py:84
# (24% annualized live target). Mirrored, not imported, to keep this module
# independent of web-route imports.
LOAN_GATE_TARGET_PCT: float = 24.0

# Daily snapshots required before the gate fires BLOCK. Below this we fail
# open (WARN) since a 30-day spike doesn't tell us much about loan policy.
MIN_SNAPSHOTS_FOR_BLOCK: int = 90


@dataclass(frozen=True)
class RepayRec:
    loan_id: int
    contract_reference: str
    rate_pct: float           # loan's annual rate in percent (5.0 = 5%, not 0.05)
    outstanding: float        # outstanding principal in loan's own currency
    currency: str
    reason: str               # human-readable, embedded in the proposal record


@dataclass(frozen=True)
class LoanGateReport:
    r90_annual_pct: Optional[float]   # None = insufficient history
    r365_annual_pct: Optional[float]
    binding_return_pct: Optional[float]   # min(r90, r365); None if either is None
    target_pct: float
    new_loan_status: Literal["GO", "WARN", "BLOCK"]
    snapshots_available: int          # how many AccountSnapshot rows the query saw
    repay_recommendations: list[RepayRec] = field(default_factory=list)
    note: str = ""                    # human-readable reason for non-GO statuses


def _outstanding(loan: Loan) -> float:
    """Mirrors src/borrower/headroom.py:127 _outstanding — sum movements with
    direction implied by movement_type."""
    from src.borrower.models import MovementType
    out = 0.0
    for m in loan.movements:
        if m.movement_type == MovementType.DISBURSEMENT:
            out += m.amount
        elif m.movement_type == MovementType.PRINCIPAL_RESTRUCTURE:
            out += m.amount
        elif m.movement_type == MovementType.PRINCIPAL_REPAYMENT:
            out -= m.amount
    return out


def _trailing_pct(lookback_days: int) -> tuple[Optional[float], int]:
    """Query AccountSnapshot from Maggy DB. Returns (raw_pct, n_snapshots_seen).

    raw_pct = (current_nlv - oldest_in_window_nlv) / oldest * 100, OR None if
    fewer than `lookback_days` snapshots exist or NLV values are non-positive.

    `_rolling_nlv_return_pct` in risk.py orders snapshots by date desc and
    takes rows[0] (newest) vs rows[-1] (oldest in window). Same here."""
    with get_db() as db:
        rows = (
            db.query(AccountSnapshot)
            .order_by(AccountSnapshot.date.desc())
            .limit(lookback_days + 1)
            .all()
        )
    n = len(rows)
    if n < lookback_days + 1:
        return None, n
    current = rows[0].net_liquidation
    past = rows[-1].net_liquidation
    if current <= 0 or past <= 0:
        return None, n
    return (current - past) / past * 100.0, n


def _annualize(raw_pct: Optional[float], window_days: int) -> Optional[float]:
    """Convert a raw window return to an annualized rate.
    For 365d window, raw_pct ≈ annualized already; for shorter windows we
    compound: (1 + raw/100) ** (365/window) - 1, then * 100."""
    if raw_pct is None:
        return None
    return ((1.0 + raw_pct / 100.0) ** (365.0 / window_days) - 1.0) * 100.0


def compute_loan_gates() -> LoanGateReport:
    """Build the report. Reads Maggy DB (AccountSnapshot) + Bruno DB (Loan).

    Fail-open semantics: when AccountSnapshot history is thin the gate returns
    WARN with empty repay_recommendations. Callers (form, scheduler) treat
    WARN as 'allow but show note' — never as BLOCK. Only BLOCK actually
    refuses a form submission."""
    raw90, n90 = _trailing_pct(90)
    raw365, n365 = _trailing_pct(365)
    snapshots_available = max(n90, n365)
    r90 = _annualize(raw90, 90)
    r365 = _annualize(raw365, 365)

    if r90 is None or r365 is None:
        binding = None
    else:
        binding = min(r90, r365)

    if binding is None or snapshots_available < MIN_SNAPSHOTS_FOR_BLOCK:
        status: Literal["GO", "WARN", "BLOCK"] = "WARN"
        note = (
            f"Insufficient NLV history ({snapshots_available} snapshots, "
            f"need ≥{MIN_SNAPSHOTS_FOR_BLOCK}). Gate fails open — new loans "
            "permitted, no repay proposals generated."
        )
        return LoanGateReport(
            r90_annual_pct=r90,
            r365_annual_pct=r365,
            binding_return_pct=binding,
            target_pct=LOAN_GATE_TARGET_PCT,
            new_loan_status=status,
            snapshots_available=snapshots_available,
            repay_recommendations=[],
            note=note,
        )

    if binding < LOAN_GATE_TARGET_PCT:
        status = "BLOCK"
        note = (
            f"Binding return {binding:.2f}% (min of 90d {r90:.2f}%, "
            f"365d {r365:.2f}%) < {LOAN_GATE_TARGET_PCT:.0f}% target."
        )
    else:
        status = "GO"
        note = (
            f"Binding return {binding:.2f}% ≥ {LOAN_GATE_TARGET_PCT:.0f}% target."
        )

    # Repay recommendations: any active loan whose rate > binding.
    recs: list[RepayRec] = []
    bruno = get_session_factory()()
    try:
        active = bruno.query(Loan).filter(Loan.status == LoanStatus.ACTIVE).all()
        for loan in active:
            rate_pct = (loan.interest_rate_annual or 0.0) * 100.0
            if rate_pct > binding:
                outstanding = _outstanding(loan)
                if outstanding <= 0:
                    continue  # already fully repaid via movements; skip
                recs.append(RepayRec(
                    loan_id=loan.id,
                    contract_reference=loan.contract_reference or f"loan #{loan.id}",
                    rate_pct=rate_pct,
                    outstanding=outstanding,
                    currency=loan.currency,
                    reason=(
                        f"Trading return {binding:.2f}% < loan rate {rate_pct:.2f}%"
                    ),
                ))
    finally:
        bruno.close()

    return LoanGateReport(
        r90_annual_pct=r90,
        r365_annual_pct=r365,
        binding_return_pct=binding,
        target_pct=LOAN_GATE_TARGET_PCT,
        new_loan_status=status,
        snapshots_available=snapshots_available,
        repay_recommendations=recs,
        note=note,
    )
