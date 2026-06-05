"""
Loan document storage helpers.

Filesystem layout:
    data/contracts/{loan_id}/{uuid}.{ext}

UUID-based storage paths avoid filename collisions when two uploads share the
same original name. The original filename is preserved separately on the
LoanDocument row for display.

Accepted formats for the legal-truth layer (LEGAL_CONTEXT.md sets a high bar —
we want canonical, verifiable signed artifacts):

  - PDF                          — DocuSign output, scans, plain signed PDFs
  - ASiC-E / BDOC (.asice/.sce/  — Estonian/EU digital-signature containers
    .bdoc)                         (Smart-ID, Mobiil-ID, ID-card; Dokobit, DigiDoc)
  - DDOC (.ddoc)                 — legacy Estonian XML signature container

These container types are how Estonian eID-signed agreements arrive, so the
already-signed shareholder loans can be uploaded as-is. Validation is by
extension + magic bytes — browsers send unreliable MIME for .asice/.bdoc
(often application/octet-stream or application/zip), so we do NOT trust the
upload's Content-Type; the canonical MIME is derived from the extension.

Size cap is 10 MB per file. Adjust at the top of this module if needed.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


CONTRACTS_ROOT = Path("data/contracts")
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


def _is_pdf(b: bytes) -> bool:
    return b.startswith(b"%PDF")


def _is_zip(b: bytes) -> bool:
    # ASiC-E / BDOC 2.x are ZIP containers. PK\x03\x04 = local file header;
    # PK\x05\x06 = empty archive end-of-central-directory.
    return b.startswith(b"PK\x03\x04") or b.startswith(b"PK\x05\x06")


def _is_xml(b: bytes) -> bool:
    return b.lstrip()[:5].lower().startswith(b"<?xml")


# ext -> (canonical mime, magic-byte validator). Validation is ext+magic;
# the upload's declared MIME is ignored for the container types.
_FORMATS = {
    ".pdf":   ("application/pdf", _is_pdf),
    ".asice": ("application/vnd.etsi.asic-e+zip", _is_zip),
    ".sce":   ("application/vnd.etsi.asic-e+zip", _is_zip),
    ".bdoc":  ("application/vnd.etsi.asic-e+zip", _is_zip),
    ".ddoc":  ("application/x-ddoc", _is_xml),
}
ALLOWED_EXT = set(_FORMATS)


class DocumentValidationError(ValueError):
    """Raised when an uploaded file fails validation. Message is safe to surface."""


@dataclass(frozen=True)
class StoredDocument:
    storage_path: str    # relative to repo root, e.g. "data/contracts/4/a1b2…f9.asice"
    sha256_hash: str
    size_bytes: int
    mime_type: str


def store_upload(loan_id: int, *, content: bytes, filename: str, mime_type: str) -> StoredDocument:
    """Validate + persist a signed-document upload under data/contracts/{loan_id}/.

    Accepts PDF and Estonian/EU signature containers (.asice/.sce/.bdoc/.ddoc).
    Validation is by extension + magic bytes (the declared `mime_type` is not
    trusted for containers). Raises DocumentValidationError on failure. On
    success returns a StoredDocument; the caller creates the LoanDocument row.
    """
    if not content:
        raise DocumentValidationError("Empty file uploaded.")
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise DocumentValidationError(
            f"File too large ({len(content):,} bytes). Limit is {MAX_FILE_SIZE_BYTES // (1024*1024)} MB."
        )

    ext = Path(filename or "").suffix.lower()
    fmt = _FORMATS.get(ext)
    if fmt is None:
        raise DocumentValidationError(
            "Unsupported file type. Accepted: PDF, or a signed container "
            "(.asice / .sce / .bdoc / .ddoc)."
        )
    canonical_mime, magic_ok = fmt
    if not magic_ok(content):
        raise DocumentValidationError(
            f"File contents don't match a {ext} document (failed signature/magic-byte check)."
        )

    # Persist
    target_dir = CONTRACTS_ROOT / str(loan_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    uuid_name = uuid.uuid4().hex + ext
    target_path = target_dir / uuid_name
    target_path.write_bytes(content)

    digest = hashlib.sha256(content).hexdigest()
    return StoredDocument(
        storage_path=str(target_path),
        sha256_hash=digest,
        size_bytes=len(content),
        mime_type=canonical_mime,
    )


def read_for_download(storage_path: str) -> Optional[Path]:
    """Resolve a stored document path to a real file. Returns None if missing
    or if the path tries to escape CONTRACTS_ROOT."""
    p = Path(storage_path).resolve()
    root = CONTRACTS_ROOT.resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    if not p.exists() or not p.is_file():
        return None
    return p


def delete_file(storage_path: str) -> bool:
    """Remove a stored document file. Returns True on success, False if not
    found or outside CONTRACTS_ROOT. Safe to call before/after the DB row
    deletion; the caller decides ordering."""
    p = read_for_download(storage_path)
    if p is None:
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False
