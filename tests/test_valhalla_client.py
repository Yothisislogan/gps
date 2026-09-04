"""Valhalla client tests.

Everything here runs against ``httpx.MockTransport`` — no routing engine, no
network.  The point is to pin the request *shape*, because those field names are
the contract with a service this test cannot reach.
"""

from __future__ import annotations

import json

import httpx
import pytest

from common.models import Coordinate
from common.polyline import encode
from common.valhalla import (
    NICARAGUA_AUTO_COSTING,
    AsyncValhallaClient,
    ValhallaClient,
    ValhallaError,
    build_route_payload,
    summarize_route,
)

MGA = (12.1415, -86.1682)
GRANADA = (11.9344, -85.9560)


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def route_response(*, length_km: float = 32.6, time_s: float = 2400.0) -> dict:
    return {
        "trip": {
            "summary": {"length": length_km, "time": time_s},
            "legs": [
                {
                    "shape": encode([MGA, (12.05, -86.05), GRANADA]),
                    "maneuvers": [
                        {
                            "type": 1,
                            "instruction": "Conduzca al este por Carretera Norte.",
                            "verbal_pre_transition_instruction": "Conduzca al este.",
                            "street_names": ["Carretera Norte"],
                            "time": 300.0,
                            "length": 4.2,
                            "begin_shape_index": 0,
                            "end_shape_index": 1,
                        },
                        {
                            "type": 27,
                            "instruction": "Tome la segunda salida de la rotonda.",
                            "roundabout_exit_count": 2,
                            "time": 60.0,
                            "length": 0.2,
                            "begin_shape_index": 1,
                            "end_shape_index": 2,
                        },
                    ],
                }
            ],
        }
    }


