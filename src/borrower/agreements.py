"""
Loan agreement generation.

Fills the markdown agreement template (src/borrower/templates/
loan_agreement_external_v1.md) with a loan's variables and renders it to both
HTML and PDF. This is the *generation* half of the locked "Path 3 + Option C"
contract architecture (src/borrower/CLAUDE.md): Bruno is source of truth for
loan data; the agreement is generated from a template; both parties sign
externally (or via an e-sign provider — see esign.py); the signed PDF is
uploaded back as the canonical legal artifact (loan_documents).

Generated drafts are NOT the signed legal artifact and do NOT satisfy the
DRAFT -> ACTIVE activation gate. They live in their own table
(LoanAgreementDraft) and on disk under data/agreement_drafts/{loan_id}/, kept
separate from the uploaded-PDF store in documents.py (data/contracts/).

LEGAL GUARD: the v1 template is explicitly *not yet reviewed by Estonian
counsel*. Every rendered artifact carries a prominent DRAFT banner +
watermark until a lawyer-reviewed v2 exists. See LEGAL_CONTEXT.md.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from jinja2 import Environment, StrictUndefined
from num2words import num2words

from src.borrower.models import (
    CounterpartyType,
    InterestTreatment,
    PaymentFrequency,
    RepaymentStructure,
    CounterpartyTier,
    Loan,
    LoanStatus,
)

DRAFTS_ROOT = Path("data/agreement_drafts")
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
TEMPLATE_NAME = "loan_agreement_external_v1.md"
TEMPLATE_VERSION = "v1-draft"

# Map PaymentFrequency -> (period in months, singular unit word for the prose).
# AT_MATURITY has no recurring period.
_FREQ_MONTHS = {
    PaymentFrequency.MONTHLY: 1,
    PaymentFrequency.QUARTERLY: 3,
    PaymentFrequency.SEMIANNUAL: 6,
    PaymentFrequency.ANNUAL: 12,
    PaymentFrequency.AT_MATURITY: None,
}
_FREQ_UNIT = {
    PaymentFrequency.MONTHLY: "month",
    PaymentFrequency.QUARTERLY: "quarter",
    PaymentFrequency.SEMIANNUAL: "six-month period",
    PaymentFrequency.ANNUAL: "year",
    PaymentFrequency.AT_MATURITY: "period",
}

_CURRENCY_WORD = {"EUR": "euro", "USD": "US dollars", "GBP": "pounds sterling", "AUD": "Australian dollars"}


# ---------------------------------------------------------------------------
# Operator inputs — fields the template needs that aren't on the Loan record.
# Collected on the New Loan form; gate Create Loan until all required are set.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OperatorField:
    name: str
    label: str
    required: bool
    default: str = ""
    help: str = ""


OPERATOR_FIELDS = [
    OperatorField("borrower_represented_by", "MesiCap signatory (name)", True,
                  help="Who signs for MesiCap — usually Rain."),
    OperatorField("borrower_title", "MesiCap signatory title", True, "Board Member"),
    OperatorField("borrower_notice_email", "MesiCap notice email", True, "rain.rosimannus@gmail.com"),
    OperatorField("place_of_signing", "Place of signing", True, "Tallinn, Estonia"),
    OperatorField("purpose_description", "Purpose (free text for the contract)", True,
                  help="Expands on the loan's purpose, e.g. 'general corporate purposes and trading working capital'."),
    OperatorField("default_cure_days", "Default cure period (business days)", True, "15"),
    OperatorField("minimum_net_worth", "Minimum net-worth covenant", True,
                  "two times (2.0x) the outstanding Loan principal"),
]


class AgreementError(ValueError):
    """Raised on a problem the operator can fix; message is safe to surface."""


@dataclass(frozen=True)
class RenderedDraft:
    markdown_body: str
    html: str
    pdf_bytes: bytes
    sha256: str
    variables: dict


# ---------------------------------------------------------------------------
# Variable resolution
# ---------------------------------------------------------------------------
def _months_between(d1: date, d2: date) -> int:
    """Whole calendar months from d1 to d2 (>= 0)."""
    return max(0, (d2.year - d1.year) * 12 + (d2.month - d1.month))


def _add_months(d: date, months: int, day_of_month: Optional[int]) -> date:
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    day = day_of_month or d.day
    # clamp day to the month's length
    for dd in range(min(day, 31), 27, -1):
        try:
            return date(year, month, dd)
        except ValueError:
            continue
    return date(year, month, min(day, 28))


def _shareholder_totals(session, exclude_loan_id: Optional[int]) -> tuple[float, int]:
    """Aggregate principal_max + count of shareholder-tier loans (for the
    subordination disclosure, §11.3). Best-effort: uses principal_max as a
    proxy for outstanding at draft time. EUR-naive aggregate."""
    q = session.query(Loan).join(
        Loan.lender
    ).filter(
        Loan.status.in_([LoanStatus.ACTIVE, LoanStatus.DRAFT]),
    )
    total, count = 0.0, 0
    for ln in q.all():
        if exclude_loan_id and ln.id == exclude_loan_id:
            continue
        lender = ln.lender
        if lender is not None and lender.tier == CounterpartyTier.SHAREHOLDER:
            total += ln.principal_max or 0.0
            count += 1
    return total, count


def resolve_variables(loan, counterparty, operator_inputs: dict, session) -> dict:
    """Build the full Jinja context for the template from the loan record, the
    lender's counterparty record, operator inputs, and computed fields."""
    freq = loan.payment_frequency
    period_months = _FREQ_MONTHS.get(freq)
    first_payment = (
        _add_months(loan.origination_date, period_months, loan.payment_day_of_month)
        if period_months else loan.maturity_date
    )
    installment_count = None
    if loan.repayment_structure == RepaymentStructure.AMORTIZING and period_months:
        span = _months_between(loan.origination_date, loan.maturity_date)
        installment_count = max(1, round(span / period_months))

    sh_total, sh_count = _shareholder_totals(session, exclude_loan_id=loan.id)

    def _int_or(v, fallback):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return fallback

    return {
        # Borrower (operator input)
        "borrower": {
            "represented_by": (operator_inputs.get("borrower_represented_by") or "").strip(),
            "title": (operator_inputs.get("borrower_title") or "").strip(),
            "notice_email": (operator_inputs.get("borrower_notice_email") or "").strip(),
        },
        # Lender (counterparty record)
        "counterparty": {
            "name": counterparty.name,
            "type": "company" if counterparty.type == CounterpartyType.COMPANY else "individual",
            "registration_number": counterparty.registration_number or "",
            "legal_form": counterparty.legal_form or "",
            "address": counterparty.address or "",
            "contact_email": counterparty.contact_email or "",
            "iban": counterparty.iban or "",
            "represented_by": counterparty.represented_by or "",
            "represented_by_title": counterparty.represented_by_title or "",
        },
        # Economic terms (loan record)
        "principal_max": loan.principal_max,
        "currency": loan.currency,
        "interest_rate_pct": (loan.interest_rate_annual or 0.0) * 100.0,
        "interest_rate_type": loan.interest_rate_type.value.replace("_", " "),
        "day_count_convention": loan.day_count_convention.value.replace("_", "/"),
        "interest_treatment": loan.interest_treatment.value,
        "repayment_structure": loan.repayment_structure.value,
        "payment_frequency": freq.value.replace("_", " "),
        "payment_frequency_unit": _FREQ_UNIT.get(freq, "period"),
        "payment_day_of_month": loan.payment_day_of_month or "",
        "installment_amount": loan.installment_amount or 0.0,
        "installment_count": installment_count if installment_count is not None else "",
        "first_interest_payment_date": first_payment.isoformat(),
        "first_payment_date": first_payment.isoformat(),
        # Dates
        "contract_date": loan.contract_date.isoformat(),
        "origination_date": loan.origination_date.isoformat(),
        "maturity_date": loan.maturity_date.isoformat(),
        # Early repayment
        "early_repayment_allowed": loan.early_repayment_allowed,
        "early_repayment_notice_days": loan.early_repayment_notice_days or 0,
        # Operator inputs
        "place_of_signing": (operator_inputs.get("place_of_signing") or "").strip(),
        "purpose_description": (operator_inputs.get("purpose_description") or "").strip(),
        "default_cure_days": _int_or(operator_inputs.get("default_cure_days"), 15),
        "minimum_net_worth": (operator_inputs.get("minimum_net_worth") or "").strip(),
        # Subordination disclosure (computed)
        "shareholder_loan_aggregate": round(sh_total, 2),
        "shareholder_loan_count": sh_count,
        "shareholder_loan_currency": "EUR",
    }


