"""
Standard Books (by Excellent / HansaWorld) REST client for posting journals.

Create semantics (REST API v2):
    POST http://host/api/{company}/{register}
         ?set_field.{Field}=...&set_row_field.{n}.{Field}=...
    Basic auth (http://user:pass@host) or OAuth Bearer.
The server returns XML: <data register=".." sequence=".." url="/api/..">.

DRY-RUN (default): nothing is sent. `post_journal` renders the exact param set
it would POST and returns a synthetic result, so the journals can be reviewed
before any credentials exist.

⚠ FIELD NAMES: the TRBlock header/row field names below (TransDate, Comment,
RefStr; row Account/Debit/Credit/Stp) are the standard HansaWorld Transaction
fields, but exact spelling can vary by version. Before going live, GET one
existing TR record from the target server and reconcile names — see
`describe_register()`.
"""
from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Tuple

import requests

from src.bookkeeping.config import BookkeepingConfig
from src.bookkeeping.journal import JournalEntry
from src.core.logger import get_logger

log = get_logger(__name__)

# TRBlock field mapping — adjust per the target server's schema (see module note).
HDR_DATE = "TransDate"
HDR_COMMENT = "Comment"
HDR_REFERENCE = "RefStr"      # external idempotency key lands here
ROW_ACCOUNT = "Account"
ROW_DEBIT = "Debit"
ROW_CREDIT = "Credit"
ROW_TEXT = "Stp"


@dataclass
class PostResult:
    ok: bool
    reference: str
    dry_run: bool
    url: str = ""             # server record URL on success
    sequence: str = ""
    error: str = ""
    params: Dict[str, str] | None = None   # populated in dry-run for inspection


class StandardBooksClient:
    def __init__(self, cfg: BookkeepingConfig):
        self.cfg = cfg
        self.sb = cfg.standard_books

    # ── URL / params ────────────────────────────────────────────────────
    def _endpoint(self, register: str | None = None) -> str:
        reg = register or self.sb.transaction_register
        return f"{self.sb.base_url}/api/{self.sb.company}/{reg}"

    def _journal_params(self, je: JournalEntry) -> List[Tuple[str, str]]:
        """Build the ordered (key, value) param list for one journal POST."""
        params: List[Tuple[str, str]] = [
            (f"set_field.{HDR_DATE}", je.date),
            (f"set_field.{HDR_COMMENT}", je.comment),
            (f"set_field.{HDR_REFERENCE}", je.reference),
        ]
        for i, row in enumerate(je.rows):
            params.append((f"set_row_field.{i}.{ROW_ACCOUNT}", row.account))
            if row.debit:
                params.append((f"set_row_field.{i}.{ROW_DEBIT}", f"{row.debit:.2f}"))
            if row.credit:
                params.append((f"set_row_field.{i}.{ROW_CREDIT}", f"{row.credit:.2f}"))
            if row.text:
                params.append((f"set_row_field.{i}.{ROW_TEXT}", row.text))
        return params

    # ── posting ─────────────────────────────────────────────────────────
    def post_journal(self, je: JournalEntry) -> PostResult:
        params = self._journal_params(je)
        param_dict = dict(params)

        if self.cfg.dry_run or not self.cfg.has_live_credentials:
            return PostResult(
                ok=True, reference=je.reference, dry_run=True, params=param_dict,
            )

        url = self._endpoint()
        try:
            resp = requests.post(
                url,
                params=params,
                auth=(self.sb.username, self.sb.password),
                timeout=30,
            )
            resp.raise_for_status()
            rec_url, seq = _parse_post_response(resp.text)
            log.info("journal_posted", reference=je.reference, url=rec_url, seq=seq)
            return PostResult(
                ok=True, reference=je.reference, dry_run=False,
                url=rec_url, sequence=seq,
            )
        except Exception as e:
            log.warning("journal_post_failed", reference=je.reference, error=str(e))
            return PostResult(ok=False, reference=je.reference, dry_run=False, error=str(e))

    def describe_register(self, register: str | None = None, limit: int = 1) -> str:
        """GET a few existing records (raw XML) to reconcile field names.

        Live-only helper for setup; returns '' in dry-run / without credentials.
        """
        if self.cfg.dry_run or not self.cfg.has_live_credentials:
            return ""
        url = self._endpoint(register)
        resp = requests.get(
            url, params={"range": f"1:{limit}"},
            auth=(self.sb.username, self.sb.password), timeout=30,
        )
        resp.raise_for_status()
        return resp.text

    # ── dry-run rendering ───────────────────────────────────────────────
    def render_dry_run(self, je: JournalEntry) -> str:
        """A human-readable T-account view + the literal request line."""
        lines = [
            f"  {je.date}  {je.comment}",
            f"    ref={je.reference}  [{je.source_kind}]  ({je.currency})",
        ]
        for r in je.rows:
            dr = f"{r.debit:>12,.2f}" if r.debit else " " * 12
            cr = f"{r.credit:>12,.2f}" if r.credit else " " * 12
            lines.append(f"      {r.account:<22} Dr {dr}  Cr {cr}   {r.text}")
        lines.append(
            f"      {'= TOTAL':<22}    {je.total_debit:>12,.2f}     {je.total_credit:>12,.2f}"
            + ("   ✓balanced" if je.is_balanced() else "   ✗UNBALANCED")
        )
        params = self._journal_params(je)
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params)
        lines.append(f"    → POST {self._endpoint()}?{qs}")
        return "\n".join(lines)


def _parse_post_response(text: str) -> Tuple[str, str]:
    """Pull the record url + sequence from a v2 <data ...> response."""
    try:
        root = ET.fromstring(text)
        data = root if root.tag == "data" else root.find(".//data")
        if data is not None:
            return data.get("url", ""), data.get("sequence", "")
    except Exception:
        pass
    return "", ""
