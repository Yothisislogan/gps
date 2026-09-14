"""Publication checks exercise real file locks; external services are replaced."""

import fcntl
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.publication import PublicationGuard
from scripts import releases


def generation(tmp_path, rid, revision=5):
    state = tmp_path / rid
    state.mkdir()
    (state / "writes.lock").touch()
    return {
        "id": rid,
        "state": str(state),
        "port": 8400 if rid == "old" else 8401,
        "asset": ("a" if rid == "old" else "b") * 20,
        "publication_revision": revision,
        "snapshot_revision": revision,
    }


@pytest.mark.parametrize(
    "method,path,blocked",
    [
        ("POST", "/api/report", True),
        ("POST", "/api/alias", True),
        ("POST", "/api/poi/suggest", True),
        ("POST", "/admin/poi/1", True),
        ("GET", "/api/search", False),
        ("POST", "/api/route", False),
    ],
)
def test_old_generation_keeps_reads_but_rejects_mutations(tmp_path, method, path, blocked):
    item = generation(tmp_path, "old")
    Path(item["state"], "writes-paused").touch()
    app = FastAPI()
    app.add_middleware(PublicationGuard, directory=Path(item["state"]), release_id="old")
    app.add_api_route(path, lambda: {"ok": True}, methods=[method])
    response = TestClient(app).request(method, path)
    assert response.status_code == (503 if blocked else 200)
    if blocked:
        assert response.headers["retry-after"] == "30"
        assert response.json()["error"]["code"] == "publication_pending"
    else:
        assert response.headers["x-nicanav-release"] == "old"


def test_mutation_holds_shared_lock_through_commit(tmp_path):
    item = generation(tmp_path, "old")
    app = FastAPI()
    app.add_middleware(PublicationGuard, directory=Path(item["state"]), release_id="old")

    @app.post("/api/report")
    def commit():
        with Path(item["state"], "writes.lock").open("rb") as lock, pytest.raises(BlockingIOError):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return {"ok": True}

    assert TestClient(app).post("/api/report").status_code == 200
    with releases.pause_writes(item):
        assert Path(item["state"], "writes-paused").exists()
    assert not Path(item["state"], "writes-paused").exists()


def publication(tmp_path, monkeypatch, *, actual=5, http_ok=True):
    old, new = generation(tmp_path, "old"), generation(tmp_path, "new", 9)
    new["snapshot_revision"] = 5
    Path(new["state"], "writes-paused").touch()
    registry = {"active": "old", "public_url": "https://example.invalid", "releases": {"old": old}}
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry))
    switches = []
    monkeypatch.setattr(releases, "revision", lambda r: actual if r["id"] == "old" else 9)
    monkeypatch.setattr(releases, "install_gateway", lambda *args: switches.append(args[-1]))
    monkeypatch.setattr(
        releases.httpx,
        "get",
        lambda *a, **kw: SimpleNamespace(
            status_code=200 if http_ok else 503, headers={"x-nicanav-release": "new"}
        ),
    )
    return registry, path, old, new, switches


def test_new_correction_prevents_switch_and_restores_writes(tmp_path, monkeypatch):
    registry, path, old, new, switches = publication(tmp_path, monkeypatch, actual=6)
    with pytest.raises(RuntimeError, match="New corrections"):
        releases.promote(registry, new, path)
    assert switches == []
    assert json.loads(path.read_text())["active"] == "old"
    assert not Path(old["state"], "writes-paused").exists()
    assert Path(new["state"], "writes-paused").exists()


def test_failed_public_probe_restores_gateway_and_writes(tmp_path, monkeypatch):
    registry, path, old, new, switches = publication(tmp_path, monkeypatch, http_ok=False)
    with pytest.raises(RuntimeError, match="smoke check"):
        releases.promote(registry, new, path)
    assert switches == ["new", "old"]
    assert json.loads(path.read_text())["active"] == "old"
    assert not Path(old["state"], "writes-paused").exists()


def test_success_keeps_old_snapshot_readonly_and_opens_new(tmp_path, monkeypatch):
    registry, path, old, new, switches = publication(tmp_path, monkeypatch)
    releases.promote(registry, new, path)
    assert switches == ["new"]
    assert json.loads(path.read_text())["active"] == "new"
    assert Path(old["state"], "writes-paused").exists()
    assert not Path(new["state"], "writes-paused").exists()


def test_rollback_refuses_to_discard_new_edits(tmp_path, monkeypatch):
    registry, path, _old, new, switches = publication(tmp_path, monkeypatch, actual=6)
    with pytest.raises(RuntimeError, match="New corrections"):
        releases.promote(registry, new, path, rollback=True)
    assert switches == []


def test_gateway_keeps_generation_and_asset_routes_and_rejects_injection(tmp_path):
    old, new = generation(tmp_path, "old"), generation(tmp_path, "new")
    rendered = releases.gateway({"old": old, "new": new}, "new")
    assert "/data-releases/old/" in rendered and "127.0.0.1:8400/" in rendered
    assert "/data-releases/new/" in rendered and "127.0.0.1:8401/" in rendered
    assert "location /data-releases/ { return 410; }" in rendered
    old["id"] = "bad; include /etc/private;"
    with pytest.raises(ValueError):
        releases.gateway({"old": old, "new": new}, "new")


@pytest.mark.parametrize("value", ["abc$other", "quote'and\\slash", "a/b+c=="])
def test_secret_env_roundtrip(value):
    assert (
        dotenv_values(stream=io.StringIO(releases.env_text({"SECRET": value})))["SECRET"] == value
    )


def test_coverage_and_route_regressions_block_publication():
    with pytest.raises(RuntimeError):
        releases.gate_counts(1000, 899)
    with pytest.raises(RuntimeError):
        releases.gate_counts(0, 0)
    releases.gate_counts(1000, 950)
    previous = {"results": [{"id": "mga", "distance_km": 10, "duration_min": 20}]}
    with pytest.raises(RuntimeError, match="25%"):
        releases.gate_routes(
            previous, {"results": [{"id": "mga", "distance_km": 20, "duration_min": 20}]}
        )
    with pytest.raises(RuntimeError):
        releases.gate_routes(previous, {"results": []})
    releases.gate_routes(previous, previous)


def test_host_commands_use_compose_relative_paths_and_loopback_services(tmp_path, monkeypatch):
    from scripts.run_host import host_environment

    env_file = tmp_path / "infra/.env"
    env_file.parent.mkdir()
    env_file.write_text(
        "NICANAV_DATA_DIR=../data\nNICANAV_PG_BIND_PORT=5544\nNICANAV_MEILI_PORT=7755\n"
    )
    for name in ("NICANAV_DATA_DIR", "NICANAV_PG_HOST", "NICANAV_PG_PORT", "NICANAV_MEILI_URL"):
        monkeypatch.delenv(name, raising=False)
    env = host_environment(env_file)
    assert env["NICANAV_DATA_DIR"] == str(tmp_path / "data")
    assert env["NICANAV_PG_HOST"] == "127.0.0.1"
    assert env["NICANAV_PG_PORT"] == "5544"
    assert env["NICANAV_MEILI_URL"] == "http://127.0.0.1:7755"
