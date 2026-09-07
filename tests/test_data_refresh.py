"""Failure-path regression checks for unattended refreshes."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from common.data_status import read_status
from pipeline.pois import fetch_overture as overture
from pipeline.status import save, update_run

ROOT = Path(__file__).resolve().parents[1]


def test_failed_and_partial_runs_do_not_advance_full_success(tmp_path):
    path = tmp_path / "nightly.json"
    update_run(path, "start")
    update_run(path, "succeeded")
    success = read_status(path)["last_full_success_at"]
    update_run(path, "start")
    update_run(path, "failed", "load pois")
    assert read_status(path)["last_full_success_at"] == success
    assert read_status(path)["stage"] == "load pois"
    update_run(path, "start", scope="partial")
    update_run(path, "succeeded")
    assert read_status(path)["last_full_success_at"] == success


@pytest.mark.parametrize("content", ["{", "[]", '{"schema_version": 2}', '{"schema_version": 1}'])
def test_invalid_metadata_is_unknown(tmp_path, content):
    path = tmp_path / "broken.json"
    assert read_status(path) == {"status": "unknown"}
    path.write_text(content)
    assert read_status(path) == {"status": "unknown"}


def extract_args(tmp_path):
    return [
        "--release",
        "2026-08-19.0",
        "--no-cache",
        "--skip-unchanged",
        "--output",
        str(tmp_path / "places.jsonl"),
        "--metadata",
        str(tmp_path / "overture.json"),
    ]


def feature():
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-86, 12]},
        "properties": {"name": "Cafe"},
    }


def test_unchanged_extract_skips_but_changed_file_refetches(tmp_path, monkeypatch):
    calls = []

    def fetch(*args, **kwargs):
        calls.append(args)
        yield feature()

    monkeypatch.setattr(overture, "fetch", fetch)
    args = extract_args(tmp_path)
    assert overture.main(args) == 0
    original = (tmp_path / "overture.json").read_text()
    assert overture.main(args) == 3
    assert len(calls) == 1
    assert (tmp_path / "overture.json").read_text() == original
    (tmp_path / "places.jsonl").write_text("tampered")
    assert overture.main(args) == 0
    assert len(calls) == 2


@pytest.mark.parametrize("stream_failure", [False, True])
def test_empty_or_interrupted_extract_preserves_last_good_data(
    tmp_path, monkeypatch, stream_failure
):
    output = tmp_path / "places.jsonl"
    metadata = tmp_path / "overture.json"
    output.write_text("previous data")
    metadata.write_text("previous metadata")

    def fetch(*args, **kwargs):
        if stream_failure:
            yield feature()
            raise RuntimeError("connection lost")
        return
        yield  # pragma: no cover

    monkeypatch.setattr(overture, "fetch", fetch)
    assert overture.main(extract_args(tmp_path)) == 1
    assert output.read_text() == "previous data"
    assert metadata.read_text() == "previous metadata"


def test_failed_release_discovery_does_not_claim_current_data():
    class Connection:
        def execute(self, *_):
            raise RuntimeError("unavailable")

    with pytest.raises(RuntimeError, match="Cannot establish"):
        overture.discover_release(Connection(), allow_fallback=False)


def test_invalid_release_rejected_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(overture, "_connect", lambda: pytest.fail("must not connect"))
    args = extract_args(tmp_path)
    args[1] = "2026-08-19.0'; DROP TABLE places"
    assert overture.main(args) == 1


@pytest.fixture
def shell_pipeline(tmp_path):
    root = tmp_path / "repo"
    pipeline = root / "pipeline"
    pipeline.mkdir(parents=True)
    for name in ("lib.sh", "nightly.sh", "monthly_overture.sh"):
        shutil.copy(ROOT / "pipeline" / name, pipeline / name)
    for name in ("fetch_osm.sh", "build_tiles.sh", "build_valhalla.sh"):
        path = pipeline / name
        path.write_text(
            '#!/bin/bash\necho "$(basename "$0") $*" >> "$CALL_LOG"\n[ "$FAIL_SCRIPT" != "$(basename "$0")" ]\n'
        )
        path.chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    python = bindir / "python3"
    python.write_text(f"""#!/bin/bash
