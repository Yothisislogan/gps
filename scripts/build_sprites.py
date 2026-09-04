#!/usr/bin/env python3
"""Generate the MapLibre sprite sheet from the taxonomy.

No third-party dependencies: the pipeline image stays slim and there is nothing
to install, so this rasterises its own glyphs and writes its own PNG with
``zlib`` and ``struct``.

The glyphs are **signed distance fields**, which buys three things that matter
more here than pictorial detail: MapLibre can recolour them at runtime
(``icon-color``, used to mark verified places), they stay crisp at any zoom, and
the distance can be computed analytically from primitives rather than by
rasterising and blurring.

They are deliberately simple geometric marks — a pump, a tyre, a fork and knife,
a cross, a bed — not detailed pictograms. That is a stated limitation, not an
oversight: hand-drawn SVGs by a designer would be better, and
``web/icons/*.svg`` is written alongside the sheet so they can be replaced
without touching this script.

    python3 scripts/build_sprites.py
    python3 scripts/build_sprites.py --check   # every category has an icon
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.pois.taxonomy import all_categories  # noqa: E402

ICON_SIZE = 24
#: Distance-field range in pixels. MapLibre's own sprites use 3; a slightly
#: wider field survives the 0.7x downscale the style applies at low zoom.
SDF_RANGE = 4.0
PADDING = 2


# --------------------------------------------------------------------------- #
# Signed-distance primitives.  Positive inside, negative outside, in pixels.
# --------------------------------------------------------------------------- #


def sd_circle(px: float, py: float, cx: float, cy: float, r: float) -> float:
    return r - math.hypot(px - cx, py - cy)


def sd_ring(px: float, py: float, cx: float, cy: float, r: float, thickness: float) -> float:
    return thickness / 2 - abs(math.hypot(px - cx, py - cy) - r)


def sd_box(
    px: float, py: float, cx: float, cy: float, half_w: float, half_h: float, radius: float = 0.0
) -> float:
    dx = abs(px - cx) - (half_w - radius)
    dy = abs(py - cy) - (half_h - radius)
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0))
    inside = min(max(dx, dy), 0.0)
    return radius - (outside + inside)


def sd_segment(
    px: float, py: float, x0: float, y0: float, x1: float, y1: float, thickness: float
) -> float:
    vx, vy = x1 - x0, y1 - y0
    wx, wy = px - x0, py - y0
    length_sq = vx * vx + vy * vy
    t = 0.0 if length_sq == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / length_sq))
    return thickness / 2 - math.hypot(px - (x0 + t * vx), py - (y0 + t * vy))


def sd_triangle(
    px: float, py: float, cx: float, cy: float, size: float, thickness: float = 0.0
) -> float:
    """An upward triangle, filled (thickness 0) or outlined."""
    points = [
        (cx, cy - size),
        (cx - size * 0.9, cy + size * 0.75),
        (cx + size * 0.9, cy + size * 0.75),
    ]
    edges = [
        sd_segment(px, py, *points[i], *points[(i + 1) % 3], thickness or 2.0) for i in range(3)
    ]
    outline = max(edges)
    if thickness:
        return outline
    # Filled: inside if on the same side of every edge.
    inside = True
    for i in range(3):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % 3]
        if (x1 - x0) * (py - y0) - (y1 - y0) * (px - x0) > 0:
            inside = False
            break
    return abs(outline) if inside else outline


Shape = list[tuple[str, tuple[float, ...]]]

C = ICON_SIZE / 2  # centre


def _glyphs() -> dict[str, Shape]:
    """Every glyph as a union of primitives, in a 24x24 box."""
    return {
        # --- food and drink ---
        "restaurante": [
            ("seg", (8, 5, 8, 19, 1.8)),
            ("seg", (6, 5, 6, 10, 1.4)),
            ("seg", (10, 5, 10, 10, 1.4)),
            ("seg", (16, 5, 16, 19, 1.8)),
            ("box", (16, 8, 2.2, 3.5, 1.5)),
        ],
        "fritanga": [
            ("seg", (5, 15, 19, 15, 2.0)),
            ("circle", (9, 11, 2.2)),
            ("circle", (14, 11, 2.2)),
            ("seg", (5, 18, 19, 18, 1.2)),
        ],
        "comedor": [("box", (12, 12, 6.5, 4.5, 1.5)), ("seg", (7, 8, 17, 8, 1.6))],
        "buffet": [
            ("box", (12, 13, 7, 3.5, 1.2)),
            ("circle", (8.5, 9, 1.6)),
            ("circle", (12, 9, 1.6)),
            ("circle", (15.5, 9, 1.6)),
        ],
        "cafetin": [
            ("box", (11, 12, 4.5, 4.5, 1.2)),
            ("ring", (16.5, 11.5, 2.4, 1.4)),
            ("seg", (6, 18, 18, 18, 1.6)),
        ],
        "cafe": [
            ("box", (11, 12, 4.5, 4.5, 1.2)),
            ("ring", (16.5, 11.5, 2.4, 1.4)),
            ("seg", (6, 18, 18, 18, 1.6)),
        ],
        "bar": [
            ("seg", (6, 6, 18, 6, 1.8)),
            ("seg", (6, 6, 12, 13, 1.6)),
            ("seg", (18, 6, 12, 13, 1.6)),
            ("seg", (12, 13, 12, 18, 1.6)),
            ("seg", (8, 18, 16, 18, 1.6)),
        ],
        "discoteca": [
            ("circle", (9, 16, 2.6)),
            ("seg", (11, 16, 11, 6, 1.6)),
            ("seg", (11, 6, 18, 8, 1.6)),
        ],
        "heladeria": [("circle", (12, 9, 4.0)), ("tri", (12, 16, 4.0, 0))],
        "panaderia": [("box", (12, 12, 7, 4.0, 3.0)), ("seg", (8, 12, 16, 12, 1.2))],
        "reposteria": [
            ("box", (12, 15, 6.5, 3.0, 1.0)),
            ("box", (12, 10.5, 5, 2.0, 0.8)),
            ("seg", (12, 5, 12, 8, 1.2)),
        ],
        "pizzeria": [
            ("tri", (12, 13, 7.5, 2.0)),
            ("circle", (11, 12, 1.2)),
            ("circle", (14, 15, 1.2)),
        ],
        "comida_rapida": [
            ("box", (12, 14, 7, 2.6, 1.2)),
            ("box", (12, 10, 6, 2.2, 2.0)),
            ("seg", (6, 17, 18, 17, 1.4)),
        ],
        # --- shops ---
        "pulperia": [
            ("box", (12, 14, 6.5, 4.5, 1.0)),
            ("seg", (7, 9.5, 17, 9.5, 1.6)),
            ("seg", (9, 9.5, 9, 6, 1.2)),
            ("seg", (15, 9.5, 15, 6, 1.2)),
        ],
        "supermercado": [
            ("box", (12, 13, 6, 4, 1.0)),
            ("circle", (9, 19, 1.5)),
            ("circle", (15, 19, 1.5)),
            ("seg", (5, 7, 8, 9, 1.4)),
        ],
        "mercado": [
            ("seg", (4, 10, 20, 10, 2.0)),
            ("box", (12, 15, 7, 4.5, 1.0)),
            ("seg", (6, 10, 6, 6, 1.2)),
            ("seg", (18, 10, 18, 6, 1.2)),
        ],
        "tienda": [("box", (12, 14, 6.5, 4.5, 1.0)), ("seg", (7, 9.5, 17, 9.5, 1.6))],
        "ferreteria": [
            ("seg", (7, 17, 15, 9, 2.2)),
            ("box", (16, 8, 2.6, 2.6, 0.8)),
            ("seg", (6, 8, 10, 8, 1.6)),
        ],
        "distribuidora": [
            ("box", (11, 14, 6, 3.5, 0.6)),
            ("box", (17, 13, 2.6, 2.5, 0.6)),
            ("circle", (9, 18.5, 1.6)),
            ("circle", (16, 18.5, 1.6)),
        ],
        "centro_comercial": [
            ("box", (12, 15, 7.5, 4.0, 0.8)),
            ("seg", (4, 10, 12, 6, 1.6)),
            ("seg", (20, 10, 12, 6, 1.6)),
        ],
        "libreria": [
            ("seg", (8, 6, 8, 18, 1.8)),
            ("seg", (12, 6, 12, 18, 1.8)),
            ("seg", (16, 7, 16, 18, 1.8)),
        ],
        # --- auto ---
        "gasolinera": [
            ("box", (10, 13, 4.2, 6.0, 0.8)),
            ("seg", (15, 9, 18, 9, 1.4)),
            ("seg", (18, 9, 18, 15, 1.4)),
            ("circle", (18, 16.5, 1.4)),
            ("seg", (6, 19, 14, 19, 1.6)),
        ],
        "vulcanizacion": [("ring", (12, 12, 7.0, 2.6)), ("ring", (12, 12, 2.6, 1.6))],
        "taller_mecanico": [
            ("seg", (7, 17, 14, 10, 2.4)),
            ("circle", (16, 8, 3.2)),
            ("circle", (17.5, 6.5, 1.6)),
        ],
        "lavado_autos": [
            ("box", (12, 15, 6.5, 3.0, 1.2)),
            ("circle", (8, 8, 1.2)),
            ("circle", (12, 6.5, 1.2)),
            ("circle", (16, 8, 1.2)),
        ],
        "repuestos": [("ring", (12, 12, 5.0, 2.2)), ("circle", (12, 12, 1.8))],
        "parqueo": [("seg", (9, 6, 9, 18, 2.2)), ("ring", (13, 9.5, 3.0, 2.2))],
        # --- money ---
        "banco": [
            ("seg", (5, 11, 19, 11, 1.8)),
            ("tri", (12, 8, 5.0, 1.6)),
            ("seg", (8, 12, 8, 17, 1.6)),
            ("seg", (12, 12, 12, 17, 1.6)),
            ("seg", (16, 12, 16, 17, 1.6)),
            ("seg", (5, 18.5, 19, 18.5, 1.8)),
        ],
        "cajero": [
            ("box", (12, 12, 6.5, 5.5, 1.2)),
            ("seg", (8, 10, 16, 10, 1.4)),
            ("seg", (9, 15, 13, 15, 1.4)),
        ],
        "casa_de_cambio": [
            ("seg", (6, 9, 16, 9, 1.6)),
            ("seg", (13, 6, 16, 9, 1.6)),
            ("seg", (18, 15, 8, 15, 1.6)),
            ("seg", (11, 18, 8, 15, 1.6)),
        ],
        "remesas": [
            ("box", (12, 12, 7, 4.5, 0.8)),
            ("seg", (5, 9, 12, 14, 1.4)),
            ("seg", (19, 9, 12, 14, 1.4)),
        ],
        # --- health ---
        "hospital": [("seg", (12, 5, 12, 19, 3.2)), ("seg", (5, 12, 19, 12, 3.2))],
        "clinica": [("seg", (12, 7, 12, 17, 2.4)), ("seg", (7, 12, 17, 12, 2.4))],
        "farmacia": [
            ("seg", (12, 6, 12, 18, 2.8)),
            ("seg", (6, 12, 18, 12, 2.8)),
            ("ring", (12, 12, 8.0, 1.2)),
        ],
        "dentista": [
            ("circle", (9.5, 10, 3.4)),
            ("circle", (14.5, 10, 3.4)),
            ("seg", (9, 13, 8, 18, 1.8)),
            ("seg", (15, 13, 16, 18, 1.8)),
        ],
        "veterinaria": [
            ("circle", (12, 14, 3.4)),
            ("circle", (8, 9, 1.8)),
            ("circle", (12, 7.5, 1.8)),
            ("circle", (16, 9, 1.8)),
        ],
        # --- tourism ---
        "hotel": [
            ("seg", (5, 8, 5, 18, 2.0)),
            ("box", (13, 15, 6, 2.4, 1.0)),
            ("circle", (9, 11, 2.0)),
            ("seg", (5, 18, 19, 18, 1.6)),
        ],
        "hostal": [
            ("seg", (5, 8, 5, 18, 2.0)),
            ("box", (13, 15, 6, 2.4, 1.0)),
            ("circle", (9, 11, 2.0)),
        ],
        "hotel_paso": [
            ("seg", (5, 8, 5, 18, 2.0)),
            ("box", (13, 15, 6, 2.4, 1.0)),
            ("circle", (9, 11, 2.0)),
            ("seg", (17, 6, 20, 9, 1.4)),
        ],
        "playa": [
            ("seg", (4, 16, 20, 16, 1.8)),
            ("circle", (8, 9, 2.6)),
            ("seg", (13, 16, 16, 8, 1.6)),
            ("seg", (16, 8, 19, 10, 1.4)),
        ],
        "mirador": [
            ("circle", (12, 10, 3.0)),
            ("seg", (12, 13, 12, 19, 1.8)),
            ("seg", (8, 19, 16, 19, 1.6)),
            ("ring", (12, 10, 5.5, 1.2)),
        ],
        "volcan": [
            ("tri", (12, 13, 8.0, 2.0)),
            ("seg", (9, 7, 15, 7, 1.6)),
            ("seg", (12, 7, 12, 3.5, 1.2)),
        ],
        "laguna": [("ring", (12, 13, 6.5, 2.0)), ("seg", (7, 7, 17, 7, 1.4))],
        "museo": [
            ("seg", (5, 11, 19, 11, 1.8)),
            ("tri", (12, 8, 5.0, 1.6)),
            ("seg", (8, 12, 8, 17, 1.8)),
            ("seg", (16, 12, 16, 17, 1.8)),
            ("seg", (5, 18.5, 19, 18.5, 1.8)),
        ],
        "iglesia": [
            ("seg", (12, 4, 12, 19, 2.0)),
            ("seg", (8, 8, 16, 8, 1.8)),
            ("box", (12, 16, 4.5, 3.0, 0.6)),
        ],
        "parque": [("circle", (12, 9, 4.2)), ("seg", (12, 12, 12, 19, 1.8))],
        "sitio_turistico": [("circle", (12, 12, 4.5)), ("ring", (12, 12, 7.5, 1.4))],
        # --- services ---
        "policia": [("tri", (12, 12, 7.0, 2.2)), ("circle", (12, 11, 1.8))],
        "bomberos": [
            ("seg", (12, 5, 9, 12, 2.0)),
            ("seg", (9, 12, 14, 11, 2.0)),
            ("circle", (12, 16, 3.4)),
        ],
        "correo": [
            ("box", (12, 12, 7, 4.5, 0.8)),
            ("seg", (5, 9, 12, 14, 1.4)),
            ("seg", (19, 9, 12, 14, 1.4)),
        ],
        "embajada": [("seg", (8, 4, 8, 20, 1.8)), ("box", (13.5, 8, 5.0, 3.2, 0.4))],
        "universidad": [
            ("seg", (4, 10, 12, 7, 1.8)),
            ("seg", (20, 10, 12, 7, 1.8)),
            ("seg", (8, 12, 8, 17, 1.4)),
            ("seg", (16, 12, 16, 17, 1.4)),
            ("seg", (8, 17, 16, 17, 1.4)),
        ],
        "escuela": [
            ("seg", (4, 10, 12, 7, 1.8)),
            ("seg", (20, 10, 12, 7, 1.8)),
            ("box", (12, 15, 5, 3.5, 0.6)),
        ],
        "gimnasio": [
            ("seg", (5, 12, 19, 12, 2.0)),
            ("box", (6.5, 12, 1.6, 4.0, 0.4)),
            ("box", (17.5, 12, 1.6, 4.0, 0.4)),
        ],
        "salon_belleza": [
            ("circle", (9, 8, 2.2)),
            ("circle", (9, 16, 2.2)),
            ("seg", (11, 9, 18, 15, 1.6)),
            ("seg", (11, 15, 18, 9, 1.6)),
        ],
        "lavanderia": [
            ("box", (12, 12, 6.0, 6.5, 1.2)),
            ("ring", (12, 13, 3.4, 1.6)),
            ("seg", (8, 7, 16, 7, 1.4)),
        ],
        # --- transport ---
        "terminal_buses": [
            ("box", (12, 12, 6.5, 6.0, 1.4)),
            ("seg", (7, 12, 17, 12, 1.4)),
            ("circle", (9, 17.5, 1.6)),
            ("circle", (15, 17.5, 1.6)),
        ],
        "parada_bus": [
            ("box", (13, 11, 4.5, 5.0, 1.0)),
            ("seg", (6, 5, 6, 19, 1.8)),
            ("seg", (6, 5, 12, 5, 1.6)),
        ],
        "aeropuerto": [
            ("seg", (4, 13, 20, 11, 2.2)),
            ("seg", (10, 6, 15, 12, 2.0)),
            ("seg", (7, 18, 12, 13, 1.6)),
        ],
        "puerto": [
            ("seg", (12, 5, 12, 18, 1.8)),
            ("circle", (12, 6, 1.8)),
            ("seg", (8, 9, 16, 9, 1.6)),
            ("ring", (12, 15, 5.0, 1.6)),
        ],
        "taxi": [
            ("box", (12, 14, 7, 3.0, 1.0)),
            ("box", (12, 10, 4.5, 2.2, 0.6)),
            ("circle", (8.5, 17.5, 1.4)),
            ("circle", (15.5, 17.5, 1.4)),
        ],
        # --- landmarks ---
        "rotonda": [
            ("ring", (12, 12, 5.0, 2.0)),
            ("seg", (12, 2, 12, 6, 1.6)),
            ("seg", (12, 18, 12, 22, 1.6)),
            ("seg", (2, 12, 6, 12, 1.6)),
            ("seg", (18, 12, 22, 12, 1.6)),
        ],
        "semaforo": [
            ("box", (12, 11, 3.0, 6.5, 1.2)),
            ("circle", (12, 7, 1.2)),
            ("circle", (12, 11, 1.2)),
            ("circle", (12, 15, 1.2)),
            ("seg", (12, 17, 12, 20, 1.4)),
        ],
        "puente": [
            ("seg", (3, 15, 21, 15, 1.8)),
            ("ring", (12, 15, 6.0, 1.6)),
            ("seg", (5, 15, 5, 19, 1.4)),
            ("seg", (19, 15, 19, 19, 1.4)),
        ],
        "monumento": [("seg", (12, 4, 12, 17, 2.6)), ("box", (12, 18.5, 5.5, 1.6, 0.4))],
        "estadio": [("ring", (12, 12, 7.5, 2.0)), ("seg", (12, 6, 12, 18, 1.4))],
        "cementerio": [("seg", (12, 6, 12, 19, 2.0)), ("seg", (8, 10, 16, 10, 1.8))],
        # --- fallback ---
        "otro": [("circle", (12, 12, 3.2)), ("ring", (12, 12, 6.5, 1.4))],
        # --- map furniture (not a POI category) ---
        "oneway": [
            ("seg", (5, 12, 17, 12, 2.0)),
            ("seg", (13, 8, 18, 12, 2.0)),
            ("seg", (13, 16, 18, 12, 2.0)),
        ],
    }


def _distance(shape: Shape, px: float, py: float) -> float:
    """Signed distance to the union of a glyph's primitives."""
    best = -1e9
    for kind, args in shape:
        if kind == "circle":
            value = sd_circle(px, py, *args)
        elif kind == "ring":
            value = sd_ring(px, py, *args)
        elif kind == "box":
            value = sd_box(px, py, *args)
        elif kind == "seg":
            value = sd_segment(px, py, *args)
        elif kind == "tri":
            value = sd_triangle(px, py, *args)
        else:
            raise ValueError(f"unknown primitive {kind!r}")
        best = max(best, value)
    return best


