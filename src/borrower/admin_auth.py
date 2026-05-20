"""
Admin-side magic-link auth for `/borrower/*` (MesiCap principals).

Mirrors src/lender_portal/auth.py but on PrincipalUser + PrincipalSession
tables and with its own cookie. Lender-side and admin-side sessions are
independent — logging in to one does not grant access to the other.

See docs/governance.md §5.7 (auth pattern) and CLAUDE.md.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Request, Response

from src.borrower.models import PrincipalSession, PrincipalUser, get_session_factory


MAGIC_LINK_TTL = timedelta(minutes=15)
SESSION_TTL = timedelta(days=30)
SESSION_COOKIE = "mesicap_admin_session"

# Dev mode: print the magic link to stdout instead of sending email.
DEV_LOG_MAGIC_LINKS = os.environ.get("BRUNO_ADMIN_PROD") != "1"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


_BrunoSession = get_session_factory()


@dataclass(frozen=True)
class AuthedPrincipal:
    """Plain snapshot of an authenticated admin user. Detached from any DB
    session so callers can pass it through their own session boundaries."""
    id: int
    email: str
    name: str


def request_magic_link(email: str, request: Optional[Request] = None) -> dict:
    """Issue an admin magic-link token. Behavior mirrors the lender flow."""
    email_norm = (email or "").strip().lower()
    if not email_norm:
        return {"status": "unknown_email"}

    session = _BrunoSession()
    try:
        user = session.query(PrincipalUser).filter(PrincipalUser.email == email_norm).first()
        if user is None:
            if DEV_LOG_MAGIC_LINKS:
                print(f"[bruno-admin] magic-link request for unknown email: {email_norm}")
                return {"status": "unknown_email"}
            return {"status": "sent"}

        if user.locked_at is not None:
            return {"status": "locked"}

        now = datetime.utcnow()
        if user.magic_link_sent_at and (now - user.magic_link_sent_at) < timedelta(minutes=20):
            return {"status": "rate_limited"}

        token = _new_token()
        user.magic_link_token_hash = _hash(token)
        user.magic_link_expires_at = now + MAGIC_LINK_TTL
        user.magic_link_sent_at = now
        session.commit()

        magic_url = f"/borrower/magic/{token}"
        if DEV_LOG_MAGIC_LINKS:
            print(f"[bruno-admin] magic link for {email_norm}: {magic_url} (expires {user.magic_link_expires_at}Z)")

        out = {"status": "sent"}
        if DEV_LOG_MAGIC_LINKS:
            out["magic_url"] = magic_url
        return out
    finally:
        session.close()


def consume_magic_link(token: str, request: Optional[Request] = None) -> Optional[str]:
    """Consume a magic-link token. Returns a fresh session token on success."""
    if not token:
        return None
    h = _hash(token)
    session = _BrunoSession()
    try:
        user = session.query(PrincipalUser).filter(PrincipalUser.magic_link_token_hash == h).first()
        if user is None or user.locked_at is not None:
            return None
        if user.magic_link_expires_at is None or user.magic_link_expires_at < datetime.utcnow():
            return None

        user.magic_link_token_hash = None
        user.magic_link_expires_at = None
        user.last_login_at = datetime.utcnow()

        session_token = _new_token()
        ip = ua = None
        if request is not None:
            try:
                ip = request.client.host if request.client else None
                ua = request.headers.get("user-agent")
            except Exception:
                pass
        ps = PrincipalSession(
            principal_user_id=user.id,
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


def current_principal(request: Request) -> Optional[AuthedPrincipal]:
    """Resolve the request's cookie to an AuthedPrincipal snapshot. None if
    no cookie, cookie invalid, session expired, or user locked."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    h = _hash(token)
    session = _BrunoSession()
    try:
        ps = session.query(PrincipalSession).filter(PrincipalSession.session_token_hash == h).first()
        if ps is None:
            return None
        if ps.expires_at < datetime.utcnow():
            session.delete(ps)
            session.commit()
            return None
        user = session.query(PrincipalUser).filter(PrincipalUser.id == ps.principal_user_id).first()
        if user is None or user.locked_at is not None:
            return None
        snap = AuthedPrincipal(id=user.id, email=user.email, name=user.name)
        ps.last_seen_at = datetime.utcnow()
        session.commit()
        return snap
    finally:
        session.close()


def set_session_cookie(response: Response, session_token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=session_token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=not DEV_LOG_MAGIC_LINKS,
        path="/",  # admin spans /borrower/* and (eventually) other admin paths
    )


def clear_session(request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        h = _hash(token)
        session = _BrunoSession()
        try:
            ps = session.query(PrincipalSession).filter(PrincipalSession.session_token_hash == h).first()
            if ps:
                session.delete(ps)
                session.commit()
        finally:
            session.close()
    response.delete_cookie(SESSION_COOKIE, path="/")
