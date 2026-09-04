"""Admin moderation UI tests.

Same pattern as tests/test_api.py: a fake Database on ``app.state``, and the
TestClient context is deliberately not entered so the lifespan does not replace
the fakes.

The escaping test is the one that matters most. POI names and user notes come
from OSM, Overture and the public, and one of them will eventually contain a
script tag.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.deps import RateLimiter
from api.main import create_app
from common.config import Settings

ADMIN_USER = "admin"
ADMIN_PASSWORD = "un-secreto-largo"
AUTH = {
    "Authorization": "Basic " + base64.b64encode(f"{ADMIN_USER}:{ADMIN_PASSWORD}".encode()).decode()
}


class FakeAdminDatabase:
    """Answers the admin SQL by matching on a distinctive fragment."""

    def __init__(self) -> None:
        self.available = True
        self.writes: list[tuple[str, Any]] = []
        self.fail_reads = False
        self.poi_name = "Restaurante El Zaguán"

    async def _fetch(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if self.fail_reads:
            raise RuntimeError("database is down")
        # The counts query mentions every table, so it must be matched first.
        if "AS pois," in sql:
            return [
                {
                    "pois": 12500,
                    "pois_circle": 8200,
                    "pois_verified": 310,
                    "match_queue": 42,
                    "suggestions": 3,
                    "flags": 1,
                    "reports": 7,
                    "aliases": 12,
                    "closures": 2,
                    "landmarks": 140,
                    "former_landmarks": 18,
                }
            ]
        if "FROM poi_match_queue q" in sql:
            return [
                {
                    "id": 1,
                    "left_key": "osm:node/1",
                    "right_key": "overture:08f",
                    "score": 0.72,
                    "name_score": 0.8,
                    "distance_m": 34.0,
                    "category_ok": True,
                    "created_at": "2026-09-01",
                    "left_name": "Restaurante El Zaguán",
                    "left_category": "restaurante",
                    "left_source": "osm",
                    "left_lat": 11.93,
                    "left_lon": -85.95,
                    "right_name": "EL ZAGUAN",
                    "right_category": "restaurante",
                    "right_source": "overture",
                    "right_lat": 11.9301,
                    "right_lon": -85.9502,
                }
            ]
        if "FROM poi_suggestion" in sql:
            return [
                {
                    "id": "s1",
                    "payload": {"name": "Fritanga Doña Pilar", "category": "fritanga"},
                    "lat": 12.12,
                    "lon": -86.27,
                    "created_at": "2026-09-01",
                }
            ]
        if "FROM report" in sql:
            return [
                {
                    "id": "r1",
                    "kind": "via_cerrada",
                    "note": "Cauce desbordado",
                    "photo_url": None,
                    "votes": 3,
                    "lat": 12.14,
                    "lon": -86.25,
                    "created_at": "2026-09-01",
                }
            ]
        if "FROM alias" in sql:
            return [
                {
                    "id": "a1",
                    "text": "De la Rotonda El Güegüense, 2c al sur",
                    "confidence": 0.7,
                    "hits": 4,
                    "created_at": "2026-09-01",
                    "lat": 12.133,
                    "lon": -86.281,
                }
            ]
        if "FROM closure" in sql:
            return [
                {
                    "id": "c1",
                    "reason": "Cauce desbordado",
                    "note": None,
                    "starts_at": "2026-09-01",
                    "ends_at": None,
                    "active": True,
                    "source": "admin",
                    "created_by": "admin",
                    "geom": '{"type":"Polygon","coordinates":[[[-86.2,12.1]]]}',
                    "lat": 12.1,
                    "lon": -86.2,
                }
            ]
        if "FROM poi p WHERE p.id" in sql:
            return [
                {
                    "id": "9f2c",
                    "name": self.poi_name,
                    "name_alt": [],
                    "category": "restaurante",
                    "subcategory": None,
                    "cuisine": [],
                    "lat": 11.9302,
                    "lon": -85.9553,
                    "address_text": None,
                    "city": "Granada",
                    "phone": "+50525522522",
                    "whatsapp": None,
                    "website": None,
                    "facebook": None,
                    "instagram": None,
                    "opening_hours": "Mo-Sa 12:00-22:00",
                    "price_level": None,
                    "status": "open",
                    "confidence": 0.9,
                    "verified_at": None,
                    "verified_by": None,
                    "sources": {},
                }
            ]
        if "FROM poi_source WHERE poi_id" in sql:
            return [
                {
                    "source": "osm",
                    "source_id": "node/1",
                    "name": "Restaurante El Zaguán",
                    "category": "restaurante",
                    "license": "ODbL-1.0",
                    "confidence": 0.6,
                    "fetched_at": "2026-09-01",
                }
            ]
        if "SELECT taken_at, metrics" in sql:
            return [
                {
                    "taken_at": "2026-09-03",
                    "metrics": json.dumps(
                        {
                            "road_km_total": 4120.5,
                            "surface_tagged_share": 0.42,
                            "oneway_tagged_share_driver_critical": 0.61,
                            "roundabouts": 38,
                            "traffic_calming": 512,
                            "milestones": 22,
                        }
                    ),
                }
            ]
        return []

    async def _execute(self, sql: str, params: Any = None) -> dict[str, Any]:
        self.writes.append((sql, params))
        return {"id": "1"}


@pytest.fixture
def app_and_db(tmp_path):
    settings = Settings(
        _env_file=None,
        tiles_dir=tmp_path / "tiles",
        data_dir=tmp_path,
        admin_user=ADMIN_USER,
        admin_password=ADMIN_PASSWORD,
    )
    app = create_app(settings)
    database = FakeAdminDatabase()
    app.state.db = database
    app.state.limiter = RateLimiter()
    return app, database


@pytest.fixture
def client(app_and_db):
    app, _ = app_and_db
    return TestClient(app)


class TestAuth:
    def test_pages_require_credentials(self, client):
        response = client.get("/admin")
        assert response.status_code == 401
        assert "Basic" in response.headers.get("www-authenticate", "")

    def test_wrong_password_is_rejected(self, client):
        bad = {"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()}
        assert client.get("/admin", headers=bad).status_code == 401

    def test_malformed_header_is_rejected(self, client):
        assert client.get("/admin", headers={"Authorization": "Basic !!!"}).status_code == 401

    def test_refuses_to_run_without_a_configured_password(self, tmp_path):
        # Falling open would hand the moderation queue to the internet.
        app = create_app(Settings(_env_file=None, admin_password="", tiles_dir=tmp_path))
        app.state.db = FakeAdminDatabase()
        app.state.limiter = RateLimiter()
        assert TestClient(app).get("/admin", headers=AUTH).status_code == 503


class TestPages:
    @pytest.mark.parametrize(
        "path", ["/admin", "/admin/queue", "/admin/aliases", "/admin/closures"]
    )
    def test_renders(self, client, path: str):
        response = client.get(path, headers=AUTH)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "nicanav" in response.text

    def test_dashboard_shows_the_numbers(self, client):
        body = client.get("/admin", headers=AUTH).text
        assert "12500" in body
        assert "«donde fue»" in body
        assert "38" in body  # rotondas from the KPI snapshot

    def test_dashboard_reports_missing_tiles(self, client):
        assert "no existe" in client.get("/admin", headers=AUTH).text

    def test_queue_shows_both_sides_of_a_pair(self, client):
        body = client.get("/admin/queue", headers=AUTH).text
        assert "Restaurante El Zaguán" in body
        assert "EL ZAGUAN" in body
        assert "0.72" in body
        assert "34 m" in body

    def test_aliases_show_the_parser_reading(self, client):
        body = client.get("/admin/aliases", headers=AUTH).text
        # Approving an alias whose landmark the parser could not find teaches
        # the system nothing, so the reading is on the page.
        assert "Rotonda El Güegüense" in body
        assert "cuadra" in body

    def test_closures_warn_about_the_blast_radius(self, client):
        body = client.get("/admin/closures", headers=AUTH).text
        assert "todas" in body.lower()
        assert "longitud, latitud" in body or "[longitud, latitud]" in body

    def test_poi_edit_lists_provenance_and_licences(self, client):
        body = client.get("/admin/poi/9f2c", headers=AUTH).text
        assert "ODbL-1.0" in body
        assert "restaurante" in body

    def test_missing_poi_is_404(self, client, app_and_db):
        _, database = app_and_db

        async def empty(sql, params=None):
            return []

        database._fetch = empty
        assert client.get("/admin/poi/nope", headers=AUTH).status_code == 404


class TestEscaping:
    def test_a_script_tag_in_a_poi_name_is_escaped(self, client, app_and_db):
        _, database = app_and_db
        database.poi_name = '<script>alert("xss")</script>'
        body = client.get("/admin/poi/9f2c", headers=AUTH).text
        assert "<script>alert" not in body
        assert "&lt;script&gt;" in body


class TestActions:
    def test_decide_a_pair(self, client, app_and_db):
        _, database = app_and_db
        response = client.post("/admin/queue/1/decide", data={"decision": "merge"}, headers=AUTH)
        assert response.status_code == 200
        assert "Unidos" in response.text
        sql, params = database.writes[-1]
        assert "poi_match_queue" in sql
        # The audit trail is the difference between a queue and a mess.
        assert ADMIN_USER in params

    def test_an_invalid_decision_is_rejected(self, client):
        assert (
            client.post(
                "/admin/queue/1/decide", data={"decision": "maybe"}, headers=AUTH
            ).status_code
            == 400
        )

    def test_verify_stamps_the_user(self, client, app_and_db):
        _, database = app_and_db
        response = client.post("/admin/poi/9f2c/verify", headers=AUTH)
        assert response.status_code == 200
        assert ADMIN_USER in response.text
        assert "verified_at = now()" in database.writes[-1][0]

    def test_update_rejects_an_unknown_category(self, client):
        response = client.post(
            "/admin/poi/9f2c",
            data={"name": "X", "category": "no_existe", "lat": 12.1, "lon": -86.2},
            headers=AUTH,
        )
        assert response.status_code == 400

    def test_update_rejects_a_point_outside_nicaragua(self, client):
        response = client.post(
            "/admin/poi/9f2c",
            data={"name": "X", "category": "restaurante", "lat": 48.85, "lon": 2.35},
            headers=AUTH,
        )
        assert response.status_code == 400

    def test_update_saves_and_redirects(self, client, app_and_db):
        _, database = app_and_db
        response = client.post(
            "/admin/poi/9f2c",
            data={
                "name": "Restaurante El Zaguán",
                "category": "restaurante",
                "lat": 11.9302,
                "lon": -85.9553,
                "poi_status": "open",
            },
            headers=AUTH,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "UPDATE poi" in database.writes[-1][0]

    def test_closure_rejects_swapped_coordinates(self, client):
        # Lat-first would exclude a patch of the Pacific instead of the flooded
        # cauce, and nothing downstream would report an error.
        response = client.post(
            "/admin/closures",
            data={
                "reason": "Cauce",
                "ring": json.dumps([[12.1, -86.2], [12.1, -86.1], [12.2, -86.1]]),
            },
            headers=AUTH,
        )
        assert response.status_code == 400
        assert "invertidas" in response.json()["detail"]

    def test_closure_needs_three_points(self, client):
        response = client.post(
            "/admin/closures",
            data={"reason": "x", "ring": json.dumps([[-86.2, 12.1]])},
            headers=AUTH,
        )
        assert response.status_code == 400

    def test_closure_is_created_and_closed(self, client, app_and_db):
        _, database = app_and_db
        ring = [[-86.27, 12.14], [-86.26, 12.14], [-86.26, 12.15], [-86.27, 12.15]]
        response = client.post(
            "/admin/closures",
            data={"reason": "Cauce desbordado", "ring": json.dumps(ring)},
            headers=AUTH,
            follow_redirects=False,
        )
        assert response.status_code == 303
        sql, params = database.writes[-1]
        assert "INSERT INTO closure" in sql
        geometry = json.loads(params[0])
        # The ring must be closed for PostGIS to accept it as a polygon.
        assert geometry["coordinates"][0][0] == geometry["coordinates"][0][-1]

    def test_deactivate_closure(self, client, app_and_db):
        _, database = app_and_db
        response = client.post("/admin/closures/c1/deactivate", headers=AUTH)
        assert response.status_code == 200
        assert "active = false" in database.writes[-1][0]

    def test_actions_reject_get(self, client):
        assert client.get("/admin/queue/1/decide", headers=AUTH).status_code == 405
        assert client.get("/admin/poi/9f2c/verify", headers=AUTH).status_code in (404, 405)


class TestDegradation:
    def test_pages_render_when_the_database_is_down(self, client, app_and_db):
        _, database = app_and_db
        database.fail_reads = True
        for path in ("/admin", "/admin/queue", "/admin/aliases", "/admin/closures"):
            response = client.get(path, headers=AUTH)
            assert response.status_code == 200, path
            assert "nicanav" in response.text
