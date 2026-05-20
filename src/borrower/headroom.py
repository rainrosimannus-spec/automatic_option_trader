"""
Bruno Headroom Calculator — four-metric debt-burden framework.

Implements docs/governance.md "debt burden control" / CLAUDE.md "four-metric
framework". The calculator answers a single operational question: **is it
safe to take this new loan?** Answered as a per-metric green/amber/red plus
a composite go/warn/refuse verdict.

The four metrics:

1. **Asset Coverage** = gross_nlv / external_debt   (target ≥ 2.0×)
   "Gross unencumbered assets" per CLAUDE.md — cash + market value of
   positions, NOT net-of-debt. Subordinated/shareholder debt doesn't reduce
   the collateral pool because in a wind-down subordinated creditors stand
   behind external lenders. External debt only.

2. **Liquidity Reserve** = cash_available / cash_debt_service_12m   (target ≥ 2.0×)
   Cash available divided by 12-month cash debt service (sum of scheduled
   payments due in the next 365 days for non-capitalizing loans).

3. **Operating Cash Coverage** = expected_annual_return / cash_debt_service_12m   (target ≥ 1.5×)
   Forward-looking projection: can Maggy+Winston generate enough cash to
   cover the next 12 months of debt service comfortably?

4. **Net Worth** = gross_nlv − total_debt   (informational only)
   Tracked but not binding.

Status tiers (red < amber < green):

    | Metric           | Green       | Amber           | Red          |
    | ---------------- | ----------- | --------------- | ------------ |
    | Asset Coverage   | ≥ 2.0×      | ≥ 1.5× < 2.0×   | < 1.5×       |
    | Liquidity Res.   | ≥ 2.0×      | ≥ 1.0× < 2.0×   | < 1.0×       |
    | Op. Cash Cov.    | ≥ 1.5×      | ≥ 1.0× < 1.5×   | < 1.0×       |

A new loan is acceptable iff **all three binding metrics stay green or amber
after the new loan is added** (CLAUDE.md). Any red → form warns loudly.

External vs internal debt classification: a loan counts as "external" iff its
lender Counterparty has `tier != CounterpartyTier.SHAREHOLDER`. Treats all
shareholder loans as subordinated debt regardless of `is_subordinated` (the
formal amendment is legal work, not data — see CLAUDE.md "Subordination").
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from src.borrower.models import (
    Counterparty, CounterpartyTier, HeadroomInputs, Loan, LoanStatus,
    MovementType, Payment, PaymentStatus, get_session_factory,
)


# === Thresholds (governance.md / CLAUDE.md) ===
ASSET_COVERAGE_GREEN = 2.0
ASSET_COVERAGE_AMBER = 1.5
LIQUIDITY_GREEN = 2.0
LIQUIDITY_AMBER = 1.0
OPCASH_GREEN = 1.5
OPCASH_AMBER = 1.0


@dataclass(frozen=True)
class Metric:
    name: str
    value: Optional[float]      # the ratio, or None if not computable (e.g. zero denominator)
    status: str                 # 'green' | 'amber' | 'red' | 'na'
    description: str
    numerator: float
    denominator: float
    numerator_label: str
    denominator_label: str


@dataclass(frozen=True)
class HeadroomReport:
    asset_coverage: Metric
    liquidity_reserve: Metric
    operating_cash_coverage: Metric
    net_worth_eur: float            # informational
    external_debt_eur: float
    subordinated_debt_eur: float
    cash_debt_service_12m_eur: float
    inputs_source: str              # 'manual' | 'ibkr_snapshot' | 'none'
    inputs_as_of: Optional[str]     # iso datetime

    @property
    def binding(self) -> list[Metric]:
        return [self.asset_coverage, self.liquidity_reserve, self.operating_cash_coverage]

    @property
    def verdict(self) -> str:
        """Composite over the three binding metrics:
           'go'      = all green
           'caution' = at least one amber, none red
           'refuse'  = at least one red
           'na'      = at least one not-applicable (can't compute)
        """
        statuses = {m.status for m in self.binding}
        if "na" in statuses:
            return "na"
        if "red" in statuses:
            return "refuse"
        if "amber" in statuses:
            return "caution"
        return "go"


def _classify(value: Optional[float], green: float, amber: float) -> str:
    if value is None:
        return "na"
    if value >= green:
        return "green"
    if value >= amber:
        return "amber"
    return "red"


def _ratio(num: float, denom: float) -> Optional[float]:
    if denom is None or denom <= 0:
        return None
    return num / denom


def _outstanding(loan: Loan) -> float:
    out = 0.0
    for m in loan.movements:
        if m.movement_type == MovementType.DISBURSEMENT:
            out += m.amount
        elif m.movement_type == MovementType.PRINCIPAL_RESTRUCTURE:
            out += m.amount
        elif m.movement_type == MovementType.PRINCIPAL_REPAYMENT:
            out -= m.amount
    return out


def _cash_debt_service_12m(loan: Loan, as_of: date) -> float:
    """Scheduled payment amount due in [as_of, as_of+365d] for a single loan.
    Capitalizing loans have no scheduled cash service in this window (interest
    accrues to maturity); only payments with status SCHEDULED or OVERDUE count."""
    horizon = as_of + timedelta(days=365)
    total = 0.0
    for p in loan.payments:
        if p.status in (PaymentStatus.SCHEDULED, PaymentStatus.OVERDUE):
            if as_of <= p.scheduled_date <= horizon:
                total += p.scheduled_amount
    return total


def aggregate_debt(session, as_of: Optional[date] = None) -> dict:
    """Sum loan outstandings + 12-month cash debt service across the live
    portfolio, partitioned into external vs subordinated (shareholder)."""
    as_of = as_of or date.today()
    external = 0.0
    sub = 0.0
    cash_service = 0.0
    loans = session.query(Loan).filter(Loan.status == LoanStatus.ACTIVE).all()
    for loan in loans:
        outstanding = _outstanding(loan)
        lender = loan.lender
        is_external = (lender.tier != CounterpartyTier.SHAREHOLDER)
        if is_external:
            external += outstanding
            cash_service += _cash_debt_service_12m(loan, as_of)
        else:
            sub += outstanding
    return {
        "external_debt_eur": external,
        "subordinated_debt_eur": sub,
        "cash_debt_service_12m_eur": cash_service,
        "total_debt_eur": external + sub,
    }


def compute_headroom(
    gross_nlv_eur: float,
    cash_eur: float,
    expected_annual_return_eur: float,
    new_loan_principal_eur: float = 0.0,
    new_loan_is_external: bool = True,
    new_loan_annual_cash_service_eur: float = 0.0,
    as_of: Optional[date] = None,
    inputs_source: str = "manual",
    inputs_as_of: Optional[str] = None,
) -> HeadroomReport:
    """
    Compute the four-metric headroom report.

    The hypothetical-new-loan inputs (`new_loan_*`) are optional — set them to
    evaluate "what would happen if we added this loan?". Defaults zero out, so
    the same function serves both the current-state view and the evaluate-new
    view.
    """
    as_of = as_of or date.today()
    session = get_session_factory()()
    try:
        debt = aggregate_debt(session, as_of)
    finally:
        session.close()

    # Layer the hypothetical loan in
    external_debt = debt["external_debt_eur"]
    sub_debt = debt["subordinated_debt_eur"]
    cash_service_12m = debt["cash_debt_service_12m_eur"]

    if new_loan_principal_eur > 0:
        if new_loan_is_external:
            external_debt += new_loan_principal_eur
            cash_service_12m += max(0.0, new_loan_annual_cash_service_eur)
        else:
            sub_debt += new_loan_principal_eur

    total_debt = external_debt + sub_debt

    # --- 1. Asset Coverage ---
    asset_cov_ratio = _ratio(gross_nlv_eur, external_debt)
    asset_cov = Metric(
        name="Asset Coverage",
        value=asset_cov_ratio,
        status=_classify(asset_cov_ratio, ASSET_COVERAGE_GREEN, ASSET_COVERAGE_AMBER) if external_debt > 0 else "na",
        description=(
            "Gross unencumbered NLV divided by external debt. "
            f"Target ≥ {ASSET_COVERAGE_GREEN}× (amber {ASSET_COVERAGE_AMBER}–{ASSET_COVERAGE_GREEN}×, red below)."
            + (" — No external debt yet, ratio not applicable." if external_debt <= 0 else "")
        ),
        numerator=gross_nlv_eur,
        denominator=external_debt,
        numerator_label="Gross NLV",
        denominator_label="External debt",
    )

    # --- 2. Liquidity Reserve ---
    liq_ratio = _ratio(cash_eur, cash_service_12m)
    liq = Metric(
        name="Liquidity Reserve",
        value=liq_ratio,
        status=_classify(liq_ratio, LIQUIDITY_GREEN, LIQUIDITY_AMBER) if cash_service_12m > 0 else "na",
        description=(
            f"Cash divided by 12-month cash debt service. Target ≥ {LIQUIDITY_GREEN}× "
            f"(amber {LIQUIDITY_AMBER}–{LIQUIDITY_GREEN}×, red below)."
            + (" — No cash debt service in next 12 months, ratio not applicable." if cash_service_12m <= 0 else "")
        ),
        numerator=cash_eur,
        denominator=cash_service_12m,
        numerator_label="Cash",
        denominator_label="12m cash debt service",
    )

    # --- 3. Operating Cash Coverage ---
    opcash_ratio = _ratio(expected_annual_return_eur, cash_service_12m)
    opcash = Metric(
        name="Operating Cash Coverage",
        value=opcash_ratio,
        status=_classify(opcash_ratio, OPCASH_GREEN, OPCASH_AMBER) if cash_service_12m > 0 else "na",
        description=(
            f"Expected annual trading return divided by 12-month cash debt service. "
            f"Target ≥ {OPCASH_GREEN}× (amber {OPCASH_AMBER}–{OPCASH_GREEN}×, red below)."
            + (" — No cash debt service in next 12 months, ratio not applicable." if cash_service_12m <= 0 else "")
        ),
        numerator=expected_annual_return_eur,
        denominator=cash_service_12m,
        numerator_label="Expected return",
        denominator_label="12m cash debt service",
    )

    return HeadroomReport(
        asset_coverage=asset_cov,
        liquidity_reserve=liq,
        operating_cash_coverage=opcash,
        net_worth_eur=gross_nlv_eur - total_debt,
        external_debt_eur=external_debt,
        subordinated_debt_eur=sub_debt,
        cash_debt_service_12m_eur=cash_service_12m,
        inputs_source=inputs_source,
        inputs_as_of=inputs_as_of,
    )


def get_or_init_inputs(session) -> HeadroomInputs:
    """Fetch the single HeadroomInputs row, creating an empty one if missing."""
    row = session.query(HeadroomInputs).first()
    if row is None:
        row = HeadroomInputs(gross_nlv_eur=0.0, cash_eur=0.0, expected_annual_return_eur=0.0)
        session.add(row)
        session.commit()
        session.refresh(row)
    return row
