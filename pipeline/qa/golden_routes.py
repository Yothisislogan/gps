"""Golden-route regression tests against the live router.

This is what lets the nightly build run unattended.  Every OSM edit, every
costing tweak and every Valhalla upgrade can silently change how the router
sends people through Managua; a fixed set of routes a local driver would
recognise, checked every night, is the only thing that catches it before a
driver does.

Each case in ``docs/golden_routes.json`` asserts some combination of:

* **distance and duration** within a tolerance — catches a costing change that
  makes everything 20 % slower;
* **must_pass_near** waypoints — encodes the route a local would actually take,
  which is how a "shorter" route through a barrio's dirt streets gets caught;
* **must_avoid** zones — the Mercado Oriental's interior, a bus terminal's
  forecourt: places a router will happily send you through and a driver will
  not go;
* **geometry overlap** against a recorded reference shape, once field GPX exists.

Every case carries a ``source``.  ``agent`` means the expectations were reasoned
out from coordinates and road classes by a model, not measured; ``driven`` means
somebody drove it with a GPS logger and the numbers came off the track.  Only
``driven`` cases are ground truth, so only they can fail the build — an
``agent`` case is reported, diffed and tracked, but a mismatch against a guess
is a fact about the guess.  ``--strict`` overrides that when you want to see the
whole suite red.

Overlap is measured as the fraction of sampled points on the candidate route
that fall within a buffer of the reference line.  That beats comparing shapes
point-for-point: two encodings of the same drive differ in vertex placement
everywhere and agree on the ground, which is what matters.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common.config import get_settings
from common.geo import haversine_m, interpolate_along, line_length_m, nearest_point_on_line
from common.polyline import decode
from common.valhalla import ValhallaClient, ValhallaError, summarize_route
from pipeline.common.io import atomic_write_text, setup_logging

__all__ = ["Case", "CaseResult", "check_case", "load_cases", "main", "overlap_fraction"]

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_ROUTES_PATH = REPO_ROOT / "docs" / "golden_routes.json"

# A case is ground truth only when a person drove it. Anything else is a
# hypothesis with a tolerance attached.
BINDING_SOURCES = frozenset({"driven"})
VALID_SOURCES = frozenset({"agent", "driven"})

DEFAULTS: dict[str, float] = {
    "min_overlap": 0.9,
    "buffer_m": 30.0,
    "sample_interval_m": 25.0,
    "pass_near_radius_m": 400.0,
}


@dataclass(frozen=True)
class Case:
    """One golden route, as loaded from the JSON."""

    id: str
    name: str
    origin: tuple[float, float]
    destination: tuple[float, float]
    expected_km: float | None = None
    km_tolerance: float = 5.0
    expected_minutes: float | None = None
    minutes_tolerance: float = 10.0
    must_pass_near: tuple[dict[str, Any], ...] = ()
    must_avoid: tuple[dict[str, Any], ...] = ()
    reference_shape: str | None = None
    reference_source: str | None = None
    source: str = "agent"
    notes: str = ""
    tags: tuple[str, ...] = ()

    @property
    def binding(self) -> bool:
        """Whether a failure here should fail the build."""
        return self.source in BINDING_SOURCES


@dataclass
class CaseResult:
    """The verdict on one case, with every failure named."""

    case: Case
    ok: bool = True
    distance_km: float | None = None
    duration_min: float | None = None
    overlap: float | None = None
    failures: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.case.id,
            "name": self.case.name,
            "source": self.case.source,
            "binding": self.case.binding,
            "ok": self.ok,
            "distance_km": self.distance_km,
            "duration_min": self.duration_min,
            "overlap": self.overlap,
            "failures": self.failures,
            "error": self.error,
        }


def load_cases(path: Path | str | None = None) -> tuple[list[Case], dict[str, float]]:
    """Read the golden-route file; returns the cases and the file's defaults."""
    resolved = Path(path) if path else GOLDEN_ROUTES_PATH
    document = json.loads(resolved.read_text(encoding="utf-8"))
    defaults = {**DEFAULTS, **(document.get("defaults") or {})}

    cases: list[Case] = []
    seen: set[str] = set()
    for raw in document.get("routes") or []:
        case_id = raw["id"]
        if case_id in seen:
            raise ValueError(f"duplicate golden-route id: {case_id}")
        seen.add(case_id)
        source = raw.get("source", "agent")
        if source not in VALID_SOURCES:
            raise ValueError(
                f"golden route {case_id}: source={source!r}, expected one of "
                + ", ".join(sorted(VALID_SOURCES))
            )
        cases.append(
            Case(
                id=case_id,
                name=raw.get("name", case_id),
                origin=(float(raw["from"]["lat"]), float(raw["from"]["lon"])),
                destination=(float(raw["to"]["lat"]), float(raw["to"]["lon"])),
                expected_km=raw.get("expected_km"),
                km_tolerance=float(raw.get("km_tolerance", 5.0)),
                expected_minutes=raw.get("expected_minutes"),
                minutes_tolerance=float(raw.get("minutes_tolerance", 10.0)),
                must_pass_near=tuple(raw.get("must_pass_near") or ()),
                must_avoid=tuple(raw.get("must_avoid") or ()),
                reference_shape=raw.get("reference_shape"),
                reference_source=raw.get("reference_source"),
                source=source,
                notes=raw.get("notes", ""),
                tags=tuple(raw.get("tags") or ()),
            )
        )
    return cases, defaults


