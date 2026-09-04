"""Find roads the router cannot reach.

A disconnected road network is the quietest data failure there is: the map draws
correctly, search finds the street, and routing to it fails or sends the driver
somewhere else entirely.  It happens constantly in practice — a new residential
street drawn a metre short of the one it meets, a bridge split across two ways
that never share a node.

The check is the one docs/PLAN.md section 8 describes: ask Valhalla for a large
isochrone from MGA, then list every named road in the circle whose geometry falls
outside it.  Anything on that list is either genuinely unreachable by car or a
connectivity bug worth an evening in JOSM.

Point-in-polygon is implemented here rather than pulled from shapely because the
pipeline image should stay slim and this is forty lines of ray casting.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from common.config import get_settings
from common.geo import CURATION_RADIUS_M, MGA_LAT, MGA_LON, distance_from_mga_m
from common.valhalla import ValhallaClient, ValhallaError
from pipeline.common.io import atomic_write_text, read_geojsonseq, setup_logging

__all__ = ["main", "point_in_geometry", "point_in_polygon", "unreachable_roads"]

log = logging.getLogger(__name__)


def point_in_ring(lat: float, lon: float, ring: Sequence[Sequence[float]]) -> bool:
    """Ray casting over one GeoJSON ring of ``[lon, lat]`` pairs.

    A point exactly on an edge is *unspecified* by this algorithm and may land
    either way.  That is acceptable here — the question is "can the router reach
    this road", and a road sitting exactly on an isochrone boundary is reachable
    by definition — but it is stated rather than silently assumed.
    """
    inside = False
    count = len(ring)
    if count < 3:
        return False
    for i in range(count):
        x1, y1 = float(ring[i][0]), float(ring[i][1])
        x2, y2 = float(ring[(i + 1) % count][0]), float(ring[(i + 1) % count][1])
        if (y1 > lat) != (y2 > lat):
            x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_at:
                inside = not inside
    return inside


def point_in_polygon(lat: float, lon: float, rings: Sequence[Sequence[Sequence[float]]]) -> bool:
    """A GeoJSON Polygon: inside the outer ring and outside every hole.

    Isochrones really do have holes — an unreachable pocket inside a reachable
    area is exactly the thing this job is looking for.
    """
    if not rings:
        return False
    if not point_in_ring(lat, lon, rings[0]):
        return False
    return not any(point_in_ring(lat, lon, hole) for hole in rings[1:])


def point_in_geometry(lat: float, lon: float, geometry: dict[str, Any]) -> bool:
    """Point-in-polygon over a Polygon, MultiPolygon or FeatureCollection."""
    kind = geometry.get("type")
    if kind == "Polygon":
        return point_in_polygon(lat, lon, geometry.get("coordinates") or [])
    if kind == "MultiPolygon":
        return any(point_in_polygon(lat, lon, rings) for rings in geometry.get("coordinates") or [])
    if kind == "Feature":
        return point_in_geometry(lat, lon, geometry.get("geometry") or {})
    if kind == "FeatureCollection":
        return any(
            point_in_geometry(lat, lon, feature) for feature in geometry.get("features") or []
        )
    # A LineString contour (Valhalla returns these when polygons=false) cannot
    # answer containment; say so rather than guessing.
    return False


def _representative_point(feature: dict[str, Any]) -> tuple[float, float] | None:
    """A point on the road: its midpoint vertex, which is inside any sane way."""
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates") or []
    if geometry.get("type") == "MultiLineString" and coordinates:
        coordinates = coordinates[0]
    if geometry.get("type") not in {"LineString", "MultiLineString"} or not coordinates:
        return None
    middle = coordinates[len(coordinates) // 2]
    return (float(middle[1]), float(middle[0]))


def unreachable_roads(
    features: Iterable[dict[str, Any]],
    reachable: dict[str, Any],
    *,
    circle_only: bool = True,
    named_only: bool = True,
) -> list[dict[str, Any]]:
    """Roads whose representative point falls outside the reachable area.

    ``named_only`` is on by default: an unnamed service road that the router
    cannot reach is usually a driveway, while an unreachable *named* road is
    almost always a connectivity bug.
    """
    stranded: list[dict[str, Any]] = []
    for feature in features:
        tags = feature.get("properties") or {}
        highway = tags.get("highway")
        if not highway or highway in {"footway", "path", "steps", "cycleway", "pedestrian"}:
            continue
        name = tags.get("name")
        if named_only and not name:
            continue
        point = _representative_point(feature)
        if point is None:
            continue
        if circle_only and distance_from_mga_m(*point) > CURATION_RADIUS_M:
            continue
        if not point_in_geometry(point[0], point[1], reachable):
            stranded.append(
                {
                    "name": name,
                    "highway": highway,
                    "lat": round(point[0], 6),
                    "lon": round(point[1], 6),
                    "id": feature.get("id"),
                    "josm": f"https://www.openstreetmap.org/#map=18/{point[0]:.5f}/{point[1]:.5f}",
                }
            )
    return stranded


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.qa.islands",
        description="List roads in the circle that the router cannot reach from MGA.",
    )
    parser.add_argument(
        "--roads", type=Path, default=settings.data_dir / "exports" / "roads.geojsonseq"
    )
    parser.add_argument("--valhalla-url", default=settings.valhalla_url)
    parser.add_argument("--lat", type=float, default=MGA_LAT)
    parser.add_argument("--lon", type=float, default=MGA_LON)
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=180.0,
        help="isochrone contour; anything unreached in this long is suspect (default 180)",
    )
    parser.add_argument("--include-unnamed", action="store_true")
    parser.add_argument(
        "--threshold", type=int, default=25, help="fail above this many stranded roads"
    )
    parser.add_argument("--json", dest="json_out", type=Path, default=None)
    parser.add_argument(
        "--isochrone",
        type=Path,
        default=None,
        help="use a saved isochrone GeoJSON instead of calling Valhalla",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    if not args.roads.exists():
        log.error("no road export at %s; run pipeline/fetch_osm.sh first", args.roads)
        return 2

    if args.isochrone:
        reachable = json.loads(args.isochrone.read_text(encoding="utf-8"))
    else:
        try:
            with ValhallaClient(args.valhalla_url) as client:
                reachable = client.isochrone(
                    args.lat, args.lon, contours_minutes=(args.max_minutes,), polygons=True
                )
        except ValhallaError:
            log.exception("could not compute the isochrone")
            return 2

    stranded = unreachable_roads(
        read_geojsonseq(args.roads), reachable, named_only=not args.include_unnamed
    )

    print(f"{len(stranded)} road(s) unreachable within {args.max_minutes:.0f} min of MGA")
    for road in stranded[:40]:
        print(f"  {road['highway']:<12} {road['name'] or '(sin nombre)':<40} {road['josm']}")
    if len(stranded) > 40:
        print(f"  … and {len(stranded) - 40} more")

    if args.json_out:
        atomic_write_text(
            args.json_out,
            json.dumps({"count": len(stranded), "roads": stranded}, ensure_ascii=False, indent=2),
        )
        print(f"wrote {args.json_out}")

    if len(stranded) > args.threshold:
        print(f"\nabove the threshold of {args.threshold}: the network has connectivity problems")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
