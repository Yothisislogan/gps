"""Audit the taxonomy against a real Overture release.

Answers one question: **what fraction of Nicaraguan places does the taxonomy
actually map, and what is it missing?** Run it after every Overture release —
the category list changes, and an unmapped category is silent, because ``otro``
is a valid answer and nobody notices thousands of places quietly losing their
icon.

Reads the release's Parquet directly over HTTPS range requests rather than
through DuckDB. That is deliberate: DuckDB needs its ``httpfs`` extension, which
is a separate download that a locked-down network can block, whereas this needs
only ``pyarrow`` and the bucket's public HTTP endpoint. Row-group statistics on
``bbox`` prune the scan to the handful of groups covering Nicaragua — about
10 MB of a 10.5 GB release.

    python3 -m pipeline.pois.audit_overture
    python3 -m pipeline.pois.audit_overture --release 2026-08-19.0 --json out.json
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import sys
import time
import urllib.request
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from common.geo import NICARAGUA_BBOX
from pipeline.common.io import atomic_write_text, setup_logging
from pipeline.pois.taxonomy import category_for_overture

__all__ = ["HttpRangeFile", "audit", "list_release_files", "main"]

log = logging.getLogger(__name__)

BUCKET = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
#: Overture keeps only the two most recent releases on S3, so a pinned release
#: 404s within about two months. Discovery is the default.
PREFIX = "release/"
COUNTRY = "NI"

#: Only these columns are read. Projection is most of why the scan is cheap.
COLUMNS = ("categories", "basic_category", "taxonomy", "addresses", "bbox")


class HttpRangeFile(io.RawIOBase):
    """A seekable read-only file over HTTP range requests.

    pyarrow reads a Parquet footer, then only the column chunks it needs; both
    become ``Range`` requests here, so a 700 MB file costs a few megabytes.
    """

    def __init__(self, url: str, size: int, *, timeout: float = 120.0, retries: int = 3) -> None:
        self.url = url
        self.size = size
        self.pos = 0
        self.bytes_read = 0
        self._timeout = timeout
        self._retries = retries

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        else:
            self.pos = self.size + offset
        return self.pos

    def tell(self) -> int:
        return self.pos

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = self.size - self.pos
        if size == 0 or self.pos >= self.size:
            return b""
        end = min(self.pos + size, self.size) - 1
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={self.pos}-{end}"})
        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    data = response.read()
                break
            except Exception as exc:
                last = exc
                if attempt == self._retries - 1:
                    raise
                time.sleep(2 * (attempt + 1))
        else:  # pragma: no cover - defensive
            raise RuntimeError(f"range request failed: {last}")
        self.pos += len(data)
        self.bytes_read += len(data)
        return data

    def readinto(self, buffer) -> int:  # type: ignore[override]
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)


def _get(url: str, timeout: float = 120.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read().decode("utf-8")


def discover_release() -> str:
    """Newest release prefix on the bucket."""
    body = _get(f"{BUCKET}?list-type=2&delimiter=/&prefix={PREFIX}")
    releases = sorted(set(re.findall(r"<Prefix>release/([^/<]+)/</Prefix>", body)), reverse=True)
    if not releases:
        raise RuntimeError("no releases found on the Overture bucket")
    log.info("releases on S3: %s", ", ".join(releases))
    return releases[0]


def list_release_files(release: str) -> list[tuple[str, int]]:
    """``(key, size)`` for every places Parquet in a release."""
    body = _get(f"{BUCKET}?list-type=2&prefix={PREFIX}{release}/theme%3Dplaces/type%3Dplace/")
    keys = re.findall(r"<Key>([^<]+)</Key>", body)
    sizes = [int(value) for value in re.findall(r"<Size>(\d+)</Size>", body)]
    return [pair for pair in zip(keys, sizes, strict=False) if pair[0].endswith(".parquet")]


def _overlapping_row_groups(metadata: Any, bbox: tuple[float, float, float, float]) -> list[int]:
    """Row groups whose bbox statistics intersect the target box.

    A group with no statistics is kept: guessing it away could silently drop
    real places.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    first = metadata.row_group(0)
    index = {first.column(i).path_in_schema: i for i in range(first.num_columns)}
    needed = ("bbox.xmin", "bbox.xmax", "bbox.ymin", "bbox.ymax")
    if not all(name in index for name in needed):
        return list(range(metadata.num_row_groups))

    keep: list[int] = []
    for group in range(metadata.num_row_groups):
        row_group = metadata.row_group(group)
        stats = {name: row_group.column(index[name]).statistics for name in needed}
        if not all(stat is not None and stat.has_min_max for stat in stats.values()):
            keep.append(group)
            continue
        if stats["bbox.xmax"].max < min_lon or stats["bbox.xmin"].min > max_lon:
            continue
        if stats["bbox.ymax"].max < min_lat or stats["bbox.ymin"].min > max_lat:
            continue
        keep.append(group)
    return keep


