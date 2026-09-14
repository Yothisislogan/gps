"""Render the navigation mark at installable PWA sizes; no external artwork."""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1] / "web" / "icons"
ROOT.mkdir(parents=True, exist_ok=True)
SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#0b6e4f"/><path d="M256 112 370 382 256 322 142 382Z" fill="#f6f3ee"/></svg>\n'
(ROOT / "favicon.svg").write_text(SVG)
for name, size in [
    ("app-192", 192),
    ("app-512", 512),
    ("app-maskable-512", 512),
    ("apple-touch-icon", 180),
]:
    scale = 4
    image = Image.new("RGB", (size * scale, size * scale), "#0b6e4f")
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [
            (int(x / 512 * size * scale), int(y / 512 * size * scale))
            for x, y in [(256, 112), (370, 382), (256, 322), (142, 382)]
        ],
        fill="#f6f3ee",
    )
    image.resize((size, size), Image.Resampling.LANCZOS).save(ROOT / f"{name}.png")
