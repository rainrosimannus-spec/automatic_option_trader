"""
E-signature provider integration (Phase 2).

Two providers, picked per loan / lender:
  - Dokobit   — Estonian/Baltic eID: Smart-ID, Mobiil-ID, ID-card. Hosted API,
                returns an ASiC-E (.asice) container. Best for local lenders.
  - DocuSign  — international eSignature, returns a signed PDF. For lenders
                without Estonian eID.

Gating (same rule as merit_api.py): live provider calls run only on Rasmus's
clone where `cfg.app.bruno_run_integrations` is True. On Rain's dev codebase
`send_for_signature()` is a no-op that logs a skip and returns None — nothing
leaves the box. The *completion* path (`register_signed_agreement`) is pure DB
+ local storage and runs everywhere; it's what turns a downloaded signed
artifact into the canonical loan_documents AGREEMENT and locks the draft.

Flow:
  generated draft (PDF)  --send_for_signature-->  provider envelope
  signer signs (Smart-ID / Mobiil-ID / DocuSign email)
  provider notifies / we poll  --fetch_signed-->  signed bytes
  register_signed_agreement()  ->  loan_documents AGREEMENT + draft LOCKED
                                     (locked_reason='esign_complete')

This terminal state is identical to the manual external path (upload the
signed PDF/.asice yourself), so the DRAFT -> ACTIVE activation gate behaves
the same regardless of which route produced the signed artifact.

Credentials live in `.env` (gitignored); see deploy/bruno.env.example:
    DOKOBIT_API_TOKEN, DOKOBIT_BASE_URL
    DOCUSIGN_BASE_URL, DOCUSIGN_ACCOUNT_ID, DOCUSIGN_INTEGRATION_KEY,
    DOCUSIGN_USER_ID, DOCUSIGN_PRIVATE_KEY  (JWT grant)

NOTE: the provider request/poll plumbing below follows each vendor's documented
REST shapes but has NOT been exercised against a live account (no creds on this
codebase). Validate endpoints + payloads on Rasmus's clone before first real
use. The completion path IS covered by tests.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from src.core.logger import get_logger
from src.borrower import agreements, documents
from src.borrower.models import DocumentType, LoanDocument

log = get_logger(__name__)

PROVIDERS = ("dokobit", "docusign")


class ESignError(ValueError):
    """Raised on a problem worth surfacing to the operator."""


@dataclass
class ESignEnvelope:
    provider: str            # 'dokobit' | 'docusign'
    envelope_id: str         # provider's token / envelopeId
    status: str              # provider-native status string
    signing_url: Optional[str] = None   # where the signer goes (if returned)
    raw: dict = field(default_factory=dict)


def _is_enabled() -> bool:
    """Live provider calls run only when bruno_run_integrations is on (prod)."""
    try:
        from src.core.config import get_settings
        return bool(getattr(get_settings().app, "bruno_run_integrations", False))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Send for signature  (gated — no-op on dev)
# ---------------------------------------------------------------------------
def send_for_signature(loan, draft, signers: list[dict], provider: str) -> Optional[ESignEnvelope]:
    """Create a signing envelope for a generated draft's PDF.

    `signers` is a list of {name, email, (phone/personal_code for Dokobit eID)}.
    Returns an ESignEnvelope, or None when integrations are disabled (dev) —
    callers must handle None and tell the operator e-sign is off here.
    """
    provider = (provider or "").lower().strip()
    if provider not in PROVIDERS:
        raise ESignError(f"Unknown e-sign provider '{provider}'. Use one of: {', '.join(PROVIDERS)}.")
    if not _is_enabled():
        log.info("skipping_external_integration_dev_codebase esign_send provider=%s loan_id=%s", provider, getattr(loan, "id", "?"))
        return None
    if not draft or not draft.pdf_path:
        raise ESignError("No generated draft PDF to send for signature.")
    pdf = agreements.read_pdf(draft.pdf_path)
    if pdf is None:
        raise ESignError("Draft PDF missing on disk.")
    content = pdf.read_bytes()
    if provider == "dokobit":
        return _dokobit_send(loan, content, signers)
    return _docusign_send(loan, content, signers)


def fetch_signed(envelope: ESignEnvelope) -> tuple[bytes, str, str]:
    """Download the completed signed artifact. Returns (content, filename, mime).
    Raises ESignError if not complete / disabled. Caller passes the result to
    register_signed_agreement()."""
    if not _is_enabled():
        raise ESignError("E-sign integrations are disabled on this codebase.")
    if envelope.provider == "dokobit":
        return _dokobit_fetch(envelope)
    return _docusign_fetch(envelope)


# ---------------------------------------------------------------------------
# Completion path — pure DB + local storage, runs everywhere, TESTED.
# ---------------------------------------------------------------------------
def register_signed_agreement(
    session, loan, *, content: bytes, filename: str, mime_type: str,
    uploaded_by: Optional[str] = None, reason: str = "esign_complete",
) -> LoanDocument:
    """Store a completed signed artifact as the loan's canonical AGREEMENT and
    lock any generated draft. Mirrors what the manual upload route does, so the
    e-sign and external paths converge on the same state. Caller commits.

    Accepts PDF (DocuSign) or .asice/.bdoc (Dokobit/eID) — validated by
    documents.store_upload."""
    stored = documents.store_upload(loan.id, content=content, filename=filename, mime_type=mime_type)
    doc = LoanDocument(
        loan_id=loan.id,
        document_type=DocumentType.AGREEMENT,
        filename=filename,
        storage_path=stored.storage_path,
        sha256_hash=stored.sha256_hash,
        size_bytes=stored.size_bytes,
        mime_type=stored.mime_type,
        description="Signed via e-sign provider",
        uploaded_by=uploaded_by or "esign",
    )
    session.add(doc)
    session.flush()
    loan.agreement_document_path = stored.storage_path
    agreements.lock_drafts(session, loan.id, reason)
    log.info("esign_signed_agreement_registered loan_id=%s doc_id=%s mime=%s", loan.id, doc.id, stored.mime_type)
    return doc


# ---------------------------------------------------------------------------
# Provider plumbing — documented REST shapes; validate live before production.
# ---------------------------------------------------------------------------
def _dokobit_creds() -> str:
    token = os.environ.get("DOKOBIT_API_TOKEN", "").strip()
    if not token:
        raise ESignError("DOKOBIT_API_TOKEN not set.")
    return token


def _dokobit_base() -> str:
    # Production gateway.dokobit.com; sandbox gateway-sandbox.dokobit.com
    return os.environ.get("DOKOBIT_BASE_URL", "https://gateway.dokobit.com").rstrip("/")


def _dokobit_send(loan, pdf_content: bytes, signers: list[dict]) -> ESignEnvelope:
    """Create a Dokobit signing task. The signed result is an ASiC-E container.
    Ref: Dokobit Gateway 'POST /signing/create.json?access_token=...'."""
    import base64
    import httpx
    token = _dokobit_creds()
    payload = {
        "type": "pdf",
        "name": (loan.contract_reference or f"loan-{loan.id}") + ".pdf",
        "files": [{"name": f"loan-{loan.id}.pdf", "digest": None,
                   "content": base64.b64encode(pdf_content).decode()}],
        "signers": [
            {"name": s.get("name", ""), "code": s.get("personal_code", ""),
             "phone": s.get("phone", ""), "email": s.get("email", "")}
            for s in signers
        ],
    }
    url = f"{_dokobit_base()}/signing/create.json?access_token={token}"
    try:
        r = httpx.post(url, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        raise ESignError(f"Dokobit create failed: {e}")
    token_id = data.get("token") or data.get("signing_token") or ""
    return ESignEnvelope(provider="dokobit", envelope_id=token_id,
                         status=data.get("status", "created"),
                         signing_url=data.get("url"), raw=data)


def _dokobit_fetch(envelope: ESignEnvelope) -> tuple[bytes, str, str]:
    import base64
    import httpx
    token = _dokobit_creds()
    url = f"{_dokobit_base()}/signing/{envelope.envelope_id}/status.json?access_token={token}"
    try:
        r = httpx.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        raise ESignError(f"Dokobit status failed: {e}")
    if data.get("status") != "signed":
        raise ESignError(f"Dokobit envelope not complete (status={data.get('status')}).")
    b64 = data.get("file", {}).get("content") if isinstance(data.get("file"), dict) else data.get("content")
    if not b64:
        raise ESignError("Dokobit response had no signed container content.")
    return base64.b64decode(b64), f"signed-loan.asice", "application/vnd.etsi.asic-e+zip"


def _docusign_send(loan, pdf_content: bytes, signers: list[dict]) -> ESignEnvelope:
    """Create + send a DocuSign envelope (JWT-authed). Returns a signed PDF on
    completion. Ref: DocuSign eSignature REST 'POST /v2.1/accounts/{acct}/envelopes'.
    JWT grant + envelope payload construction to be finalized on the clone."""
    raise ESignError(
        "DocuSign send is scaffolded but not yet wired (JWT grant + envelope "
        "payload need finalizing on Rasmus's clone with real credentials)."
    )


def _docusign_fetch(envelope: ESignEnvelope) -> tuple[bytes, str, str]:
    raise ESignError("DocuSign fetch is scaffolded but not yet wired.")
