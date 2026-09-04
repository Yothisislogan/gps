"""FastAPI application factory.

Serves the dynamic half of nicanav: search, geocoding, POI cards, the routing
proxy and user submissions.  Everything the browser fetches heavily — tiles,
style, fonts, sprites, the app shell — is a static file behind nginx and never
touches this process.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.clients.db import Database
from api.clients.meili import MeiliClient
from api.deps import RateLimiter
from api.errors import install_error_handlers
from api.routers import admin, geocode, health, poi, route, search, submissions
from common.config import Settings, get_settings
from common.valhalla import AsyncValhallaClient

__all__ = ["app", "create_app"]

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the clients on startup, close them on shutdown.

    A database that will not connect is logged and left unavailable rather than
    crashing the process: on a single box, Postgres restarting must not black
    out the map for everyone currently driving.
    """
    settings: Settings = app.state.settings
    app.state.limiter = RateLimiter()
    app.state.valhalla = AsyncValhallaClient(settings.valhalla_url)
    app.state.meili = MeiliClient(
        settings.meili_url, api_key=settings.meili_key, index=settings.meili_index
    )
    app.state.db = Database(settings.database_url)
    await app.state.db.connect()
    try:
        yield
    finally:
        await app.state.valhalla.aclose()
        await app.state.meili.aclose()
        await app.state.db.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app.  Tests call this with their own settings."""
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper())

    app = FastAPI(
        title="nicanav",
        version="0.1.0",
        summary="Mapa y navegación abierta para Nicaragua",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    install_error_handlers(app)

    for module in (health, search, geocode, poi, route, submissions):
        app.include_router(module.router, prefix="/api")

    # The moderation UI is HTML, not JSON, and lives outside /api. nginx puts
    # Basic auth in front of it; the router checks the credentials again itself.
    app.include_router(admin.router)
    app.mount(
        "/admin/static",
        StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
        name="admin-static",
    )

    return app


app = create_app()