class TestPayloadShape:
    def test_locations_accept_tuples_coordinates_and_dicts(self):
        from_tuple = build_route_payload([MGA, GRANADA])
        from_model = build_route_payload(
            [Coordinate(lat=MGA[0], lon=MGA[1]), Coordinate(lat=GRANADA[0], lon=GRANADA[1])]
        )
        from_dict = build_route_payload(
            [{"lat": MGA[0], "lon": MGA[1]}, {"lat": GRANADA[0], "lon": GRANADA[1]}]
        )
        assert from_tuple["locations"] == from_model["locations"] == from_dict["locations"]
        assert from_tuple["locations"][0] == {"lat": 12.1415, "lon": -86.1682, "type": "break"}

    def test_spanish_is_the_default_language_at_the_top_level(self):
        # Nesting these under directions_options still "works" for language and
        # units, but Valhalla silently drops the other directions options from
        # there, so everything stays top-level.
        payload = build_route_payload([MGA, GRANADA])
        assert payload["language"] == "es-ES"
        assert payload["units"] == "kilometers"
        assert "directions_options" not in payload

    def test_costing_options_are_nested_under_the_costing_name(self):
        payload = build_route_payload([MGA, GRANADA], costing="bicycle")
        assert "bicycle" in payload["costing_options"]
        assert payload["costing"] == "bicycle"

    def test_tracks_are_off_by_default(self):
        # highway=track in Nicaragua is a farm path; routing a rental car onto
        # one is the fastest way to lose a user.
        assert build_route_payload([MGA, GRANADA])["costing_options"]["auto"]["use_tracks"] == 0.0
        assert NICARAGUA_AUTO_COSTING["use_tracks"] == 0.0

    def test_unpaved_is_allowed_by_default_and_excluded_on_request(self):
        # Many Nicaraguan places are only reachable on dirt, so the default must
        # not exclude it; the discouragement lives in the build-time speed table.
        default = build_route_payload([MGA, GRANADA])["costing_options"]["auto"]
        assert "exclude_unpaved" not in default

        avoided = build_route_payload([MGA, GRANADA], avoid_unpaved=True)["costing_options"]["auto"]
        assert avoided["exclude_unpaved"] is True

    def test_no_invented_costing_keys(self):
        # Valhalla ignores unknown costing keys silently, so a typo here would
        # never surface as an error — only as routes that ignore the setting.
        known = {
            "use_tracks", "use_living_streets", "service_penalty", "maneuver_penalty",
            "use_ferry", "use_highways", "use_tolls", "country_crossing_penalty",
            "shortest", "exclude_unpaved", "top_speed", "alley_penalty", "gate_penalty",
            "destination_only_penalty", "closure_factor", "speed_penalty_factor",
            "service_factor", "use_distance", "ignore_closures", "fixed_speed",
        }
        for options in (
            build_route_payload([MGA, GRANADA])["costing_options"]["auto"],
            build_route_payload([MGA, GRANADA], avoid_unpaved=True)["costing_options"]["auto"],
        ):
            assert set(options) <= known, set(options) - known

    def test_heading_applies_only_to_the_first_location(self):
        payload = build_route_payload([MGA, GRANADA], heading=270.0)
        assert payload["locations"][0]["heading"] == 270.0
        assert payload["locations"][0]["heading_tolerance"] == 45
        assert "heading" not in payload["locations"][1]

    def test_heading_is_normalised_into_range(self):
        assert build_route_payload([MGA, GRANADA], heading=450.0)["locations"][0]["heading"] == 90.0

    def test_exclude_polygons_keep_geojson_lon_lat_order(self):
        # A ring written lat-first would exclude a patch of ocean instead of the
        # flooded cauce, and nothing would report an error.
        ring = [[-86.20, 12.10], [-86.10, 12.10], [-86.10, 12.20], [-86.20, 12.10]]
        payload = build_route_payload([MGA, GRANADA], exclude_polygons=[ring])
        assert payload["exclude_polygons"] == [ring]
        for lon, lat in payload["exclude_polygons"][0]:
            assert -88 < lon < -82, "first element must be longitude"
            assert 10 < lat < 15, "second element must be latitude"

    def test_osrm_format_asks_for_banner_and_voice_instructions(self):
        payload = build_route_payload([MGA, GRANADA], output_format="osrm")
        assert payload["format"] == "osrm"
        # Top level, not nested: nested under directions_options these are dropped
        # without any error and the nav client gets no guidance.
        assert payload["banner_instructions"] is True
        assert payload["voice_instructions"] is True
        assert payload["turn_lanes"] is True

    def test_native_format_does_not_ask_for_nav_instructions(self):
        payload = build_route_payload([MGA, GRANADA])
        assert "banner_instructions" not in payload
        assert "format" not in payload

    def test_alternates_and_date_time_pass_through(self):
        payload = build_route_payload(
            [MGA, GRANADA], alternates=3, date_time={"type": 1, "value": "2026-09-04T08:00"}
        )
        assert payload["alternates"] == 3
        assert payload["date_time"]["type"] == 1

    def test_zero_alternates_is_omitted(self):
        assert "alternates" not in build_route_payload([MGA, GRANADA], alternates=0)

    def test_overrides_win_over_the_nicaraguan_defaults(self):
        payload = build_route_payload([MGA, GRANADA], costing_overrides={"use_tracks": 1.0})
        assert payload["costing_options"]["auto"]["use_tracks"] == 1.0

    def test_defaults_are_not_mutated_between_calls(self):
        build_route_payload([MGA, GRANADA], costing_overrides={"use_tracks": 1.0})
        assert NICARAGUA_AUTO_COSTING["use_tracks"] == 0.0


class TestSummarize:
    def test_extracts_distance_duration_shape_and_maneuvers(self):
        summary = summarize_route(route_response())
        assert summary.distance_km == pytest.approx(32.6)
        assert summary.duration_s == pytest.approx(2400.0)
        assert len(summary.shape) == 3
        assert summary.shape[0] == pytest.approx(MGA, abs=1e-6)
        assert len(summary.maneuvers) == 2
        assert "rotonda" in summary.maneuvers[1]["instruction"]

    def test_shape_is_decoded_at_precision_six(self):
        # Precision 5 would put this route in the Pacific.
        summary = summarize_route(route_response())
        assert summary.shape[-1] == pytest.approx(GRANADA, abs=1e-6)

    def test_multiple_legs_are_concatenated(self):
        response = route_response()
        response["trip"]["legs"].append(
            {"shape": encode([GRANADA, (11.90, -85.90)]), "maneuvers": [{"type": 4}]}
        )
        summary = summarize_route(response)
        assert len(summary.shape) == 5
        assert len(summary.maneuvers) == 3

    def test_alternates_are_addressable(self):
        response = route_response()
        response["alternates"] = [{"trip": route_response(length_km=40.0)["trip"]}]
        assert summarize_route(response, alternate=0).distance_km == pytest.approx(40.0)

    def test_missing_alternate_raises(self):
        with pytest.raises(ValhallaError, match="no alternate"):
            summarize_route(route_response(), alternate=2)

    def test_missing_trip_raises(self):
        with pytest.raises(ValhallaError, match="no trip"):
            summarize_route({"error": "No path could be found"})