def render_glyph(shape: Shape, size: int = ICON_SIZE) -> list[list[int]]:
    """Rasterise a glyph to an alpha-only signed distance field.

    Encoding is MapLibre's: alpha 128 is the shape's edge, higher is inside,
    and the gradient spans :data:`SDF_RANGE` pixels either side.
    """
    rows: list[list[int]] = []
    for y in range(size):
        row = []
        for x in range(size):
            distance = _distance(shape, x + 0.5, y + 0.5)
            value = 0.5 + 0.5 * (distance / SDF_RANGE)
            row.append(max(0, min(255, round(value * 255))))
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# PNG writing (stdlib only)
# --------------------------------------------------------------------------- #


def write_png(
    path: Path, width: int, height: int, pixels: list[list[tuple[int, int, int, int]]]
) -> None:
    """Write an 8-bit RGBA PNG. Filter type 0 on every scanline."""
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    path.write_bytes(png)


def glyph_to_svg(name: str, shape: Shape) -> str:
    """An editable SVG of the same glyph, so a designer can replace it."""
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {ICON_SIZE} {ICON_SIZE}" '
        f'width="{ICON_SIZE}" height="{ICON_SIZE}" fill="none" stroke="currentColor" '
        f'stroke-linecap="round" stroke-linejoin="round">',
        f"<!-- {name}: generated by scripts/build_sprites.py; replace freely -->",
    ]
    for kind, args in shape:
        if kind == "circle":
            cx, cy, r = args
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="currentColor"/>')
        elif kind == "ring":
            cx, cy, r, thickness = args
            parts.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" stroke-width="{thickness}"/>')
        elif kind == "box":
            cx, cy, hw, hh, radius = args
            parts.append(
                f'<rect x="{cx - hw}" y="{cy - hh}" width="{hw * 2}" height="{hh * 2}" '
                f'rx="{radius}" fill="currentColor"/>'
            )
        elif kind == "seg":
            x0, y0, x1, y1, thickness = args
            parts.append(
                f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" stroke-width="{thickness}"/>'
            )
        elif kind == "tri":
            cx, cy, size, thickness = args
            points = f"{cx},{cy - size} {cx - size * 0.9},{cy + size * 0.75} {cx + size * 0.9},{cy + size * 0.75}"
            fill = "none" if thickness else "currentColor"
            parts.append(
                f'<polygon points="{points}" fill="{fill}" stroke-width="{thickness or 1}"/>'
            )
    parts.append("</svg>")
    return "\n".join(parts)


