"""Export published POIs for tippecanoe and for the search index.

Reads from PostGIS when it is reachable (the system of record, including every
moderation decision) and falls back to the conflation output otherwise, so a
database outage degrades to "yesterday's POIs" rather than "no POIs".

Two outputs, deliberately separate:

* ``pois.geojsonseq`` — for tippecanoe.  Carries per-feature ``tippecanoe``
  minzoom hints so that the categories a driver needs (gasolinera,
  vulcanización, hospital, cajero) survive ``--drop-densest-as-needed`` at low
  zoom while a pulpería does not.
* ``pois_index.jsonl`` — for Meilisearch, in the document shape docs/SPEC.md
  section 8 defines.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from common.config import get_settings
from common.text import normalize
from pipeline.common.io import atomic_write_text, read_geojsonseq, setup_logging, write_geojsonseq
from pipeline.pois.taxonomy import FALLBACK_CATEGORY, group_of, min_zoom_for

__all__ = ["iter_from_db", "main", "to_index_document", "to_tile_feature"]

log = logging.getLogger(__name__)

_SELECT_POIS = """
SELECT p.id::text AS id, p.name, p.name_alt, p.category, p.subcategory, p.cuisine,
       ST_Y(p.geom) AS lat, ST_X(p.geom) AS lon,
       p.address_text, p.city, p.phone, p.whatsapp, p.website, p.facebook, p.instagram,
       p.opening_hours, p.price_level, p.status, p.confidence, p.popularity,
       p.verified_at, p.sources
FROM poi p
WHERE p.status <> 'closed'
ORDER BY p.category, p.id
"""

_SELECT_GAZETTEER = """
SELECT g.id::text AS id, g.name, g.name_alt, g.kind, g.city, g.former, g.popularity,
       ST_Y(g.geom) AS lat, ST_X(g.geom) AS lon
FROM gazetteer g
"""


def to_tile_feature(row: dict[str, Any]) -> dict[str, Any]:
    """One POI as a tippecanoe-ready GeoJSON feature.

    Tile properties are kept to what the *map* needs to draw and to what a tap
    needs to open the card: the id, the label, the icon key and the flags the
    style branches on.  Phone numbers and opening hours are fetched per card
    from the API instead of being baked into every tile — they change far more
    often than the tiles are rebuilt.
    """
    category = row.get("category") or FALLBACK_CATEGORY
    minzoom = min_zoom_for(category)
    properties = {
        "id": row["id"],
        "name": row["name"],
        "category": category,
        "group": group_of(category),
        "verified": bool(row.get("verified_at")),
        "rank": _rank(row),
    }
    if row.get("subcategory"):
        properties["subcategory"] = row["subcategory"]

    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [float(row["lon"]), float(row["lat"])]},
        "properties": properties,
        # tippecanoe reads this extension object per feature: it is how the
        # categories drivers actually need stay visible when the densest cells
        # are being thinned.
        "tippecanoe": {"minzoom": minzoom, "maxzoom": 14},
    }


def _rank(row: dict[str, Any]) -> int:
    """Label priority within a tile, 1 (most important) to 10.

    Verified places outrank unverified ones, and a place with contact details is
    more likely to be a real, current business than a bare name.
    """
    score = 5
    if row.get("verified_at"):
        score -= 2
    if row.get("phone") or row.get("facebook") or row.get("whatsapp"):
        score -= 1
    score -= round(2 * float(row.get("popularity") or 0.0))
    return max(1, min(10, score))


def to_index_document(row: dict[str, Any]) -> dict[str, Any]:
    """One POI as a Meilisearch document (docs/SPEC.md section 8)."""
    category = row.get("category") or FALLBACK_CATEGORY
    name = row["name"]
    alt_names = list(row.get("name_alt") or [])
    return {
        "id": f"poi:{row['id']}",
        "kind": "poi",
        "name": name,
        "alt_names": alt_names,
        # Accent-folded copy so "gueguense" matches "Güegüense" even when typo
        # tolerance would not stretch that far.
        "name_norm": normalize(" ".join([name, *alt_names])),
        "category": category,
        "group": group_of(category),
        "subcategory": row.get("subcategory"),
        "address_text": row.get("address_text"),
        "city": row.get("city"),
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "_geo": {"lat": float(row["lat"]), "lng": float(row["lon"])},
        "popularity": float(row.get("popularity") or 0.0),
        "verified": bool(row.get("verified_at")),
        "former": False,
    }


def gazetteer_document(row: dict[str, Any]) -> dict[str, Any]:
    """One landmark as a Meilisearch document.

    Landmarks are indexed alongside POIs because the relative-address resolver
    searches the same index when PostGIS is unavailable, and because people
    search for "rotonda centroamérica" as readily as for a restaurant.
    """
    name = row["name"]
    alt_names = list(row.get("name_alt") or [])
    return {
        "id": f"landmark:{row['id']}",
        "kind": "landmark",
        "name": name,
        "alt_names": alt_names,
        "name_norm": normalize(" ".join([name, *alt_names])),
        "category": row.get("kind"),
        "group": "referencia",
        "city": row.get("city"),
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "_geo": {"lat": float(row["lat"]), "lng": float(row["lon"])},
        "popularity": float(row.get("popularity") or 0.0),
        "verified": True,
        "former": bool(row.get("former")),
    }


def iter_from_db(dsn: str, sql: str = _SELECT_POIS) -> Iterator[dict[str, Any]]:
    """Stream rows from PostGIS without loading the country into memory."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn) as conn, conn.cursor(row_factory=dict_row, name="poi_export") as cur:
        cur.itersize = 2_000
        cur.execute(sql)
        yield from cur


