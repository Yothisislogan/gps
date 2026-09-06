"""PostGIS access for the API.

Written against psycopg 3's async pool, but built so that **a database outage
degrades the product instead of taking it down**: routing, tiles and search all
work without Postgres, and only the endpoints that genuinely need a row (POI
cards, submissions) fail.  ``Database.available`` says which world we are in.

All SQL lives here.  Routers call methods, never strings, so column names in
db/migrations/001_init.sql have exactly one place to be wrong.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from api.errors import ApiError
from common.models import PoiCard, PoiPhoto, PoiStatus, SearchHit, SearchKind

__all__ = ["Database"]

log = logging.getLogger(__name__)

#: Columns selected for a POI card.  Kept as one string so the row -> model
#: mapping below cannot drift from the query.
_POI_COLUMNS = """
    p.id::text, p.name, p.name_alt, p.category, p.subcategory, p.cuisine,
    ST_Y(p.geom) AS lat, ST_X(p.geom) AS lon, p.address_text, p.city,
    p.phone, p.whatsapp, p.website, p.facebook, p.instagram, p.opening_hours,
    p.price_level, p.status, p.confidence, p.verified_at, p.verified_by, p.sources
"""


class Database:
    """Lazy async connection pool with graceful degradation."""

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 8) -> None:
        self.dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: Any = None
        self.available = False
        self._connect_lock = asyncio.Lock()
        self._retry_at = 0.0
        self._closed = False

    async def connect(self) -> None:
        """Open the pool.  A failure here is logged, not raised.

        The API must still serve tiles, search and routing when Postgres is down
        — on a one-box deployment, a database restart should not black out the
        map for everyone on the road.
        """
        async with self._connect_lock:
            if self._closed or self.available or time.monotonic() < self._retry_at:
                return
            await self._connect()

    async def _connect(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
        try:
            from psycopg_pool import AsyncConnectionPool
        except ImportError:  # pragma: no cover - psycopg is an api-image dependency
            log.warning("psycopg_pool not installed; database features are off")
            return
        try:
            self._pool = AsyncConnectionPool(
                self.dsn,
                min_size=self._min_size,
                max_size=self._max_size,
                open=False,
                timeout=1.0,
                kwargs={"connect_timeout": 3},
                reconnect_failed=self._reconnect_failed,
            )
            await self._pool.open(wait=True, timeout=1.0)
            self.available = True
            log.info("database pool ready")
        except Exception:
            log.warning("database unavailable; retrying on a later request", exc_info=True)
            if self._pool is not None:
                await self._pool.close()
            self._pool = None
            self.available = False
            self._retry_at = time.monotonic() + 5.0

    async def _reconnect_failed(self, pool: Any) -> None:
        # psycopg stops replenishing after its retry budget; the next request
        # must be able to create a new pool after a prolonged database outage.
        if self._pool is pool:
            self.available = False
            self._retry_at = time.monotonic() + 5.0

    async def close(self) -> None:
        async with self._connect_lock:
            self._closed = True
            if self._pool is not None:
                await self._pool.close()
                self._pool = None
            self.available = False

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[Any]:
        if not self.available:
            await self.connect()
        if self._pool is None or not self.available:
            raise self._unavailable()
        from psycopg import OperationalError
        from psycopg_pool import PoolTimeout

        try:
            async with self._pool.connection(timeout=1.0) as conn:
                yield conn
        except PoolTimeout as exc:
            # Saturation is temporary too, but rebuilding a busy pool makes it worse.
            raise self._unavailable() from exc
        except OperationalError as exc:
            self.available = False
            self._retry_at = time.monotonic() + 5.0
            raise self._unavailable() from exc

    @staticmethod
    def _unavailable() -> ApiError:
        return ApiError(
            "database_unavailable",
            "La base de datos no está disponible en este momento. Probá de nuevo.",
            status_code=503,
        )

    async def _fetch(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        from psycopg.rows import dict_row

        async with self._connection() as conn:
            conn.row_factory = dict_row
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return list(await cur.fetchall())

    async def _execute(self, sql: str, params: Any = None) -> Any:
        from psycopg.rows import dict_row

        async with self._connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            row = await cur.fetchone() if cur.description else None
            return row

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #

    async def health(self) -> bool:
        try:
            await self._fetch("SELECT 1")
            return True
        except Exception:
            return False

    async def get_poi(self, poi_id: str) -> PoiCard | None:
        rows = await self._fetch(f"SELECT {_POI_COLUMNS} FROM poi p WHERE p.id = %s", (poi_id,))
        if not rows:
            return None
        photos = await self._fetch(
            "SELECT url, credit, license, taken_at FROM poi_photo WHERE poi_id = %s ORDER BY id",
            (poi_id,),
        )
        return _row_to_card(rows[0], photos)

    async def pois_near(
        self,
        lat: float,
        lon: float,
        *,
        radius_m: float = 1000,
        category: str | None = None,
        limit: int = 30,
    ) -> list[SearchHit]:
        """Nearest POIs, used as the fallback when Meilisearch is down."""
        sql = f"""
            SELECT {_POI_COLUMNS},
                   ST_Distance(p.geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography) AS distance_m
            FROM poi p
            WHERE ST_DWithin(p.geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
              AND p.status <> 'closed'
              AND (%s::text IS NULL OR p.category = %s)
            ORDER BY distance_m ASC
            LIMIT %s
        """
        rows = await self._fetch(sql, (lon, lat, lon, lat, radius_m, category, category, limit))
        return [_row_to_hit(row) for row in rows]

    async def pois_along_route(
        self,
        shape: list[tuple[float, float]],
        *,
        radius_m: float = 300,
        category: str | None = None,
        limit: int = 50,
    ) -> list[SearchHit]:
        """POIs within ``radius_m`` of a route line — "search along route".

        The shape is passed as GeoJSON rather than WKT so the driver does the
        escaping; coordinates go in ``[lon, lat]`` order.
        """
        if len(shape) < 2:
            return []
        line = json.dumps({"type": "LineString", "coordinates": [[lon, lat] for lat, lon in shape]})
        sql = f"""
            WITH route AS (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326) AS geom)
            SELECT {_POI_COLUMNS},
                   ST_Distance(p.geom::geography, route.geom::geography) AS distance_m
            FROM poi p, route
            WHERE ST_DWithin(p.geom::geography, route.geom::geography, %s)
              AND p.status <> 'closed'
              AND (%s::text IS NULL OR p.category = %s)
            ORDER BY distance_m ASC
            LIMIT %s
        """
        rows = await self._fetch(sql, (line, radius_m, category, category, limit))
        return [_row_to_hit(row) for row in rows]

    async def find_landmarks(
        self, query: str, *, former: bool | None = None, limit: int = 5, city: str | None = None
    ) -> list[dict[str, Any]]:
        """Trigram search over the gazetteer, for the relative-address resolver.

        Ordering puts exact-ish name similarity first and popularity second: when
        somebody says "de la rotonda", they mean the famous one.
        """
        sql = """
            SELECT g.id::text AS id, g.name, g.kind, g.city, g.former, g.popularity,
                   ST_Y(g.geom) AS lat, ST_X(g.geom) AS lon,
                   GREATEST(
                     similarity(nicanav_normalize(g.name), nicanav_normalize(%s)),
                     COALESCE((SELECT MAX(similarity(nicanav_normalize(alt), nicanav_normalize(%s)))
                               FROM unnest(g.name_alt) AS alt), 0)
                   ) AS score
            FROM gazetteer g
            WHERE (%s::boolean IS NULL OR g.former = %s)
              AND (%s::text IS NULL OR nicanav_normalize(g.city) = nicanav_normalize(%s))
              AND (nicanav_normalize(g.name) %% nicanav_normalize(%s)
                   OR EXISTS (SELECT 1 FROM unnest(g.name_alt) AS alt
                              WHERE nicanav_normalize(alt) %% nicanav_normalize(%s)))
            ORDER BY score DESC, g.popularity DESC
            LIMIT %s
        """
        return await self._fetch(
            sql, (query, query, former, former, city, city, query, query, limit)
        )

    async def find_alias(self, text: str, *, limit: int = 3) -> list[dict[str, Any]]:
        """Look up a learned address string.

        An approved alias is ground truth — a human already dragged the pin to
        the right door — so callers must rank these above any parse.
        """
        sql = """
            SELECT a.id::text AS id, a.text, a.confidence, a.hits, a.poi_id::text AS poi_id,
                   ST_Y(a.geom) AS lat, ST_X(a.geom) AS lon,
                   similarity(a.text_norm, nicanav_normalize(%s)) AS score
            FROM alias a
            WHERE a.status = 'approved' AND a.text_norm %% nicanav_normalize(%s)
            ORDER BY score DESC, a.hits DESC
            LIMIT %s
        """
        return await self._fetch(sql, (text, text, limit))

    async def active_closures(self) -> list[list[list[float]]]:
        """Closure rings for Valhalla ``exclude_polygons`` (GeoJSON [lon, lat]).

        Only rows that are active *now*: a closure with a past ``ends_at`` must
        stop affecting routes without anybody remembering to clear it.
        """
        rows = await self._fetch(
            """
            SELECT ST_AsGeoJSON(geom) AS geom
            FROM closure
            WHERE active
              AND starts_at <= now()
              AND (ends_at IS NULL OR ends_at > now())
            """
        )
        rings: list[list[list[float]]] = []
        for row in rows:
            try:
                geometry = json.loads(row["geom"])
            except (TypeError, ValueError):
                continue
            if geometry.get("type") == "Polygon" and geometry.get("coordinates"):
                rings.append([[float(x), float(y)] for x, y in geometry["coordinates"][0]])
        return rings

    async def nearest_street(self, lat: float, lon: float, *, radius_m: float = 200) -> str | None:
        """Name of the nearest named landmark/POI, for reverse geocoding context.

        The road graph itself lives in Valhalla, not Postgres, so this returns
        the nearest *named thing* — which is what a Nicaraguan address uses
        anyway.
        """
        rows = await self._fetch(
            """
            SELECT name FROM gazetteer
            WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography, %s)
            ORDER BY geom <-> ST_SetSRID(ST_MakePoint(%s, %s), 4326)
            LIMIT 1
            """,
            (lon, lat, radius_m, lon, lat),
        )
        return rows[0]["name"] if rows else None

    async def highway_geometry(self, highway_key: str) -> list[list[float]] | None:
        """Ordered ``[[lon, lat], ...]`` centreline for a named carretera.

        Feeds the km-post geocoder, which measures chainage along it.  Returns
        ``None`` when the highway is unknown, so the caller can say "no sé dónde
        queda esa carretera" instead of guessing.
        """
        rows = await self._fetch(
            "SELECT ST_AsGeoJSON(geom) AS geom FROM highway WHERE highway_key = %s",
            (highway_key,),
        )
        if not rows:
            return None
        try:
            geometry = json.loads(rows[0]["geom"])
        except (TypeError, ValueError):
            return None
        if geometry.get("type") != "LineString":
            return None
        return [[float(x), float(y)] for x, y in geometry.get("coordinates", [])]

    async def kmpost_calibration(self, highway_key: str) -> list[dict[str, Any]]:
        """Surveyed km posts for a highway, nearest-first by km.

        OSM geometry length and the ministry's signs disagree; the signs win, so
        these rows correct the along-distance.
        """
        return await self._fetch(
            """
            SELECT km, ST_Y(geom) AS lat, ST_X(geom) AS lon, source, verified_at
            FROM kmpost WHERE highway_key = %s ORDER BY km
            """,
            (highway_key,),
        )

    async def latest_kpis(self, scope: str = "circle") -> dict[str, Any] | None:
        rows = await self._fetch(
            "SELECT taken_at, metrics FROM kpi_snapshot WHERE scope = %s ORDER BY taken_at DESC LIMIT 1",
            (scope,),
        )
        return rows[0] if rows else None

    # ------------------------------------------------------------------ #
    # Writes (everything lands in a moderation queue)
    # ------------------------------------------------------------------ #

    async def insert_report(
        self,
        kind: str,
        lat: float,
        lon: float,
        note: str | None,
        photo_url: str | None,
        client_id: str | None,
    ) -> str:
        row = await self._execute(
            """
            INSERT INTO report (kind, geom, note, photo_url, client_id)
            VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s, %s)
            RETURNING id::text AS id
            """,
            (kind, lon, lat, note, photo_url, client_id),
        )
        return str(row["id"])

    async def insert_suggestion(
        self, payload: dict[str, Any], lat: float, lon: float, client_id: str | None
    ) -> str:
        row = await self._execute(
            """
            INSERT INTO poi_suggestion (payload, geom, client_id)
            VALUES (%s::jsonb, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s)
            RETURNING id::text AS id
            """,
            (json.dumps(payload, ensure_ascii=False), lon, lat, client_id),
        )
        return str(row["id"])

    async def insert_alias(
        self, text: str, lat: float, lon: float, *, poi_id: str | None, created_by: str | None
    ) -> str:
        """Record a corrected address string.

        A repeated correction is stronger evidence than a first one, so an exact
        repeat bumps ``hits`` and confidence instead of inserting a duplicate.
        """
        row = await self._execute(
            """
            INSERT INTO alias (text, geom, poi_id, created_by)
            VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 4326), %s, %s)
            RETURNING id::text AS id
            """,
            (text, lon, lat, poi_id, created_by),
        )
        return str(row["id"])

    async def log_search(
        self, q: str, hits: int, lat: float | None, lon: float | None, kind: str | None = None
    ) -> None:
        """Log a query for the search-success KPI.

        Positions are rounded to ~1 km before they are stored: enough to find
        coverage gaps, not enough to follow anybody home.
        """
        if not self.available:
            return
        try:
            await self._execute(
                "INSERT INTO search_log (q, kind, hits, lat_coarse, lon_coarse) VALUES (%s, %s, %s, %s, %s)",
                (q[:200], kind, hits, _coarse(lat), _coarse(lon)),
            )
        except Exception:
            log.debug("search log insert failed", exc_info=True)

    async def log_route(
        self,
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
        *,
        costing: str,
        length_km: float | None,
        duration_s: float | None,
        alternates: int,
        reroute: bool,
    ) -> None:
        if not self.available:
            return
        try:
            await self._execute(
                """
                INSERT INTO route_log (from_lat, from_lon, to_lat, to_lon, costing, length_km,
                                       duration_s, alternates, reroute)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    _coarse(from_lat),
                    _coarse(from_lon),
                    _coarse(to_lat),
                    _coarse(to_lon),
                    costing,
                    length_km,
                    duration_s,
                    alternates,
                    reroute,
                ),
            )
        except Exception:
            log.debug("route log insert failed", exc_info=True)


