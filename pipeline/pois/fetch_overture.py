"""Pull Overture Places for Nicaragua and normalise them for conflation.

Overture is the reason this project can list Nicaraguan businesses at all:
it carries Meta's places data, and in Nicaragua a business's real presence is a
Facebook page.  Foursquare's open places, Microsoft's and AllThePlaces' scrapes
ride along in the same release.

Two things make this job less trivial than "read some parquet":

* **The schema is mid-migration.**  Through the 2026-08 release a place's
  category lives in ``categories.primary``; from the 2026-09-23.0 quarterly
  release that column is removed in favour of ``basic_category`` and
  ``taxonomy``.  This job asks the file what columns it has and builds the query
  to match, so a release boundary is a log line rather than an outage.
* **Licences must stay attached.**  Overture's rows carry a per-source licence
  (CDLA-Permissive-2.0 for Meta/Microsoft, Apache-2.0 for Foursquare, CC0 for
  AllThePlaces).  That value is carried through to ``poi_source.license`` so the
  published dataset can be attributed correctly instead of being laundered into
  one undifferentiated blob.

Output: data/exports/src_overture.geojsonseq (PoiRecord-shaped properties)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from common.config import get_settings
from common.data_status import read_status
from common.geo import NICARAGUA_BBOX
from pipeline.common.io import atomic_output, ensure_dir, setup_logging, write_geojsonseq
from pipeline.pois.taxonomy import category_for_overture
from pipeline.status import save, timestamp

__all__ = ["build_query", "fetch", "main", "record_from_row"]

log = logging.getLogger(__name__)

S3_BASE = "s3://overturemaps-us-west-2/release"

#: Only the two most recent releases stay on S3, so a hardcoded release goes
#: stale within two months.  This is the fallback when discovery fails; the job
#: prefers whatever ``--release`` says or what it can list.
FALLBACK_RELEASE = "2026-08-19.0"

#: Overture publishes places worldwide; the bounding box alone bleeds into
#: Honduras and Costa Rica, so the country filter is not optional.
COUNTRY = "NI"

_RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")


def _connect():
    """Open DuckDB with the extensions the Overture read needs."""
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET s3_region='us-west-2';")
    return con


def discover_release(con, *, allow_fallback: bool = True) -> str:
    """Find the newest release on S3, falling back to the pinned one.

    Overture retains only the last two releases, so "latest" has to be looked up
    rather than remembered.
    """
    try:
        rows = con.execute(
            f"SELECT DISTINCT regexp_extract(file, 'release/([^/]+)/', 1) AS release "
            f"FROM glob('{S3_BASE}/*/theme=places/type=place/*.parquet') ORDER BY release DESC"
        ).fetchall()
        releases = [row[0] for row in rows if row[0] and _RELEASE_RE.match(row[0])]
        if releases:
            log.info("overture releases available: %s", ", ".join(releases))
            return releases[0]
    except Exception:
        log.warning("could not list Overture releases", exc_info=True)
    if not allow_fallback:
        raise RuntimeError("Cannot establish newest Overture release; retry or specify --release")
    return FALLBACK_RELEASE


def available_columns(con, release: str) -> set[str]:
    """Top-level column names in this release's place files."""
    rows = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{S3_BASE}/{release}/theme=places/type=place/*') LIMIT 0"
    ).fetchall()
    return {row[0] for row in rows}


def build_query(release: str, columns: set[str], *, bbox=None, min_confidence: float = 0.3) -> str:
    """Compose the extract SQL for whichever schema this release carries.

    ``taxonomy``/``basic_category`` are preferred; ``categories`` is selected
    only while it still exists.  Selecting a dropped column would fail the whole
    job, and selecting neither would silently produce uncategorised places.
    """
    if not _RELEASE_RE.fullmatch(release):
        raise ValueError("Invalid Overture release identifier")
    min_lon, min_lat, max_lon, max_lat = bbox or NICARAGUA_BBOX

    parts = [
        "id",
        "names.primary AS name",
        "confidence",
        "ST_X(ST_GeomFromWKB(geometry)) AS lon",
        "ST_Y(ST_GeomFromWKB(geometry)) AS lat",
        "CAST(phones AS JSON) AS phones",
        "CAST(websites AS JSON) AS websites",
        "CAST(socials AS JSON) AS socials",
        "addresses[1].freeform AS addr_freeform",
        "addresses[1].locality AS addr_locality",
        "sources[1].dataset AS src_dataset",
    ]
    parts.append(
        "sources[1].license AS src_license" if "sources" in columns else "NULL AS src_license"
    )
    parts.append("brand.names.primary AS brand" if "brand" in columns else "NULL AS brand")
    parts.append(
        "operating_status" if "operating_status" in columns else "NULL AS operating_status"
    )
    # The migration: prefer the new fields, keep the old one while it exists.
    parts.append("basic_category" if "basic_category" in columns else "NULL AS basic_category")
    parts.append(
        "taxonomy.primary AS tax_primary" if "taxonomy" in columns else "NULL AS tax_primary"
    )
    parts.append(
        "categories.primary AS legacy_category"
        if "categories" in columns
        else "NULL AS legacy_category"
    )

    return f"""
        SELECT {", ".join(parts)}
        FROM read_parquet('{S3_BASE}/{release}/theme=places/type=place/*', hive_partitioning=1)
        WHERE bbox.xmin BETWEEN {min_lon} AND {max_lon}
          AND bbox.ymin BETWEEN {min_lat} AND {max_lat}
          AND addresses[1].country = '{COUNTRY}'
          AND confidence >= {min_confidence}
    """


def _json_list(value: Any) -> list[str]:
    """Overture list columns arrive as JSON strings via ``CAST(... AS JSON)``."""
    if value in (None, "", "null"):
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in (parsed or []) if item]


def _pick_social(socials: Sequence[str], needle: str) -> str | None:
    for url in socials:
        if needle in url.lower():
            return url
    return None


def record_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """One Overture row -> one conflation record."""
    name = (row.get("name") or "").strip()
    if not name or row.get("lat") is None or row.get("lon") is None:
        return None

    overture_category = (
        row.get("tax_primary") or row.get("basic_category") or row.get("legacy_category")
    )
    category = category_for_overture(overture_category)
    if category is None:
        # Keep the place rather than dropping it: an uncategorised restaurant is
        # still a real business, and the admin queue can classify it later.
        category = "otro"

    socials = _json_list(row.get("socials"))
    phones = _json_list(row.get("phones"))
    websites = _json_list(row.get("websites"))

    from pipeline.pois.fetch_osm_pois import normalize_phone

    status = "unverified"
    operating = (row.get("operating_status") or "").strip().lower()
    if operating in {"closed", "closed_permanently", "permanently_closed"}:
        status = "closed"
    elif operating == "open":
        status = "open"

    record = {
        "source": "overture",
        "source_id": str(row["id"]),
        "gers_id": str(row["id"]),
        "name": name,
        "category": category,
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "status": status,
        "phone": normalize_phone(phones[0] if phones else None),
        "whatsapp": None,
        "website": websites[0] if websites else None,
        "facebook": _pick_social(socials, "facebook"),
        "instagram": _pick_social(socials, "instagram"),
        "address_text": (row.get("addr_freeform") or "").strip() or None,
        "city": (row.get("addr_locality") or "").strip() or None,
        "brand": (row.get("brand") or "").strip() or None,
        "confidence": float(row.get("confidence") or 0.0),
        # Provenance travels with the row; see docs/LICENSES.md.
        "overture_dataset": row.get("src_dataset"),
        "overture_license": row.get("src_license"),
        "overture_category": overture_category,
    }
    return {k: v for k, v in record.items() if v not in (None, "", [])}


def fetch(
    release: str | None = None,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    min_confidence: float = 0.3,
    parquet_out: Path | None = None,
) -> Iterator[dict[str, Any]]:
    """Query Overture and yield GeoJSON features ready for the conflator."""
    con = _connect()
    resolved = release or discover_release(con)
    log.info("using Overture release %s", resolved)

    columns = available_columns(con, resolved)
    log.info("release columns: %s", ", ".join(sorted(columns)))
    if "categories" not in columns and "taxonomy" not in columns:
        raise RuntimeError(
            f"release {resolved} has neither `categories` nor `taxonomy`; the schema moved again"
        )

    query = build_query(resolved, columns, bbox=bbox, min_confidence=min_confidence)

    if parquet_out is not None:
        ensure_dir(parquet_out.parent)
        # Keeping the raw pull lets a conflation change be re-run without paying
        # for the download again — the release is immutable, so this is a cache.
        con.execute(f"COPY ({query}) TO '{parquet_out}' (FORMAT PARQUET)")
        log.info("cached raw pull at %s", parquet_out)
        cursor = con.execute(f"SELECT * FROM '{parquet_out}'")
    else:
        cursor = con.execute(query)

    names = [description[0] for description in cursor.description]
    kept = skipped = 0
    while True:
        rows = cursor.fetchmany(5_000)
        if not rows:
            break
        for values in rows:
            record = record_from_row(dict(zip(names, values, strict=False)))
            if record is None:
                skipped += 1
                continue
            kept += 1
            yield {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [record["lon"], record["lat"]]},
                "properties": record,
            }
    log.info("kept %d Overture places, skipped %d", kept, skipped)


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.pois.fetch_overture",
        description="Download Overture Places for Nicaragua and normalise them for conflation.",
    )
    parser.add_argument("--release", default=None, help="e.g. 2026-08-19.0 (default: newest on S3)")
    parser.add_argument(
        "--output", type=Path, default=settings.data_dir / "exports" / "src_overture.geojsonseq"
    )
    parser.add_argument(
        "--parquet-cache",
        type=Path,
        default=settings.data_dir / "overture" / "places.parquet",
        help="keep the raw pull so conflation can be re-run without re-downloading",
    )
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--skip-unchanged",
        action="store_true",
        help="exit 3 if a matching extract is already present",
    )
    parser.add_argument("--metadata", type=Path, default=settings.metadata_dir / "overture.json")
    parser.add_argument("--min-confidence", type=float, default=0.3)
    parser.add_argument("--log-level", default="INFO")
    return parser


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)
    try:
        if args.release and not _RELEASE_RE.fullmatch(args.release):
            raise ValueError("Invalid Overture release identifier")
        if args.release:
            resolved = args.release
        else:
            con = _connect()
            try:
                # Do not turn an unsuccessful daily check into a fallback success.
                resolved = discover_release(con, allow_fallback=False)
            finally:
                con.close()
        previous = read_status(args.metadata)
        if (
            args.skip_unchanged
            and previous.get("release") == resolved
            and previous.get("min_confidence") == args.min_confidence
            and args.output.is_file()
            and args.output.stat().st_size > 0
            and previous.get("sha256") == file_sha256(args.output)
        ):
            log.info("release %s unchanged; keeping validated extract", resolved)
            return 3
        # Guard against a valid query returning no Nicaragua places. The old
        # extract and metadata remain intact on empty results or stream failure.
        with atomic_output(args.output) as candidate:
            written = write_geojsonseq(
                candidate,
                fetch(
                    resolved,
                    min_confidence=args.min_confidence,
                    parquet_out=None if args.no_cache else args.parquet_cache,
                ),
            )
            if written == 0:
                raise RuntimeError("Empty Overture extract; refusing to replace previous data")
        save(
            args.metadata,
            {
                "status": "imported",
                "release": resolved,
                "imported_at": timestamp(),
                "records": written,
                "min_confidence": args.min_confidence,
                "sha256": file_sha256(args.output),
            },
        )
    except Exception:
        log.exception("Overture fetch failed")
        return 1
    log.info("wrote %d records to %s", written, args.output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
