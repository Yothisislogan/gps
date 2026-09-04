#!/usr/bin/env python3
"""Generate the MapLibre styles from one description.

The style is generated rather than hand-written for two reasons: the night
variant is the same layer list with a different palette (maintaining two 900-line
JSON files by hand guarantees they drift), and the POI layer has to stay in step
with docs/taxonomy.csv, which is data.

Schema: OpenMapTiles, as produced by Planetiler — see the layer and attribute
names in docs/SPEC.md and the transportation class values below.

    python3 scripts/build_style.py            # writes both variants
    python3 scripts/build_style.py --check    # verifies they are up to date
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.pois.taxonomy import all_categories  # noqa: E402

# Spanish first, everywhere. This expression appears on every label layer.
NAME_ES = ["coalesce", ["get", "name:es"], ["get", "name"]]

ATTRIBUTION = (
    '<a href="https://www.openstreetmap.org/copyright">© OpenStreetMap</a> · '
    '<a href="https://overturemaps.org">Overture Maps Foundation</a>'
)

FONT_REGULAR = ["Noto Sans Regular"]
FONT_BOLD = ["Noto Sans Bold"]
FONT_ITALIC = ["Noto Sans Italic"]


class Palette:
    """One colour scheme. Night is not "dark day" — it is a different design.

    On a windshield mount after dark the map is the brightest object in the car,
    so the night palette drops overall luminance hard and keeps contrast only
    where it carries meaning: the road you are on, the next turn, the labels.
    """

    def __init__(self, **colours: str) -> None:
        self.__dict__.update(colours)


DAY = Palette(
    background="#f6f3ee",
    water="#a9d3e4",
    water_line="#8fc3d8",
    wood="#d5e3c8",
    grass="#e2ecd6",
    sand="#f2e9d2",
    park="#d9e8cd",
    building="#e4ded3",
    building_outline="#d6cec0",
    motorway="#f2a65a",
    motorway_case="#d98b3c",
    trunk="#f7c873",
    trunk_case="#dda94f",
    primary="#ffffff",
    primary_case="#c9bda9",
    secondary="#ffffff",
    secondary_case="#d3c8b6",
    minor="#ffffff",
    minor_case="#ddd4c4",
    service="#f4f0e8",
    track="#e6dcc6",
    unpaved="#c8a878",
    boundary="#b3a9a0",
    label="#3f3a33",
    label_halo="#ffffffcc",
    water_label="#3d6f88",
    poi="#5b544a",
    poi_halo="#ffffffdd",
    verified="#2f7d4f",
)

NIGHT = Palette(
    background="#141821",
    water="#0f2233",
    water_line="#16344b",
    wood="#172019",
    grass="#181f1a",
    sand="#221f18",
    park="#16211a",
    building="#1b2029",
    building_outline="#222836",
    motorway="#7a5a2c",
    motorway_case="#4d3a1d",
    trunk="#6d5730",
    trunk_case="#463824",
    primary="#3a4150",
    primary_case="#242a35",
    secondary="#333a47",
    secondary_case="#222833",
    minor="#2b313c",
    minor_case="#1e232c",
    service="#242a33",
    track="#2a2b24",
    unpaved="#5a4a30",
    boundary="#3a4250",
    label="#d7dbe2",
    label_halo="#0b0e14cc",
    water_label="#7fa8c4",
    poi="#c3c8d1",
    poi_halo="#0b0e14dd",
    verified="#4fbf83",
)

#: OpenMapTiles `transportation.class` values, in draw order (lowest first) with
#: the palette key, the minimum zoom and the width stops each uses.
ROAD_CLASSES: list[dict[str, Any]] = [
    {
        "cls": "track",
        "colour": "track",
        "case": None,
        "minzoom": 13,
        "widths": [[13, 0.6], [15, 1.4], [18, 4]],
    },
    {
        "cls": "service",
        "colour": "service",
        "case": "minor_case",
        "minzoom": 13,
        "widths": [[13, 0.8], [15, 2], [18, 8]],
    },
    {
        "cls": "minor",
        "colour": "minor",
        "case": "minor_case",
        "minzoom": 12,
        "widths": [[12, 1], [14, 2.5], [16, 6], [18, 14]],
    },
    {
        "cls": "tertiary",
        "colour": "secondary",
        "case": "secondary_case",
        "minzoom": 11,
        "widths": [[11, 1.2], [14, 3.5], [16, 8], [18, 18]],
    },
    {
        "cls": "secondary",
        "colour": "secondary",
        "case": "secondary_case",
        "minzoom": 9,
        "widths": [[9, 1.2], [13, 3.5], [16, 10], [18, 22]],
    },
    {
        "cls": "primary",
        "colour": "primary",
        "case": "primary_case",
        "minzoom": 7,
        "widths": [[7, 1], [12, 3.5], [16, 12], [18, 26]],
    },
    {
        "cls": "trunk",
        "colour": "trunk",
        "case": "trunk_case",
        "minzoom": 6,
        "widths": [[6, 1.2], [12, 4], [16, 14], [18, 28]],
    },
    {
        "cls": "motorway",
        "colour": "motorway",
        "case": "motorway_case",
        "minzoom": 4,
        "widths": [[4, 0.8], [12, 4.5], [16, 16], [18, 32]],
    },
]


def interpolate(stops: list[list[float]], base: float = 1.4) -> list[Any]:
    """MapLibre exponential interpolation over zoom."""
    expression: list[Any] = ["interpolate", ["exponential", base], ["zoom"]]
    for zoom, value in stops:
        expression.extend([zoom, value])
    return expression


def road_layers(palette: Palette) -> list[dict[str, Any]]:
    """Casing + fill per road class, plus the unpaved overlay.

    Unpaved roads get a dashed overlay in an earth tone. Knowing a road is dirt
    *before* turning onto it is a real Nicaraguan need — half the network is
    unpaved, a rental car on a cauce road is a genuinely bad afternoon, and no
    other map here shows it.
    """
    layers: list[dict[str, Any]] = []

    for spec in ROAD_CLASSES:
        base_filter = ["==", ["get", "class"], spec["cls"]]
        if spec["case"]:
            layers.append(
                {
                    "id": f"road-{spec['cls']}-case",
                    "type": "line",
                    "source": "base",
                    "source-layer": "transportation",
                    "minzoom": spec["minzoom"],
                    "filter": ["all", base_filter, ["!=", ["get", "brunnel"], "tunnel"]],
                    "layout": {"line-cap": "round", "line-join": "round"},
                    "paint": {
                        "line-color": getattr(palette, spec["case"]),
                        "line-width": interpolate([[z, w + 2] for z, w in spec["widths"]]),
                    },
                }
            )
        layers.append(
            {
                "id": f"road-{spec['cls']}",
                "type": "line",
                "source": "base",
                "source-layer": "transportation",
                "minzoom": spec["minzoom"],
                "filter": base_filter,
                "layout": {"line-cap": "round", "line-join": "round"},
                "paint": {
                    "line-color": getattr(palette, spec["colour"]),
                    "line-width": interpolate(spec["widths"]),
                    # Tunnels read as ghosts rather than disappearing.
                    "line-opacity": [
                        "case",
                        ["==", ["get", "brunnel"], "tunnel"],
                        0.45,
                        1.0,
                    ],
                },
            }
        )

    layers.append(
        {
            "id": "road-unpaved",
            "type": "line",
            "source": "base",
            "source-layer": "transportation",
            "minzoom": 11,
            "filter": ["all", ["==", ["get", "surface"], "unpaved"]],
            "layout": {"line-cap": "butt", "line-join": "round"},
            "paint": {
                "line-color": palette.unpaved,
                "line-width": interpolate([[11, 1], [14, 2.5], [18, 8]]),
                "line-dasharray": [1.5, 1.5],
                "line-opacity": 0.9,
            },
        }
    )
    return layers


def poi_layers(palette: Palette) -> list[dict[str, Any]]:
    """The project's own POI layer, keyed on the taxonomy.

    ``symbol-sort-key`` uses the rank the exporter computed, so when labels
    collide the verified place with a phone number wins over a bare name.
    """
    categories = all_categories()
    min_zoom = min(category.min_zoom for category in categories)
    return [
        {
            "id": "poi-icon",
            "type": "symbol",
            "source": "pois",
            "source-layer": "poi",
            "minzoom": min_zoom,
            "layout": {
                "icon-image": ["concat", "nicanav-", ["get", "category"]],
                "icon-size": interpolate([[12, 0.7], [16, 1.0], [19, 1.2]]),
                "icon-allow-overlap": False,
                "symbol-sort-key": ["to-number", ["get", "rank"], 5],
                "text-field": NAME_ES,
                "text-font": FONT_REGULAR,
                "text-size": interpolate([[13, 10], [16, 12], [19, 14]]),
                "text-anchor": "top",
                "text-offset": [0, 1.1],
                "text-max-width": 8,
                "text-optional": True,
            },
            "paint": {
                "icon-color": [
                    "case",
                    ["==", ["get", "verified"], True],
                    palette.verified,
                    palette.poi,
                ],
                "text-color": palette.poi,
                "text-halo-color": palette.poi_halo,
                "text-halo-width": 1.4,
            },
        }
    ]


def build_style(palette: Palette, *, name: str, tiles_base: str = "/tiles") -> dict[str, Any]:
    """The whole style document."""
    return {
        "version": 8,
        "name": name,
        "metadata": {
            "nicanav:generator": "scripts/build_style.py",
            "nicanav:schema": "openmaptiles",
        },
        # `url:` and not `tiles:` — with a tile template MapLibre never reads the
        # archive's maxzoom, assumes 22, requests zooms that do not exist, and
        # high zoom goes blank with no error and no overzoom.
        "sources": {
            "base": {
                "type": "vector",
                "url": f"pmtiles://{tiles_base}/base.pmtiles",
                "attribution": ATTRIBUTION,
            },
            "pois": {"type": "vector", "url": f"pmtiles://{tiles_base}/pois.pmtiles"},
        },
        "glyphs": "/fonts/{fontstack}/{range}.pbf",
        "sprite": "/sprites/nicanav",
        "light": {"anchor": "viewport", "color": "#ffffff", "intensity": 0.3},
        "layers": [
            {
                "id": "background",
                "type": "background",
                "paint": {"background-color": palette.background},
            },
            {
                "id": "landcover-wood",
                "type": "fill",
                "source": "base",
                "source-layer": "landcover",
                "minzoom": 7,
                "filter": ["in", ["get", "class"], ["literal", ["wood", "forest"]]],
                "paint": {"fill-color": palette.wood, "fill-opacity": 0.7},
            },
            {
                "id": "landcover-grass",
                "type": "fill",
                "source": "base",
                "source-layer": "landcover",
                "minzoom": 8,
                "filter": ["in", ["get", "class"], ["literal", ["grass", "farmland"]]],
                "paint": {"fill-color": palette.grass, "fill-opacity": 0.6},
            },
            {
                "id": "landcover-sand",
                "type": "fill",
                "source": "base",
                "source-layer": "landcover",
                "minzoom": 9,
                "filter": ["==", ["get", "class"], "sand"],
                "paint": {"fill-color": palette.sand},
            },
            {
                "id": "park",
                "type": "fill",
                "source": "base",
                "source-layer": "park",
                "minzoom": 9,
                "paint": {"fill-color": palette.park, "fill-opacity": 0.6},
            },
            {
                "id": "water",
                "type": "fill",
                "source": "base",
                "source-layer": "water",
                "paint": {"fill-color": palette.water},
            },
            {
                "id": "waterway",
                "type": "line",
                "source": "base",
                "source-layer": "waterway",
                "minzoom": 9,
                "paint": {
                    "line-color": palette.water_line,
                    "line-width": interpolate([[9, 0.5], [14, 1.5], [18, 5]]),
                },
            },
            {
                "id": "building",
                "type": "fill",
                "source": "base",
                "source-layer": "building",
                # Buildings from z15: below that they are noise, and Managua's
                # coverage is patchy enough that showing them earlier would
                # advertise the gaps rather than the city.
                "minzoom": 15,
                "paint": {
                    "fill-color": palette.building,
                    "fill-outline-color": palette.building_outline,
                    "fill-opacity": interpolate([[15, 0.4], [16.5, 0.9]], base=1.0),
                },
            },
            *road_layers(palette),
            {
                "id": "road-oneway",
                "type": "symbol",
                "source": "base",
                "source-layer": "transportation",
                # One-way arrows are the difference between a legible city grid
                # and a guess, and Managua is full of one-way pairs.
                "minzoom": 15,
                "filter": ["in", ["get", "oneway"], ["literal", [1, -1]]],
                "layout": {
                    "symbol-placement": "line",
                    "symbol-spacing": 180,
                    "icon-image": "nicanav-oneway",
                    "icon-size": 0.7,
                    "icon-rotate": ["case", ["==", ["get", "oneway"], -1], 180, 0],
                    "icon-rotation-alignment": "map",
                    "icon-allow-overlap": False,
                },
                "paint": {"icon-opacity": 0.6},
            },
            {
                "id": "boundary-country",
                "type": "line",
                "source": "base",
                "source-layer": "boundary",
                "filter": ["<=", ["get", "admin_level"], 2],
                "paint": {
                    "line-color": palette.boundary,
                    "line-width": interpolate([[4, 0.6], [10, 1.6]]),
                    "line-dasharray": [3, 2],
                },
            },
            {
                "id": "road-label",
                "type": "symbol",
                "source": "base",
                "source-layer": "transportation_name",
                "minzoom": 13,
                "layout": {
                    "symbol-placement": "line",
                    "text-field": NAME_ES,
                    "text-font": FONT_REGULAR,
                    "text-size": interpolate([[13, 10], [18, 14]]),
                    "text-max-angle": 30,
                },
                "paint": {
                    "text-color": palette.label,
                    "text-halo-color": palette.label_halo,
                    "text-halo-width": 1.4,
                },
            },
            {
                "id": "place-neighbourhood",
                "type": "symbol",
                "source": "base",
                "source-layer": "place",
                # Barrios, repartos and residenciales ARE the address system
                # here, so they are labelled early and prominently.
                "minzoom": 13,
                "filter": [
                    "in",
                    ["get", "class"],
                    ["literal", ["neighbourhood", "suburb", "quarter"]],
                ],
                "layout": {
                    "text-field": NAME_ES,
                    "text-font": FONT_ITALIC,
                    "text-size": interpolate([[13, 11], [17, 14]]),
                    "text-transform": "uppercase",
                    "text-letter-spacing": 0.08,
                    "text-max-width": 8,
                },
                "paint": {
                    "text-color": palette.label,
                    "text-halo-color": palette.label_halo,
                    "text-halo-width": 1.6,
                    "text-opacity": 0.85,
                },
            },
            {
                "id": "place-town",
                "type": "symbol",
                "source": "base",
                "source-layer": "place",
                "minzoom": 8,
                "filter": ["in", ["get", "class"], ["literal", ["town", "village"]]],
                "layout": {
                    "text-field": NAME_ES,
                    "text-font": FONT_REGULAR,
                    "text-size": interpolate([[8, 11], [14, 15]]),
                    "text-max-width": 8,
                },
                "paint": {
                    "text-color": palette.label,
                    "text-halo-color": palette.label_halo,
                    "text-halo-width": 1.6,
                },
            },
            {
                "id": "place-city",
                "type": "symbol",
                "source": "base",
                "source-layer": "place",
                "minzoom": 5,
                "filter": ["in", ["get", "class"], ["literal", ["city", "country"]]],
                "layout": {
                    "text-field": NAME_ES,
                    "text-font": FONT_BOLD,
                    "text-size": interpolate([[5, 12], [12, 20]]),
                    "text-max-width": 9,
                },
                "paint": {
                    "text-color": palette.label,
                    "text-halo-color": palette.label_halo,
                    "text-halo-width": 1.8,
                },
            },
            {
                "id": "water-label",
                "type": "symbol",
                "source": "base",
                "source-layer": "water_name",
                "minzoom": 6,
                "layout": {
                    "text-field": NAME_ES,
                    "text-font": FONT_ITALIC,
                    "text-size": interpolate([[6, 11], [14, 16]]),
                    "text-max-width": 8,
                },
                "paint": {
                    "text-color": palette.water_label,
                    "text-halo-color": palette.label_halo,
                    "text-halo-width": 1.2,
                },
            },
            *poi_layers(palette),
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "web" / "style")
    parser.add_argument("--tiles-base", default="/tiles")
    parser.add_argument("--check", action="store_true", help="verify the committed files match")
    args = parser.parse_args(argv)

    variants = {
        "nicanav.json": build_style(DAY, name="nicanav", tiles_base=args.tiles_base),
        "nicanav-night.json": build_style(NIGHT, name="nicanav night", tiles_base=args.tiles_base),
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stale = []
    for filename, style in variants.items():
        path = args.out_dir / filename
        rendered = json.dumps(style, indent=2, ensure_ascii=False) + "\n"
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != rendered:
                stale.append(filename)
            continue
        path.write_text(rendered, encoding="utf-8")
        print(f"wrote {path} ({len(style['layers'])} layers)")

    if args.check:
        if stale:
            print(f"stale: {', '.join(stale)} — run scripts/build_style.py", file=sys.stderr)
            return 1
        print("styles are up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
