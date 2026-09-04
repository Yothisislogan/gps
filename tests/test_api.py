"""API tests.

The app is exercised end to end through FastAPI's TestClient with fake clients
in ``app.state`` — no Postgres, no Meilisearch, no Valhalla.  The lifespan is
deliberately not entered, so the fakes stay in place.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.deps import RateLimiter
from api.main import create_app
from common.config import Settings
from common.models import PoiCard, PoiStatus, SearchHit, SearchKind
from common.polyline import encode
from common.valhalla import ValhallaError

MGA = (12.1415, -86.1682)
GRANADA = (11.9344, -85.9560)


class FakeDatabase:
    """In-memory stand-in with the same surface the routers use."""

    def __init__(self) -> None:
        self.available = True
        self.closures: list[list[list[float]]] = []
        self.landmarks: list[dict[str, Any]] = [
            {
                "id": "gz-1",
                "name": "Rotonda El Güegüense",
                "kind": "rotonda",
                "city": "Managua",
                "former": False,
                "popularity": 0.9,
                "lat": 12.1352,
                "lon": -86.2807,
                "score": 0.95,
            }
        ]
        self.aliases: list[dict[str, Any]] = []
        self.pois: dict[str, PoiCard] = {
            "9f2c0000-0000-0000-0000-000000000001": PoiCard(
                id="9f2c0000-0000-0000-0000-000000000001",
                name="Restaurante El Zaguán",
                category="restaurante",
                lat=11.9302,
                lon=-85.9553,
                phone="+50525522522",
                opening_hours="Mo-Sa 12:00-22:00",
                status=PoiStatus.OPEN,
            )
        }
        self.reports: list[tuple] = []
        self.suggestions: list[tuple] = []
        self.alias_writes: list[tuple] = []
        self.searches: list[tuple] = []
        self.routes: list[dict[str, Any]] = []
        self.highways: dict[str, list[list[float]]] = {}

    async def health(self) -> bool:
        return self.available

    async def active_closures(self):
        return self.closures

    async def find_landmarks(self, name, *, former=None, limit=5, city=None):
        rows = self.landmarks
        if former is not None:
            rows = [row for row in rows if bool(row["former"]) is bool(former)]
        return rows[:limit]

    async def find_alias(self, text, *, limit=3):
        return self.aliases[:limit]

    async def get_poi(self, poi_id):
        return self.pois.get(poi_id)

    async def pois_near(self, lat, lon, *, radius_m=1000, category=None, limit=30):
        return [
            SearchHit(
                id=card.id,
                kind=SearchKind.POI,
                name=card.name,
                lat=card.lat,
                lon=card.lon,
                category=card.category,
                distance_m=120.0,
            )
            for card in self.pois.values()
        ][:limit]

    async def pois_along_route(self, shape, *, radius_m=300, category=None, limit=50):
        return await self.pois_near(shape[0][0], shape[0][1], limit=limit)

    async def nearest_street(self, lat, lon, *, radius_m=200):
        return "Rotonda El Güegüense"

    async def highway_geometry(self, key):
        return self.highways.get(key)

    async def kmpost_calibration(self, key):
        return []

    async def insert_report(self, kind, lat, lon, note, photo_url, client_id):
        self.reports.append((kind, lat, lon, note, photo_url, client_id))
        return "report-1"

    async def insert_suggestion(self, payload, lat, lon, client_id):
        self.suggestions.append((payload, lat, lon, client_id))
        return "suggestion-1"

    async def insert_alias(self, text, lat, lon, *, poi_id, created_by):
        self.alias_writes.append((text, lat, lon, poi_id, created_by))
        return "alias-1"

    async def log_search(self, q, hits, lat, lon, kind=None):
        self.searches.append((q, hits, lat, lon, kind))

    async def log_route(self, *args, **kwargs):
        self.routes.append(kwargs)

    async def _fetch(self, sql, params=None):
        if "gazetteer" in sql:
            return self.landmarks
        return []


class FakeMeili:
    def __init__(self, hits: list[dict[str, Any]] | None = None) -> None:
        self.hits = hits if hits is not None else []
        self.calls: list[dict[str, Any]] = []
        self.fail = False

    async def search(self, query, **kwargs):
        from api.clients.meili import MeiliError

        self.calls.append({"q": query, **kwargs})
        if self.fail:
            raise MeiliError("down")
        return {"hits": self.hits, "estimatedTotalHits": len(self.hits)}

    async def health(self) -> bool:
        return not self.fail


class FakeValhalla:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.error: Exception | None = None

    async def route(self, locations, **kwargs):
        if self.error is not None:
            raise self.error
        self.payloads.append({"locations": list(locations), **kwargs})
        return {
            "trip": {
                "summary": {"length": 32.6, "time": 2400.0},
                "legs": [{"shape": encode([MGA, GRANADA]), "maneuvers": []}],
            }
        }

    async def snap(self, lat, lon, **kwargs):
        return (lat, lon)

    async def status(self, **kwargs):
        return {"version": "3.5.1", "tileset_last_modified": 1_700_000_000}


@pytest.fixture
def app_and_fakes(tmp_path):
    settings = Settings(_env_file=None, tiles_dir=tmp_path / "tiles", data_dir=tmp_path)
    app = create_app(settings)
    database, meili, valhalla = FakeDatabase(), FakeMeili(), FakeValhalla()
    app.state.db = database
    app.state.meili = meili
    app.state.valhalla = valhalla
    app.state.limiter = RateLimiter()
    return app, database, meili, valhalla


@pytest.fixture
def client(app_and_fakes):
    app, *_ = app_and_fakes
    # No `with`: entering the context would run the lifespan and replace the fakes.
    return TestClient(app)


class TestHealth:
    def test_reports_each_dependency_separately(self, client, app_and_fakes):
        _, _, meili, _ = app_and_fakes
        meili.fail = True
        body = client.get("/api/healthz").json()
        assert body["status"] == "ok"
        assert body["valhalla"]["ok"] is True
        assert body["meili"]["ok"] is False
        assert body["db"]["ok"] is True

    def test_reports_missing_tiles(self, client):
        assert client.get("/api/healthz").json()["tiles"]["base.pmtiles"] is None


class TestRoute:
    def test_routes_two_points(self, client, app_and_fakes):
        response = client.post(
            "/api/route",
            json={
                "locations": [
                    {"lat": MGA[0], "lon": MGA[1]},
                    {"lat": GRANADA[0], "lon": GRANADA[1]},
                ]
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["trip"]["summary"]["length"] == 32.6
        assert body["nicanav"]["closures_applied"] == 0
        assert body["nicanav"]["language"] == "es-ES"

    def test_injects_active_closures(self, client, app_and_fakes):
        _, database, _, valhalla = app_and_fakes
        ring = [[-86.20, 12.10], [-86.10, 12.10], [-86.10, 12.20], [-86.20, 12.10]]
        database.closures = [ring]
        response = client.post(
            "/api/route",
            json={
                "locations": [
                    {"lat": MGA[0], "lon": MGA[1]},
                    {"lat": GRANADA[0], "lon": GRANADA[1]},
                ]
            },
        )
        assert response.json()["nicanav"]["closures_applied"] == 1
        assert valhalla.payloads[-1]["exclude_polygons"] == [ring]

    def test_closures_can_be_skipped(self, client, app_and_fakes):
        _, database, _, valhalla = app_and_fakes
        database.closures = [[[-86.2, 12.1], [-86.1, 12.1], [-86.1, 12.2], [-86.2, 12.1]]]
        client.post(
            "/api/route",
            json={
                "locations": [
                    {"lat": MGA[0], "lon": MGA[1]},
                    {"lat": GRANADA[0], "lon": GRANADA[1]},
                ],
                "exclude_closures": False,
            },
        )
        assert valhalla.payloads[-1]["exclude_polygons"] is None

    def test_logs_the_trip_coarsely(self, client, app_and_fakes):
        _, database, _, _ = app_and_fakes
        client.post(
            "/api/route",
            json={
                "locations": [
                    {"lat": MGA[0], "lon": MGA[1]},
                    {"lat": GRANADA[0], "lon": GRANADA[1]},
                ]
            },
        )
        assert database.routes[-1]["length_km"] == 32.6
        assert database.routes[-1]["reroute"] is False

    def test_heading_marks_a_reroute(self, client, app_and_fakes):
        _, database, _, valhalla = app_and_fakes
        client.post(
            "/api/route",
            json={
                "locations": [
                    {"lat": MGA[0], "lon": MGA[1]},
                    {"lat": GRANADA[0], "lon": GRANADA[1]},
                ],
                "heading": 180,
            },
        )
        assert valhalla.payloads[-1]["heading"] == 180
        assert database.routes[-1]["reroute"] is True

    def test_rejects_a_single_location(self, client):
        response = client.post("/api/route", json={"locations": [{"lat": MGA[0], "lon": MGA[1]}]})
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"

    def test_rejects_too_many_locations(self, client):
        response = client.post(
            "/api/route",
            json={"locations": [{"lat": 12.0 + i / 100, "lon": -86.0} for i in range(11)]},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "too_many_locations"

    def test_valhalla_no_road_error_is_translated_to_spanish(self, client, app_and_fakes):
        _, _, _, valhalla = app_and_fakes
        valhalla.error = ValhallaError("no edges", status=400, payload={"error_code": 171})
        response = client.post(
            "/api/route",
            json={
                "locations": [
                    {"lat": MGA[0], "lon": MGA[1]},
                    {"lat": GRANADA[0], "lon": GRANADA[1]},
                ]
            },
        )
        assert response.status_code == 502
        body = response.json()
        assert body["error"]["code"] == "no_road_nearby"
        assert "calle cercana" in body["error"]["message"]

    def test_rate_limit_returns_429(self, client, app_and_fakes):
        app, *_ = app_and_fakes
        app.state.settings.rate_limit_route = "2/minute"
        payload = {
            "locations": [{"lat": MGA[0], "lon": MGA[1]}, {"lat": GRANADA[0], "lon": GRANADA[1]}]
        }
        statuses = [client.post("/api/route", json=payload).status_code for _ in range(4)]
        assert statuses[:2] == [200, 200]
        assert statuses[2] == 429
        assert client.post("/api/route", json=payload).json()["error"]["code"] == "rate_limited"


class TestSearch:
    def test_returns_index_hits(self, client, app_and_fakes):
        _, _, meili, _ = app_and_fakes
        meili.hits = [
            {
                "id": "poi:abc",
                "kind": "poi",
                "name": "Fritanga La Fe",
                "lat": 12.12,
                "lon": -86.27,
                "category": "fritanga",
                "verified": True,
                "_geoDistance": 240,
            }
        ]
        body = client.get(
            "/api/search", params={"q": "fritanga", "lat": 12.1, "lon": -86.27}
        ).json()
        assert body["hits"][0]["name"] == "Fritanga La Fe"
        assert body["hits"][0]["id"] == "abc"
        assert body["hits"][0]["distance_m"] == 240
        assert body["hits"][0]["verified"] is True
        assert body["took_ms"] >= 0

    def test_position_biases_the_query(self, client, app_and_fakes):
        _, _, meili, _ = app_and_fakes
        client.get(
            "/api/search", params={"q": "farmacia", "lat": 12.1, "lon": -86.2, "radius_m": 2000}
        )
        call = meili.calls[0]
        assert call["lat"] == 12.1 and call["radius_m"] == 2000
        assert call["sort_by_distance"] is True

    def test_falls_back_to_postgis_when_the_index_is_down(self, client, app_and_fakes):
        _, _, meili, _ = app_and_fakes
        meili.fail = True
        body = client.get("/api/search", params={"q": "zaguan", "lat": 11.93, "lon": -85.95}).json()
        assert body["hits"], "a search-index outage must not empty the map"
        assert body["hits"][0]["name"] == "Restaurante El Zaguán"

    def test_address_queries_come_back_with_a_geocode_block(self, client):
        body = client.get(
            "/api/search", params={"q": "De la Rotonda El Güegüense, 2c al sur, 1c abajo"}
        ).json()
        assert body["geocode"] is not None
        assert body["geocode"]["parsed_as"] == "relative"
        candidate = body["geocode"]["candidates"][0]
        assert candidate["lat"] < 12.1352 and candidate["lon"] < -86.2807

    def test_plain_queries_have_no_geocode_block(self, client, app_and_fakes):
        _, _, meili, _ = app_and_fakes
        meili.hits = [{"id": "poi:1", "kind": "poi", "name": "Pulpería", "lat": 12.1, "lon": -86.2}]
        assert client.get("/api/search", params={"q": "pulperia"}).json()["geocode"] is None

    def test_filters_are_passed_through(self, client, app_and_fakes):
        _, _, meili, _ = app_and_fakes
        client.get("/api/search", params={"q": "x", "kind": "poi", "category": "gasolinera"})
        assert 'kind = "poi"' in meili.calls[0]["filters"]
        assert 'category = "gasolinera"' in meili.calls[0]["filters"]

    def test_search_is_logged(self, client, app_and_fakes):
        _, database, _, _ = app_and_fakes
        client.get("/api/search", params={"q": "vulcanizacion", "lat": 12.1415, "lon": -86.1682})
        assert database.searches[-1][0] == "vulcanizacion"

    def test_empty_query_is_rejected(self, client):
        assert client.get("/api/search", params={"q": ""}).status_code == 422


class TestGeocode:
    def test_relative_address(self, client):
        body = client.get(
            "/api/geocode", params={"q": "De la Rotonda El Güegüense, 2c al sur"}
        ).json()
        assert body["parsed_as"] == "relative"
        candidate = body["candidates"][0]
        assert candidate["landmark_name"] == "Rotonda El Güegüense"
        assert candidate["relative"]["offsets"][0]["distance_m"] == 168.0  # 2 cuadras
        assert candidate["snapped_to_road"] is True

    def test_pasted_coordinates(self, client):
        body = client.get("/api/geocode", params={"q": "12.1415, -86.1682"}).json()
        assert body["parsed_as"] == "coordinate"
        assert body["candidates"][0]["lat"] == pytest.approx(12.1415)
        assert body["candidates"][0]["confidence"] == 1.0

    def test_a_confirmed_alias_wins_over_a_parse(self, client, app_and_fakes):
        _, database, _, _ = app_and_fakes
        database.aliases = [
            {
                "id": "a1",
                "text": "De la Rotonda El Güegüense, 2c al sur",
                "lat": 12.1000,
                "lon": -86.2800,
                "score": 0.99,
                "hits": 5,
                "poi_id": None,
            }
        ]
        body = client.get(
            "/api/geocode", params={"q": "De la Rotonda El Güegüense, 2c al sur"}
        ).json()
        assert body["candidates"][0]["lat"] == pytest.approx(12.1000)
        assert "confirmada" in body["candidates"][0]["notes"][0]

    def test_unknown_landmark_falls_back_to_the_index(self, client, app_and_fakes):
        _, database, meili, _ = app_and_fakes
        database.landmarks = []
        meili.hits = [
            {"id": "poi:9", "kind": "poi", "name": "Rotonda Inventada", "lat": 12.2, "lon": -86.3}
        ]
        body = client.get("/api/geocode", params={"q": "De la Rotonda Inventada, 2c al sur"}).json()
        assert body["candidates"], "a missing landmark should degrade, not return nothing"

    def test_candidates_are_confidence_sorted(self, client):
        body = client.get(
            "/api/geocode", params={"q": "De la Rotonda El Güegüense, 1c al sur"}
        ).json()
        confidences = [c["confidence"] for c in body["candidates"]]
        assert confidences == sorted(confidences, reverse=True)


class TestKmPost:
    """ "Km 12.5 Carretera a Masaya" is a real Nicaraguan address."""

    def _stub_highway(self, database):
        # A straight synthetic Carretera a Masaya running south-east from
        # Managua, long enough that km 12.5 lands on it.
        from common.geo import destination_point

        start = (12.1150, -86.2504)
        end = destination_point(*start, 135.0, 30_000)
        database.highways["carretera_a_masaya"] = [
            [start[1], start[0]],
            [end[1], end[0]],
        ]

    def test_resolves_a_km_post(self, client, app_and_fakes):
        _, database, _, _ = app_and_fakes
        self._stub_highway(database)
        body = client.get("/api/geocode", params={"q": "Km 12.5 Carretera a Masaya"}).json()
        assert body["candidates"], body
        candidate = body["candidates"][0]
        assert candidate["method"] == "kmpost"

        from common.geo import haversine_m

        assert haversine_m(12.1150, -86.2504, candidate["lat"], candidate["lon"]) == pytest.approx(
            12_500, rel=0.05
        )

    def test_unknown_highway_returns_no_kmpost_candidate(self, client):
        # No highway geometry is loaded in the fake database.
        body = client.get("/api/geocode", params={"q": "Km 5 Carretera a Ninguna Parte"}).json()
        assert all(c["method"] != "kmpost" for c in body["candidates"])

    def test_a_broken_kmpost_module_does_not_break_the_cascade(self, client, monkeypatch):
        # Every geocoding strategy is wrapped: one failing must not take the
        # others down, because a user typing an address gets one shot.
        import api.routers.geocode as geocode_router

        class Exploding:
            @staticmethod
            def looks_like_kmpost(_text):
                return True

            @staticmethod
            def parse(_text):
                raise RuntimeError("boom")

        monkeypatch.setattr(geocode_router, "_kmpost_module", lambda: Exploding)
        body = client.get(
            "/api/geocode", params={"q": "De la Rotonda El Güegüense, 2c al sur"}
        ).json()
        assert body["candidates"], "the relative-address path must still answer"


class TestReverse:
    def test_a_broken_reverse_module_falls_back(self, client, monkeypatch):
        import api.routers.geocode as geocode_router

        class Exploding:
            @staticmethod
            def reverse(*_args, **_kwargs):
                raise RuntimeError("boom")

        monkeypatch.setattr(geocode_router, "_reverse_module", lambda: Exploding)
        body = client.get("/api/reverse", params={"lat": 12.1352, "lon": -86.2807}).json()
        assert body["candidates"], "reverse must always answer something"

    def test_falls_back_to_the_nearest_landmark(self, client):
        body = client.get("/api/reverse", params={"lat": 12.1352, "lon": -86.2807}).json()
        assert body["candidates"], "reverse must always answer something"
        assert "Güegüense" in body["candidates"][0]["label"]

    def test_rejects_an_out_of_range_latitude(self, client):
        assert client.get("/api/reverse", params={"lat": 999, "lon": -86.2}).status_code == 422


class TestPoi:
    POI_ID = "9f2c0000-0000-0000-0000-000000000001"

    def test_card(self, client):
        body = client.get(f"/api/poi/{self.POI_ID}").json()
        assert body["name"] == "Restaurante El Zaguán"
        assert body["opening_hours"] == "Mo-Sa 12:00-22:00"
        assert body["status"] == "open"

    def test_missing_poi_is_a_spanish_404(self, client):
        response = client.get("/api/poi/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "poi_not_found"

    def test_along_route(self, client):
        polyline = encode([MGA, GRANADA])
        body = client.get("/api/poi/along_route", params={"polyline": polyline}).json()
        assert body["hits"][0]["name"] == "Restaurante El Zaguán"

    def test_along_route_rejects_a_broken_polyline(self, client):
        response = client.get("/api/poi/along_route", params={"polyline": "!!!!"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_polyline"

    def test_literal_path_wins_over_the_id_parameter(self, client):
        # /poi/along_route must not be read as a POI whose id is "along_route".
        assert (
            client.get(
                "/api/poi/along_route", params={"polyline": encode([MGA, GRANADA])}
            ).status_code
            == 200
        )


class TestSubmissions:
    def test_report(self, client, app_and_fakes):
        _, database, _, _ = app_and_fakes
        response = client.post(
            "/api/report",
            json={"kind": "via_cerrada", "lat": 12.14, "lon": -86.25, "note": "Cauce desbordado"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        assert database.reports[-1][0] == "via_cerrada"

    def test_report_outside_nicaragua_is_rejected(self, client):
        response = client.post("/api/report", json={"kind": "bache", "lat": 48.85, "lon": 2.35})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "outside_nicaragua"

    def test_swapped_coordinates_are_caught(self, client):
        # A client that sends (lon, lat) lands in the Indian Ocean.
        response = client.post("/api/report", json={"kind": "bache", "lat": -86.25, "lon": 12.14})
        assert response.status_code == 400

    def test_unknown_report_kind_is_rejected(self, client):
        response = client.post("/api/report", json={"kind": "ovni", "lat": 12.1, "lon": -86.2})
        assert response.status_code == 422

    def test_suggest_poi(self, client, app_and_fakes):
        _, database, _, _ = app_and_fakes
        response = client.post(
            "/api/poi/suggest",
            json={
                "name": "Fritanga Doña Pilar",
                "category": "fritanga",
                "lat": 12.12,
                "lon": -86.27,
                "whatsapp": "+50588887777",
            },
        )
        assert response.status_code == 200
        assert database.suggestions[-1][0]["name"] == "Fritanga Doña Pilar"

    def test_alias_learning(self, client, app_and_fakes):
        _, database, _, _ = app_and_fakes
        response = client.post(
            "/api/alias",
            json={"text": "De la Rotonda El Güegüense, 2c al sur", "lat": 12.133, "lon": -86.281},
        )
        assert response.status_code == 200
        assert database.alias_writes[-1][0].startswith("De la Rotonda")

    def test_client_fingerprint_is_hashed_not_stored_raw(self, client, app_and_fakes):
        _, database, _, _ = app_and_fakes
        client.post(
            "/api/report",
            json={"kind": "bache", "lat": 12.1, "lon": -86.2},
            headers={"X-Forwarded-For": "190.53.1.1"},
        )
        fingerprint = database.reports[-1][-1]
        assert "190.53" not in fingerprint
        assert len(fingerprint) == 32

    def test_report_rate_limit(self, client, app_and_fakes):
        app, *_ = app_and_fakes
        app.state.settings.rate_limit_report = "1/minute"
        payload = {"kind": "bache", "lat": 12.1, "lon": -86.2}
        assert client.post("/api/report", json=payload).status_code == 200
        assert client.post("/api/report", json=payload).status_code == 429


class TestErrorEnvelope:
    def test_every_error_uses_the_same_shape(self, client):
        for response in (
            client.get("/api/poi/00000000-0000-0000-0000-000000000000"),
            client.get("/api/search", params={"q": ""}),
            client.post("/api/report", json={"kind": "bache", "lat": 48.0, "lon": 2.0}),
        ):
            body = response.json()
            assert set(body) == {"error"}
            assert isinstance(body["error"]["code"], str)
            assert isinstance(body["error"]["message"], str)
