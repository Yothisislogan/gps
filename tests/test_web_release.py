"""Exercise the release packager with actual files and runtime configuration."""

import gzip
import json
import os
import subprocess
from pathlib import Path

from common.config import Settings
from scripts import render_web_config

BUILDER = Path(__file__).resolve().parents[1] / "scripts/build_web.mjs"


def test_packaged_config_and_style_resolve_to_the_same_generation(tmp_path, monkeypatch):
    web = tmp_path / "web"
    (web / "style").mkdir(parents=True)
    (web / "js").mkdir()
    (web / "style/nicanav.json").write_text(
        json.dumps({"sources": {"base": {"url": "pmtiles:///tiles/base.pmtiles"}}})
    )
    (web / "js/map.js").write_text("export const ok = true;")
    (web / "index.html").write_text('<script src="/js/map.js"></script>')
    (web / "manifest.webmanifest").write_text("{}")
    (web / "sw.js").write_text("const VERSION = 'dev'; const SHELL = ['/js/map.js'];")
    monkeypatch.setattr(
        render_web_config, "get_settings", lambda: Settings(_env_file=None, release_id="candidate")
    )
    config = render_web_config.build_config()
    (web / "config.js").write_text("window.NICANAV_CONFIG = " + json.dumps(config) + ";")
    env = {**os.environ, "NICANAV_RELEASE_ID": "candidate"}
    subprocess.run(["node", str(BUILDER)], cwd=tmp_path, env=env, check=True, capture_output=True)
    output = tmp_path / "dist/web"
    version = json.loads((output / "release.json").read_text())["version"]
    result = json.loads((output / "config.js").read_text().split(" = ", 1)[1].rstrip(";"))
    assert result["apiBase"] == "/data-releases/candidate/api"
    style = output / result["styleUrl"].lstrip("/")
    assert style.exists()
    assert (
        json.loads(style.read_text())["sources"]["base"]["url"]
        == "pmtiles:///data-releases/candidate/tiles/base.pmtiles"
    )
    assert gzip.decompress(Path(str(style) + ".gz").read_bytes()) == style.read_bytes()
    assert f"/releases/{version}/js/map.js" in (output / "sw.js").read_text()
    (web / "js/map.js").write_text("export const ok = false;")
    subprocess.run(["node", str(BUILDER)], cwd=tmp_path, env=env, check=True, capture_output=True)
    assert json.loads((output / "release.json").read_text())["version"] != version
    assert (output / "releases" / version / "js/map.js").read_text() == "export const ok = true;"