def build(out_dir: Path, icons_dir: Path, *, pixel_ratio: int = 1) -> dict[str, Any]:
    """Pack every glyph into one sheet and return the sprite index."""
    glyphs = _glyphs()
    names = sorted(glyphs)

    columns = math.ceil(math.sqrt(len(names)))
    rows = math.ceil(len(names) / columns)
    cell = ICON_SIZE + PADDING
    width, height = columns * cell, rows * cell

    canvas = [[(255, 255, 255, 0) for _ in range(width)] for _ in range(height)]
    index: dict[str, Any] = {}

    # The style keys icons on the POI's `category` (the snake_case id), while
    # docs/taxonomy.csv also names an `icon` in kebab-case. Both spellings are
    # emitted for the same sprite so neither convention can silently miss.
    aliases: dict[str, str] = {}
    for category in all_categories():
        icon_key = category.icon.replace("-", "_")
        if icon_key != category.category_id:
            aliases[category.icon] = category.category_id

    for position, name in enumerate(names):
        gx = (position % columns) * cell
        gy = (position // columns) * cell
        field = render_glyph(glyphs[name])
        for y in range(ICON_SIZE):
            for x in range(ICON_SIZE):
                canvas[gy + y][gx + x] = (255, 255, 255, field[y][x])
        entry = {
            "x": gx,
            "y": gy,
            "width": ICON_SIZE,
            "height": ICON_SIZE,
            "pixelRatio": pixel_ratio,
            # SDF is what lets the style recolour a verified place's icon.
            "sdf": True,
        }
        index[f"nicanav-{name}"] = entry
        for alias, target in aliases.items():
            if target == name:
                index[f"nicanav-{alias}"] = dict(entry)

    out_dir.mkdir(parents=True, exist_ok=True)
    write_png(out_dir / "nicanav.png", width, height, canvas)
    (out_dir / "nicanav.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    icons_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (icons_dir / f"{name}.svg").write_text(glyph_to_svg(name, glyphs[name]), encoding="utf-8")

    return index


def check() -> int:
    """Every taxonomy category must have a glyph.

    A category added without one draws nothing on the map, and nobody notices
    until a user does — so the nightly build fails here instead.
    """
    glyphs = _glyphs()
    missing = [
        category.category_id
        for category in all_categories()
        if category.category_id not in glyphs and category.icon.replace("-", "_") not in glyphs
    ]
    if missing:
        print(f"missing glyphs for: {', '.join(sorted(set(missing)))}", file=sys.stderr)
        return 1
    print(f"all {len(all_categories())} categories have an icon ({len(glyphs)} glyphs)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "web" / "sprites")
    parser.add_argument("--icons-dir", type=Path, default=REPO_ROOT / "web" / "icons")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    if args.check:
        return check()

    status = check()
    index = build(args.out_dir, args.icons_dir)
    print(f"wrote {args.out_dir / 'nicanav.png'} and the index ({len(index)} icons)")
    print(f"wrote {len(index)} editable SVGs to {args.icons_dir}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
