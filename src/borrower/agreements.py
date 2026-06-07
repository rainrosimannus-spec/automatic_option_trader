"""
Loan agreement generation.

Fills the markdown agreement template (src/borrower/templates/
loan_agreement_external_v2.md) with a loan's variables and renders it to both
HTML and PDF. This is the *generation* half of the locked "Path 3 + Option C"
contract architecture (src/borrower/CLAUDE.md): Bruno is source of truth for
loan data; the agreement is generated from a template; both parties sign
externally (or via an e-sign provider — see esign.py); the signed PDF is
uploaded back as the canonical legal artifact (loan_documents).

Generated drafts are NOT the signed legal artifact and do NOT satisfy the
DRAFT -> ACTIVE activation gate. They live in their own table
(LoanAgreementDraft) and on disk under data/agreement_drafts/{loan_id}/, kept
separate from the uploaded-PDF store in documents.py (data/contracts/).

BILINGUAL: the agreement is maintained in Estonian and English (§15.6 — the
Estonian version controls in case of discrepancy). There is one template file
per language with identical Jinja variable names; resolve_variables() localises
the prose fields so a single context renders either side. The language is
chosen per loan, defaulting to the lender's jurisdiction (default_language_for:
Estonian for an Estonian lender, English otherwise). The chosen template's
filename is recorded on the draft, so regenerate/edit re-render in the same
language (language_for_template()).

LEGAL GUARD (review-gated, per language): the draft banner + watermark exist to
stop an un-reviewed template being mistaken for a signable contract. They are
emitted ONLY while the active template is unreviewed (is_template_reviewed()).
The English v2 is counsel-reviewed and renders clean/signable; the Estonian
translation is freshly authored and carries a "-draft" version, so it renders
with the guard until counsel signs off (then drop the "-draft" suffix in
TEMPLATE_VERSIONS, exactly as was done for English on 2026-06-06). See
LEGAL_CONTEXT.md.
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

from datetime import datetime

from src.borrower.models import (
    CounterpartyType,
    InterestTreatment,
    PaymentFrequency,
    RepaymentStructure,
    CounterpartyTier,
    Loan,
    LoanStatus,
    LoanAgreementDraft,
    AgreementDraftStatus,
)

DRAFTS_ROOT = Path("data/agreement_drafts")
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# Bilingual production: the agreement is maintained in Estonian and English
# (§15.6 — the Estonian version controls in case of discrepancy). One template
# file per language; same Jinja variable names, so a single resolved context
# renders either side. Language is chosen per loan (operator picks, defaulting
# to the lender's jurisdiction — see default_language_for()).
DEFAULT_LANGUAGE = "en"
LANGUAGES = ("et", "en")
LANGUAGE_LABELS = {"et": "Estonian (eesti keel)", "en": "English"}
TEMPLATE_NAMES = {
    "en": "loan_agreement_external_v2.md",
    "et": "loan_agreement_external_v2_et.md",
}
# Per-language version string. A version ending in "-draft" marks an UN-reviewed
# template and auto-engages the draft banner/watermark guard (see below). The
# English v2 was promoted to reviewed on 2026-06-06; the Estonian translation is
# freshly authored and NOT yet counsel-reviewed, so it carries "-draft" and
# renders with the guard until counsel signs off (then drop the "-draft" suffix,
# exactly as was done for English).
TEMPLATE_VERSIONS = {"en": "v2", "et": "v2-et-draft"}

# Back-compat module-level defaults (English). Existing callers that reference
# the singular constants keep working; language-aware callers use the helpers.
TEMPLATE_NAME = TEMPLATE_NAMES[DEFAULT_LANGUAGE]
TEMPLATE_VERSION = TEMPLATE_VERSIONS[DEFAULT_LANGUAGE]


def _norm_lang(language: Optional[str]) -> str:
    lang = (language or DEFAULT_LANGUAGE).strip().lower()
    return lang if lang in TEMPLATE_NAMES else DEFAULT_LANGUAGE


def template_name_for(language: Optional[str]) -> str:
    return TEMPLATE_NAMES[_norm_lang(language)]


def template_version_for(language: Optional[str]) -> str:
    return TEMPLATE_VERSIONS[_norm_lang(language)]


def language_for_template(template_name: Optional[str]) -> str:
    """Reverse-map a stored template filename to its language. Used by
    regenerate/edit so a draft re-renders in the language it was created in."""
    for lang, name in TEMPLATE_NAMES.items():
        if name == template_name:
            return lang
    return DEFAULT_LANGUAGE


def is_template_reviewed(language: Optional[str]) -> bool:
    """The draft banner + watermark are a guard against an *un-reviewed* template
    being mistaken for a signable contract. They render only while the active
    template is unreviewed. Derive that from the version string so the guard
    auto-engages for any "-draft" template without touching render code."""
    return not template_version_for(language).endswith("-draft")


_ESTONIA_HINTS = ("estonia", "eesti")


def default_language_for(counterparty) -> str:
    """Default agreement language for a lender: Estonian when the lender is in
    Estonia, English otherwise. Prefers the structured `country` field (ISO-2,
    defaults to 'EE'); falls back to scanning the free-text address."""
    country = (getattr(counterparty, "country", "") or "").strip().upper()
    if country:
        return "et" if country == "EE" else "en"
    address = (getattr(counterparty, "address", "") or "").lower()
    return "et" if any(h in address for h in _ESTONIA_HINTS) else "en"


# Back-compat: True iff the default-language (English) template is reviewed.
TEMPLATE_REVIEWED = is_template_reviewed(DEFAULT_LANGUAGE)

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
# Estonian localisation. num2words has no Estonian support, and the prose words
# below ("monthly", "fixed", "bullet loan", …) are English in the EN template;
# the Estonian template needs them in Estonian. Same Jinja variable names — the
# VALUES are localised here so one context dict renders either template.
# ---------------------------------------------------------------------------
_RATE_TYPE_ET = {"fixed": "fikseeritud", "floating": "ujuv", "zero": "null"}
_FREQ_ADVERB_ET = {
    "monthly": "igakuiselt",
    "quarterly": "kord kvartalis",
    "semiannual": "kord poolaastas",
    "annual": "kord aastas",
    "at_maturity": "lõpptähtajal",
}
_FREQ_UNIT_ET = {
    PaymentFrequency.MONTHLY: "kuu",
    PaymentFrequency.QUARTERLY: "kvartali",
    PaymentFrequency.SEMIANNUAL: "poolaasta",
    PaymentFrequency.ANNUAL: "aasta",
    PaymentFrequency.AT_MATURITY: "perioodi",
}
_STRUCTURE_WORD_ET = {
    "bullet": "ühekordse lõppmaksega laen",
    "amortizing": "amortiseeruv laen",
    "revolving": "uuenev krediidiliin",
}
# Currency unit in the Estonian partitive (the case numbers take when counting):
# "kaks eurot", "üheksakümmend senti".
_CURRENCY_WORD_ET = {
    "EUR": ("eurot", "senti"),
    "USD": ("USA dollarit", "senti"),
    "GBP": ("naelsterlingit", "penni"),
    "AUD": ("Austraalia dollarit", "senti"),
}

_ET_ONES = ["null", "üks", "kaks", "kolm", "neli", "viis", "kuus", "seitse", "kaheksa", "üheksa"]
_ET_TEENS = ["kümme", "üksteist", "kaksteist", "kolmteist", "neliteist", "viisteist",
             "kuusteist", "seitseteist", "kaheksateist", "üheksateist"]
_ET_TENS = {2: "kakskümmend", 3: "kolmkümmend", 4: "nelikümmend", 5: "viiskümmend",
            6: "kuuskümmend", 7: "seitsekümmend", 8: "kaheksakümmend", 9: "üheksakümmend"}


def _et_below_1000(n: int) -> str:
    parts = []
    h, rem = divmod(n, 100)
    if h:
        parts.append("sada" if h == 1 else _ET_ONES[h] + "sada")
    if rem:
        if rem < 10:
            parts.append(_ET_ONES[rem])
        elif rem < 20:
            parts.append(_ET_TEENS[rem - 10])
        else:
            t, o = divmod(rem, 10)
            parts.append(_ET_TENS[t])
            if o:
                parts.append(_ET_ONES[o])
    return " ".join(parts)


def _et_integer(n: int) -> str:
    """Cardinal number in Estonian words. Correct for 0 .. billions."""
    if n == 0:
        return "null"
    parts = []
    billions, rem = divmod(n, 1_000_000_000)
    millions, rem = divmod(rem, 1_000_000)
    thousands, rest = divmod(rem, 1000)
    if billions:
        parts.append(_et_below_1000(billions) + (" miljard" if billions == 1 else " miljardit"))
    if millions:
        parts.append(_et_below_1000(millions) + (" miljon" if millions == 1 else " miljonit"))
    if thousands:
        parts.append("tuhat" if thousands == 1 else _et_below_1000(thousands) + " tuhat")
    if rest:
        parts.append(_et_below_1000(rest))
    return " ".join(parts)


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
                  "one and a half times (1.5x) the outstanding Loan principal"),
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


def _build_terms_summary(loan, freq, language: str) -> str:
    """Plain-language economic summary for clause 1.2, generated from the
    structured fields. States the rate as ANNUAL and the cadence as payments so
    a reader can never parse the headline as "7% per month". Localised."""
    months = _months_between(loan.origination_date, loan.maturity_date)
    pct = (loan.interest_rate_annual or 0.0) * 100.0
    struct = loan.repayment_structure.value
    treat = loan.interest_treatment.value
    if language == "et":
        if months and months % 12 == 0:
            term = f"{months // 12}-aastane"
        elif months:
            term = f"{months}-kuune"
        else:
            term = ""
        structure = _STRUCTURE_WORD_ET.get(struct, f"{struct.replace('_', ' ')} laen")
        rate = f"{('%g' % pct).replace('.', ',')}% aastane intressimäär"
        freq_word = _FREQ_ADVERB_ET.get(freq.value, freq.value)
        if treat == "capitalizing":
            pay = "intress kapitaliseeritakse ja makstakse lõpptähtajal"
        elif treat == "amortizing":
            pay = f"{freq_word} tasutavate amortiseeruvate osamaksetena"
        else:
            pay = f"{freq_word} tasutavate intressimaksetega"
        head = " ".join(p for p in [term, structure] if p)
        return f"{head}, {rate}, {pay}"
    # English (default)
    if months and months % 12 == 0:
        term = f"{months // 12}-year"
    elif months:
        term = f"{months}-month"
    else:
        term = ""
    structure = {
        "bullet": "bullet loan",
        "amortizing": "amortizing loan",
        "revolving": "revolving facility",
    }.get(struct, f"{struct.replace('_', ' ')} loan")
    rate = f"{pct:g}% annual interest"
    freq_word = freq.value.replace("_", " ")
    if treat == "capitalizing":
        pay = "interest capitalized and paid at maturity"
    elif treat == "amortizing":
        pay = f"{freq_word} amortizing installments"
    else:
        pay = f"{freq_word} interest payments"
    return " ".join(p for p in [term, structure] if p) + f" at {rate}, with {pay}"


def resolve_variables(loan, counterparty, operator_inputs: dict, session,
                      language: str = DEFAULT_LANGUAGE) -> dict:
    """Build the full Jinja context for the template from the loan record, the
    lender's counterparty record, operator inputs, and computed fields. Prose
    fields (rate type, payment cadence, terms summary, amount-in-words) are
    rendered in `language`; the same variable names serve both templates."""
    language = _norm_lang(language)
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

    terms_summary = _build_terms_summary(loan, freq, language)

    def _fmt_amount(value) -> str:
        s = f"{(value or 0.0):,.2f}"          # 30,000.00 (en grouping)
        if language == "et":                  # 30 000,00 (space group, comma decimal)
            s = s.replace(",", " ").replace(".", ",")
        return s

    def _fmt_pct(value) -> str:
        s = f"{(value or 0.0):.2f}"            # 11.55
        return s.replace(".", ",") if language == "et" else s

    # Localised labels for prose fields the template interpolates as-is.
    if language == "et":
        interest_rate_type_label = _RATE_TYPE_ET.get(
            loan.interest_rate_type.value, loan.interest_rate_type.value)
        payment_frequency_label = _FREQ_ADVERB_ET.get(freq.value, freq.value)
        payment_frequency_unit_label = _FREQ_UNIT_ET.get(freq, "perioodi")
    else:
        interest_rate_type_label = loan.interest_rate_type.value.replace("_", " ")
        payment_frequency_label = freq.value.replace("_", " ")
        payment_frequency_unit_label = _FREQ_UNIT.get(freq, "period")

    def _int_or(v, fallback):
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return fallback

    return {
        "language": language,
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
        "principal_formatted": _fmt_amount(loan.principal_max),
        "installment_formatted": _fmt_amount(loan.installment_amount or 0.0),
        "currency": loan.currency,
        "interest_rate_pct": (loan.interest_rate_annual or 0.0) * 100.0,
        "interest_rate_pct_formatted": _fmt_pct((loan.interest_rate_annual or 0.0) * 100.0),
        "default_rate_pct_formatted": _fmt_pct((loan.interest_rate_annual or 0.0) * 100.0 + 2),
        "interest_rate_type": interest_rate_type_label,
        "day_count_convention": loan.day_count_convention.value.replace("_", "/"),
        "interest_treatment": loan.interest_treatment.value,
        "repayment_structure": loan.repayment_structure.value,
        "payment_frequency": payment_frequency_label,
        "payment_frequency_unit": payment_frequency_unit_label,
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
        "terms_summary": terms_summary,
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


def _to_words_et(amount: float, currency: str) -> str:
    """Amount in Estonian words, e.g. 8682.90 EUR ->
    'kaheksa tuhat kuussada kaheksakümmend kaks eurot ja üheksakümmend senti'.
    num2words has no Estonian support, so this is hand-rolled (see _et_integer)."""
    cur = (currency or "EUR").upper()
    whole = int(amount)
    cents = int(round((amount - whole) * 100))
    if cents == 100:                       # rounding carry
        whole += 1
        cents = 0
    unit, cent_unit = _CURRENCY_WORD_ET.get(cur, (cur, "senti"))
    return f"{_et_integer(whole)} {unit} ja {_et_integer(cents)} {cent_unit}"


def render_markdown(context: dict, language: Optional[str] = None) -> str:
    """Fill the language's template with the context. Strips the developer/
    lawyer-note HTML comments first so they never reach the lender-facing
    document. `language` defaults to context['language'] (set by
    resolve_variables), then to the module default."""
    language = _norm_lang(language or context.get("language"))
    raw = (TEMPLATE_DIR / template_name_for(language)).read_text(encoding="utf-8")
    cleaned = _COMMENT_RE.sub("", raw).lstrip("\n")
    env = Environment(undefined=StrictUndefined, autoescape=False)
    speller = _to_words_et if language == "et" else _to_words
    env.filters["currency_words"] = lambda amt: speller(amt, context.get("currency", "EUR"))
    return env.from_string(cleaned).render(**context)


# Draft chrome — emitted only for an un-reviewed template (see is_template_reviewed).
# Localised so an Estonian draft carries an Estonian warning.
_BANNER_HTML = {
    "en": (
        '<div class="draft-banner">DRAFT — generated by Bruno from template '
        "{tmpl}. NOT reviewed by Estonian counsel. Not a signed agreement. "
        "Review and sign externally; upload the signed PDF to activate the loan.</div>"
    ),
    "et": (
        '<div class="draft-banner">MUSTAND — Bruno poolt koostatud mallist '
        "{tmpl}. EI OLE läbinud Eesti õigusnõustaja kontrolli. Ei ole "
        "allkirjastatud leping. Vaadake üle ja allkirjastage väljaspool süsteemi; "
        "laenu aktiveerimiseks laadige üles allkirjastatud PDF.</div>"
    ),
}
_WATERMARK_TEXT = {"en": "DRAFT", "et": "MUSTAND"}
_DRAFT_PAGE_FOOTER = {
    "en": '"DRAFT — not legal advice — page " counter(page)',
    "et": '"MUSTAND — ei ole õigusnõu — lk " counter(page)',
}
_CLEAN_PAGE_FOOTER = {"en": '"page " counter(page)', "et": '"lk " counter(page)'}
_TITLE = {"en": "Loan Agreement", "et": "Laenuleping"}
_DRAFT_SUFFIX = {"en": " (DRAFT)", "et": " (MUSTAND)"}

_HTML_SHELL = """<!DOCTYPE html>
<html lang="{lang}"><head><meta charset="utf-8">
<title>{doc_title} — {title}{title_suffix}</title>
<style>
  @page {{ size: A4; margin: 22mm 20mm; @bottom-center {{ content: {page_footer}; font-size: 8pt; color: #999; }} }}
  body {{ font-family: "DejaVu Serif", Georgia, serif; font-size: 10.5pt; line-height: 1.5; color: #111; max-width: 760px; margin: 0 auto; padding: 16px; }}
  h1 {{ font-size: 18pt; text-align: center; }} h2 {{ font-size: 12.5pt; border-bottom: 1px solid #ccc; padding-bottom: 2px; margin-top: 22px; }}
  .draft-banner {{ background: #fde68a; border: 1px solid #b45309; color: #7c2d12; padding: 10px 14px; border-radius: 6px; font-weight: 700; font-size: 9.5pt; margin-bottom: 20px; }}
  {watermark_css}
  hr {{ border: none; border-top: 1px solid #ddd; margin: 18px 0; }}
  table {{ border-collapse: collapse; }}
</style></head>
<body>{banner}{body}</body></html>"""


def _watermark_css(language: str) -> str:
    return (
        'body::before { content: "' + _WATERMARK_TEXT[language] + '"; position: fixed; '
        "top: 42%; left: 18%; font-size: 130pt; color: rgba(180,83,9,0.07); "
        "transform: rotate(-35deg); z-index: -1; }"
    )


def render_html(markdown_body: str, title: str, language: str = DEFAULT_LANGUAGE) -> str:
    import markdown as _md
    language = _norm_lang(language)
    body_html = _md.markdown(markdown_body, extensions=["extra", "sane_lists"])
    if is_template_reviewed(language):
        # Reviewed production template: clean, signable — no draft chrome.
        return _HTML_SHELL.format(
            lang=language,
            doc_title=_TITLE[language],
            title=title,
            title_suffix="",
            page_footer=_CLEAN_PAGE_FOOTER[language],
            watermark_css="",
            banner="",
            body=body_html,
        )
    return _HTML_SHELL.format(
        lang=language,
        doc_title=_TITLE[language],
        title=title,
        title_suffix=_DRAFT_SUFFIX[language],
        page_footer=_DRAFT_PAGE_FOOTER[language],
        watermark_css=_watermark_css(language),
        banner=_BANNER_HTML[language].format(tmpl=template_version_for(language)),
        body=body_html,
    )


def render_pdf(html: str) -> bytes:
    from weasyprint import HTML
    return HTML(string=html).write_pdf()


def render_all(loan, counterparty, operator_inputs: dict, session,
               language: str = DEFAULT_LANGUAGE) -> RenderedDraft:
    """Resolve variables and render markdown + HTML + PDF in one shot, in
    `language` ('et' or 'en'). Raises AgreementError if required inputs are
    missing."""
    language = _norm_lang(language)
    missing = required_variables(loan, counterparty, operator_inputs)
    if missing:
        raise AgreementError(
            "Cannot generate the agreement — missing: " + "; ".join(missing)
        )
    variables = resolve_variables(loan, counterparty, operator_inputs, session, language)
    md = render_markdown(variables, language)
    html = render_html(md, title=loan.contract_reference or f"Loan {loan.id}", language=language)
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


def lock_drafts(session, loan_id: int, reason: str) -> int:
    """Lock every non-locked agreement draft on a loan — called when a signed
    agreement is registered (manual upload or e-sign completion). Returns the
    number locked. Caller commits. reason: 'signed_upload' | 'esign_complete'."""
    n = 0
    for d in session.query(LoanAgreementDraft).filter_by(loan_id=loan_id).all():
        if d.status != AgreementDraftStatus.LOCKED:
            d.status = AgreementDraftStatus.LOCKED
            d.locked_at = datetime.utcnow()
            d.locked_reason = reason
            n += 1
    return n


def read_pdf(pdf_path: str) -> Optional[Path]:
    """Resolve a stored draft PDF path, guarding against escapes from
    DRAFTS_ROOT. Returns None if missing/outside."""
    p = Path(pdf_path).resolve()
    try:
        p.relative_to(DRAFTS_ROOT.resolve())
    except ValueError:
        return None
    return p if p.exists() and p.is_file() else None
