"""Nightly data-quality KPIs for the curation circle.

These are the numbers that decide where the next field drive goes (docs/PLAN.md
section 8).  They are deliberately unflattering: the point is to see the gaps,
not to produce a green dashboard.  "84 % of primary roads have a surface tag"
is a to-do list; "coverage: good" is not.

Input is the OSM road export produced by ``pipeline/fetch_osm.sh``
(``osmium export`` over the circle clip), plus optional counts from PostGIS.
Both are optional individually — a database outage should not stop the road KPIs
from being computed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from common.config import get_settings
from common.geo import CURATION_RADIUS_M, distance_from_mga_m, line_length_m
from pipeline.common.io import atomic_write_text, read_geojsonseq, setup_logging

__all__ = ["compute_road_kpis", "main"]

log = logging.getLogger(__name__)

#: The classes worth tracking separately.  Below `residential` the tagging is too
#: sparse in Nicaragua for a percentage to mean anything.
TRACKED_CLASSES = (
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "service",
    "track",
)

#: Classes where a missing `oneway` or `surface` tag actually costs a driver
#: something — these are the roads routing decisions are made on.
DRIVER_CRITICAL = ("primary", "secondary", "tertiary")


def _feature_length_m(feature: dict[str, Any]) -> float:
    geometry = feature.get("geometry") or {}
    if geometry.get("type") == "LineString":
        return line_length_m(geometry.get("coordinates") or [])
    if geometry.get("type") == "MultiLineString":
        return sum(line_length_m(part) for part in geometry.get("coordinates") or [])
    return 0.0


def _first_coordinate(feature: dict[str, Any]) -> tuple[float, float] | None:
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates") or []
    if geometry.get("type") == "LineString" and coordinates:
        return (float(coordinates[0][1]), float(coordinates[0][0]))
    if geometry.get("type") == "MultiLineString" and coordinates and coordinates[0]:
        return (float(coordinates[0][0][1]), float(coordinates[0][0][0]))
    return None


def compute_road_kpis(
    features: Iterable[dict[str, Any]], *, circle_only: bool = True
) -> dict[str, Any]:
    """Road-network KPIs from an OSM GeoJSON-seq export.

    ``circle_only`` restricts the count to the 48.3 km curation area, which is
    what the percentages are supposed to describe — mixing in the whole country
    would hide exactly the gaps this is meant to expose.
    """
    length_by_class: dict[str, float] = defaultdict(float)
    surfaced_by_class: dict[str, float] = defaultdict(float)
    oneway_by_class: dict[str, float] = defaultdict(float)
    maxspeed_by_class: dict[str, float] = defaultdict(float)
    unpaved_m = 0.0
    roundabouts = 0
    traffic_calming = 0
    milestones = 0
    named_m = 0.0
    total_m = 0.0
    surface_values: Counter[str] = Counter()

    for feature in features:
        tags = feature.get("properties") or {}
        highway = tags.get("highway")
        if not highway:
            # The same export carries the nodes we count below.
            if tags.get("traffic_calming"):
                traffic_calming += 1
            if highway == "milestone" or tags.get("highway") == "milestone":
                milestones += 1
            continue

        if circle_only:
            point = _first_coordinate(feature)
            if point is None or distance_from_mga_m(*point) > CURATION_RADIUS_M:
                continue

        length_m = _feature_length_m(feature)
        if length_m <= 0:
            continue

        road_class = highway.replace("_link", "")
        total_m += length_m
        length_by_class[road_class] += length_m

        surface = (tags.get("surface") or "").strip()
        if surface:
            surfaced_by_class[road_class] += length_m
            surface_values[surface] += 1
            if surface not in {"asphalt", "concrete", "paved", "paving_stones", "concrete:plates"}:
                unpaved_m += length_m
        if tags.get("oneway"):
            oneway_by_class[road_class] += length_m
        if tags.get("maxspeed"):
            maxspeed_by_class[road_class] += length_m
        if tags.get("name"):
            named_m += length_m
        if tags.get("junction") in {"roundabout", "circular"}:
            roundabouts += 1
        if tags.get("traffic_calming"):
            traffic_calming += 1
        if highway == "milestone":
            milestones += 1

    def _share(numerator: dict[str, float], classes: Sequence[str]) -> float:
        total = sum(length_by_class.get(name, 0.0) for name in classes)
        if total <= 0:
            return 0.0
        return round(sum(numerator.get(name, 0.0) for name in classes) / total, 4)

    return {
        "scope": "circle" if circle_only else "country",
        "road_km_total": round(total_m / 1000, 1),
        "road_km_by_class": {
            name: round(length_by_class.get(name, 0.0) / 1000, 1)
            for name in TRACKED_CLASSES
            if length_by_class.get(name)
        },
        "surface_tagged_share": _share(surfaced_by_class, TRACKED_CLASSES),
        # The one that matters most for routing quality: a missing oneway on a
        # Managua primary sends drivers the wrong way up a dual carriageway.
        "oneway_tagged_share_driver_critical": _share(oneway_by_class, DRIVER_CRITICAL),
        "surface_tagged_share_driver_critical": _share(surfaced_by_class, DRIVER_CRITICAL),
        "maxspeed_tagged_share_driver_critical": _share(maxspeed_by_class, DRIVER_CRITICAL),
        "named_share": round(named_m / total_m, 4) if total_m else 0.0,
        "unpaved_km": round(unpaved_m / 1000, 1),
        "roundabouts": roundabouts,
        "traffic_calming": traffic_calming,
        "milestones": milestones,
        "surface_values": dict(surface_values.most_common(12)),
    }


_POI_SQL = """
SELECT category,
       count(*) AS total,
       count(*) FILTER (WHERE verified_at > now() - interval '12 months') AS verified_recent,
       count(*) FILTER (WHERE phone IS NOT NULL OR facebook IS NOT NULL OR whatsapp IS NOT NULL) AS contactable