def iter_from_file(path: Path) -> Iterator[dict[str, Any]]:
    """Fallback source: the conflation output, flattened to row dicts."""
    for feature in read_geojsonseq(path):
        properties = dict(feature.get("properties") or {})
        coordinates = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coordinates) >= 2:
            properties.setdefault("lon", coordinates[0])
            properties.setdefault("lat", coordinates[1])
        if properties.get("name") and properties.get("lat") is not None:
            properties.setdefault("id", properties.get("source_id") or properties["name"])
            yield properties


def _write_index(path: Path, documents: Iterable[dict[str, Any]]) -> int:
    lines = [json.dumps(document, ensure_ascii=False) for document in documents]
    atomic_write_text(path, "\n".join(lines) + ("\n" if lines else ""))
    return len(lines)


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    exports = settings.data_dir / "exports"
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.pois.export_geojson",
        description="Export published POIs for tippecanoe and Meilisearch.",
    )
    parser.add_argument("--dsn", default=settings.database_url)
    parser.add_argument(
        "--fallback-input",
        type=Path,
        default=exports / "pois_merged.geojsonseq",
        help="used when the database is unreachable",
    )
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="explicit offline/demo export only; excludes moderation",
    )
    parser.add_argument("--tiles-output", type=Path, default=exports / "pois.geojsonseq")
    parser.add_argument("--index-output", type=Path, default=exports / "pois_index.jsonl")
    parser.add_argument(
        "--no-gazetteer", action="store_true", help="skip landmark documents in the index"
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    rows: list[dict[str, Any]]
    landmarks: list[dict[str, Any]] = []
    try:
        rows = list(iter_from_db(args.dsn))
        if not args.no_gazetteer:
            landmarks = list(iter_from_db(args.dsn, _SELECT_GAZETTEER))
        log.info("read %d POIs and %d landmarks from the database", len(rows), len(landmarks))
    except Exception:
        if not args.allow_fallback:
            log.exception("Database export failed; refusing to bypass moderation")
            return 1
        log.warning("database unavailable; falling back to %s", args.fallback_input, exc_info=True)
        if not args.fallback_input.exists():
            log.error("no fallback input either; nothing to export")
            return 1
        rows = list(iter_from_file(args.fallback_input))
        log.info("read %d POIs from %s", len(rows), args.fallback_input)

    if not rows:
        # Publishing an empty tile set would blank the map. Yesterday's is better.
        log.error("no POIs to export; refusing to publish an empty layer")
        return 1

    try:
        written = write_geojsonseq(args.tiles_output, (to_tile_feature(row) for row in rows))
        documents = [to_index_document(row) for row in rows]
        documents.extend(gazetteer_document(row) for row in landmarks)
        indexed = _write_index(args.index_output, documents)
    except Exception:
        log.exception("export failed")
        return 1

    log.info("wrote %d tile features and %d index documents", written, indexed)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
