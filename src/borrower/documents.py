"""
Loan document storage helpers.

Filesystem layout:
    data/contracts/{loan_id}/{uuid}.{ext}

UUID-based storage paths avoid filename collisions when two uploads share the
same original name. The original filename is preserved separately on the
LoanDocument row for display.

PDF-only by default (LEGAL_CONTEXT.md sets a high bar for the legal-truth
layer — we want a single canonical format for archived contracts). Size cap
is 10 MB per file. Adjust at the top of this module if requirements change.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


CONTRACTS_ROOT = Path("data/contracts")
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_MIME = {"application/pdf"}
ALLOWED_EXT = {".pdf"}


class DocumentValidationError(ValueError):
    """Raised when an uploaded file fails validation. Message is safe to surface."""


@dataclass(frozen=True)
class StoredDocument:
    storage_path: str    # relative to repo root, e.g. "data/contracts/4/a1b2…f9.pdf"
    sha256_hash: str
    size_bytes: int
    mime_type: str


def store_upload(loan_id: int, *, content: bytes, filename: str, mime_type: str) -> StoredDocument:
    """Validate + persist a file to disk under data/contracts/{loan_id}/.

    Raises DocumentValidationError on validation failure. On success returns a
    StoredDocument with the relative path, hash, and size. The caller is
    responsible for creating the LoanDocument row referencing the returned
    path.
    """
    if not content:
        raise DocumentValidationError("Empty file uploaded.")
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise DocumentValidationError(
            f"File too large ({len(content):,} bytes). Limit is {MAX_FILE_SIZE_BYTES // (1024*1024)} MB."
        )

    # MIME validation
    mime_norm = (mime_type or "").lower().strip()
    if mime_norm not in ALLOWED_MIME:
        raise DocumentValidationError(
            f"Unsupported file type: {mime_type or 'unknown'}. Only PDF is accepted."
        )

    # Extension check on the original filename (defense in depth)
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise DocumentValidationError(f"Filename must end in {sorted(ALLOWED_EXT)}.")

    # Magic-byte check: PDFs start with %PDF
    if not content.startswith(b"%PDF"):
        raise DocumentValidationError("File does not look like a PDF (missing %PDF header).")

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
        mime_type=mime_norm,
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