def sample_line(
    shape: Sequence[tuple[float, float]], interval_m: float
) -> list[tuple[float, float]]:
    """Points every ``interval_m`` along a route, endpoints included.

    Sampling rather than using the raw vertices is what makes overlap comparable
    between two encodings of the same drive: a long straight stretch of
    Carretera Norte may be two vertices in one shape and forty in another.
    """
    if len(shape) < 2:
        return list(shape)
    coordinates = [[lon, lat] for lat, lon in shape]
    total = line_length_m(coordinates)
    if total <= 0:
        return [shape[0]]
    steps = max(1, int(total // max(interval_m, 1.0)))
    points = [interpolate_along(coordinates, min(total, i * interval_m)) for i in range(steps + 1)]
    if points[-1] != shape[-1]:
        points.append(shape[-1])
    return points


def overlap_fraction(
    candidate: Sequence[tuple[float, float]],
    reference: Sequence[tuple[float, float]],
    *,
    buffer_m: float = 30.0,
    sample_interval_m: float = 25.0,
) -> float:
    """Fraction of the candidate route that runs within ``buffer_m`` of the reference.

    Returns 0.0 when either shape is unusable rather than raising: a missing
    reference is a case that has not been surveyed yet, not a build failure.
    """
    if len(candidate) < 2 or len(reference) < 2:
        return 0.0
    reference_coords = [[lon, lat] for lat, lon in reference]
    samples = sample_line(candidate, sample_interval_m)
    if not samples:
        return 0.0
    inside = sum(
        1
        for lat, lon in samples
        if nearest_point_on_line(lat, lon, reference_coords)[1] <= buffer_m
    )
    return inside / len(samples)


def _passes_near(
    shape: Sequence[tuple[float, float]], waypoint: dict[str, Any], default_radius_m: float
) -> bool:
    radius = float(waypoint.get("radius_m", default_radius_m))
    target = (float(waypoint["lat"]), float(waypoint["lon"]))
    return any(haversine_m(lat, lon, *target) <= radius for lat, lon in shape)


def _point_in_ring(lat: float, lon: float, ring: Sequence[Sequence[float]]) -> bool:
    """Ray casting over a GeoJSON ring of ``[lon, lat]`` pairs."""
    inside = False
    count = len(ring)
    for i in range(count):
        x1, y1 = float(ring[i][0]), float(ring[i][1])
        x2, y2 = float(ring[(i + 1) % count][0]), float(ring[(i + 1) % count][1])
        if (y1 > lat) != (y2 > lat):
            x_at = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_at:
                inside = not inside
    return inside


def _violates_avoid(shape: Sequence[tuple[float, float]], zone: dict[str, Any]) -> bool:
    """True when the route enters a zone it was told to stay out of."""
    if zone.get("kind") == "polygon" or zone.get("ring"):
        ring = zone.get("ring") or []
        return any(_point_in_ring(lat, lon, ring) for lat, lon in shape)
    if zone.get("lat") is not None:
        radius = float(zone.get("radius_m", 100.0))
        target = (float(zone["lat"]), float(zone["lon"]))
        return any(haversine_m(lat, lon, *target) <= radius for lat, lon in shape)
    return False


def check_case(case: Case, client: ValhallaClient, defaults: dict[str, float]) -> CaseResult:
    """Route one case and judge it."""
    result = CaseResult(case=case)
    try:
        response = client.route([case.origin, case.destination], alternates=0)
        summary = summarize_route(response)
    except ValhallaError as exc:
        result.ok = False
        result.error = str(exc)
        return result
    except Exception as exc:
        result.ok = False
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    result.distance_km = summary.distance_km
    result.duration_min = summary.duration_s / 60.0
    shape = summary.shape

    if case.expected_km is not None:
        delta = abs(summary.distance_km - case.expected_km)
        if delta > case.km_tolerance:
            result.failures.append(
                f"distancia {summary.distance_km:.1f} km, esperada {case.expected_km:.1f} ±{case.km_tolerance:.1f}"
            )

    if case.expected_minutes is not None:
        minutes = summary.duration_s / 60.0
        if abs(minutes - case.expected_minutes) > case.minutes_tolerance:
            result.failures.append(
                f"duración {minutes:.0f} min, esperada {case.expected_minutes:.0f} ±{case.minutes_tolerance:.0f}"
            )

    for waypoint in case.must_pass_near:
        if not _passes_near(shape, waypoint, defaults["pass_near_radius_m"]):
            label = waypoint.get("label") or f"{waypoint['lat']:.4f},{waypoint['lon']:.4f}"
            result.failures.append(f"no pasa por {label}")

    for zone in case.must_avoid:
        if _violates_avoid(shape, zone):
            result.failures.append(f"pasa por {zone.get('label', 'zona prohibida')}")

    if case.reference_shape:
        reference = decode(case.reference_shape)
        result.overlap = overlap_fraction(
            shape,
            reference,
            buffer_m=defaults["buffer_m"],
            sample_interval_m=defaults["sample_interval_m"],
        )
        if result.overlap < defaults["min_overlap"]:
            result.failures.append(
                f"solapamiento {result.overlap:.0%}, mínimo {defaults['min_overlap']:.0%}"
            )

    result.ok = not result.failures
    return result


def _print_table(results: Sequence[CaseResult]) -> None:
    width = max((len(r.case.name) for r in results), default=20)
    for result in results:
        if result.ok:
            mark = "ok  "
        elif result.case.binding:
            mark = "FAIL"
        else:
            # Not "FAIL": the route may well be right and the guess wrong.
            mark = "diff"
        distance = (
            f"{result.distance_km:6.1f} km" if result.distance_km is not None else "     — km"
        )
        duration = (
            f"{result.duration_min:5.0f} min" if result.duration_min is not None else "    — min"
        )
        origin = "" if result.case.binding else "  (agent)"
        print(f"  {mark}  {result.case.name:<{width}}  {distance}  {duration}{origin}")
        if result.error:
            print(f"          error: {result.error}")
        for failure in result.failures:
            print(f"          - {failure}")


def _compare(results: Sequence[CaseResult], baseline_path: Path) -> None:
    """Report what moved since a previous run.

    A costing change that shifts one route by 8 km is invisible in a pass/fail
    table if the tolerance was 10; the diff is what makes it obvious.
    """
    try:
        baseline = {
            row["id"]: row
            for row in json.loads(baseline_path.read_text(encoding="utf-8"))["results"]
        }
    except (OSError, KeyError, ValueError):
        print(f"  (no usable baseline at {baseline_path})")
        return

    print("\nchanges since the baseline:")
    moved = False
    for result in results:
        previous = baseline.get(result.case.id)
        if not previous or previous.get("distance_km") is None or result.distance_km is None:
            continue
        delta_km = result.distance_km - previous["distance_km"]
        delta_min = (result.duration_min or 0) - (previous.get("duration_min") or 0)
        if abs(delta_km) >= 0.2 or abs(delta_min) >= 1.0:
            moved = True
            print(f"  {result.case.name}: {delta_km:+.1f} km, {delta_min:+.0f} min")
    if not moved:
        print("  (nothing moved)")


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.qa.golden_routes",
        description="Route the golden pairs and report regressions.",
    )
    parser.add_argument("--routes", type=Path, default=GOLDEN_ROUTES_PATH)
    parser.add_argument("--valhalla-url", default=settings.valhalla_url)
    parser.add_argument("--only", action="append", default=None, help="run only these ids")
    parser.add_argument(
        "--tag", action="append", default=None, help="run only cases with these tags"
    )
    parser.add_argument(
        "--json", dest="json_out", type=Path, default=None, help="write results as JSON"
    )
    parser.add_argument(
        "--baseline", type=Path, default=None, help="compare against a previous --json run"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail on agent-sourced mismatches too (default: report only)",
    )
    parser.add_argument("--log-level", default="WARNING")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level)

    try:
        cases, defaults = load_cases(args.routes)
    except (OSError, ValueError):
        log.exception("could not load %s", args.routes)
        return 2

    if args.only:
        wanted = set(args.only)
        cases = [case for case in cases if case.id in wanted]
    if args.tag:
        tags = set(args.tag)
        cases = [case for case in cases if tags & set(case.tags)]
    if not cases:
        print("no golden routes selected")
        return 2

    print(f"golden routes: {len(cases)} case(s) against {args.valhalla_url}")
    with ValhallaClient(args.valhalla_url) as client:
        results = [check_case(case, client, defaults) for case in cases]

    _print_table(results)

    mismatched = [result for result in results if not result.ok]
    unreachable = [result for result in results if result.error]

    # A route the router could not compute at all is a broken stack, not a bad
    # guess: that fails whatever the case's source says.
    blocking = [
        result for result in mismatched if result.error or result.case.binding or args.strict
    ]
    blocking_ids = {id(result) for result in blocking}
    advisory = [result for result in mismatched if id(result) not in blocking_ids]

    print(f"\n{len(results) - len(mismatched)}/{len(results)} matched expectations")
    if advisory:
        print(
            f"{len(advisory)} of the mismatches are agent-sourced estimates and do not fail the run"
        )
    if not any(result.case.binding for result in results):
        print(
            "no driven routes in this file yet — nothing here is ground truth. "
            'Drive one with a GPS logger, set its source to "driven", and this '
            "suite starts asserting."
        )

    if args.json_out:
        atomic_write_text(
            args.json_out,
            json.dumps(
                {
                    "valhalla_url": args.valhalla_url,
                    "matched": len(results) - len(mismatched),
                    "blocking_failures": len(blocking),
                    "total": len(results),
                    "results": [result.as_dict() for result in results],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
        print(f"wrote {args.json_out}")

    if args.baseline:
        _compare(results, args.baseline)

    if unreachable and len(unreachable) == len(results):
        print("\nthe router answered nothing at all — check that valhalla is up")
        return 2
    return 1 if blocking else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