if [ "$1" = -c ] || [ "$2" = pipeline.status ]; then
  exec {sys.executable} "$@"
fi
echo "$*" >> "$CALL_LOG"
if [ "$2" = pipeline.pois.fetch_overture ]; then exit "${{STUB_FETCH_CODE:-0}}"; fi
if [ "$2" = "$FAIL_MODULE" ]; then exit 7; fi
exit 0
""")
    python.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "PYTHONPATH": str(ROOT),
        "NICANAV_DATA_DIR": str(tmp_path / "data"),
        "NICANAV_METADATA_DIR": str(tmp_path / "metadata"),
        "CALL_LOG": str(tmp_path / "calls"),
        "FAIL_SCRIPT": "",
        "FAIL_MODULE": "",
    }

    Path(env["NICANAV_METADATA_DIR"]).mkdir()
    save(
        Path(env["NICANAV_METADATA_DIR"]) / "overture.json",
        {"status": "imported", "sha256": "test-source"},
    )

    def run(name, *args, **overrides):
        result = subprocess.run(
            ["bash", str(pipeline / name), *args],
            env={**env, **overrides},
            capture_output=True,
            text=True,
        )
        log = Path(env["CALL_LOG"])
        return result, log.read_text() if log.exists() else "", Path(env["NICANAV_METADATA_DIR"])

    return run


def test_nightly_stops_before_using_stale_input(shell_pipeline):
    result, calls, metadata = shell_pipeline("nightly.sh", FAIL_SCRIPT="fetch_osm.sh")
    assert result.returncode != 0
    assert "build_tiles" not in calls
    assert "conflate" not in calls
    status = read_status(metadata / "nightly.json")
    assert status["status"] == "failed"
    assert status["stage"] == "fetch osm"
    assert status["last_full_success_at"] is None


def test_failure_inside_python_step_stops_publication(shell_pipeline):
    result, calls, metadata = shell_pipeline("nightly.sh", FAIL_MODULE="pipeline.pois.load_pois")
    assert result.returncode == 7
    assert "export_geojson" not in calls
    assert "build_index" not in calls
    assert read_status(metadata / "nightly.json")["stage"] == "load pois"


def test_partial_run_never_claims_full_success(shell_pipeline):
    result, _, metadata = shell_pipeline("nightly.sh", "--skip-valhalla")
    assert result.returncode == 0, result.stderr
    status = read_status(metadata / "nightly.json")
    assert status["scope"] == "partial"
    assert status["last_full_success_at"] is None


def test_overture_retries_incomplete_publication_then_skips(shell_pipeline):
    result, calls, metadata = shell_pipeline(
        "monthly_overture.sh", STUB_FETCH_CODE="3", FAIL_MODULE="pipeline.pois.load_pois"
    )
    assert result.returncode != 0
    assert "conflate" in calls
    result, _, _ = shell_pipeline("monthly_overture.sh", STUB_FETCH_CODE="3")
    assert result.returncode == 0, result.stderr
    success = read_status(metadata / "monthly_overture.json")["last_full_success_at"]
    result, calls, _ = shell_pipeline("monthly_overture.sh", STUB_FETCH_CODE="3")
    assert result.returncode == 0
    assert read_status(metadata / "monthly_overture.json")["status"] == "unchanged"
    assert read_status(metadata / "monthly_overture.json")["last_full_success_at"] == success
    assert calls.count("pipeline.pois.conflate") == 2


def test_overture_new_manual_import_is_not_mistaken_for_published(shell_pipeline):
    result, _, metadata = shell_pipeline("monthly_overture.sh")
    assert result.returncode == 0
    save(metadata / "overture.json", {"status": "imported", "sha256": "new-source"})
    result, calls, _ = shell_pipeline("monthly_overture.sh", STUB_FETCH_CODE="3")
    assert result.returncode == 0
    assert calls.count("pipeline.pois.conflate") == 2


def test_overture_fetch_failure_prevents_downstream_work(shell_pipeline):
    result, calls, metadata = shell_pipeline("monthly_overture.sh", STUB_FETCH_CODE="1")
    assert result.returncode == 1
    assert "conflate" not in calls
    assert read_status(metadata / "monthly_overture.json")["status"] == "failed"
