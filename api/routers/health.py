"""Health and version endpoint.

Returns per-dependency status rather than a single boolean, because "the map
works but search is down" is a real and common state on a one-box deployment,
and the client degrades differently for each.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from api.clients.db import Database
from api.clients.meili import MeiliClient
from common.config import Settings
from common.data_status import data_status
from common.valhalla import AsyncValhallaClient

from ..deps import get_settings_dep

router = APIRouter(tags=["health"])

VERSION = os.environ.get("NICANAV_VERSION", "0.1.0")


@router.get("/healthz")
async def healthz(
    request: Request, settings: Settings = Depends(get_settings_dep)
) -> dict[str, Any]:
    """Liveness plus dependency readiness.  Never raises; always 200."""
    valhalla: AsyncValhallaClient | None = getattr(request.app.state, "valhalla", None)
    meili: MeiliClient | None = getattr(request.app.state, "meili", None)
    database: Database | None = getattr(request.app.state, "db", None)

    valhalla_ok = False
    tileset: Any = None
    if valhalla is not None:
        try:
            status_payload = await valhalla.status()
            valhalla_ok = True
            tileset = status_payload.get("tileset_last_modified")
        except Exception:
            valhalla_ok = False

    return {
        "status": "ok",
        "version": VERSION,
        "valhalla": {"ok": valhalla_ok, "tileset_last_modified": tileset},
        "meili": {"ok": await meili.health() if meili is not None else False},
        "db": {"ok": await database.health() if database is not None else False},
        "tiles": _tiles_status(settings),
        "data": data_status(settings.metadata_dir),
    }


def _tiles_status(settings: Settings) -> dict[str, Any]:
    """Age and size of the published tile files.

    A stale ``base.pmtiles`` is the quietest failure this stack has: everything
    keeps working, just with last month's roads, so the age is surfaced here and
    on the admin dashboard.
    """
    out: dict[str, Any] = {}
    for name in ("base.pmtiles", "pois.pmtiles", "buildings.pmtiles"):
        path = Path(settings.tiles_dir) / name
        if path.exists():
            stat = path.stat()
            out[name] = {"bytes": stat.st_size, "mtime": int(stat.st_mtime)}
        else:
            out[name] = None
    return out
