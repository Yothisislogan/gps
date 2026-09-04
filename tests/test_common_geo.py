"""Tests for geodesy and the Nicaraguan direction table.

The direction assertions are the important ones: Managua's cardinal slang is
topographic (lake north, hills south) and solar (arriba east, abajo west).
Swapping any pair silently rotates every parsed address.
"""

from __future__ import annotations

import math
from typing import ClassVar

import pytest

from common.geo import (
    CURATION_RADIUS_M,
    MGA_LAT,
    MGA_LON,
    bbox_around,
    circle_polygon,
    cuadra_length_m,
    destination_point,
    distance_from_mga_m,
    haversine_m,
    initial_bearing_deg,
    interpolate_along,
    line_length_m,
    local_projection,
    nearest_point_on_line,
    resolve_direction,
)

GRANADA = (11.9344, -85.9560)
MASAYA = (11.9744, -86.0940)
LEON = (12.4379, -86.8780)


class TestDirections:
    @pytest.mark.parametrize(
        ("phrase", "bearing"),
        [
            ("norte", 0.0),
            ("al lago", 0.0),
            ("hacia el lago", 0.0),
            ("sur", 180.0),
            ("a la montaña", 180.0),
            ("hacia la montaña", 180.0),
            ("este", 90.0),
            ("arriba", 90.0),
            ("hacia arriba", 90.0),
            ("al oriente", 90.0),
            ("oeste", 270.0),
            ("abajo", 270.0),
            ("hacia abajo", 270.0),
            ("al poniente", 270.0),
            ("AL SUR", 180.0),
            ("Al Lago", 0.0),
        ],
    )
    def test_nicaraguan_slang(self, phrase: str, bearing: float):
        assert resolve_direction(phrase) == bearing

    def test_lake_is_north_and_hills_are_south(self):
        assert resolve_direction("al lago") == resolve_direction("norte")
        assert resolve_direction("a la montana") == resolve_direction("sur")

    def test_arriba_is_east_not_north(self):
        # The single most common way to get Nicaraguan addresses wrong.
        assert resolve_direction("arriba") == 90.0
        assert resolve_direction("abajo") == 270.0

    @pytest.mark.parametrize("phrase", ["", "rotonda", "cuadra", "xyz"])
    def test_unknown_returns_none(self, phrase: str):
        assert resolve_direction(phrase) is None


class TestGeodesy:
    def test_haversine_matches_known_distances(self):
        # Published straight-line distances from the build plan, ±1 km.
        assert haversine_m(MGA_LAT, MGA_LON, *GRANADA) == pytest.approx(32_600, abs=1_000)
        assert haversine_m(MGA_LAT, MGA_LON, *MASAYA) == pytest.approx(20_000, abs=2_000)
        assert haversine_m(MGA_LAT, MGA_LON, *LEON) == pytest.approx(83_000, abs=3_000)

    def test_zero_distance(self):
        assert haversine_m(MGA_LAT, MGA_LON, MGA_LAT, MGA_LON) == pytest.approx(0.0, abs=1e-6)

    @pytest.mark.parametrize("bearing", [0.0, 45.0, 90.0, 180.0, 270.0, 359.0])
    def test_destination_round_trips(self, bearing: float):
        lat, lon = destination_point(MGA_LAT, MGA_LON, bearing, 1_500)
        assert haversine_m(MGA_LAT, MGA_LON, lat, lon) == pytest.approx(1_500, abs=1.0)
        # Compare on the circle: due north comes back as 359.999..., not 0.0.
        delta = abs(initial_bearing_deg(MGA_LAT, MGA_LON, lat, lon) - bearing) % 360.0
        assert min(delta, 360.0 - delta) < 0.5

    def test_destination_east_increases_longitude(self):
        lat, lon = destination_point(MGA_LAT, MGA_LON, 90.0, 1_000)
        assert lon > MGA_LON
        assert lat == pytest.approx(MGA_LAT, abs=1e-4)

    def test_destination_north_increases_latitude(self):
        lat, lon = destination_point(MGA_LAT, MGA_LON, 0.0, 1_000)
        assert lat > MGA_LAT
        assert lon == pytest.approx(MGA_LON, abs=1e-6)

    def test_distance_from_mga(self):
        assert distance_from_mga_m(*GRANADA) == pytest.approx(32_600, abs=1_000)


