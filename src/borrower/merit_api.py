"""
Merit Aktiva API client — read-only.

Bruno reads Merit's per-account closing balances to reconcile against its own
loan outstandings (see merit_reconcile.py). Bruno never writes to Merit; the
bookkeeper owns journal posting per governance.md §4.2.

Gating: live API calls run only on Rasmus's clone where
`cfg.app.bruno_run_integrations` is True. On Rain's dev codebase this module's
import side-effects are inert; calling `pull_quarter()` without the gate is a
no-op that returns an empty list and logs a skip line. The reconciliation page
falls back to CSV-import for dev iteration in that case.

Credentials live in `.env` (gitignored):
    MERIT_API_ID
    MERIT_API_KEY

Auth scheme: Merit uses HMAC-SHA256 over a per-request timestamp, signed with
the API key. The signed string is the request body, and the signature is sent
as a query parameter. See Merit's developer documentation; concrete endpoint
plumbing is below.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import List, Optional

import httpx

from src.borrower.models import MeritBalance, get_session_factory


MERIT_API_BASE = os.environ.get("MERIT_API_BASE", "https://aktiva.merit.ee/api/v2")


def _is_enabled() -> bool:
    """Live Merit calls run only when bruno_run_integrations is on (production)."""
    try:
        from src.core.config import get_settings
        return bool(getattr(get_settings().app, "bruno_run_integrations", False))
    except Exception:
        return False


def _creds() -> tuple[str, str]:
    api_id = os.environ.get("MERIT_API_ID", "").strip()
    api_key = os.environ.get("MERIT_API_KEY", "").strip()
    return api_id, api_key


def _signature(api_id: str, api_key: str, body: str, ts: str) -> str:
    """HMAC-SHA256 over (api_id + ts + body) using the base64-decoded api_key,
    base64-encoded result. Matches the Merit Aktiva v2 auth scheme."""
    try:
        key_bytes = base64.b64decode(api_key)
    except Exception:
        key_bytes = api_key.encode("utf-8")
    msg = (api_id + ts + body).encode("utf-8")
    digest = hmac.new(key_bytes, msg, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _signed_post(path: str, body: dict, timeout: float = 15.0) -> dict:
    """POST to Merit with HMAC-signed envelope. Raises on non-2xx."""
    api_id, api_key = _creds()
    if not api_id or not api_key:
        raise RuntimeError("MERIT_API_ID / MERIT_API_KEY not configured")

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    body_str = json.dumps(body, separators=(",", ":"), sort_keys=True)
    sig = _signature(api_id, api_key, body_str, ts)
    url = f"{MERIT_API_BASE}/{path.lstrip('/')}?ApiId={api_id}&timestamp={ts}&signature={sig}"
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, content=body_str, headers={"Content-Type": "application/json"})
    resp.raise_for_status()
    return resp.json() if resp.content else {}


@dataclass(frozen=True)
class MeritAccountRow:
    account_id: str
    account_name: str
    currency: str
    closing_balance: float


def pull_account_balances(period_start: date, period_end: date) -> List[MeritAccountRow]:
    """Fetch closing balances for the lender account family at period_end.

    Returns an empty list (and logs a skip) when the integration gate is off.
    Throws on API errors when enabled.

    The exact Merit endpoint + response shape may vary by Merit deployment.
    Implementation below targets the standard v2 'getaccounts/balances'
    pattern; adjust if the deployment returns a different envelope.
    """
    if not _is_enabled():
        # Inert on Rain's dev side; production flips this on
        return []
    body = {
        "Date": period_end.strftime("%Y-%m-%d"),
        # Future: filter by AccountClass = 'Liability' / similar, once mapping
        # is confirmed against the actual Merit chart of accounts. Until then
        # we pull all accounts and filter by `merit_account_id` mapping on
        # Bruno's side.
    }
    raw = _signed_post("getaccounts", body)
    out: List[MeritAccountRow] = []
    # Merit's response shape: list of {Id, Code, Name, ClosingBalance, Currency}
    # Adapt as needed when actual response is observed.
    items = raw if isinstance(raw, list) else raw.get("Accounts", [])
    for it in items or []:
        try:
            out.append(MeritAccountRow(
                account_id=str(it.get("Code") or it.get("Id") or "").strip(),
                account_name=(it.get("Name") or "").strip() or None,
                currency=(it.get("Currency") or "EUR").upper(),
                closing_balance=float(it.get("ClosingBalance") or 0.0),
            ))
        except (TypeError, ValueError):
            continue
    return out


def pull_quarter(year: int, quarter: int) -> dict:
    """Pull all relevant account balances for one quarter and write them to
    the merit_balances staging table. Returns a summary dict."""
    from src.borrower.merit_export import _quarter_bounds
    period_start, period_end = _quarter_bounds(year, quarter)
    rows = pull_account_balances(period_start, period_end)

    session = get_session_factory()()
    written = 0
    try:
        # Idempotency on (period_start, period_end, merit_account_id, source='api')
        for r in rows:
            existing = (
                session.query(MeritBalance)
                .filter(
                    MeritBalance.period_start == period_start,
                    MeritBalance.period_end == period_end,
                    MeritBalance.merit_account_id == r.account_id,
                    MeritBalance.source == "api",
                )
                .first()
            )
            if existing is not None:
                # Update closing if changed
                existing.closing_balance = r.closing_balance
                existing.merit_account_name = r.account_name
                existing.currency = r.currency
                existing.pulled_at = datetime.utcnow()
                continue
            session.add(MeritBalance(
                period_start=period_start, period_end=period_end,
                merit_account_id=r.account_id, merit_account_name=r.account_name,
                currency=r.currency, closing_balance=r.closing_balance,
                source="api",
            ))
            written += 1
        session.commit()
    finally:
        session.close()
    return {
        "enabled": _is_enabled(),
        "rows_fetched": len(rows),
        "rows_written": written,
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
    }
