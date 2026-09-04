"""User submissions: reports, suggested places and learned addresses.

Everything here is rate-limited and lands in a moderation queue.  Nothing a user
posts is published without review — that rule is what keeps a crowd-sourced map
usable, and it is enforced by the schema (``status = 'pending'`` defaults), not
just by convention.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from api.clients.db import Database
from api.errors import ApiError
from common.geo import NICARAGUA_BBOX
from common.models import AliasSubmission, PoiSuggestion, ReportSubmission

from ..deps import client_fingerprint, get_db, rate_limit

router = APIRouter(tags=["submissions"])
log = logging.getLogger(__name__)


def _require_in_nicaragua(lat: float, lon: float) -> None:
    """Reject submissions outside the country.

    Not security — a wrong-hemisphere pin is almost always a swapped lat/lon in
    a client, and catching it here keeps the moderation queue clean.
    """
    min_lon, min_lat, max_lon, max_lat = NICARAGUA_BBOX
    if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
        raise ApiError("outside_nicaragua", "Ese punto está fuera de Nicaragua.")


@router.post("/report", dependencies=[Depends(rate_limit("rate_limit_report"))])
async def create_report(
    body: ReportSubmission,
    request: Request,
    database: Database = Depends(get_db),
) -> dict[str, str]:
    """A road closure, pothole, flood, wrong one-way or closed business."""
    _require_in_nicaragua(body.lat, body.lon)
    report_id = await database.insert_report(
        body.kind.value,
        body.lat,
        body.lon,
        body.note,
        body.photo_url,
        client_fingerprint(request),
    )
    log.info("report %s (%s) at %.4f,%.4f", report_id, body.kind.value, body.lat, body.lon)
    return {"id": report_id, "status": "pending", "message": "¡Gracias! Lo vamos a revisar."}


@router.post("/poi/suggest", dependencies=[Depends(rate_limit("rate_limit_report"))])
async def suggest_poi(
    body: PoiSuggestion,
    request: Request,
    database: Database = Depends(get_db),
) -> dict[str, str]:
    """ "Sugerir un lugar" — the path for businesses that exist only on Facebook."""
    _require_in_nicaragua(body.lat, body.lon)
    suggestion_id = await database.insert_suggestion(
        body.model_dump(mode="json"), body.lat, body.lon, client_fingerprint(request)
    )
    return {"id": suggestion_id, "status": "pending", "message": "¡Gracias! Lo vamos a revisar."}


@router.post("/alias", dependencies=[Depends(rate_limit("rate_limit_report"))])
async def create_alias(
    body: AliasSubmission,
    request: Request,
    database: Database = Depends(get_db),
) -> dict[str, str]:
    """A corrected address string.

    This is the flywheel: every time somebody drags the pin to the right door,
    the string they typed becomes ground truth for the next person who types it.
    """
    _require_in_nicaragua(body.lat, body.lon)
    alias_id = await database.insert_alias(
        body.text.strip(),
        body.lat,
        body.lon,
        poi_id=body.poi_id,
        created_by=client_fingerprint(request),
    )
    return {"id": alias_id, "status": "pending", "message": "Gracias por corregir la ubicación."}
