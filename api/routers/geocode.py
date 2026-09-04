"""Geocoding: Nicaraguan relative addresses, km posts and coordinates.

This is the endpoint the rest of the product is built around, because in
Nicaragua "an address" is a sentence, not a street number.  The order of
attempts matters and is deliberate:

1. a **learned alias** — somebody already dragged a pin onto this exact string,
   which beats any parser;
2. a **pasted coordinate** (WhatsApp location pins arrive this way constantly);
3. a **km post** ("Km 12.5 Carretera a Masaya");
4. a **relative address** ("De la Rotonda El Güegüense, 2c al sur");
5. whatever the plain search index says.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.clients.db import Database
from api.clients.meili import MeiliClient, MeiliError
from common.config import Settings
from common.models import GeocodeCandidate, GeocodeMethod, GeocodeResponse
from common.valhalla import AsyncValhallaClient
from pipeline.geocode import relative_address

from ..deps import get_db, get_meili, get_settings_dep, get_valhalla, rate_limit

router = APIRouter(tags=["geocoding"])
log = logging.getLogger(__name__)

#: "12.1415, -86.1682" and the variants WhatsApp and Google Maps produce.
_COORD_RE = re.compile(
    r"^\s*(?P<lat>-?\d{1,2}(?:\.\d+)?)\s*[,; ]\s*(?P<lon>-?\d{1,3}(?:\.\d+)?)\s*$"
)


def _kmpost_module():
    """Import the km-post geocoder lazily.

    Soft dependency on purpose: the API image can be built without the pipeline
    package, and km-post lookups then simply do not fire.  Everything else keeps
    working.
    """
    try:
        from pipeline.geocode import kmpost
    except ImportError:  # pragma: no cover - exercised only in a slim image
        return None
    return kmpost


def _reverse_module():
    """Import the reverse relative geocoder lazily (see :func:`_kmpost_module`)."""
    try:
        from pipeline.geocode import reverse
    except ImportError:  # pragma: no cover
        return None
    return reverse


@router.get(
    "/geocode",
    response_model=GeocodeResponse,
    dependencies=[Depends(rate_limit("rate_limit_search"))],
)
async def geocode(
    q: str = Query(min_length=2, max_length=250),
    lat: float | None = Query(default=None, ge=-90, le=90),
    lon: float | None = Query(default=None, ge=-180, le=180),
    city: str | None = None,
    limit: int = Query(default=5, ge=1, le=5),
    database: Database = Depends(get_db),
    meili: MeiliClient = Depends(get_meili),
    valhalla: AsyncValhallaClient = Depends(get_valhalla),
    settings: Settings = Depends(get_settings_dep),
) -> GeocodeResponse:
    """Resolve an address string to at most ``limit`` scored candidates."""
    candidates = await geocode_candidates(
        q,
        database=database,
        meili=meili,
        valhalla=valhalla,
        city=city,
        lat=lat,
        lon=lon,
        limit=limit,
    )
    return GeocodeResponse(
        query=q,
        candidates=candidates[:limit],
        parsed_as=candidates[0].method if candidates else None,
    )


async def geocode_candidates(
    q: str,
    *,
    database: Database,
    meili: MeiliClient,
    valhalla: AsyncValhallaClient | None = None,
    city: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    limit: int = 5,
) -> list[GeocodeCandidate]:
    """The geocoding cascade, reusable by ``/search``."""
    query = q.strip()

    coordinate = _parse_coordinate(query)
    if coordinate is not None:
        return [coordinate]

    aliases = await _alias_candidates(query, database)
    if aliases and aliases[0].confidence >= 0.9:
        return aliases

    candidates: list[GeocodeCandidate] = list(aliases)

    kmpost = _kmpost_module()
    if kmpost is not None and kmpost.looks_like_kmpost(query):
        try:
            candidates.extend(await _kmpost_candidates(kmpost, query, database))
        except Exception:
            log.warning("km-post geocoding failed for %r", query, exc_info=True)

    if relative_address.looks_like_relative_address(query):
        try:
            candidates.extend(
                await _relative_candidates(query, database, meili, valhalla, city=city, limit=limit)
            )
        except Exception:
            log.warning("relative geocoding failed for %r", query, exc_info=True)

    if not candidates:
        candidates.extend(await _index_candidates(query, meili, lat=lat, lon=lon, limit=limit))

    candidates.sort(key=lambda candidate: candidate.confidence, reverse=True)
    return candidates


def _parse_coordinate(query: str) -> GeocodeCandidate | None:
    """Accept a pasted "lat, lon" — how a WhatsApp location pin arrives."""
    match = _COORD_RE.match(query)
    if not match:
        return None
    lat, lon = float(match.group("lat")), float(match.group("lon"))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return GeocodeCandidate(
        lat=lat,
        lon=lon,
        label=f"{lat:.5f}, {lon:.5f}",
        confidence=1.0,
        method=GeocodeMethod.COORDINATE,
        notes=["Coordenadas"],
    )


async def _alias_candidates(query: str, database: Database) -> list[GeocodeCandidate]:
    """Learned corrections: a human already put this pin in the right place."""
    try:
        rows = await database.find_alias(query)
    except Exception:
        return []
    candidates = []
    for row in rows:
        score = float(row.get("score") or 0.0)
        # Repeated corrections are stronger evidence than a single one.
        hits_bonus = min(0.1, 0.02 * float(row.get("hits") or 1))
        candidates.append(
            GeocodeCandidate(
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                label=row["text"],
                confidence=round(min(1.0, score * 0.95 + hits_bonus), 4),
                method=GeocodeMethod.RELATIVE,
                poi_id=row.get("poi_id"),
                notes=["Dirección confirmada por usuarios"],
            )
        )
    return candidates


async def _kmpost_candidates(kmpost: Any, query: str, database: Database) -> list[GeocodeCandidate]:
    """Delegate to the km-post geocoder, feeding it geometry from the database.

    The geometry and the surveyed calibration points are fetched here (async)
    and handed to the resolver as plain callables, so the geocoder itself stays
    synchronous and database-free.
    """
    parsed = kmpost.parse(query)
    if parsed is None:
        return []
    highway_key = getattr(parsed, "highway_key", None)
    if not highway_key:
        return []

    geometry = await database.highway_geometry(highway_key)
    if not geometry:
        return []
    calibration = await database.kmpost_calibration(highway_key)

    return list(
        kmpost.resolve(
            parsed,
            lambda _key: geometry,
            calibration=[
                (float(row["km"]), float(row["lat"]), float(row["lon"])) for row in calibration
            ],
        )
    )


async def _relative_candidates(
    query: str,
    database: Database,
    meili: MeiliClient,
    valhalla: AsyncValhallaClient | None,
    *,
    city: str | None,
    limit: int,
) -> list[GeocodeCandidate]:
    """Parse the address, then resolve it against the gazetteer."""
    parsed = relative_address.parse(query, city=city)
    if parsed is None:
        return []

    landmarks = await _find_landmarks(
        parsed.landmark_query or parsed.landmark_text,
        former=parsed.former_landmark,
        database=database,
        meili=meili,
        city=city,
    )
    if not landmarks:
        return []

    snapped: dict[tuple[float, float], tuple[float, float] | None] = {}

    def snap(point_lat: float, point_lon: float):
        return snapped.get((point_lat, point_lon))

    # Resolve once without snapping to learn which points need a snap, then snap
    # them concurrently-ish and resolve again.  Two cheap passes beat blocking
    # the event loop inside a synchronous callback.
    first_pass = relative_address.resolve(parsed, lambda *_: landmarks, city=city, limit=limit)
    if valhalla is not None:
        for candidate in first_pass:
            point = await valhalla.snap(candidate.lat, candidate.lon)
            snapped[(candidate.lat, candidate.lon)] = point
        return relative_address.resolve(
            parsed, lambda *_: landmarks, city=city, snap=snap, limit=limit
        )
    return first_pass


async def _find_landmarks(
    name: str,
    *,
    former: bool,
    database: Database,
    meili: MeiliClient,
    city: str | None,
) -> list[relative_address.LandmarkMatch]:
    """Look the landmark up in PostGIS first, then fall back to the index.

    PostGIS trigram search is authoritative (it is the same table the pipeline
    writes); Meilisearch is the fallback when the database is down, and is
    better at typos.
    """
    matches: list[relative_address.LandmarkMatch] = []
    try:
        rows = await database.find_landmarks(
            name, former=True if former else None, city=city, limit=5
        )
        matches = [
            relative_address.LandmarkMatch(
                id=row["id"],
                name=row["name"],
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                score=float(row.get("score") or 0.5),
                city=row.get("city"),
                former=bool(row.get("former")),
                kind=row.get("kind"),
                popularity=float(row.get("popularity") or 0.0),
            )
            for row in rows
        ]
    except Exception:
        log.debug("gazetteer lookup failed; falling back to the index", exc_info=True)

    if matches:
        return matches

    try:
        payload = await meili.search(name, limit=5, filters=['kind = "landmark"'])
    except MeiliError:
        return []
    hits = payload.get("hits") or []
    total = len(hits) or 1
    return [
        relative_address.LandmarkMatch(
            id=str(hit.get("id")),
            name=hit.get("name", name),
            lat=float(hit["lat"]),
            lon=float(hit["lon"]),
            # Meilisearch does not return a similarity score, so rank position is
            # the only signal available; keep it modest so a fuzzy index hit can
            # never masquerade as a confident address.
            score=round(0.75 - 0.1 * index / total, 4),
            city=hit.get("city"),
            former=bool(hit.get("former")),
            popularity=float(hit.get("popularity") or 0.0),
        )
        for index, hit in enumerate(hits)
        if hit.get("lat") is not None and hit.get("lon") is not None
    ]


async def _index_candidates(
    query: str, meili: MeiliClient, *, lat: float | None, lon: float | None, limit: int
) -> list[GeocodeCandidate]:
    """Last resort: treat the query as a plain name search."""
    try:
        payload = await meili.search(
            query, limit=limit, lat=lat, lon=lon, radius_m=50_000 if lat else None
        )
    except MeiliError:
        return []
    candidates = []
    for index, hit in enumerate(payload.get("hits") or []):
        if hit.get("lat") is None or hit.get("lon") is None:
            continue
        candidates.append(
            GeocodeCandidate(
                lat=float(hit["lat"]),
                lon=float(hit["lon"]),
                label=hit.get("name") or query,
                confidence=round(max(0.2, 0.7 - 0.05 * index), 4),
                method=GeocodeMethod.INDEX,
                poi_id=str(hit["id"]).split(":", 1)[-1] if hit.get("kind") == "poi" else None,
            )
        )
    return candidates


@router.get(
    "/reverse",
    response_model=GeocodeResponse,
    dependencies=[Depends(rate_limit("rate_limit_search"))],
)
async def reverse(
    lat: float = Query(ge=-90, le=90),
    lon: float = Query(ge=-180, le=180),
    city: str | None = None,
    database: Database = Depends(get_db),
) -> GeocodeResponse:
    """Turn a pin into the sentence a Nicaraguan would actually say.

    "De la Rotonda El Güegüense, 2c al sur, 1c abajo" belongs on the share sheet
    next to the coordinates — that is how locations get sent over WhatsApp here.
    """
    landmarks = await _nearby_landmarks(lat, lon, database)
    reverse_module = _reverse_module()

    if reverse_module is not None and landmarks:
        address = reverse_module.reverse(lat, lon, landmarks, city=city)
        if address is not None:
            return GeocodeResponse(
                query=f"{lat:.5f},{lon:.5f}",
                parsed_as=GeocodeMethod.RELATIVE,
                candidates=[
                    GeocodeCandidate(
                        lat=lat,
                        lon=lon,
                        label=address.render(),
                        confidence=0.8,
                        method=GeocodeMethod.RELATIVE,
                        relative=address,
                        landmark_name=address.landmark_text,
                    )
                ],
            )

    # Fallback: name the nearest known thing rather than returning nothing.
    nearest = await database.nearest_street(lat, lon)
    label = f"Cerca de {nearest}" if nearest else f"{lat:.5f}, {lon:.5f}"
    return GeocodeResponse(
        query=f"{lat:.5f},{lon:.5f}",
        parsed_as=GeocodeMethod.COORDINATE,
        candidates=[
            GeocodeCandidate(
                lat=lat, lon=lon, label=label, confidence=0.4, method=GeocodeMethod.COORDINATE
            )
        ],
    )


async def _nearby_landmarks(
    lat: float, lon: float, database: Database, *, radius_m: float = 1500
) -> list[relative_address.LandmarkMatch]:
    """Candidate landmarks around a point, for reverse addressing."""
    if not getattr(database, "available", False):
        return []
    try:
        rows = await database._fetch(
            """
            SELECT g.id::text AS id, g.name, g.kind, g.city, g.former, g.popularity,
                   ST_Y(g.geom) AS lat, ST_X(g.geom) AS lon
            FROM gazetteer g
            WHERE ST_DWithin(g.geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            ORDER BY g.popularity DESC
            LIMIT 25
            """,
            (lon, lat, radius_m),
        )
    except Exception:
        return []
    return [
        relative_address.LandmarkMatch(
            id=row["id"],
            name=row["name"],
            lat=float(row["lat"]),
            lon=float(row["lon"]),
            score=1.0,
            city=row.get("city"),
            former=bool(row.get("former")),
            kind=row.get("kind"),
            popularity=float(row.get("popularity") or 0.0),
        )
        for row in rows
    ]
