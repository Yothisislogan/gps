"""Load the conflated POI export into PostGIS.

After conflation the database is the system of record: the admin queue edits
rows here, users flag them here, and the published tiles and search index are
both derived from here.  That ordering matters — if tiles were built straight
from the conflation output, every moderation decision would be silently undone
by the next nightly build.

Upserts are keyed on provenance rather than on position, so a POI that moves 20 m
between releases updates in place instead of duplicating, and a human-verified
row is never overwritten by an automated one.
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
from common.geo import CURATION_RADIUS_M, distance_from_mga_m
from pipeline.common.io import read_geojsonseq, setup_logging

__all__ = ["load", "main"]

log = logging.getLogger(__name__)

#: Licences by source, kept per row so the published dataset stays attributable.
#: See docs/LICENSES.md.
_LICENSES = {
    "osm": "ODbL-1.0",
    "overture": "CDLA-Permissive-2.0",
    "survey": "ODbL-1.0",
    "user": "ODbL-1.0",
    "wikidata": "CC0-1.0",
}

_UPSERT_POI = """
INSERT INTO poi (
    name, name_alt, category, subcategory, cuisine, geom, address_text, city,
    phone, whatsapp, website, facebook, instagram, opening_hours, price_level,
    status, confidence, popularity, sources, in_circle
)
VALUES (
    %(name)s, %(name_alt)s, %(category)s, %(subcategory)s, %(cuisine)s,
    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), %(address_text)s, %(city)s,
    %(phone)s, %(whatsapp)s, %(website)s, %(facebook)s, %(instagram)s,
    %(opening_hours)s, %(price_level)s, %(status)s, %(confidence)s, %(popularity)s,
    %(sources)s::jsonb, %(in_circle)s
)
ON CONFLICT DO NOTHING
RETURNING id
"""

#: Find the existing row for a merged POI by any of its source ids.  A GERS id
#: is the strongest key (it survives Overture releases); an OSM id is next.
_FIND_EXISTING = """
SELECT id, verified_at
FROM poi
WHERE (%(gers_id)s::text IS NOT NULL AND sources ->> 'overture_gers_id' = %(gers_id)s)
   OR (%(osm_id)s::text  IS NOT NULL AND sources ->> 'osm_id' = %(osm_id)s)
LIMIT 1
"""

#: Fields an automated import may refresh.  Position, name and category are
#: refreshed only when the row has never been human-verified — a field survey
#: outranks anything a scraper says, which is the whole point of surveying.
_UPDATE_UNVERIFIED = """
UPDATE poi SET
    name = %(name)s, name_alt = %(name_alt)s, category = %(category)s,
    subcategory = %(subcategory)s, cuisine = %(cuisine)s,
    geom = ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326),
    address_text = COALESCE(%(address_text)s, address_text),
    city = COALESCE(%(city)s, city),
    phone = COALESCE(%(phone)s, phone),
    whatsapp = COALESCE(%(whatsapp)s, whatsapp),
    website = COALESCE(%(website)s, website),
    facebook = COALESCE(%(facebook)s, facebook),
    instagram = COALESCE(%(instagram)s, instagram),
    opening_hours = COALESCE(%(opening_hours)s, opening_hours),
    status = %(status)s, confidence = %(confidence)s,
    sources = poi.sources || %(sources)s::jsonb,
    in_circle = %(in_circle)s
WHERE id = %(id)s
"""

#: A verified row keeps its own name, category and position; only contact details
#: and provenance are topped up, because those go stale fastest and a survey
#: rarely captures a new Facebook page.
_UPDATE_VERIFIED = """
UPDATE poi SET
    phone = COALESCE(phone, %(phone)s),
    whatsapp = COALESCE(whatsapp, %(whatsapp)s),
    website = COALESCE(website, %(website)s),
    facebook = COALESCE(facebook, %(facebook)s),
    instagram = COALESCE(instagram, %(instagram)s),
    opening_hours = COALESCE(opening_hours, %(opening_hours)s),
    sources = poi.sources || %(sources)s::jsonb
WHERE id = %(id)s
"""

_UPSERT_SOURCE = """
INSERT INTO poi_source (poi_id, source, source_id, name, category, geom, license, confidence, raw)
VALUES (%(poi_id)s, %(source)s, %(source_id)s, %(name)s, %(category)s,
        ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326), %(license)s, %(confidence)s, %(raw)s::jsonb)
ON CONFLICT (source, source_id) DO UPDATE SET
    poi_id = EXCLUDED.poi_id,
    name = EXCLUDED.name,
    category = EXCLUDED.category,
    geom = EXCLUDED.geom,
    confidence = EXCLUDED.confidence,
    raw = EXCLUDED.raw,
    fetched_at = now()
