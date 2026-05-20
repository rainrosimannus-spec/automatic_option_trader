"""
FastAPI application factory for the web dashboard.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.web.routes import dashboard, positions, trades, controls, api, screener, suggestions
from src.web.routes import portfolio as portfolio_route
from src.web.routes import consigliere as consigliere_route
from src.web.routes import ipo as ipo_route
from src.web.routes import borrower as borrower_route
from src.lender_portal import router as lender_portal_route
from src.borrower.admin_auth import current_principal as _current_admin_principal
from src.borrower.deadman import compute_state as _compute_deadman_state


# Paths within /borrower that don't require admin auth (login flow itself + assets)
_ADMIN_AUTH_EXEMPT_PREFIXES = (
    "/borrower/login",
    "/borrower/magic/",
    "/borrower/logout",
)


def _admin_auth_disabled() -> bool:
    """The admin-auth middleware can be disabled when /borrower/* is already
    behind an outer auth layer (e.g., Caddy basic-auth on app.rosimannus.ee).

    Set env BRUNO_ADMIN_AUTH_DISABLE=1 to skip Tier G's magic-link gate.
    Default off — i.e., the magic-link gate is active by default. Re-enable
    after deploys that move /borrower/* past the outer auth boundary."""
    import os as _os
    return _os.environ.get("BRUNO_ADMIN_AUTH_DISABLE") == "1"


_STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Maggy & Winston Trading Dashboard",
        description="Automated options & portfolio management system",
        version="0.2.0",
    )

    # Static files
    _STATIC_DIR.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # Admin auth gate on /borrower/* (governance.md §5.7 / Tier G).
    # Also enforces the dead-man freeze (governance.md §3.2 / Tier L): when
    # the dead-man state is frozen, write methods return 423 Locked and
    # the principal is told to log in (which auto-rearms the timer).
    # Lender portal at /lenders/* has its own auth and is unaffected.
    @app.middleware("http")
    async def _admin_auth_gate(request: Request, call_next):
        path = request.url.path
        # Match /borrower exactly OR /borrower/anything — but not /borrowerXYZ.
        if path == "/borrower" or path.startswith("/borrower/"):
            exempt = any(path.startswith(p) for p in _ADMIN_AUTH_EXEMPT_PREFIXES)
            disabled = _admin_auth_disabled()
            if not exempt and not disabled and _current_admin_principal(request) is None:
                return RedirectResponse(url="/borrower/login", status_code=303)
            # Dead-man freeze: block writes when frozen. Read paths still serve
            # (so the principal can see what's happening) — only POST/PUT/
            # PATCH/DELETE are blocked. Independent of admin-auth being on.
            if not exempt and request.method in ("POST", "PUT", "PATCH", "DELETE"):
                state = _compute_deadman_state()
                if state.is_frozen:
                    from fastapi.responses import JSONResponse
                    return JSONResponse(
                        status_code=423,
                        content={
                            "detail": (
                                "Dead-man freeze in effect — no principal has logged in "
                                f"for {state.days_since_last_login} days "
                                f"(freeze threshold {state.freeze_threshold_days}). "
                                "Logging in via /borrower/login rearms the timer."
                            ),
                        },
                    )
        return await call_next(request)

    # Routes
    app.include_router(dashboard.router)
    app.include_router(positions.router, prefix="/positions")
    app.include_router(trades.router, prefix="/trades")
    app.include_router(controls.router, prefix="/controls")
    app.include_router(api.router, prefix="/api")
    app.include_router(screener.router)
    app.include_router(suggestions.router, prefix="/suggestions")
    app.include_router(portfolio_route.router)
    app.include_router(consigliere_route.router)
    app.include_router(ipo_route.router, prefix="/ipo")
    app.include_router(borrower_route.router, prefix="/borrower")
    app.include_router(lender_portal_route.router, prefix="/lenders")

    # Convenience redirect for nav link
    from fastapi.responses import RedirectResponse as _RR
    @app.get("/options-suggestions")
    def _options_suggestions_redirect():
        return _RR(url="/suggestions/options", status_code=307)

    return app
