"""Turn the osmium POI export into conflation-ready records.

``pipeline/fetch_osm.sh`` does the OSM parsing with ``osmium tags-filter`` and
``osmium export``, which keeps a full OSM reader out of Python and is an order of
magnitude faster than anything this process could do.  What is left — and what
lives here — is the judgement: which tags make a place, what the place is called
in Spanish, which of the half-dozen ways Nicaraguans record a phone number this
row used, and where a mapped building's marker should sit.

Input:  data/exports/osm_pois.geojsonseq  (osmium export, raw OSM tags)
Output: data/exports/src_osm.geojsonseq   (PoiRecord-shaped properties)
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from common.config import get_settings
from common.geo import NICARAGUA_BBOX, distance_from_mga_m
from pipeline.common.io import read_geojsonseq, setup_logging, write_geojsonseq
from pipeline.pois.taxonomy import category_for_osm

__all__ = ["build_record", "iter_records", "main", "polygon_centroid"]

log = logging.getLogger(__name__)

#: Tag keys carrying an alternative name worth indexing.  ``old_name`` matters
#: more here than elsewhere: Nicaraguans navigate by what a place used to be
#: called long after the sign changes.
_ALT_NAME_KEYS = (
    "alt_name",
    "old_name",
    "short_name",
    "name:es",
    "name:en",
    "official_name",
    "loc_name",
    "brand",
)

#: The same contact detail is tagged under several keys; first non-empty wins.
_CONTACT_KEYS: dict[str, tuple[str, ...]] = {
    "phone": ("phone", "contact:phone", "contact:mobile", "mobile"),
    "whatsapp": ("contact:whatsapp", "whatsapp"),
    "website": ("website", "contact:website", "url"),
    "facebook": ("contact:facebook", "facebook"),
    "instagram": ("contact:instagram", "instagram"),
}

_ADDRESS_KEYS = ("addr:full", "addr:place", "address", "description")

#: Nicaraguan numbers are +505 plus eight digits.  Local tagging drops the
#: country code about half the time, which breaks tel: links and wa.me links.
_NI_LOCAL_PHONE_RE = re.compile(r"^\s*(?:\(?505\)?[\s-]*)?([2578]\d{7})\s*$")


def _first(tags: Mapping[str, str], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = (tags.get(key) or "").strip()
        if value:
            return value
    return None


def normalize_phone(raw: str | None) -> str | None:
    """Normalise a Nicaraguan phone number to ``+505XXXXXXXX``.

    Returns the input unchanged when it is already international but not
    Nicaraguan (border towns legitimately list Costa Rican numbers), and ``None``
    when it is not a phone number at all.
    """
    if not raw:
        return None
    first = raw.split(";")[0].strip()
    compact = re.sub(r"[\s().-]", "", first)
    if compact.startswith("+"):
        return compact if re.fullmatch(r"\+\d{8,15}", compact) else None
    match = _NI_LOCAL_PHONE_RE.match(compact)
    return f"+505{match.group(1)}" if match else None


def polygon_centroid(coordinates: Sequence[Sequence[Sequence[float]]]) -> tuple[float, float]:
    """Area centroid of a GeoJSON polygon's outer ring, as ``(lon, lat)``.

    A mapped restaurant is often a building outline; its marker belongs in the
    middle of the building, not on an arbitrary corner.  Degenerate rings (zero
    area, which happens with badly drawn buildings) fall back to the mean of the
    vertices instead of dividing by zero.
    """
    ring = coordinates[0] if coordinates else []
    if len(ring) < 3:
        if not ring:
            raise ValueError("empty polygon")
        return (float(ring[0][0]), float(ring[0][1]))

    area = cx = cy = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = float(ring[i][0]), float(ring[i][1])
        x1, y1 = float(ring[i + 1][0]), float(ring[i + 1][1])
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) < 1e-12:
        points = ring[:-1] or ring
        return (
            sum(float(p[0]) for p in points) / len(points),
            sum(float(p[1]) for p in points) / len(points),
        )
    area *= 0.5
    return (cx / (6.0 * area), cy / (6.0 * area))


def _coordinates_of(geometry: Mapping[str, Any]) -> tuple[float, float] | None:
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not coordinates:
        return None
    if kind == "Point":
        return (float(coordinates[0]), float(coordinates[1]))
    if kind == "Polygon":
        return polygon_centroid(coordinates)
    if kind == "MultiPolygon":
        return polygon_centroid(coordinates[0])
    return None


def build_record(feature: Mapping[str, Any]) -> dict[str, Any] | None:
    """One osmium feature -> one conflation record, or ``None`` to skip it.

    Skipped: anything without a name (an unnamed pharmacy is not a listing worth
    publishing, though it may still be worth mapping), anything the taxonomy does
    not recognise, and anything outside Nicaragua's bounding box.
    """
    tags = dict(feature.get("properties") or {})
    geometry = feature.get("geometry") or {}
    point = _coordinates_of(geometry)
    if point is None:
        return None
    lon, lat = point

    min_lon, min_lat, max_lon, max_lat = NICARAGUA_BBOX
    if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
        return None

    name = (tags.get("name") or "").strip()
    if not name:
        return None

    category = category_for_osm(tags)
    if category is None:
        return None

    source_id = str(feature.get("id") or tags.get("@id") or "").strip()
    if not source_id:
        return None

    alt_names = []
    for key in _ALT_NAME_KEYS:
        value = (tags.get(key) or "").strip()
        if value and value != name and value not in alt_names:
            alt_names.append(value)

    # A `disused:` or `was:` prefix is OSM's way of saying the business closed
    # but the building is still there — exactly the "cerrado" the app must show
    # rather than silently keep advertising.
    closed = any(key.startswith(("disused:", "was:", "abandoned:")) for key in tags)

    record: dict[str, Any] = {
        "source": "osm",
        "source_id": source_id,
        "name": name,
        "name_alt": alt_names,
        "category": category,
        "lat": lat,
        "lon": lon,
        "status": "closed" if closed else "unverified",
        "phone": normalize_phone(_first(tags, _CONTACT_KEYS["phone"])),
        "whatsapp": normalize_phone(_first(tags, _CONTACT_KEYS["whatsapp"])),
        "website": _first(tags, _CONTACT_KEYS["website"]),
        "facebook": _first(tags, _CONTACT_KEYS["facebook"]),
        "instagram": _first(tags, _CONTACT_KEYS["instagram"]),
        "opening_hours": (tags.get("opening_hours") or "").strip() or None,
        "address_text": _first(tags, _ADDRESS_KEYS),
        "city": (tags.get("addr:city") or "").strip() or None,
        "brand": (tags.get("brand") or "").strip() or None,
        # OSM points are hand-placed by somebody who stood there, so they start
        # with more credit than a scraped listing; the conflator uses this only
        # as a tie-breaker.
        "confidence": 0.6,
    }

    cuisine = (tags.get("cuisine") or "").strip()
    if cuisine:
        record["cuisine"] = [part.strip() for part in cuisine.split(";") if part.strip()]

    if not record["address_text"] and tags.get("addr:street"):
        housenumber = (tags.get("addr:housenumber") or "").strip()
        record["address_text"] = f"{tags['addr:street']} {housenumber}".strip()

    return {k: v for k, v in record.items() if v not in (None, [], "")}


def iter_records(path: Path, *, circle_only: bool = False) -> Iterator[dict[str, Any]]:
    """Read the osmium export and yield GeoJSON features with record properties."""
    kept = skipped = 0
    for feature in read_geojsonseq(path):
        record = build_record(feature)
        if record is None:
            skipped += 1
            continue
        if circle_only and distance_from_mga_m(record["lat"], record["lon"]) > 48_300:
            skipped += 1
            continue
        kept += 1
        yield {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [record["lon"], record["lat"]]},
            "properties": record,
        }
    log.info("kept %d OSM POIs, skipped %d", kept, skipped)


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    exports = settings.data_dir / "exports"
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.pois.fetch_osm_pois",
        description="Normalise the osmium POI export into conflation records.",
    )
    parser.add_argument("--input", type=Path, default=exports / "osm_pois.geojsonseq")
    parser.add_argument("--output", type=Path, default=exports / "src_osm.geojsonseq")
    parser.add_argument(
        "--circle-only",
        action="store_true",
        help="keep only POIs inside the 48.3 km curation circle (default: the whole country)",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    if not args.input.exists():
        log.error("no osmium export at %s; run pipeline/fetch_osm.sh first", args.input)
        return 1
    try:
        written = write_geojsonseq(
            args.output, iter_records(args.input, circle_only=args.circle_only)
        )
    except Exception:
        log.exception("OSM POI ingest failed")
        return 1
    log.info("wrote %d records to %s", written, args.output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
