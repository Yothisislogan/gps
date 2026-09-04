"""Static checks on the PWA.

There is no bundler and no browser in CI, so nothing catches a renamed export, a
missing translation key or an icon the sprite sheet does not contain — until a
user taps it. These tests are the substitute: they parse the source and assert
the cross-references hold.

They are deliberately structural. Nothing here claims the app *works*; that
takes a real browser on a real phone, and the runbook says so.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "web"
JS_FILES = sorted(WEB.rglob("*.js"))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def exported_names(source: str) -> set[str]:
    names = set(
        re.findall(r"^export\s+(?:async\s+)?(?:function|const|class|let)\s+(\w+)", source, re.M)
    )
    for match in re.finditer(r"^export\s*\{([^}]*)\}", source, re.M):
        for part in match.group(1).split(","):
            cleaned = part.strip().split(" as ")[-1].strip()
            if cleaned:
                names.add(cleaned)
    return names


@pytest.fixture(scope="module")
def exports() -> dict[str, set[str]]:
    return {path.name: exported_names(read(path)) for path in JS_FILES}


@pytest.fixture(scope="module")
def i18n_keys() -> set[str]:
    source = read(WEB / "js" / "ui.js")
    return set(re.findall(r"^\s{2,6}'([a-zA-Z][\w.]*)':", source, re.M))


class TestModuleGraph:
    def test_every_import_resolves(self, exports: dict[str, set[str]]):
        problems: list[str] = []
        for path in JS_FILES:
            source = read(path)
            for match in re.finditer(
                r"import\s+(?:\*\s+as\s+\w+|\{([^}]*)\})\s+from\s+'\./([\w.]+)'", source
            ):
                target = match.group(2)
                if not (WEB / "js" / target).exists():
                    problems.append(f"{path.name} imports missing module {target}")
                    continue
                for name in (match.group(1) or "").split(","):
                    cleaned = name.strip().split(" as ")[0].strip()
                    if cleaned and cleaned not in exports.get(target, set()):
                        problems.append(f"{path.name}: {target} does not export {cleaned}")
        assert not problems, "\n".join(problems)

    def test_no_bundler_only_imports(self):
        # The app is served as plain files. A bare specifier would need a
        # bundler or an import map, and there is neither.
        for path in JS_FILES:
            for match in re.finditer(r"from\s+'([^']+)'", read(path)):
                specifier = match.group(1)
                assert specifier.startswith("./") or specifier.startswith("http"), (
                    f"{path.name} imports the bare specifier {specifier!r}, "
                    "which no browser can resolve without a bundler"
                )

    def test_entry_point_is_loaded_by_the_shell(self):
        html = read(WEB / "index.html")
        assert 'src="/js/map.js"' in html
        assert 'type="module"' in html


class TestShellContract:
    """Every id the modules bind to must exist in index.html."""

    def test_dom_ids_exist(self):
        html = read(WEB / "index.html")
        present = set(re.findall(r'id="([\w-]+)"', html))
        wanted: set[str] = set()
        for path in JS_FILES:
            wanted |= set(re.findall(r"getElementById\('([\w-]+)'\)", read(path)))
        # sw.js runs without a document.
        missing = sorted(wanted - present)
        assert not missing, f"index.html is missing: {', '.join(missing)}"

    def test_sheet_markup_matches_what_ui_queries(self):
        html = read(WEB / "index.html")
        for selector in ("sheet-body", "sheet-title", "sheet-close", "sheet-grip"):
            assert selector in html, f"ui.js queries .{selector} and the shell has no such node"

    def test_lang_is_spanish(self):
        assert '<html lang="es">' in read(WEB / "index.html")

    def test_viewport_covers_the_notch(self):
        # Used on a dash mount, in landscape, on phones with cutouts.
        assert "viewport-fit=cover" in read(WEB / "index.html")

    def test_attribution_is_present_in_the_shell(self):
        html = read(WEB / "index.html")
        assert "OpenStreetMap" in html, "ODbL requires visible attribution"
        assert "Overture" in html


class TestTranslations:
    def test_every_key_used_exists(self, i18n_keys: set[str]):
        problems: list[str] = []
        for path in JS_FILES:
            if path.name == "ui.js":
                continue
            for match in re.finditer(r"\bt\('([\w.]+)'", read(path)):
                if match.group(1) not in i18n_keys:
                    problems.append(f"{path.name}: t('{match.group(1)}') is not defined")
        assert not problems, "\n".join(problems)

    def test_the_table_is_not_trivial(self, i18n_keys: set[str]):
        assert len(i18n_keys) > 100


class TestStyle:
    @pytest.fixture(scope="class")
    def style(self) -> dict:
        return json.loads(read(WEB / "style" / "nicanav.json"))

    def test_is_a_v8_style(self, style: dict):
        assert style["version"] == 8

    def test_layer_ids_are_unique(self, style: dict):
        ids = [layer["id"] for layer in style["layers"]]
        assert len(ids) == len(set(ids))

    def test_sources_use_the_pmtiles_protocol(self, style: dict):
        for name, source in style["sources"].items():
            # `url:` and not `tiles:`: with a tile template MapLibre never learns
            # the archive's maxzoom and high zoom silently goes blank.
            assert "url" in source, f"{name} must use url:, not a tiles template"
            assert source["url"].startswith("pmtiles://"), name

    def test_every_label_prefers_spanish(self, style: dict):
        for layer in style["layers"]:
            field = (layer.get("layout") or {}).get("text-field")
            if not field:
                continue
            if layer["id"].startswith("poi"):
                continue  # the POI layer's own name field
            assert field[0] == "coalesce", f"{layer['id']} does not prefer name:es"
            assert field[1] == ["get", "name:es"], layer["id"]

    def test_zooms_are_sane(self, style: dict):
        for layer in style["layers"]:
            assert 0 <= layer.get("minzoom", 0) <= 22, layer["id"]
            assert 0 <= layer.get("maxzoom", 22) <= 22, layer["id"]

    def test_unpaved_roads_are_drawn_differently(self, style: dict):
        # Knowing a road is dirt before turning onto it is a real Nicaraguan
        # need, and it is the thing other maps here do not show.
        unpaved = [layer for layer in style["layers"] if "unpaved" in layer["id"]]
        assert unpaved, "no layer distinguishes unpaved roads"
        assert unpaved[0]["paint"].get("line-dasharray")

    def test_night_variant_has_the_same_layers(self, style: dict):
        night = json.loads(read(WEB / "style" / "nicanav-night.json"))
        assert [layer["id"] for layer in night["layers"]] == [
            layer["id"] for layer in style["layers"]
        ]
        assert (
            night["layers"][0]["paint"]["background-color"]
            != style["layers"][0]["paint"]["background-color"]
        )

    def test_styles_are_up_to_date_with_the_generator(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "scripts/build_style.py", "--check"],
            cwd=WEB.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


class TestSprites:
    @pytest.fixture(scope="class")
    def sprite_index(self) -> dict:
        return json.loads(read(WEB / "sprites" / "nicanav.json"))

    def test_every_category_has_an_icon(self, sprite_index: dict):
        from pipeline.pois.taxonomy import all_categories

        missing = [
            category.category_id
            for category in all_categories()
            if f"nicanav-{category.category_id}" not in sprite_index
        ]
        assert not missing, f"no sprite for: {', '.join(missing)}"

    def test_icons_are_sdf_so_they_can_be_recoloured(self, sprite_index: dict):
        assert all(entry["sdf"] for entry in sprite_index.values())

    def test_the_style_can_resolve_its_icon_expression(self, sprite_index: dict):
        from pipeline.pois.taxonomy import all_categories

        style = json.loads(read(WEB / "style" / "nicanav.json"))
        poi_layer = next(layer for layer in style["layers"] if layer["id"] == "poi-icon")
        expression = poi_layer["layout"]["icon-image"]
        assert expression[0] == "concat" and expression[1] == "nicanav-"
        for category in all_categories():
            assert f"nicanav-{category.category_id}" in sprite_index

    def test_the_png_is_a_real_png(self):
        raw = (WEB / "sprites" / "nicanav.png").read_bytes()
        assert raw[:8] == b"\x89PNG\r\n\x1a\n"
        import struct

        width, height = struct.unpack(">II", raw[16:24])
        assert width > 0 and height > 0

    def test_oneway_arrow_exists(self, sprite_index: dict):
        style = json.loads(read(WEB / "style" / "nicanav.json"))
        arrows = next(layer for layer in style["layers"] if layer["id"] == "road-oneway")
        assert arrows["layout"]["icon-image"] in sprite_index


class TestManifest:
    @pytest.fixture(scope="class")
    def manifest(self) -> dict:
        return json.loads(read(WEB / "manifest.webmanifest"))

    def test_is_installable(self, manifest: dict):
        assert manifest["display"] == "standalone"
        assert manifest["start_url"]
        assert any(icon["sizes"] == "512x512" for icon in manifest["icons"])

    def test_is_spanish(self, manifest: dict):
        assert manifest["lang"].startswith("es")

    def test_shortcuts_point_at_real_categories(self, manifest: dict):
        from pipeline.pois.taxonomy import load_taxonomy

        taxonomy = load_taxonomy()
        for shortcut in manifest.get("shortcuts", []):
            match = re.search(r"chip=([\w]+)", shortcut["url"])
            assert match, shortcut["url"]
            assert match.group(1) in taxonomy, f"{match.group(1)} is not a category"


class TestServiceWorker:
    def test_never_caches_the_api(self):
        source = read(WEB / "sw.js")
        # A cached route or a cached "abierto ahora" is worse than an error:
        # the driver acts on it.
        assert "/api/" in source
        assert re.search(r"pathname\.startsWith\('/api/'\)\)\s*return", source)

    def test_does_not_cache_tile_ranges(self):
        source = read(WEB / "sw.js")
        assert "/tiles/" in source, "range requests cannot be cached; offline.js owns the archive"

    def test_precaches_the_shell(self):
        source = read(WEB / "sw.js")
        for asset in ("/index.html", "/css/app.css", "/js/map.js", "/style/nicanav.json"):
            assert asset in source, f"{asset} is missing from the precache list"

    def test_precache_list_points_at_files_that_exist(self):
        source = read(WEB / "sw.js")
        shell = re.search(r"const SHELL = \[(.*?)\];", source, re.S)
        assert shell
        # config.js is generated at deploy time by scripts/render_web_config.py
        # and is deliberately not in the repository.
        generated = {"/", "/config.js"}
        for entry in re.findall(r"'([^']+)'", shell.group(1)):
            if entry in generated:
                continue
            assert (WEB / entry.lstrip("/")).exists(), f"precached {entry} does not exist"
