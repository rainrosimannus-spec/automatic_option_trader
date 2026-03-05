"""
FastAPI application factory for the web dashboard.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.web.routes import dashboard, positions, trades, controls, api, screener, suggestions
from src.web.routes import portfolio as portfolio_route
from src.web.routes import consigliere as consigliere_route
from src.web.routes import ipo as ipo_route


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

    # Convenience redirect for nav link
    from fastapi.responses import RedirectResponse as _RR
    @app.get("/options-suggestions")
    def _options_suggestions_redirect():
        return _RR(url="/suggestions/options", status_code=307)

    return app