class TestClient:
    def test_route_posts_to_the_right_path(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=route_response())

        with ValhallaClient("http://valhalla:8002", client=mock_client(handler)) as client:
            result = client.route([MGA, GRANADA])
        assert seen["url"] == "http://valhalla:8002/route"
        assert seen["body"]["costing"] == "auto"
        assert result["trip"]["summary"]["length"] == 32.6

    def test_trailing_slash_in_base_url_does_not_double_up(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json=route_response())

        ValhallaClient("http://valhalla:8002/", client=mock_client(handler)).route([MGA, GRANADA])
        assert seen["url"] == "http://valhalla:8002/route"

    def test_http_error_becomes_a_valhalla_error_with_the_payload(self):
        body = {"error_code": 171, "error": "No suitable edges near location"}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json=body)

        client = ValhallaClient(client=mock_client(handler))
        with pytest.raises(ValhallaError) as excinfo:
            client.route([MGA, GRANADA])
        assert excinfo.value.status == 400
        assert excinfo.value.payload["error_code"] == 171

    def test_error_body_with_200_status_is_still_an_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": "No path could be found"})

        with pytest.raises(ValhallaError, match="No path"):
            ValhallaClient(client=mock_client(handler)).route([MGA, GRANADA])

    def test_non_json_body_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>bad gateway</html>")

        with pytest.raises(ValhallaError):
            ValhallaClient(client=mock_client(handler)).route([MGA, GRANADA])

    def test_snap_returns_the_correlated_point(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[{"edges": [{"correlated_lat": 12.1400, "correlated_lon": -86.1700}]}],
            )

        snapped = ValhallaClient(client=mock_client(handler)).snap(12.1415, -86.1682)
        assert snapped == (12.1400, -86.1700)

    def test_snap_falls_back_to_nodes(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=[{"edges": [], "nodes": [{"correlated_lat": 12.14, "correlated_lon": -86.17}]}]
            )

        assert ValhallaClient(client=mock_client(handler)).snap(12.1415, -86.1682) == (12.14, -86.17)

    def test_snap_returns_none_when_nothing_is_nearby(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"edges": [], "nodes": []}])

        assert ValhallaClient(client=mock_client(handler)).snap(12.0, -86.0) is None

    def test_snap_survives_an_outage(self):
        # The geocoder passes snap() straight to relative_address.resolve, which
        # must keep working when Valhalla is down.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"error": "unavailable"})

        assert ValhallaClient(client=mock_client(handler)).snap(12.0, -86.0) is None

    def test_isochrone_request_shape(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"type": "FeatureCollection", "features": []})

        ValhallaClient(client=mock_client(handler)).isochrone(*MGA, contours_minutes=(15, 30))
        assert seen["body"]["contours"] == [{"time": 15.0}, {"time": 30.0}]
        assert seen["body"]["locations"] == [{"lat": MGA[0], "lon": MGA[1]}]

    def test_trace_attributes_request_shape(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"edges": []})

        ValhallaClient(client=mock_client(handler)).trace_attributes(
            [MGA, GRANADA], filters=["edge.names", "edge.speed"]
        )
        assert seen["body"]["shape"][0] == {"lat": MGA[0], "lon": MGA[1]}
        assert seen["body"]["shape_match"] == "map_snap"
        assert seen["body"]["filters"]["action"] == "include"

    def test_status(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/status"
            return httpx.Response(200, json={"version": "3.5.1", "tileset_last_modified": 0})

        assert ValhallaClient(client=mock_client(handler)).status()["version"] == "3.5.1"


class TestAsyncClient:
    @pytest.mark.anyio
    async def test_route(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=route_response())

        client = AsyncValhallaClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        result = await client.route([MGA, GRANADA])
        assert result["trip"]["summary"]["length"] == 32.6
        await client.aclose()

    @pytest.mark.anyio
    async def test_snap_survives_an_outage(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("valhalla down")

        client = AsyncValhallaClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        )
        assert await client.snap(12.0, -86.0) is None
        await client.aclose()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