FROM poi
WHERE in_circle AND status <> 'closed'
GROUP BY category
ORDER BY total DESC
"""

_SEARCH_SQL = """
SELECT count(*) AS searches,
       count(*) FILTER (WHERE hits > 0) AS with_hits,
       count(*) FILTER (WHERE clicked_id IS NOT NULL) AS clicked
FROM search_log
WHERE created_at > now() - interval '7 days'
"""

_QUEUE_SQL = """
SELECT
  (SELECT count(*) FROM poi_match_queue WHERE decision = 'pending') AS match_queue,
  (SELECT count(*) FROM poi_suggestion  WHERE status   = 'pending') AS suggestions,
  (SELECT count(*) FROM report          WHERE status   = 'pending') AS reports,
  (SELECT count(*) FROM alias           WHERE status   = 'pending') AS aliases,
  (SELECT count(*) FROM closure         WHERE active)               AS active_closures,
  (SELECT count(*) FROM gazetteer)                                  AS landmarks,
  (SELECT count(*) FROM gazetteer WHERE former)                     AS former_landmarks
"""


def compute_db_kpis(dsn: str) -> dict[str, Any]:
    """POI, search and moderation KPIs from PostGIS; empty on any failure."""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        log.warning("psycopg not installed; skipping database KPIs")
        return {}

    try:
        with (
            psycopg.connect(dsn, connect_timeout=5) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            cur.execute(_POI_SQL)
            by_category = list(cur.fetchall())
            cur.execute(_SEARCH_SQL)
            search = cur.fetchone() or {}
            cur.execute(_QUEUE_SQL)
            queues = cur.fetchone() or {}
    except Exception:
        log.warning("database unavailable; skipping database KPIs", exc_info=True)
        return {}

    total = sum(row["total"] for row in by_category)
    verified = sum(row["verified_recent"] for row in by_category)
    contactable = sum(row["contactable"] for row in by_category)
    searches = search.get("searches") or 0

    return {
        "poi_total_in_circle": total,
        "poi_by_category": {row["category"]: row["total"] for row in by_category},
        # The plan's acceptance criterion for phase 2 is >= 3,000 POIs in the
        # circle, each ideally with a phone or a social link.
        "poi_verified_12mo_share": round(verified / total, 4) if total else 0.0,
        "poi_contactable_share": round(contactable / total, 4) if total else 0.0,
        "search_7d": searches,
        # Search success: a query that returns nothing is a coverage gap with a
        # user attached to it.
        "search_hit_share_7d": round((search.get("with_hits") or 0) / searches, 4)
        if searches
        else 0.0,
        "search_click_share_7d": round((search.get("clicked") or 0) / searches, 4)
        if searches
        else 0.0,
        **{f"queue_{key}": value for key, value in (queues or {}).items()},
    }


def _print_summary(metrics: dict[str, Any]) -> None:
    print(f"road km in the circle      {metrics.get('road_km_total', 0):.0f}")
    for name, km in (metrics.get("road_km_by_class") or {}).items():
        print(f"  {name:<14} {km:8.1f} km")
    print(f"surface tagged             {metrics.get('surface_tagged_share', 0):.0%}")
    print(
        f"  primary/sec/tertiary     {metrics.get('surface_tagged_share_driver_critical', 0):.0%}"
    )
    print(f"oneway on driver-critical  {metrics.get('oneway_tagged_share_driver_critical', 0):.0%}")
    print(
        f"maxspeed on driver-critical{metrics.get('maxspeed_tagged_share_driver_critical', 0):.0%}"
    )
    print(f"named roads                {metrics.get('named_share', 0):.0%}")
    print(f"rotondas                   {metrics.get('roundabouts', 0)}")
    print(f"reductores (traffic calming){metrics.get('traffic_calming', 0)}")
    print(f"km posts mapped            {metrics.get('milestones', 0)}")
    if "poi_total_in_circle" in metrics:
        print(f"POIs in the circle         {metrics['poi_total_in_circle']}")
        print(f"  verified in 12 months    {metrics.get('poi_verified_12mo_share', 0):.0%}")
        print(f"  with a phone or social   {metrics.get('poi_contactable_share', 0):.0%}")
        print(
            f"landmarks                  {metrics.get('queue_landmarks', 0)} "
            f"({metrics.get('queue_former_landmarks', 0)} «donde fue»)"
        )
        print(
            f"pending moderation         match={metrics.get('queue_match_queue', 0)} "
            f"lugares={metrics.get('queue_suggestions', 0)} "
            f"reportes={metrics.get('queue_reports', 0)} "
            f"direcciones={metrics.get('queue_aliases', 0)}"
        )


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.qa.kpis",
        description="Compute nightly data-quality KPIs for the curation circle.",
    )
    parser.add_argument(
        "--roads", type=Path, default=settings.data_dir / "exports" / "roads.geojsonseq"
    )
    parser.add_argument("--dsn", default=settings.database_url)
    parser.add_argument("--no-db", action="store_true")
    parser.add_argument(
        "--country", action="store_true", help="whole country instead of the circle"
    )
    parser.add_argument(
        "--output", type=Path, default=None, help="write the snapshot as JSON (for kpi_snapshot)"
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    metrics: dict[str, Any] = {}
    if args.roads.exists():
        metrics.update(compute_road_kpis(read_geojsonseq(args.roads), circle_only=not args.country))
    else:
        log.warning("no road export at %s; skipping road KPIs", args.roads)

    if not args.no_db:
        metrics.update(compute_db_kpis(args.dsn))

    if not metrics:
        log.error("nothing to report: no road export and no database")
        return 1

    _print_summary(metrics)

    if args.output:
        atomic_write_text(args.output, json.dumps(metrics, ensure_ascii=False, indent=2))
        log.info("wrote %s", args.output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
