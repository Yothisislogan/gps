#!/usr/bin/env python3
"""Write the 48.3 km curation circle around MGA as GeoJSON.

This is the first command in docs/PLAN.md's Phase 0 and the polygon
``pipeline/fetch_osm.sh`` hands to ``osmium extract -p``.  It also prints the
bounding box, which is what the Overture/DuckDB queries filter on.

The circle is generated rather than committed so the radius stays a single
constant in ``common/geo.py`` — a hand-edited GeoJSON would drift from the code
the moment somebody changed the radius.

    python3 scripts/circle48.py --output data/osm/circle48.geojson
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from common.geo import (  # noqa: E402
    CURATION_RADIUS_M,
    MGA_LAT,
    MGA_LON,
    bbox_around,
    circle_polygon,
    haversine_m,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--lat", type=float, default=MGA_LAT, help="centre latitude (default: MGA)")
    parser.add_argument(
        "--lon", type=float, default=MGA_LON, help="centre longitude (default: MGA)"
    )
    parser.add_argument(
        "--radius-m",
        type=float,
        default=CURATION_RADIUS_M,
        help=f"radius in metres (default {CURATION_RADIUS_M:.0f} = 30 miles)",
    )
    parser.add_argument(
        "--segments",
        type=int,
        default=128,
        help="ring vertices; more is rounder, and osmium does not care (default 128)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "data" / "osm" / "circle48.geojson",
        help="where to write the polygon",
    )
    parser.add_argument("--print", action="store_true", help="write to stdout instead of a file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.segments < 8:
        print("error: --segments must be at least 8", file=sys.stderr)
        return 2
    if args.radius_m <= 0:
        print("error: --radius-m must be positive", file=sys.stderr)
        return 2

    feature = circle_polygon(args.lat, args.lon, args.radius_m, args.segments)
    payload = json.dumps(feature, ensure_ascii=False)

    if args.print:
        sys.stdout.write(payload + "\n")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # Atomic, like everything else the pipeline publishes: osmium reading a
        # half-written polygon would clip the extract to nonsense.
        tmp = args.output.with_name(args.output.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(args.output)
        print(f"wrote {args.output}")

    ring = feature["geometry"]["coordinates"][0]
    west, south, east, north = bbox_around(args.lat, args.lon, args.radius_m)
    checked = haversine_m(args.lat, args.lon, ring[0][1], ring[0][0])

    print(f"centre       {args.lat:.4f}, {args.lon:.4f}", file=sys.stderr)
    print(
        f"radius       {args.radius_m / 1000:.1f} km  (ring vertex at {checked / 1000:.3f} km)",
        file=sys.stderr,
    )
    print(f"vertices     {len(ring)}", file=sys.stderr)
    # The form Overture's DuckDB filter and Planetiler's --bounds both want.
    print(f"bbox         {west:.5f},{south:.5f},{east:.5f},{north:.5f}", file=sys.stderr)
    print(f"overture     --bbox={west:.2f},{south:.2f},{east:.2f},{north:.2f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
