"""Routing proxy.

Clients never talk to Valhalla directly.  This endpoint exists so that four
things always happen, no matter which client is calling: active closures are
excluded, the instruction language is set, the alternate count is clamped, and
the origin/destination pair is logged coarsely for the future speed-profile
work.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from api.clients.db import Database
from api.errors import ApiError
from common.config import Settings
from common.models import RouteRequest
from common.valhalla import AsyncValhallaClient, ValhallaError

from ..deps import get_db, get_settings_dep, get_valhalla, rate_limit

router = APIRouter(tags=["routing"])
log = logging.getLogger(__name__)

#: Valhalla will happily route Managua -> Panama; the app will not.
MAX_LOCATIONS = 10


@router.post("/route", dependencies=[Depends(rate_limit("rate_limit_route"))])
async def route(
    body: RouteRequest,
    valhalla: AsyncValhallaClient = Depends(get_valhalla),
    database: Database = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> dict[str, Any]:
    """Route between two or more points, closures excluded."""
    if len(body.locations) > MAX_LOCATIONS:
        raise ApiError("too_many_locations", f"Máximo {MAX_LOCATIONS} puntos por ruta.")

    exclude_polygons: list[Any] = []
    if body.exclude_closures:
        try:
            exclude_polygons = await database.active_closures()
        except Exception:
            log.warning("closure lookup failed; routing without exclusions", exc_info=True)

    date_time = None
    if body.departure_time is not None:
        # Valhalla type 1 = "depart at"; the value is local time without a zone.
        date_time = {"type": 1, "value": body.departure_time.strftime("%Y-%m-%dT%H:%M")}

    try:
        response = await valhalla.route(
            body.locations,
            costing=body.costing,
            language=body.language or settings.default_language,
            units=body.units,
            alternates=body.alternates,
            avoid_unpaved=body.avoid_unpaved,
            exclude_polygons=exclude_polygons or None,
            heading=body.heading,
            date_time=date_time,
            output_format=None if body.format == "json" else body.format,
        )
    except ValhallaError:
        raise
    except Exception as exc:
        raise ApiError(
            "routing_unavailable",
            "El servicio de rutas no está disponible en este momento.",
            status_code=503,
        ) from exc

    summary = _summary_of(response, body.format)
    await database.log_route(
        body.locations[0].lat,
        body.locations[0].lon,
        body.locations[-1].lat,
        body.locations[-1].lon,
        costing=body.costing,
        length_km=summary.get("length_km"),
        duration_s=summary.get("duration_s"),
        alternates=body.alternates,
        reroute=body.heading is not None,
    )

    response["nicanav"] = {
        "closures_applied": len(exclude_polygons),
        "alternates_requested": body.alternates,
        "language": body.language or settings.default_language,
    }
    return response


def _summary_of(response: dict[str, Any], output_format: str) -> dict[str, float | None]:
    """Pull length/duration out of either response format, tolerantly."""
    try:
        if output_format == "osrm":
            first = (response.get("routes") or [{}])[0]
            distance = first.get("distance")
            return {
                "length_km": float(distance) / 1000.0 if distance is not None else None,
                "duration_s": float(first["duration"])
                if first.get("duration") is not None
                else None,
            }
        summary = (response.get("trip") or {}).get("summary") or {}
        return {
            "length_km": float(summary["length"]) if "length" in summary else None,
            "duration_s": float(summary["time"]) if "time" in summary else None,
        }
    except (TypeError, ValueError, IndexError, KeyError):
        return {"length_km": None, "duration_s": None}