def required_variables(loan, counterparty, operator_inputs: dict) -> list[str]:
    """Return human-readable labels of every required input still missing.
    Empty list => ready to generate. This is the single source of truth for
    the Create-Loan gate (server-side) and the form's disable-until-complete
    JS (client-side mirrors this list)."""
    missing: list[str] = []
    for f in OPERATOR_FIELDS:
        if f.required and not (operator_inputs.get(f.name) or "").strip():
            missing.append(f.label)
    # A company lender must have an authorized signatory for the signature block.
    if counterparty.type == CounterpartyType.COMPANY and not (counterparty.represented_by or "").strip():
        missing.append(f"Lender signatory — set 'represented by' on counterparty '{counterparty.name}'")
    return missing


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _to_words(amount: float, currency: str) -> str:
    cur = (currency or "EUR").upper()
    try:
        return num2words(amount, to="currency", lang="en", currency=cur)
    except (NotImplementedError, KeyError):
        whole = int(round(amount))
        return f"{num2words(whole, lang='en')} {_CURRENCY_WORD.get(cur, cur)}"


def render_markdown(context: dict) -> str:
    """Fill the template with the context. Strips the developer/lawyer-note
    HTML comments first so they never reach the lender-facing document."""
    raw = (TEMPLATE_DIR / TEMPLATE_NAME).read_text(encoding="utf-8")
    cleaned = _COMMENT_RE.sub("", raw).lstrip("\n")
    env = Environment(undefined=StrictUndefined, autoescape=False)
    env.filters["currency_words"] = lambda amt: _to_words(amt, context.get("currency", "EUR"))
    return env.from_string(cleaned).render(**context)


