"""
Standalone FastAPI app for the lender portal.

In production this runs as its own systemd-managed uvicorn process on
octoserver port 8001, served by caddy at `lender.mesicap.com`. The Bruno
admin side (`/borrower/*`) keeps living on the existing trading-dashboard
process — see governance.md §5.4 for the blast-radius rationale.

The two processes share `data/bruno.db` via SQLite (WAL handles concurrent
access). The lender process only writes `portal_sessions`,
`portal_users.last_login_at`, `contact_update_requests` — never the loan /
movement / payment / counterparty tables. This is a code invariant, not a
DB-permissions one (SQLite has no roles); the banned-terminology lint
catches one common slip, and the `_require_loan_owned_by_user` helper is
the only ownership gate in the router.

Run locally:
    .venv/bin/uvicorn src.lender_portal.standalone_app:create_lender_app \\
        --factory --host 127.0.0.1 --port 8001
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

# Load config/.env if present — same pattern as the trader (src/core/config.py).
# This lets the standalone lender process pick up BRUNO_ADMIN_AUTH_DISABLE,
# LENDER_PORTAL_PROD, SMTP_*, LENDER_BASE_URL, etc. from one shared file.
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / "config" / ".env"
if _ENV_FILE.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_ENV_FILE)
    except ImportError:
        pass

from src.lender_portal import router as lender_router


def create_lender_app() -> FastAPI:
    """Lender-only FastAPI app. Mounts the lender router at /lenders/* and
    redirects the root to /lenders/."""
    app = FastAPI(
        title="MesiCap Lender Portal",
        description="Lender-facing portal — read-only view of your loans with MesiCap",
        version="1.0.0",
        # No /docs in production — this is a public-facing portal
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Static assets (MesiCap logo, favicon). Served at /static/* from the
    # lender_portal/static/ directory — kept separate from the trading
    # dashboard's /static/ (which has M&W branding).
    _static_dir = Path(__file__).resolve().parent / "static"
    if _static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

    app.include_router(lender_router.router, prefix="/lenders")

    @app.get("/")
    def root_redirect():
        """Root of lender.mesicap.com → /lenders/ → either /lenders/login
        or /lenders/dashboard depending on session cookie."""
        return RedirectResponse(url="/lenders/", status_code=303)

    @app.get("/healthz")
    def healthz():
        """Liveness probe — no auth, no DB, just confirms the process is up."""
        return {"status": "ok", "app": "mesicap-lender-portal"}

    return app