"""


def _params(properties: dict[str, Any]) -> dict[str, Any]:
    """Map a merged POI's properties onto the SQL parameter names."""
    sources = properties.get("sources") or {}
    if isinstance(sources, str):
        sources = json.loads(sources)
    lat, lon = float(properties["lat"]), float(properties["lon"])
    return {
        "name": properties.get("name"),
        "name_alt": list(properties.get("name_alt") or []),
        "category": properties.get("category") or "otro",
        "subcategory": properties.get("subcategory"),
        "cuisine": list(properties.get("cuisine") or []),
        "lat": lat,
        "lon": lon,
        "address_text": properties.get("address_text"),
        "city": properties.get("city"),
        "phone": properties.get("phone"),
        "whatsapp": properties.get("whatsapp"),
        "website": properties.get("website"),
        "facebook": properties.get("facebook"),
        "instagram": properties.get("instagram"),
        "opening_hours": properties.get("opening_hours"),
        "price_level": properties.get("price_level"),
        "status": properties.get("status") or "unverified",
        "confidence": float(properties.get("confidence") or 0.0),
        "popularity": float(properties.get("popularity") or 0.0),
        "sources": json.dumps(sources, ensure_ascii=False),
        "in_circle": distance_from_mga_m(lat, lon) <= CURATION_RADIUS_M,
        "gers_id": sources.get("overture_gers_id"),
        "osm_id": sources.get("osm_id"),
    }


def load(features: Iterable[dict[str, Any]], dsn: str, *, batch_size: int = 500) -> dict[str, int]:
    """Upsert merged POIs; returns counts of inserted/updated/skipped rows."""
    import psycopg

    stats = {"inserted": 0, "updated": 0, "kept_verified": 0, "sources": 0}

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            pending = 0
            for feature in features:
                properties = dict(feature.get("properties") or {})
                geometry = feature.get("geometry") or {}
                coordinates = geometry.get("coordinates") or []
                if len(coordinates) >= 2:
                    properties.setdefault("lon", coordinates[0])
                    properties.setdefault("lat", coordinates[1])
                if not properties.get("name") or properties.get("lat") is None:
                    continue

                params = _params(properties)
                cur.execute(_FIND_EXISTING, params)
                existing = cur.fetchone()

                if existing is None:
                    cur.execute(_UPSERT_POI, params)
                    row = cur.fetchone()
                    poi_id = row[0] if row else None
                    stats["inserted"] += 1
                else:
                    poi_id, verified_at = existing
                    params["id"] = poi_id
                    if verified_at is None:
                        cur.execute(_UPDATE_UNVERIFIED, params)
                        stats["updated"] += 1
                    else:
                        cur.execute(_UPDATE_VERIFIED, params)
                        stats["kept_verified"] += 1

                if poi_id is not None:
                    for source, source_id in _source_pairs(properties):
                        cur.execute(
                            _UPSERT_SOURCE,
                            {
                                "poi_id": poi_id,
                                "source": source,
                                "source_id": source_id,
                                "name": properties.get("name"),
                                "category": properties.get("category"),
                                "lat": params["lat"],
                                "lon": params["lon"],
                                "license": properties.get("overture_license")
                                if source == "overture"
                                else _LICENSES.get(source, "ODbL-1.0"),
                                "confidence": params["confidence"],
                                "raw": json.dumps(properties, ensure_ascii=False),
                            },
                        )
                        stats["sources"] += 1

                pending += 1
                if pending >= batch_size:
                    conn.commit()
                    pending = 0
        conn.commit()
    return stats


def _source_pairs(properties: dict[str, Any]) -> list[tuple[str, str]]:
    """Every (source, source_id) the merged row came from."""
    sources = properties.get("sources") or {}
    if isinstance(sources, str):
        sources = json.loads(sources)
    pairs: list[tuple[str, str]] = []
    if sources.get("osm_id"):
        pairs.append(("osm", str(sources["osm_id"])))
    if sources.get("overture_gers_id"):
        pairs.append(("overture", str(sources["overture_gers_id"])))
    if sources.get("survey_id"):
        pairs.append(("survey", str(sources["survey_id"])))
    return pairs


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.pois.load_pois",
        description="Load the conflated POI export into PostGIS.",
    )
    parser.add_argument(
        "--input", type=Path, default=settings.data_dir / "exports" / "pois_merged.geojsonseq"
    )
    parser.add_argument("--dsn", default=settings.database_url)
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    if not args.input.exists():
        log.error("no conflation output at %s", args.input)
        return 1
    try:
        stats = load(read_geojsonseq(args.input), args.dsn)
    except Exception:
        log.exception("POI load failed")
        return 1
    log.info(
        "loaded: %d inserted, %d updated, %d verified rows preserved, %d source rows",
        stats["inserted"],
        stats["updated"],
        stats["kept_verified"],
        stats["sources"],
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
