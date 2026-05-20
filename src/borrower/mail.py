"""
Magic-link email delivery (governance.md §5 / Phase 3 prod).

The default in development is to print magic links to stdout (see the
DEV_LOG_MAGIC_LINKS flag in admin_auth.py / lender_portal/auth.py). In
production, set SMTP env vars and this module sends real emails. The
auth modules call `send_magic_link(...)` and don't care which mode is on —
the mode is decided here from the env.

Env vars (`.env`):
    SMTP_HOST      — required to enable real sending; if unset, no-ops
    SMTP_PORT      — default 587
    SMTP_USER      — username (often the same as the From address)
    SMTP_PASS      — password / app password
    SMTP_FROM      — From header, e.g. "MesiCap <no-reply@mesicap.com>"
    SMTP_USE_SSL   — "1" to use implicit SSL on connect (default uses STARTTLS)
    SMTP_TIMEOUT   — seconds, default 15
"""
from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional

from src.core.logger import get_logger


log = get_logger(__name__)


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    user: Optional[str]
    password: Optional[str]
    from_addr: str
    use_ssl: bool
    timeout: float


def _read_config() -> Optional[SmtpConfig]:
    """Return a SmtpConfig if SMTP_HOST is set, else None (no-op mode)."""
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        return None
    try:
        port = int(os.environ.get("SMTP_PORT", "587"))
    except ValueError:
        port = 587
    return SmtpConfig(
        host=host,
        port=port,
        user=(os.environ.get("SMTP_USER") or None),
        password=(os.environ.get("SMTP_PASS") or None),
        from_addr=os.environ.get("SMTP_FROM", "MesiCap <no-reply@mesicap.com>"),
        use_ssl=os.environ.get("SMTP_USE_SSL", "0") == "1",
        timeout=float(os.environ.get("SMTP_TIMEOUT", "15")),
    )


def is_configured() -> bool:
    return _read_config() is not None


def send_email(to_addr: str, subject: str, body_text: str, body_html: Optional[str] = None) -> dict:
    """Send a single email. Returns {sent: bool, reason: str | None}.

    No-ops with sent=False, reason='smtp_not_configured' when SMTP env is
    missing — callers should still have logged the magic-link payload
    elsewhere (admin_auth / lender_portal/auth do this in dev mode)."""
    cfg = _read_config()
    if cfg is None:
        return {"sent": False, "reason": "smtp_not_configured"}

    msg = EmailMessage()
    msg["From"] = cfg.from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    try:
        if cfg.use_ssl:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.host, cfg.port, context=ctx, timeout=cfg.timeout) as s:
                if cfg.user and cfg.password:
                    s.login(cfg.user, cfg.password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout) as s:
                s.ehlo()
                try:
                    s.starttls(context=ssl.create_default_context())
                    s.ehlo()
                except smtplib.SMTPNotSupportedError:
                    pass  # plain text (LAN relay) — proceed
                if cfg.user and cfg.password:
                    s.login(cfg.user, cfg.password)
                s.send_message(msg)
    except Exception as e:
        log.error("smtp_send_failed", error=str(e), to=to_addr)
        return {"sent": False, "reason": f"smtp_send_failed: {e}"}

    log.info("smtp_send_ok", to=to_addr, subject=subject)
    return {"sent": True, "reason": None}


def send_magic_link(to_addr: str, magic_url_path: str, *, surface: str, base_url: str = "") -> dict:
    """Send a magic-link email for `surface` in {"admin", "lender"}.

    `magic_url_path` is the absolute path (e.g., "/borrower/magic/TOKEN" or
    "/lenders/magic/TOKEN"). `base_url` is the host URL to prepend if you have
    one (e.g., "https://mesicap.com"); when empty, the email contains the
    relative path which the recipient must paste into their browser.
    """
    href = (base_url.rstrip("/") + magic_url_path) if base_url else magic_url_path
    if surface == "admin":
        subject = "Your sign-in link for MesiCap (admin)"
        intro = "You requested a sign-in link to the MesiCap loan-portfolio admin."
    else:
        subject = "Your sign-in link for MesiCap"
        intro = "You requested a sign-in link to view your loans with MesiCap."
    text = (
        f"{intro}\n\n"
        f"Open this link in your browser (valid for 15 minutes):\n\n"
        f"  {href}\n\n"
        f"If you did not request this, you can ignore the message.\n"
        f"— MesiCap Technologies OÜ"
    )
    return send_email(to_addr, subject, text)
