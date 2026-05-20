"""
Magic-link auth for the lender portal.

No passwords. The flow:
1. User enters email at /lenders/login
2. Backend issues a single-use token; hashes it; stores hash + expiry on PortalUser
3. Sends a link `/lenders/magic/{token}` by email
   (dev mode: prints to server log instead — see DEV_LOG_MAGIC_LINKS)
4. Token consumer creates a PortalSession; sets session cookie; redirects to /lenders/
5. Subsequent requests look up the session by cookie's token

See docs/governance.md §5.7 for the security rules this module enforces.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request, Response
from src.borrower.models import PortalSession, PortalUser, get_session_factory


@dataclass(frozen=True)
class AuthedUser:
    """Plain snapshot of the authenticated portal user. Avoids the
    SQLAlchemy detached-instance trap by carrying only the fields callers
    actually need."""
    id: int
    email: str
    counterparty_id: int


# === Config ===

MAGIC_LINK_TTL = timedelta(minutes=15)
SESSION_TTL = timedelta(days=30)
SESSION_COOKIE = "mesicap_lender_session"

# Dev mode: print the magic link to stdout instead of sending email.
# Production sets this to False and configures SMTP (out of scope today).
DEV_LOG_MAGIC_LINKS = os.environ.get("LENDER_PORTAL_PROD") != "1"

# Rate limit: max magic-link requests per email per hour (governance.md §5.5).
MAGIC_LINK_RATE_LIMIT_PER_HOUR = 3


def _hash(token: str) -> str:
    """SHA-256 hex digest. Tokens are 256-bit random so SHA-256 is safe here."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    """URL-safe 256-bit random token."""
    return secrets.token_urlsafe(32)


_BrunoSession = get_session_factory()


# === Magic link issue ===

def request_magic_link(email: str, request: Optional[Request] = None) -> dict:
    """
    Look up the PortalUser by email and issue a magic link. Returns a dict with
    keys: status ('sent' | 'rate_limited' | 'unknown_email' | 'locked'), and on
    'sent' also includes 'magic_url' (only when DEV_LOG_MAGIC_LINKS is true,
    so the dev can copy it from the API response or the server log).
    Always returns 'sent' for unknown emails in prod to avoid enumeration —
    but in dev we surface the truth so test loops are fast.
    """
    email_norm = (email or "").strip().lower()
    if not email_norm:
        return {"status": "unknown_email"}

    session = _BrunoSession()
    try:
        user = session.query(PortalUser).filter(PortalUser.email == email_norm).first()
        if user is None:
            if DEV_LOG_MAGIC_LINKS:
                print(f"[lender-portal] magic-link request for unknown email: {email_norm}")
                return {"status": "unknown_email"}
            return {"status": "sent"}  # don't leak enumeration in prod

        if user.locked_at is not None:
            return {"status": "locked"}

        # Rate limit: count magic-link issues for this user in the last hour
        now = datetime.utcnow()
        if user.magic_link_sent_at and (now - user.magic_link_sent_at) < timedelta(hours=1):
            # Allow at most MAGIC_LINK_RATE_LIMIT_PER_HOUR sends per hour.
            # We don't have a per-event log so we approximate: if a fresh send is
            # within 20 minutes of the previous, reject. Looser than ideal but
            # sufficient for the threat (someone spam-clicking the form).
            if (now - user.magic_link_sent_at) < timedelta(minutes=20):
                return {"status": "rate_limited"}

        token = _new_token()
        user.magic_link_token_hash = _hash(token)
        user.magic_link_expires_at = now + MAGIC_LINK_TTL
        user.magic_link_sent_at = now
        session.commit()

        magic_url = f"/lenders/magic/{token}"
        if DEV_LOG_MAGIC_LINKS:
            print(f"[lender-portal] magic link for {email_norm}: {magic_url} (expires {user.magic_link_expires_at}Z)")

        out = {"status": "sent"}
        if DEV_LOG_MAGIC_LINKS:
            out["magic_url"] = magic_url
        return out
    finally:
        session.close()


# === Magic link consume → session ===

def consume_magic_link(token: str, request: Optional[Request] = None) -> Optional[str]:
    """
    Consume a magic-link token. Returns a new session token (raw, to be set as
    a cookie) on success, or None if the token is invalid / expired / locked.
    """
    if not token:
        return None
    h = _hash(token)
    session = _BrunoSession()
    try:
        user = session.query(PortalUser).filter(PortalUser.magic_link_token_hash == h).first()
        if user is None:
            return None
        if user.locked_at is not None:
            return None
        if user.magic_link_expires_at is None or user.magic_link_expires_at < datetime.utcnow():
            return None

        # Consume the link (single-use)
        user.magic_link_token_hash = None
        user.magic_link_expires_at = None
        user.last_login_at = datetime.utcnow()

        # Create session
        session_token = _new_token()
        ip = ua = None
        if request is not None:
            try:
                ip = request.client.host if request.client else None
                ua = request.headers.get("user-agent")
            except Exception:
                pass
        ps = PortalSession(
            portal_user_id=user.id,
            session_token_hash=_hash(session_token),
            created_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + SESSION_TTL,
            last_seen_at=datetime.utcnow(),
            ip_address=ip,
            user_agent=ua,
        )
        session.add(ps)
        session.commit()
        return session_token
    finally:
        session.close()


# === Session lookup (request → user) ===

def current_user(request: Request) -> Optional[AuthedUser]:
    """
    Look up the AuthedUser bound to this request's session cookie. Returns
    None if no cookie, cookie invalid, session expired, or user locked.
    Touches last_seen_at on hit.

    Returns a plain AuthedUser snapshot (not an ORM instance) so callers
    can hold it across their own DB session without triggering detached-
    instance errors.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    h = _hash(token)
    session = _BrunoSession()
    try:
        ps = session.query(PortalSession).filter(PortalSession.session_token_hash == h).first()
        if ps is None:
            return None
        if ps.expires_at < datetime.utcnow():
            session.delete(ps)
            session.commit()
            return None
        user = session.query(PortalUser).filter(PortalUser.id == ps.portal_user_id).first()
        if user is None or user.locked_at is not None:
            return None
        snap = AuthedUser(id=user.id, email=user.email, counterparty_id=user.counterparty_id)
        ps.last_seen_at = datetime.utcnow()
        session.commit()
        return snap
    finally:
        session.close()


def set_session_cookie(response: Response, session_token: str) -> None:
    """Set the session cookie in a secure-by-default way."""
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=not DEV_LOG_MAGIC_LINKS,  # dev runs over http; prod must be https
        path="/lenders",
    )


def clear_session(request: Request, response: Response) -> None:
    """Delete the server-side session row and clear the cookie."""
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        h = _hash(token)
        session = _BrunoSession()
        try:
            ps = session.query(PortalSession).filter(PortalSession.session_token_hash == h).first()
            if ps:
                session.delete(ps)
                session.commit()
        finally:
            session.close()
    response.delete_cookie(SESSION_COOKIE, path="/lenders")
