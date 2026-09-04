"""Geodesy and Nicaraguan spatial constants.

Stdlib only: this module is imported by pipeline jobs that run without shapely
and by the API container, so it carries its own small-scale geometry.  Every
function works in WGS84 degrees in and out; internally, short-range maths uses a
local equirectangular projection, which is accurate to well under a metre over
the tens of kilometres this project cares about.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = [
    "CUADRA_M",
    "CURATION_RADIUS_M",
    "DEFAULT_CUADRA_M",
    "DIRECTION_BEARINGS",
    "MGA",
    "MGA_LAT",
    "MGA_LON",
    "NICARAGUA_BBOX",
    "VARA_M",
    "bbox_around",
    "circle_polygon",
    "cuadra_length_m",
    "destination_point",
    "distance_from_mga_m",
    "haversine_m",
    "initial_bearing_deg",
    "interpolate_along",
    "line_length_m",
    "local_projection",
    "nearest_point_on_line",
    "resolve_direction",
]

EARTH_RADIUS_M = 6_371_008.8

# --------------------------------------------------------------------------- #
# Nicaragua constants
# --------------------------------------------------------------------------- #

#: Augusto C. Sandino International Airport (MGA) — the centre of the curation
#: circle.  Tiles and the routing graph cover the whole country; this point only
#: defines where field verification effort is spent.
MGA_LAT = 12.1415
MGA_LON = -86.1682
MGA: tuple[float, float] = (MGA_LAT, MGA_LON)

#: 30 statute miles.  Chosen in the build plan; see docs/PLAN.md section 1.
CURATION_RADIUS_M = 48_300.0

#: Whole-country bounding box (min_lon, min_lat, max_lon, max_lat), padded.
NICARAGUA_BBOX: tuple[float, float, float, float] = (-87.75, 10.68, -82.60, 15.05)

#: One Nicaraguan *vara* in metres.  Addresses are still quoted in varas
#: ("75 vrs al sur") even though nobody carries a vara stick any more.
VARA_M = 0.836

#: A *cuadra* is a city block.  Length varies by the colonial grid it was laid
#: out on, so it is per-city and calibratable; 100 m is the Managua/Granada norm.
DEFAULT_CUADRA_M = 100.0
CUADRA_M: dict[str, float] = {
    "managua": 100.0,
    "granada": 100.0,
    "masaya": 100.0,
    "leon": 100.0,
    "jinotepe": 100.0,
    "diriamba": 100.0,
    "tipitapa": 100.0,
    "ciudad sandino": 100.0,
    "esteli": 100.0,
}


def cuadra_length_m(city: str | None = None) -> float:
    """Block length for a city, accent-insensitively; falls back to 100 m."""
    if not city:
        return DEFAULT_CUADRA_M
    from common.text import normalize  # local import keeps the module cycle-free

    return CUADRA_M.get(normalize(city), DEFAULT_CUADRA_M)


#: Nicaraguan direction words mapped to compass bearings in degrees.
#:
#: Managua's cardinal slang is topographic, not solar: the lake is north and the
#: hills are south, while ``arriba``/``abajo`` follow the sunrise (east/west).
#: Getting these four wrong silently rotates every parsed address by 90°, so
#: they are asserted in the test-suite.
DIRECTION_BEARINGS: dict[str, float] = {
    # north
    "norte": 0.0,
    "n": 0.0,
    "al lago": 0.0,
    "lago": 0.0,
    "hacia el lago": 0.0,
    "al norte": 0.0,
    "septentrion": 0.0,
    # east  (sunrise side)
    "este": 90.0,
    "e": 90.0,
    "arriba": 90.0,
    "al este": 90.0,
    "hacia arriba": 90.0,
    "oriente": 90.0,
    "al oriente": 90.0,
    # south (toward the hills)
    "sur": 180.0,
    "s": 180.0,
    "a la montana": 180.0,
    "montana": 180.0,
    "hacia la montana": 180.0,
    "al sur": 180.0,
    "mediodia": 180.0,
    # west
    "oeste": 270.0,
    "o": 270.0,
    "w": 270.0,
    "abajo": 270.0,
    "al oeste": 270.0,
    "hacia abajo": 270.0,
    "occidente": 270.0,
    "poniente": 270.0,
    "al poniente": 270.0,
    # intercardinals (rare in speech, common in signage)
    "noreste": 45.0,
    "sureste": 135.0,
    "suroeste": 225.0,
    "noroeste": 315.0,
}


def resolve_direction(text: str) -> float | None:
    """Map a Nicaraguan direction phrase to a bearing, or ``None``.

    Accepts the phrase with or without accents, articles and ``hacia``:
    ``"hacia la montaña"``, ``"al Sur"`` and ``"sur"`` all give 180°.
    """
    from common.text import normalize

    key = normalize(text)
    if key in DIRECTION_BEARINGS:
        return DIRECTION_BEARINGS[key]
    # Strip leading prepositions/articles one at a time: "hacia el lago" -> "lago".
    tokens = key.split()
    while tokens and tokens[0] in {"hacia", "al", "a", "el", "la", "para", "rumbo", "sobre"}:
        tokens = tokens[1:]
        candidate = " ".join(tokens)
        if candidate in DIRECTION_BEARINGS:
            return DIRECTION_BEARINGS[candidate]
    return None


# --------------------------------------------------------------------------- #
# Geodesy
# --------------------------------------------------------------------------- #


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Forward azimuth from point 1 to point 2, in degrees clockwise from north."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination_point(
    lat: float, lon: float, bearing_deg: float, distance_m: float
) -> tuple[float, float]:
    """Travel ``distance_m`` along ``bearing_deg`` from a point; returns (lat, lon)."""
    delta = distance_m / EARTH_RADIUS_M
    theta = math.radians(bearing_deg)
    phi1, lambda1 = math.radians(lat), math.radians(lon)
    sin_phi2 = math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
    phi2 = math.asin(max(-1.0, min(1.0, sin_phi2)))
    lambda2 = lambda1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    return math.degrees(phi2), (math.degrees(lambda2) + 540.0) % 360.0 - 180.0


def distance_from_mga_m(lat: float, lon: float) -> float:
    """Straight-line distance from the airport, in metres."""
    return haversine_m(MGA_LAT, MGA_LON, lat, lon)


def bbox_around(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """Bounding box (min_lon, min_lat, max_lon, max_lat) enclosing a circle.

    Derived from geodesic destination points rather than a flat metres-per-degree
    constant, because the flat approximation under-covers by a few hundred metres
    at 48 km and would clip the corners off an ``osmium extract`` bbox.  A 0.5 %
    margin absorbs the difference between the cardinal points and the true
    east/west tangent of the circle.
    """
    margin = 1.005
    north, _ = destination_point(lat, lon, 0.0, radius_m * margin)
    south, _ = destination_point(lat, lon, 180.0, radius_m * margin)
    _, east = destination_point(lat, lon, 90.0, radius_m * margin)
    _, west = destination_point(lat, lon, 270.0, radius_m * margin)
    return (min(west, east), min(south, north), max(west, east), max(south, north))


def circle_polygon(
    lat: float = MGA_LAT,
    lon: float = MGA_LON,
    radius_m: float = CURATION_RADIUS_M,
    segments: int = 128,
) -> dict:
    """GeoJSON Feature for the curation circle, ready for ``osmium extract -p``.

    The ring is generated with true geodesic destination points (not a flat
    approximation), so it stays a real circle at Nicaraguan latitudes.
    """
    ring = [
        list(reversed(destination_point(lat, lon, 360.0 * i / segments, radius_m)))
        for i in range(segments)
    ]
    ring.append(ring[0])
    return {
        "type": "Feature",
        "properties": {"name": "nicanav curation circle", "radius_m": radius_m},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


# --------------------------------------------------------------------------- #
# Local planar helpers (short-range: offsets, snapping, along-line measures)
# --------------------------------------------------------------------------- #


def local_projection(lat0: float, lon0: float):
    """Return ``(to_xy, to_lonlat)`` for an equirectangular frame at (lat0, lon0).

    ``to_xy(lat, lon) -> (x_east_m, y_north_m)``; ``to_lonlat(x, y) -> (lat, lon)``.
    Accurate to ~0.1 % over 50 km, which is far below OSM's own positional error.
    """
    m_per_deg_lat = (
        111_132.92
        - 559.82 * math.cos(2 * math.radians(lat0))
        + 1.175 * math.cos(4 * math.radians(lat0))
    )
    m_per_deg_lon = 111_412.84 * math.cos(math.radians(lat0)) - 93.5 * math.cos(
        3 * math.radians(lat0)
    )

    def to_xy(lat: float, lon: float) -> tuple[float, float]:
        return ((lon - lon0) * m_per_deg_lon, (lat - lat0) * m_per_deg_lat)

    def to_lonlat(x: float, y: float) -> tuple[float, float]:
        return (lat0 + y / m_per_deg_lat, lon0 + x / m_per_deg_lon)

    return to_xy, to_lonlat


Coord = Sequence[float]  # (lon, lat), GeoJSON order


def line_length_m(coords: Sequence[Coord]) -> float:
    """Total length of a GeoJSON ``[[lon, lat], ...]`` line, in metres."""
    return sum(
        haversine_m(coords[i][1], coords[i][0], coords[i + 1][1], coords[i + 1][0])
        for i in range(len(coords) - 1)
    )


def interpolate_along(coords: Sequence[Coord], distance_m: float) -> tuple[float, float]:
    """Point ``distance_m`` along a line, as (lat, lon).

    Clamps to the endpoints, so a km-post beyond the mapped end of a carretera
    returns the end of the carretera rather than raising.
    """
    if not coords:
        raise ValueError("interpolate_along needs at least one coordinate")
    if len(coords) == 1 or distance_m <= 0:
        return (coords[0][1], coords[0][0])
    remaining = distance_m
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i][0], coords[i][1]
        lon2, lat2 = coords[i + 1][0], coords[i + 1][1]
        seg = haversine_m(lat1, lon1, lat2, lon2)
        if seg <= 0:
            continue
        if remaining <= seg:
            bearing = initial_bearing_deg(lat1, lon1, lat2, lon2)
            return destination_point(lat1, lon1, bearing, remaining)
        remaining -= seg
    return (coords[-1][1], coords[-1][0])


def nearest_point_on_line(
    lat: float, lon: float, coords: Sequence[Coord]
) -> tuple[tuple[float, float], float, float, int]:
    """Snap a point to a polyline.

    Returns ``((lat, lon), distance_m, along_m, segment_index)`` where
    ``distance_m`` is the perpendicular offset from the line and ``along_m`` is
    how far along the line the snapped point sits.
    """
    if not coords:
        raise ValueError("nearest_point_on_line needs at least one coordinate")
    to_xy, to_lonlat = local_projection(lat, lon)
    px, py = to_xy(lat, lon)

    best = (float("inf"), 0.0, 0, 0.0, 0.0)  # dist, along, index, x, y
    travelled = 0.0
    for i in range(len(coords) - 1):
        ax, ay = to_xy(coords[i][1], coords[i][0])
        bx, by = to_xy(coords[i + 1][1], coords[i + 1][0])
        dx, dy = bx - ax, by - ay
        seg_len = math.hypot(dx, dy)
        if seg_len == 0:
            continue
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (seg_len * seg_len)))
        cx, cy = ax + t * dx, ay + t * dy
        dist = math.hypot(px - cx, py - cy)
        if dist < best[0]:
            best = (dist, travelled + t * seg_len, i, cx, cy)
        travelled += seg_len

    if best[0] == float("inf"):  # degenerate line: every segment zero-length
        only = coords[0]
        return ((only[1], only[0]), haversine_m(lat, lon, only[1], only[0]), 0.0, 0)

    dist, along, index, cx, cy = best
    return (to_lonlat(cx, cy), dist, along, index)
