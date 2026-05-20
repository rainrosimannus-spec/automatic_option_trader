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


# Paths within /borrower that don't require admin auth (login flow itself + assets)
_ADMIN_AUTH_EXEMPT_PREFIXES = (
    "/borrower/login",
    "/borrower/magic/",
    "/borrower/logout",
)


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
    # Lender portal at /lenders/* has its own auth and is unaffected.
    @app.middleware("http")
    async def _admin_auth_gate(request: Request, call_next):
        path = request.url.path
        # Match /borrower exactly OR /borrower/anything — but not /borrowerXYZ.
        if path == "/borrower" or path.startswith("/borrower/"):
            exempt = any(path.startswith(p) for p in _ADMIN_AUTH_EXEMPT_PREFIXES)
            if not exempt and _current_admin_principal(request) is None:
                return RedirectResponse(url="/borrower/login", status_code=303)
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
