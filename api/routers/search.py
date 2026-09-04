"""Search: one request per keystroke, whatever the user is typing.

The endpoint runs the index query *and*, when the string looks like a
Nicaraguan address, the geocoder — and returns both in one payload.  That is a
deliberate product decision: users do not know whether what they typed is a
"place" or an "address", and on a Claro 4G connection a second round trip is
felt.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Query

from api.clients.db import Database
from api.clients.meili import MeiliClient, MeiliError
from common.models import GeocodeResponse, SearchHit, SearchKind, SearchResponse
from common.valhalla import AsyncValhallaClient

from ..deps import get_db, get_meili, get_valhalla, rate_limit
from .geocode import geocode_candidates

router = APIRouter(tags=["search"])
log = logging.getLogger(__name__)

#: Meilisearch returns no similarity score, so hits keep index order.
_MAX_LIMIT = 50


@router.get(
    "/search",
    response_model=SearchResponse,
    dependencies=[Depends(rate_limit("rate_limit_search"))],
)
async def search(
    q: str = Query(min_length=1, max_length=250),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lon: float | None = Query(default=None, ge=-180, le=180),
    limit: int = Query(default=20, ge=1, le=_MAX_LIMIT),
    kind: SearchKind | None = None,
    category: str | None = None,
    radius_m: float | None = Query(default=None, ge=100, le=100_000),
    database: Database = Depends(get_db),
    meili: MeiliClient = Depends(get_meili),
    valhalla: AsyncValhallaClient = Depends(get_valhalla),
) -> SearchResponse:
    """Search places, streets, barrios and landmarks; parse addresses too."""
    started = time.perf_counter()
    query = q.strip()

    filters: list[str] = []
    if kind is not None:
        filters.append(f'kind = "{kind.value}"')
    if category:
        # Category values come from docs/taxonomy.csv (lowercase ascii ids), but
        # quote-escape anyway so a crafted query cannot break out of the filter.
        filters.append(f'category = "{category.replace(chr(34), "")}"')

    hits: list[SearchHit] = []
    try:
        payload = await meili.search(
            query,
            limit=limit,
            filters=filters or None,
            lat=lat,
            lon=lon,
            radius_m=radius_m,
            sort_by_distance=lat is not None and lon is not None,
        )
        hits = [hit for hit in (_to_hit(raw) for raw in payload.get("hits") or []) if hit]
    except MeiliError:
        log.warning("search index unavailable; falling back to postgis", exc_info=True)
        if lat is not None and lon is not None:
            hits = await database.pois_near(
                lat, lon, radius_m=radius_m or 3_000, category=category, limit=limit
            )

    geocode: GeocodeResponse | None = None
    candidates = await geocode_candidates(
        query, database=database, meili=meili, valhalla=valhalla, lat=lat, lon=lon, limit=3
    )
    # Only surface geocoding when it actually parsed something structural; a
    # plain index echo would just duplicate the hits list.
    structural = [c for c in candidates if c.method.value in {"relative", "kmpost", "coordinate"}]
    if structural:
        geocode = GeocodeResponse(
            query=query, candidates=structural, parsed_as=structural[0].method
        )

    await database.log_search(query, len(hits), lat, lon, kind.value if kind else None)

    return SearchResponse(
        query=query,
        hits=hits,
        geocode=geocode,
        took_ms=int((time.perf_counter() - started) * 1000),
    )


def _to_hit(raw: dict) -> SearchHit | None:
    """Map an index document to a wire hit, skipping anything unplottable."""
    if raw.get("lat") is None or raw.get("lon") is None:
        return None
    identifier = str(raw.get("id", ""))
    kind = raw.get("kind") or (identifier.split(":", 1)[0] if ":" in identifier else "poi")
    try:
        search_kind = SearchKind(kind)
    except ValueError:
        search_kind = SearchKind.POI
    geo_distance = raw.get("_geoDistance")
    return SearchHit(
        id=identifier.split(":", 1)[-1] if ":" in identifier else identifier,
        kind=search_kind,
        name=raw.get("name") or "",
        lat=float(raw["lat"]),
        lon=float(raw["lon"]),
        category=raw.get("category"),
        subcategory=raw.get("subcategory"),
        address_text=raw.get("address_text"),
        city=raw.get("city"),
        distance_m=float(geo_distance) if geo_distance is not None else None,
        verified=bool(raw.get("verified")),
        popularity=float(raw.get("popularity") or 0.0),
    )
