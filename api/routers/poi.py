"""POI cards and along-route search."""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, Query, status

from api.clients.db import Database
from api.errors import ApiError
from common.config import Settings
from common.models import PoiCard, PoiPhoto, SearchHit
from common.polyline import decode, is_valid

from ..deps import get_db, get_settings_dep, rate_limit

router = APIRouter(prefix="/poi", tags=["poi"])
log = logging.getLogger(__name__)

#: Mapillary images are fetched per card and cached in-process.  The map has no
#: Street View in Nicaragua, so this is often the only photo of a place — worth
#: the extra call, not worth making it on every render.
_PHOTO_CACHE: dict[str, list[PoiPhoto]] = {}
_PHOTO_CACHE_MAX = 2_000


@router.get("/along_route", dependencies=[Depends(rate_limit("rate_limit_search"))])
async def along_route(
    polyline: str = Query(min_length=4, description="Encoded polyline, precision 6"),
    category: str | None = None,
    radius_m: float = Query(default=300, ge=50, le=1000),
    limit: int = Query(default=30, ge=1, le=50),
    database: Database = Depends(get_db),
) -> dict[str, list[SearchHit]]:
    """POIs near a route — "is there a gas station before Masaya?".

    Declared before ``/{poi_id}`` so the literal path wins over the parameter.
    """
    if not is_valid(polyline):
        raise ApiError("invalid_polyline", "La ruta enviada no es válida.")
    try:
        shape = decode(polyline)
    except Exception as exc:
        raise ApiError("invalid_polyline", "La ruta enviada no es válida.") from exc
    if len(shape) < 2:
        raise ApiError("invalid_polyline", "La ruta enviada no es válida.")
    hits = await database.pois_along_route(shape, radius_m=radius_m, category=category, limit=limit)
    return {"hits": hits}


@router.get("/{poi_id}", response_model=PoiCard)
async def get_poi(
    poi_id: str,
    photos: bool = True,
    database: Database = Depends(get_db),
    settings: Settings = Depends(get_settings_dep),
) -> PoiCard:
    """One place's full card."""
    card = await database.get_poi(poi_id)
    if card is None:
        raise ApiError(
            "poi_not_found", "No encontramos ese lugar.", status_code=status.HTTP_404_NOT_FOUND
        )
    if photos and not card.photos and settings.mapillary_token:
        card.photos = await _mapillary_photos(card.lat, card.lon, settings.mapillary_token)
    return card


async def _mapillary_photos(
    lat: float, lon: float, token: str, *, radius_deg: float = 0.0006
) -> list[PoiPhoto]:
    """Nearest street-level images from Mapillary (CC BY-SA, credited).

    Failures are swallowed: a missing photo must never turn a working POI card
    into an error.
    """
    key = f"{lat:.5f},{lon:.5f}"
    cached = _PHOTO_CACHE.get(key)
    if cached is not None:
        return cached

    bbox = f"{lon - radius_deg},{lat - radius_deg},{lon + radius_deg},{lat + radius_deg}"
    photos: list[PoiPhoto] = []
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            response = await client.get(
                "https://graph.mapillary.com/images",
                params={"fields": "id,thumb_1024_url,captured_at", "bbox": bbox, "limit": 3},
                headers={"Authorization": f"OAuth {token}"},
            )
            if response.status_code == 200:
                for image in response.json().get("data", []):
                    if image.get("thumb_1024_url"):
                        photos.append(
                            PoiPhoto(
                                url=image["thumb_1024_url"],
                                credit="Mapillary",
                                license="CC BY-SA 4.0",
                            )
                        )
    except (httpx.HTTPError, ValueError, KeyError):
        log.debug("mapillary lookup failed for %s", key, exc_info=True)
        return []

    if len(_PHOTO_CACHE) >= _PHOTO_CACHE_MAX:
        _PHOTO_CACHE.clear()
    _PHOTO_CACHE[key] = photos
    return photos


def cached_photo_count() -> int:
    """Exposed for the admin dashboard and the tests."""
    return len(_PHOTO_CACHE)


def clear_photo_cache() -> None:
    _PHOTO_CACHE.clear()