def audit(
    release: str | None = None,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    country: str | None = COUNTRY,
) -> dict[str, Any]:
    """Count categories for one country and report what the taxonomy maps."""
    import pyarrow.parquet as pq

    resolved = release or discover_release()
    box = bbox or NICARAGUA_BBOX
    min_lon, min_lat, max_lon, max_lat = box

    files = list_release_files(resolved)
    log.info("release %s: %d place files", resolved, len(files))

    legacy: Counter[str] = Counter()
    basic: Counter[str] = Counter()
    taxonomy: Counter[str] = Counter()
    total = 0
    downloaded = 0
    started = time.monotonic()

    for key, size in files:
        handle = HttpRangeFile(BUCKET + key.replace("=", "%3D"), size)
        parquet = pq.ParquetFile(handle)
        groups = _overlapping_row_groups(parquet.metadata, box)
        if not groups:
            continue
        table = parquet.read_row_groups(groups, columns=list(COLUMNS))
        downloaded += handle.bytes_read
        hits = 0
        for row in table.to_pylist():
            bounds = row.get("bbox") or {}
            lon, lat = bounds.get("xmin"), bounds.get("ymin")
            if lon is None or lat is None:
                continue
            if not (min_lon <= lon <= max_lon and min_lat <= lat <= max_lat):
                continue
            if country:
                addresses = row.get("addresses") or []
                if not addresses or (addresses[0] or {}).get("country") != country:
                    continue
            hits += 1
            total += 1
            if value := (row.get("categories") or {}).get("primary"):
                legacy[value] += 1
            if value := row.get("basic_category"):
                basic[value] += 1
            if value := (row.get("taxonomy") or {}).get("primary"):
                taxonomy[value] += 1
        log.info(
            "  %-26s %3d/%d row groups, %5d places, %.0f MB",
            key.rsplit("/", 1)[-1][:26],
            len(groups),
            parquet.metadata.num_row_groups,
            hits,
            handle.bytes_read / 1e6,
        )

    # `categories` is removed from the 2026-09 release onward; fall back to the
    # replacement so this keeps working across the boundary.
    primary = legacy or taxonomy or basic
    source = "categories" if legacy else ("taxonomy" if taxonomy else "basic_category")

    mapped = {name: category_for_overture(name) for name in primary}
    unmapped = {name: count for name, count in primary.items() if mapped[name] is None}
    covered = sum(count for name, count in primary.items() if mapped[name] is not None)
    categorised = sum(primary.values())

    by_app: Counter[str] = Counter()
    for name, count in primary.items():
        by_app[mapped[name] or "otro"] += count

    return {
        "release": resolved,
        "country": country,
        "category_source": source,
        "places": total,
        "categorised": categorised,
        "distinct_categories": len(primary),
        "mapped_places": covered,
        "coverage": round(covered / categorised, 4) if categorised else 0.0,
        "unmapped_places": categorised - covered,
        "unmapped_categories": sorted(unmapped.items(), key=lambda pair: -pair[1]),
        "by_app_category": by_app.most_common(),
        "downloaded_mb": round(downloaded / 1e6, 1),
        "seconds": round(time.monotonic() - started, 1),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m pipeline.pois.audit_overture",
        description="Audit docs/taxonomy.csv against a real Overture release.",
    )
    parser.add_argument("--release", default=None, help="e.g. 2026-08-19.0 (default: newest on S3)")
    parser.add_argument("--country", default=COUNTRY, help="ISO country filter; empty for none")
    parser.add_argument("--min-count", type=int, default=5, help="report unmapped at or above this")
    parser.add_argument("--json", dest="json_out", type=Path, default=None)
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="exit non-zero when coverage is below this fraction (e.g. 0.75)",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    try:
        report = audit(args.release, country=args.country or None)
    except Exception:
        log.exception("audit failed")
        return 2

    print(f"release {report['release']} ({report['category_source']})")
    print(f"  places in {report['country'] or 'the bbox'}: {report['places']}")
    print(
        f"  categorised: {report['categorised']} across {report['distinct_categories']} categories"
    )
    print(f"  mapped by docs/taxonomy.csv: {report['mapped_places']} ({report['coverage']:.1%})")
    print(f"  falling to otro: {report['unmapped_places']}")
    print(f"  read {report['downloaded_mb']} MB in {report['seconds']}s")

    notable = [(n, c) for n, c in report["unmapped_categories"] if c >= args.min_count]
    print(f"\nunmapped with count >= {args.min_count}: {len(notable)}")
    for name, count in notable[:40]:
        print(f"  {count:6d}  {name}")
    if len(notable) > 40:
        print(f"  … and {len(notable) - 40} more")

    print("\nplaces by app category:")
    for name, count in report["by_app_category"][:25]:
        print(f"  {count:6d}  {name}")

    if args.json_out:
        atomic_write_text(args.json_out, json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\nwrote {args.json_out}")

    if args.fail_under is not None and report["coverage"] < args.fail_under:
        print(f"\ncoverage {report['coverage']:.1%} is below the {args.fail_under:.0%} floor")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
