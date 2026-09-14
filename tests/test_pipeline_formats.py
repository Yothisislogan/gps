"""Exercise the formats consumed by the real map tools, before country builds."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def require_tool(name):
    if not shutil.which(name):
        if os.environ.get("NICANAV_REQUIRE_MAP_TOOLS") == "1":
            pytest.fail(f"Required map integration tool is missing: {name}")
        pytest.skip(f"Map integration tool is not installed: {name}")


def environment(tmp_path):
    data = tmp_path / "data"
    return {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
        "NICANAV_DATA_DIR": str(data),
        "NICANAV_TILES_DIR": str(data / "tiles"),
        "NICANAV_METADATA_DIR": str(data / "metadata"),
        "NICANAV_VALHALLA_DIR": str(data / "valhalla"),
        "PLANETILER_JAR": str(data / "tools" / "planetiler.jar"),
        "TIPPECANOE_MAX_THREADS": "2",
    }


def run_script(script, env, *args):
    return subprocess.run(
        ["bash", str(ROOT / "pipeline" / script), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
    )


@pytest.mark.parametrize("valid", [True, False])
def test_osm_download_validation_and_exports_with_real_osmium(tmp_path, valid):
    require_tool("osmium")
    source = tmp_path / "source.osm.pbf"
    xml = tmp_path / "source.osm"
    xml.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="nicanav-test">
  <node id="1" lat="12.136" lon="-86.251" version="1">
    <tag k="amenity" v="cafe"/><tag k="name" v="Test cafe"/>
  </node>
  <node id="2" lat="12.137" lon="-86.252" version="1"/>
  <way id="3" version="1"><nd ref="1"/><nd ref="2"/>
    <tag k="highway" v="residential"/>
  </way>
</osm>
""")
    subprocess.run(
        [
            "osmium",
            "cat",
            str(xml),
            "-o",
            str(source),
            "--output-header=osmosis_replication_timestamp=2026-09-14T00:00:00Z",
        ],
        check=True,
        capture_output=True,
    )
    if not valid:
        source.write_bytes(b"not a PBF")
    digest = hashlib.md5(source.read_bytes(), usedforsecurity=False).hexdigest()
    Path(str(source) + ".md5").write_text(f"{digest}  source.osm.pbf\n")
    env = {**environment(tmp_path), "NICANAV_GEOFABRIK_URL": source.as_uri()}
    result = run_script("fetch_osm.sh", env)
    published = tmp_path / "data" / "osm" / "nicaragua-latest.osm.pbf"
    if not valid:
        assert result.returncode != 0
        assert not published.exists()
        return
    assert result.returncode == 0, result.stderr
    assert published.read_bytes() == source.read_bytes()
    exports = tmp_path / "data" / "exports"
    assert "Test cafe" in (exports / "osm_pois.geojsonseq").read_text()
    assert "LineString" in (exports / "roads.geojsonseq").read_text()
    assert not list((tmp_path / "data" / "osm").glob("*.tmp"))


def test_poi_builder_publishes_real_pmtiles(tmp_path):
    require_tool("tippecanoe")
    require_tool("java")
    exports = tmp_path / "data" / "exports"
    exports.mkdir(parents=True)
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-86.251, 12.136]},
        "properties": {"id": "test-cafe", "name": "Test cafe"},
    }
    (exports / "pois.geojsonseq").write_text(json.dumps(feature) + "\n")
    result = run_script("build_tiles.sh", environment(tmp_path), "--pois-only")
    assert result.returncode == 0, result.stderr
    archive = tmp_path / "data" / "tiles" / "pois.pmtiles"
    assert archive.read_bytes().startswith(b"PMTiles\x03")
    assert not list(archive.parent.glob("*.tmp.pmtiles"))


@pytest.mark.parametrize("checksum_kind", ["bare", "record", "wrong", "malformed"])
def test_planetiler_checksum_download_and_archive_publication(tmp_path, checksum_kind):
    env = environment(tmp_path)
    data = Path(env["NICANAV_DATA_DIR"])
    for name in ("osm", "sources", "tiles"):
        (data / name).mkdir(parents=True)
    (data / "osm" / "nicaragua-latest.osm.pbf").write_bytes(b"fixture")
    (data / "sources" / "water-polygons-split-3857.zip").touch()
    archive = data / "tiles" / "base.pmtiles"
    archive.write_bytes(b"previous published archive")
    payload = b"fixture jar"
    digest = hashlib.sha256(payload).hexdigest()
    checksum = {
        "bare": digest,
        "record": f"{digest}  planetiler.jar",
        "wrong": "0" * 64,
        "malformed": "download failed",
    }[checksum_kind]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(f"""#!{sys.executable}
import sys
from pathlib import Path
out = Path(sys.argv[sys.argv.index('-o') + 1])
out.write_bytes({checksum.encode()!r} if sys.argv[-1].endswith('.sha256') else {payload!r})
""")
    java = bindir / "java"
    java.write_text(f"""#!{sys.executable}
import sys
from pathlib import Path
out = Path(next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--output=')))
# Planetiler chooses the archive writer from the suffix, as tippecanoe does.
if out.suffix != '.pmtiles':
    raise SystemExit('Unrecognized archive format')
out.write_bytes(b'PMTiles fixture archive')
""")
    curl.chmod(0o755)
    java.chmod(0o755)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    result = run_script("build_tiles.sh", env, "--base-only")
    if checksum_kind in ("bare", "record"):
        assert result.returncode == 0, result.stderr
        assert Path(env["PLANETILER_JAR"]).read_bytes() == payload
        assert archive.read_bytes().startswith(b"PMTiles")
        assert not list(archive.parent.glob("*.tmp.pmtiles"))
    else:
        assert result.returncode != 0
        assert not Path(env["PLANETILER_JAR"]).exists()
        assert archive.read_bytes() == b"previous published archive"