_BANNER_HTML = (
    '<div class="draft-banner">DRAFT — generated by Bruno from template '
    "{tmpl}. NOT reviewed by Estonian counsel. Not a signed agreement. "
    "Review and sign externally; upload the signed PDF to activate the loan.</div>"
)

_HTML_SHELL = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Loan Agreement — {title} (DRAFT)</title>
<style>
  @page {{ size: A4; margin: 22mm 20mm; @bottom-center {{ content: "DRAFT — not legal advice — page " counter(page); font-size: 8pt; color: #999; }} }}
  body {{ font-family: "DejaVu Serif", Georgia, serif; font-size: 10.5pt; line-height: 1.5; color: #111; max-width: 760px; margin: 0 auto; padding: 16px; }}
  h1 {{ font-size: 18pt; text-align: center; }} h2 {{ font-size: 12.5pt; border-bottom: 1px solid #ccc; padding-bottom: 2px; margin-top: 22px; }}
  .draft-banner {{ background: #fde68a; border: 1px solid #b45309; color: #7c2d12; padding: 10px 14px; border-radius: 6px; font-weight: 700; font-size: 9.5pt; margin-bottom: 20px; }}
  body::before {{ content: "DRAFT"; position: fixed; top: 42%; left: 18%; font-size: 130pt; color: rgba(180,83,9,0.07); transform: rotate(-35deg); z-index: -1; }}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 18px 0; }}
  table {{ border-collapse: collapse; }}
</style></head>
<body>{banner}{body}</body></html>"""


def render_html(markdown_body: str, title: str) -> str:
    import markdown as _md
    body_html = _md.markdown(markdown_body, extensions=["extra", "sane_lists"])
    return _HTML_SHELL.format(
        title=title,
        banner=_BANNER_HTML.format(tmpl=TEMPLATE_VERSION),
        body=body_html,
    )


def render_pdf(html: str) -> bytes:
    from weasyprint import HTML
    return HTML(string=html).write_pdf()


def render_all(loan, counterparty, operator_inputs: dict, session) -> RenderedDraft:
    """Resolve variables and render markdown + HTML + PDF in one shot.
    Raises AgreementError if required inputs are missing."""
    missing = required_variables(loan, counterparty, operator_inputs)
    if missing:
        raise AgreementError(
            "Cannot generate the agreement — missing: " + "; ".join(missing)
        )
    variables = resolve_variables(loan, counterparty, operator_inputs, session)
    md = render_markdown(variables)
    html = render_html(md, title=loan.contract_reference or f"Loan {loan.id}")
    pdf = render_pdf(html)
    return RenderedDraft(
        markdown_body=md,
        html=html,
        pdf_bytes=pdf,
        sha256=hashlib.sha256(pdf).hexdigest(),
        variables=variables,
    )


# ---------------------------------------------------------------------------
# Disk storage  (data/agreement_drafts/{loan_id}/v{n}.{md,html,pdf})
# ---------------------------------------------------------------------------
def write_files(loan_id: int, version: int, rendered: RenderedDraft) -> dict:
    """Persist the rendered artifacts. Returns the relative paths."""
    target_dir = DRAFTS_ROOT / str(loan_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    base = target_dir / f"v{version}"
    md_path = base.with_suffix(".md")
    html_path = base.with_suffix(".html")
    pdf_path = base.with_suffix(".pdf")
    md_path.write_text(rendered.markdown_body, encoding="utf-8")
    html_path.write_text(rendered.html, encoding="utf-8")
    pdf_path.write_bytes(rendered.pdf_bytes)
    return {"md": str(md_path), "html": str(html_path), "pdf": str(pdf_path)}


def read_pdf(pdf_path: str) -> Optional[Path]:
    """Resolve a stored draft PDF path, guarding against escapes from
    DRAFTS_ROOT. Returns None if missing/outside."""
    p = Path(pdf_path).resolve()
    try:
        p.relative_to(DRAFTS_ROOT.resolve())
    except ValueError:
        return None
    return p if p.exists() and p.is_file() else None