def _coarse(value: float | None) -> float | None:
    """Round a coordinate to ~1 km (2 decimal places at these latitudes)."""
    return None if value is None else round(float(value), 2)


def _row_to_card(row: dict[str, Any], photos: list[dict[str, Any]] | None = None) -> PoiCard:
    return PoiCard(
        id=row["id"],
        name=row["name"],
        alt_names=list(row.get("name_alt") or []),
        category=row["category"],
        subcategory=row.get("subcategory"),
        cuisine=list(row.get("cuisine") or []),
        lat=float(row["lat"]),
        lon=float(row["lon"]),
        address_text=row.get("address_text"),
        phone=row.get("phone"),
        whatsapp=row.get("whatsapp"),
        website=row.get("website"),
        facebook=row.get("facebook"),
        instagram=row.get("instagram"),
        opening_hours=row.get("opening_hours"),
        price_level=row.get("price_level"),
        status=PoiStatus(row.get("status") or "unverified"),
        confidence=float(row.get("confidence") or 0.0),
        verified_at=row.get("verified_at"),
        verified_by=row.get("verified_by"),
        sources=row.get("sources") or {},
        photos=[
            PoiPhoto(
                url=photo["url"],
                credit=photo.get("credit"),
                license=photo.get("license"),
                taken_at=photo.get("taken_at"),
            )
            for photo in (photos or [])
        ],
    )


def _row_to_hit(row: dict[str, Any]) -> SearchHit:
    return SearchHit(
        id=row["id"],
        kind=SearchKind.POI,
        name=row["name"],
        lat=float(row["lat"]),
        lon=float(row["lon"]),
        category=row.get("category"),
        subcategory=row.get("subcategory"),
        address_text=row.get("address_text"),
        city=row.get("city"),
        distance_m=float(row["distance_m"]) if row.get("distance_m") is not None else None,
        verified=bool(row.get("verified_at")),
    )
