"""Tests for the QA harness.

The Valhalla-facing paths run against httpx.MockTransport; everything else is
pure geometry over synthetic shapes. Nothing here needs a router, a database or
a network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import httpx
import pytest

from common.geo import NICARAGUA_BBOX
from common.polyline import encode
from pipeline.qa.golden_routes import (
    Case,
    check_case,
    load_cases,
    overlap_fraction,
    sample_line,
)
from pipeline.qa.islands import point_in_geometry, point_in_polygon, unreachable_roads
from pipeline.qa.kpis import compute_road_kpis

MGA = (12.1415, -86.1682)
METROCENTRO = (12.1246, -86.2686)


def straight_route(*points: tuple[float, float], km: float = 14.0, minutes: float = 25.0) -> dict:
    return {
        "trip": {
            "summary": {"length": km, "time": minutes * 60},
            "legs": [{"shape": encode(list(points)), "maneuvers": []}],
        }
    }


def client_returning(response: dict):
    from common.valhalla import ValhallaClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    return ValhallaClient(client=httpx.Client(transport=httpx.MockTransport(handler)))


DEFAULTS = {
    "min_overlap": 0.9,
    "buffer_m": 30.0,
    "sample_interval_m": 25.0,
    "pass_near_radius_m": 400.0,
}


class TestGoldenRoutesFile:
    def test_loads(self):
        cases, defaults = load_cases()
        assert len(cases) >= 20, "the suite is meant to cover the circle, not spot-check it"
        assert defaults["min_overlap"] > 0

    def test_ids_are_unique(self):
        cases, _ = load_cases()
        assert len({case.id for case in cases}) == len(cases)

    def test_every_coordinate_is_in_nicaragua(self):
        min_lon, min_lat, max_lon, max_lat = NICARAGUA_BBOX
        for case in load_cases()[0]:
            for lat, lon in (case.origin, case.destination):
                assert min_lat <= lat <= max_lat, f"{case.id}: latitude {lat}"
                assert min_lon <= lon <= max_lon, f"{case.id}: longitude {lon}"

    def test_tolerances_are_positive_and_sane(self):
        for case in load_cases()[0]:
            assert case.km_tolerance > 0
            assert case.minutes_tolerance > 0
            if case.expected_km is not None:
                assert 0 < case.expected_km < 500, case.id
                # A tolerance wider than the distance asserts nothing.
                assert case.km_tolerance < case.expected_km, case.id

    def test_waypoint_radii_are_sane(self):
        for case in load_cases()[0]:
            for waypoint in case.must_pass_near:
                assert 0 < float(waypoint.get("radius_m", 400)) <= 5_000, case.id

    def test_cases_explain_themselves(self):
        # A golden route without a note is a number nobody can maintain.
        cases, _ = load_cases()
        assert sum(1 for case in cases if case.notes.strip()) >= len(cases) * 0.8

    def test_rejects_duplicate_ids(self, tmp_path: Path):
        path = tmp_path / "routes.json"
        entry = {
            "id": "dup",
            "from": {"lat": 12.1, "lon": -86.2},
            "to": {"lat": 12.2, "lon": -86.3},
        }
        path.write_text(json.dumps({"routes": [entry, entry]}), encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate"):
            load_cases(path)


class TestOverlap:
    LINE: ClassVar[list] = [(12.10, -86.20), (12.10, -86.10)]

    def test_identical_lines_overlap_fully(self):
        assert overlap_fraction(self.LINE, self.LINE) == 1.0

    def test_a_parallel_detour_does_not(self):
        detour = [(12.15, -86.20), (12.15, -86.10)]
        assert overlap_fraction(detour, self.LINE) == 0.0

    def test_partial_overlap_is_partial(self):
        half_off = [(12.10, -86.20), (12.10, -86.15), (12.15, -86.15), (12.15, -86.10)]
        fraction = overlap_fraction(half_off, self.LINE)
        assert 0.1 < fraction < 0.9

    def test_differently_encoded_versions_of_one_drive_still_match(self):
        # The reason overlap is buffer-based rather than vertex-based: the same
        # road encoded with more vertices must not look like a different route.
        dense = [(12.10, -86.20 + i * 0.001) for i in range(101)]
        assert overlap_fraction(dense, self.LINE) == pytest.approx(1.0)

    def test_missing_reference_scores_zero_rather_than_raising(self):
        assert overlap_fraction(self.LINE, []) == 0.0
        assert overlap_fraction([], self.LINE) == 0.0

    def test_sampling_is_dense_enough_to_catch_a_short_detour(self):
        samples = sample_line(self.LINE, 25.0)
        assert len(samples) > 400
        assert samples[0] == self.LINE[0]


class TestCheckCase:
    CASE: ClassVar[Case] = Case(
        id="mga-metrocentro",
        name="MGA -> Metrocentro",
        origin=MGA,
        destination=METROCENTRO,
        expected_km=14.0,
        km_tolerance=3.0,
        expected_minutes=25.0,
        minutes_tolerance=9.0,
    )

    def test_passes_when_everything_matches(self):
        result = check_case(self.CASE, client_returning(straight_route(MGA, METROCENTRO)), DEFAULTS)
        assert result.ok, result.failures
        assert result.distance_km == 14.0

    def test_fails_on_distance(self):
        response = straight_route(MGA, METROCENTRO, km=30.0)
        result = check_case(self.CASE, client_returning(response), DEFAULTS)
        assert not result.ok
        assert any("distancia" in failure for failure in result.failures)

    def test_fails_on_duration(self):
        response = straight_route(MGA, METROCENTRO, minutes=90.0)
        result = check_case(self.CASE, client_returning(response), DEFAULTS)
        assert not result.ok
        assert any("duración" in failure for failure in result.failures)

    def test_must_pass_near(self):
        case = Case(
            id="x",
            name="x",
            origin=MGA,
            destination=METROCENTRO,
            must_pass_near=(
                {"lat": 12.1435, "lon": -86.2145, "radius_m": 900, "label": "La Subasta"},
            ),
        )
        via = check_case(
            case, client_returning(straight_route(MGA, (12.1435, -86.2145), METROCENTRO)), DEFAULTS
        )
        assert via.ok, via.failures

        direct = check_case(case, client_returning(straight_route(MGA, METROCENTRO)), DEFAULTS)
        assert not direct.ok
        assert "no pasa por La Subasta" in direct.failures

    def test_must_avoid_polygon(self):
        case = Case(
            id="x",
            name="x",
            origin=MGA,
            destination=METROCENTRO,
            must_avoid=(
                {
                    "kind": "polygon",
                    "label": "Mercado Oriental",
                    "ring": [
                        [-86.266, 12.144],
                        [-86.25, 12.144],
                        [-86.25, 12.156],
                        [-86.266, 12.156],
                    ],
                },
            ),
        )
        through = check_case(
            case, client_returning(straight_route(MGA, (12.150, -86.258), METROCENTRO)), DEFAULTS
        )
        assert not through.ok
        assert any("Mercado Oriental" in failure for failure in through.failures)

        around = check_case(case, client_returning(straight_route(MGA, METROCENTRO)), DEFAULTS)
        assert around.ok, around.failures

    def test_overlap_failure_is_reported(self):
        reference = encode([(12.10, -86.20), (12.10, -86.10)])
        case = Case(
            id="x", name="x", origin=MGA, destination=METROCENTRO, reference_shape=reference
        )
        result = check_case(
            case, client_returning(straight_route((12.15, -86.20), (12.15, -86.10))), DEFAULTS
        )
        assert not result.ok
        assert result.overlap == 0.0
        assert any("solapamiento" in failure for failure in result.failures)

    def test_a_router_error_is_a_failed_case_not_a_crash(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error_code": 171, "error": "No suitable edges"})

        from common.valhalla import ValhallaClient

        client = ValhallaClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
        result = check_case(self.CASE, client, DEFAULTS)
        assert not result.ok
        assert result.error and "No suitable edges" in result.error


class TestKpis:
    ROADS: ClassVar[list] = [
        {
            "geometry": {"type": "LineString", "coordinates": [[-86.25, 12.11], [-86.24, 12.11]]},
            "properties": {
                "highway": "primary",
                "surface": "asphalt",
                "oneway": "yes",
                "name": "Pista Juan Pablo II",
                "maxspeed": "60",
            },
        },
        {
            "geometry": {"type": "LineString", "coordinates": [[-86.25, 12.12], [-86.24, 12.12]]},
            "properties": {"highway": "primary"},
        },
        {
            "geometry": {"type": "LineString", "coordinates": [[-86.25, 12.13], [-86.245, 12.13]]},
            "properties": {"highway": "residential", "surface": "dirt"},
        },
    ]

    def test_lengths_and_shares(self):
        kpis = compute_road_kpis(self.ROADS)
        assert kpis["road_km_total"] == pytest.approx(2.6, abs=0.3)
        # One of the two primaries has every tag; the other has none.
        assert kpis["oneway_tagged_share_driver_critical"] == pytest.approx(0.5, abs=0.02)
        assert kpis["roundabouts"] == 0

    def test_unpaved_is_counted(self):
        assert compute_road_kpis(self.ROADS)["unpaved_km"] > 0

    def test_roads_outside_the_circle_are_excluded(self):
        bluefields = {
            "geometry": {"type": "LineString", "coordinates": [[-83.77, 12.0], [-83.76, 12.0]]},
            "properties": {"highway": "primary", "surface": "asphalt"},
        }
        inside = compute_road_kpis(self.ROADS)["road_km_total"]
        with_far = compute_road_kpis([*self.ROADS, bluefields])["road_km_total"]
        assert with_far == pytest.approx(inside), (
            "the circle's percentages must describe the circle"
        )

        country = compute_road_kpis([*self.ROADS, bluefields], circle_only=False)
        assert country["road_km_total"] > inside

    def test_empty_input_does_not_divide_by_zero(self):
        kpis = compute_road_kpis([])
        assert kpis["road_km_total"] == 0
        assert kpis["surface_tagged_share"] == 0.0

    def test_roundabouts_and_calming_are_counted(self):
        extra = [
            {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-86.25, 12.14], [-86.249, 12.14]],
                },
                "properties": {"highway": "primary", "junction": "roundabout"},
            },
            {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-86.25, 12.15], [-86.249, 12.15]],
                },
                "properties": {"highway": "residential", "traffic_calming": "bump"},
            },
        ]
        kpis = compute_road_kpis([*self.ROADS, *extra])
        assert kpis["roundabouts"] == 1
        assert kpis["traffic_calming"] == 1


class TestIslands:
    OUTER: ClassVar[list] = [
        [-86.3, 12.0],
        [-86.0, 12.0],
        [-86.0, 12.3],
        [-86.3, 12.3],
        [-86.3, 12.0],
    ]
    HOLE: ClassVar[list] = [
        [-86.2, 12.1],
        [-86.1, 12.1],
        [-86.1, 12.2],
        [-86.2, 12.2],
        [-86.2, 12.1],
    ]

    def test_inside_and_outside(self):
        assert point_in_polygon(12.05, -86.25, [self.OUTER]) is True
        assert point_in_polygon(13.00, -86.25, [self.OUTER]) is False

    def test_a_hole_is_not_inside(self):
        # Isochrones really do have holes, and an unreachable pocket inside a
        # reachable area is exactly what this job hunts for.
        assert point_in_polygon(12.15, -86.15, [self.OUTER, self.HOLE]) is False

    def test_multipolygon_and_featurecollection(self):
        assert point_in_geometry(
            12.05, -86.25, {"type": "MultiPolygon", "coordinates": [[self.OUTER]]}
        )
        assert point_in_geometry(
            12.05,
            -86.25,
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Polygon", "coordinates": [self.OUTER]},
                    }
                ],
            },
        )

    def test_a_linestring_contour_cannot_answer_containment(self):
        assert (
            point_in_geometry(12.05, -86.25, {"type": "LineString", "coordinates": self.OUTER})
            is False
        )

    def test_finds_the_stranded_named_road(self):
        roads = [
            {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-86.26, 12.05], [-86.25, 12.05]],
                },
                "properties": {"highway": "residential", "name": "Calle Conectada"},
            },
            {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-86.16, 12.15], [-86.15, 12.15]],
                },
                "properties": {"highway": "residential", "name": "Calle Aislada"},
            },
        ]
        iso = {"type": "Polygon", "coordinates": [self.OUTER, self.HOLE]}
        stranded = unreachable_roads(roads, iso)
        assert [road["name"] for road in stranded] == ["Calle Aislada"]
        assert stranded[0]["josm"].startswith("https://www.openstreetmap.org/")

    def test_unnamed_roads_are_skipped_by_default(self):
        roads = [
            {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-86.16, 12.15], [-86.15, 12.15]],
                },
                "properties": {"highway": "service"},
            }
        ]
        iso = {"type": "Polygon", "coordinates": [self.OUTER, self.HOLE]}
        assert unreachable_roads(roads, iso) == []
        assert len(unreachable_roads(roads, iso, named_only=False)) == 1

    def test_footways_are_never_reported(self):
        roads = [
            {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-86.16, 12.15], [-86.15, 12.15]],
                },
                "properties": {"highway": "footway", "name": "Andén"},
            }
        ]
        assert (
            unreachable_roads(roads, {"type": "Polygon", "coordinates": [self.OUTER, self.HOLE]})
            == []
        )