class TestCirclePolygon:
    def test_ring_is_closed_and_at_the_right_radius(self):
        feature = circle_polygon(segments=64)
        ring = feature["geometry"]["coordinates"][0]
        assert ring[0] == ring[-1], "GeoJSON rings must close"
        assert len(ring) == 65
        for lon, lat in ring:
            assert haversine_m(MGA_LAT, MGA_LON, lat, lon) == pytest.approx(
                CURATION_RADIUS_M, abs=1.0
            )

    def test_coordinates_are_lon_lat_order(self):
        ring = circle_polygon(segments=8)["geometry"]["coordinates"][0]
        lons = [c[0] for c in ring]
        lats = [c[1] for c in ring]
        assert all(-88 < lon < -85 for lon in lons)
        assert all(11 < lat < 13 for lat in lats)

    def test_granada_is_inside_and_leon_is_outside(self):
        assert distance_from_mga_m(*GRANADA) < CURATION_RADIUS_M
        assert distance_from_mga_m(*LEON) > CURATION_RADIUS_M

    def test_bbox_contains_the_circle(self):
        min_lon, min_lat, max_lon, max_lat = bbox_around(MGA_LAT, MGA_LON, CURATION_RADIUS_M)
        ring = circle_polygon(segments=32)["geometry"]["coordinates"][0]
        for lon, lat in ring:
            assert min_lon <= lon <= max_lon
            assert min_lat <= lat <= max_lat


class TestLineHelpers:
    LINE: ClassVar[list[list[float]]] = [[-86.20, 12.14], [-86.10, 12.14], [-86.10, 12.20]]

    def test_line_length(self):
        expected = haversine_m(12.14, -86.20, 12.14, -86.10) + haversine_m(
            12.14, -86.10, 12.20, -86.10
        )
        assert line_length_m(self.LINE) == pytest.approx(expected, abs=1.0)

    def test_interpolate_start_middle_end(self):
        assert interpolate_along(self.LINE, 0)[1] == pytest.approx(-86.20)
        total = line_length_m(self.LINE)
        end = interpolate_along(self.LINE, total + 5_000)
        assert end == pytest.approx((12.20, -86.10), abs=1e-6)
        mid = interpolate_along(self.LINE, total / 2)
        assert haversine_m(*mid, 12.14, -86.20) == pytest.approx(total / 2, abs=50)

    def test_interpolate_single_point_line(self):
        assert interpolate_along([[-86.1, 12.1]], 500) == (12.1, -86.1)

    def test_nearest_point_on_line(self):
        (lat, lon), dist, along, index = nearest_point_on_line(12.15, -86.15, self.LINE)
        assert lat == pytest.approx(12.14, abs=1e-4)
        assert lon == pytest.approx(-86.15, abs=1e-4)
        assert dist == pytest.approx(haversine_m(12.15, -86.15, 12.14, -86.15), rel=0.01)
        assert index == 0
        assert along == pytest.approx(haversine_m(12.14, -86.20, 12.14, -86.15), rel=0.01)

    def test_nearest_point_clamps_to_endpoint(self):
        (lat, lon), _dist, along, _i = nearest_point_on_line(12.14, -86.30, self.LINE)
        assert (lat, lon) == pytest.approx((12.14, -86.20), abs=1e-5)
        assert along == pytest.approx(0.0, abs=1.0)

    def test_degenerate_line(self):
        point, dist, along, index = nearest_point_on_line(12.14, -86.19, [[-86.20, 12.14]])
        assert point == (12.14, -86.20)
        assert along == 0.0 and index == 0
        assert dist > 0


class TestLocalProjection:
    def test_round_trip(self):
        to_xy, to_lonlat = local_projection(MGA_LAT, MGA_LON)
        lat, lon = 12.1500, -86.1500
        x, y = to_xy(lat, lon)
        back_lat, back_lon = to_lonlat(x, y)
        assert (back_lat, back_lon) == pytest.approx((lat, lon), abs=1e-9)

    def test_metres_agree_with_haversine(self):
        to_xy, _ = local_projection(MGA_LAT, MGA_LON)
        x, y = to_xy(12.1500, -86.1500)
        planar = math.hypot(x, y)
        assert planar == pytest.approx(haversine_m(MGA_LAT, MGA_LON, 12.15, -86.15), rel=0.002)


class TestCuadra:
    def test_known_cities_and_fallback(self):
        assert cuadra_length_m("Managua") == 100.0
        assert cuadra_length_m("León") == 100.0  # accent-folded lookup
        assert cuadra_length_m(None) == 100.0
        assert cuadra_length_m("Bluefields") == 100.0
